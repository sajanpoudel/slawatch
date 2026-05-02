"""
Cloud Run metric fetcher.

This module talks to Cloud Monitoring and turns its time-series API output
into the MinuteBucket and LatencySample shapes the evaluator expects.

The Cloud Run SLA is defined as:
    Monthly Uptime % = (total minutes - downtime minutes) / total minutes
    A minute counts as Downtime if Error Rate > 1% with at least 100 valid
    requests in that minute.
    Error Rate = 5xx infrastructure errors / total valid requests.

We fetch run.googleapis.com/request_count grouped by response_code_class
with one-minute alignment, then re-aggregate the classes per minute. This
preserves the structure the SLA cares about and avoids the trap of asking
Cloud Monitoring to compute a cross-series ratio for us, which requires a
separate denominator filter and is harder to reason about.

For latency we fetch run.googleapis.com/request_latencies as a single
distribution covering the entire window and ask the API for the configured
percentile. This matches what teams typically mean by "p99 over the last
30 days".

References:
    https://cloud.google.com/run/docs/monitoring
    https://cloud.google.com/monitoring/api/ref_v3/rest/v3/projects.timeSeries/list
    https://cloud.google.com/run/sla
"""

from __future__ import annotations

import logging
import time
from collections import defaultdict
from datetime import datetime, timezone

from google.api_core import exceptions as gax_exceptions
from google.cloud import monitoring_v3

from ..config import Target
from ..exceptions import MetricFetchError
from .base import LatencySample, MetricFetcher, MinuteBucket, TargetSeries

logger = logging.getLogger(__name__)

REQUEST_COUNT_METRIC = "run.googleapis.com/request_count"
REQUEST_LATENCIES_METRIC = "run.googleapis.com/request_latencies"
RESOURCE_TYPE = "cloud_run_revision"
ERROR_CLASS = "5xx"

# Number of times to retry transient failures before giving up.
MAX_RETRY_ATTEMPTS = 3
INITIAL_BACKOFF_SECONDS = 1.0


class CloudRunFetcher(MetricFetcher):
    """Pulls Cloud Run metrics from Cloud Monitoring."""

    def __init__(self, project_id: str, client: monitoring_v3.MetricServiceClient | None = None) -> None:
        self.project_id = project_id
        self.client = client or monitoring_v3.MetricServiceClient()
        self.project_name = f"projects/{project_id}"

    def fetch(self, target: Target, eval_start: datetime, eval_end: datetime) -> TargetSeries:
        if target.kind != "cloud_run":
            raise ValueError(f"CloudRunFetcher cannot handle kind={target.kind!r}")

        minutes = self._fetch_request_minutes(target, eval_start, eval_end)
        latency: list[LatencySample] = []
        if target.slo.latency_p99_ms is not None:
            latency = self._fetch_latency(target, eval_start, eval_end, percentile=99)

        return TargetSeries(
            target_name=target.name,
            eval_start=eval_start,
            eval_end=eval_end,
            minutes=minutes,
            latency=latency,
        )

    def _fetch_request_minutes(
        self, target: Target, eval_start: datetime, eval_end: datetime
    ) -> list[MinuteBucket]:
        interval = self._make_interval(eval_start, eval_end)
        aggregation = monitoring_v3.Aggregation(
            alignment_period={"seconds": 60},
            per_series_aligner=monitoring_v3.Aggregation.Aligner.ALIGN_DELTA,
            cross_series_reducer=monitoring_v3.Aggregation.Reducer.REDUCE_SUM,
            group_by_fields=[
                "resource.labels.service_name",
                "metric.labels.response_code_class",
            ],
        )
        request = monitoring_v3.ListTimeSeriesRequest(
            name=self.project_name,
            filter=self._request_count_filter(target),
            interval=interval,
            view=monitoring_v3.ListTimeSeriesRequest.TimeSeriesView.FULL,
            aggregation=aggregation,
        )

        # Bucket: minute_start -> {response_code_class -> count}
        buckets: dict[datetime, dict[str, int]] = defaultdict(lambda: defaultdict(int))
        for series in self._call_with_retry(request):
            response_class = series.metric.labels.get("response_code_class", "unknown")
            for point in series.points:
                minute = self._floor_to_minute(point.interval.end_time)
                value = self._extract_int_value(point)
                buckets[minute][response_class] += value

        # Convert to MinuteBucket list. Sort chronologically so reports read naturally.
        result: list[MinuteBucket] = []
        for minute, class_counts in sorted(buckets.items()):
            total = sum(class_counts.values())
            errors = class_counts.get(ERROR_CLASS, 0)
            result.append(
                MinuteBucket(
                    minute_start=minute,
                    total_requests=total,
                    error_requests=errors,
                )
            )
        return result

    def _fetch_latency(
        self,
        target: Target,
        eval_start: datetime,
        eval_end: datetime,
        percentile: int,
    ) -> list[LatencySample]:
        interval = self._make_interval(eval_start, eval_end)
        window_seconds = max(int((eval_end - eval_start).total_seconds()), 60)
        aligner = self._percentile_aligner(percentile)
        aggregation = monitoring_v3.Aggregation(
            alignment_period={"seconds": window_seconds},
            per_series_aligner=aligner,
            cross_series_reducer=monitoring_v3.Aggregation.Reducer.REDUCE_MEAN,
            group_by_fields=["resource.labels.service_name"],
        )
        request = monitoring_v3.ListTimeSeriesRequest(
            name=self.project_name,
            filter=self._request_latencies_filter(target),
            interval=interval,
            view=monitoring_v3.ListTimeSeriesRequest.TimeSeriesView.FULL,
            aggregation=aggregation,
        )
        samples: list[LatencySample] = []
        for series in self._call_with_retry(request):
            for point in series.points:
                value = point.value.double_value or float(point.value.int64_value or 0)
                samples.append(LatencySample(percentile=percentile, value_ms=value))
        return samples

    def _request_count_filter(self, target: Target) -> str:
        parts = [
            f'metric.type = "{REQUEST_COUNT_METRIC}"',
            f'resource.type = "{RESOURCE_TYPE}"',
            f'resource.labels.service_name = "{target.service}"',
            f'resource.labels.location = "{target.region}"',
        ]
        if target.revision:
            parts.append(f'resource.labels.revision_name = "{target.revision}"')
        return " AND ".join(parts)

    def _request_latencies_filter(self, target: Target) -> str:
        parts = [
            f'metric.type = "{REQUEST_LATENCIES_METRIC}"',
            f'resource.type = "{RESOURCE_TYPE}"',
            f'resource.labels.service_name = "{target.service}"',
            f'resource.labels.location = "{target.region}"',
        ]
        if target.revision:
            parts.append(f'resource.labels.revision_name = "{target.revision}"')
        return " AND ".join(parts)

    def _make_interval(self, start: datetime, end: datetime) -> monitoring_v3.TimeInterval:
        return monitoring_v3.TimeInterval(
            start_time={"seconds": int(start.timestamp())},
            end_time={"seconds": int(end.timestamp())},
        )

    def _percentile_aligner(self, percentile: int) -> int:
        # The Aligner enum has discrete values for common percentiles. We map
        # to the closest supported value to keep the API call simple.
        mapping = {
            5: monitoring_v3.Aggregation.Aligner.ALIGN_PERCENTILE_05,
            50: monitoring_v3.Aggregation.Aligner.ALIGN_PERCENTILE_50,
            95: monitoring_v3.Aggregation.Aligner.ALIGN_PERCENTILE_95,
            99: monitoring_v3.Aggregation.Aligner.ALIGN_PERCENTILE_99,
        }
        if percentile not in mapping:
            raise ValueError(
                f"unsupported percentile {percentile}. Cloud Monitoring supports 5, 50, 95, 99."
            )
        return mapping[percentile]

    def _call_with_retry(self, request: monitoring_v3.ListTimeSeriesRequest):
        """
        Iterate the paged response, retrying transient errors with exponential
        backoff. We treat ServiceUnavailable, DeadlineExceeded, and InternalError
        as transient. Other errors (PermissionDenied, NotFound) propagate up
        because retrying will not help.
        """
        backoff = INITIAL_BACKOFF_SECONDS
        last_error: Exception | None = None
        for attempt in range(1, MAX_RETRY_ATTEMPTS + 1):
            try:
                yield from self.client.list_time_series(request=request)
                return
            except (
                gax_exceptions.ServiceUnavailable,
                gax_exceptions.DeadlineExceeded,
                gax_exceptions.InternalServerError,
            ) as exc:
                last_error = exc
                logger.warning(
                    "transient Cloud Monitoring error on attempt %s/%s: %s",
                    attempt,
                    MAX_RETRY_ATTEMPTS,
                    exc,
                )
                if attempt < MAX_RETRY_ATTEMPTS:
                    time.sleep(backoff)
                    backoff *= 2
            except gax_exceptions.GoogleAPICallError as exc:
                # Non-transient: permission denied, bad filter, etc. Surface immediately.
                raise MetricFetchError(f"Cloud Monitoring rejected the request: {exc}") from exc
        raise MetricFetchError(
            f"Cloud Monitoring did not respond after {MAX_RETRY_ATTEMPTS} attempts: {last_error}"
        ) from last_error

    @staticmethod
    def _floor_to_minute(timestamp) -> datetime:
        """
        Convert a protobuf Timestamp into a UTC datetime floored to the minute.
        Cloud Monitoring returns end_time on minute boundaries when we use a
        60-second alignment, but we floor explicitly so we are robust to small
        clock drift.
        """
        seconds = int(timestamp.seconds) if hasattr(timestamp, "seconds") else int(timestamp.timestamp())
        floored = seconds - (seconds % 60)
        return datetime.fromtimestamp(floored, tz=timezone.utc)

    @staticmethod
    def _extract_int_value(point) -> int:
        """
        request_count is INT64 cumulative, but ALIGN_DELTA can return either
        int64 or double depending on aggregation. We coerce to int.
        """
        if point.value.int64_value:
            return int(point.value.int64_value)
        if point.value.double_value:
            return int(round(point.value.double_value))
        return 0
