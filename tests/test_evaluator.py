"""Tests for the compliance evaluator."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from slawatch.config import SloConfig, Target
from slawatch.evaluator import Verdict, evaluate
from slawatch.metrics.base import LatencySample, MinuteBucket, TargetSeries


def _build_target(
    availability: float = 0.999,
    latency_p99_ms: float | None = None,
    region: str = "us-central1",
) -> Target:
    return Target(
        name="t1",
        kind="cloud_run",
        service="t1-svc",
        region=region,
        slo=SloConfig(availability=availability, latency_p99_ms=latency_p99_ms),
    )


def _build_series(
    minutes: list[MinuteBucket],
    eval_start: datetime,
    eval_end: datetime,
    latency: list[LatencySample] | None = None,
) -> TargetSeries:
    return TargetSeries(
        target_name="t1",
        eval_start=eval_start,
        eval_end=eval_end,
        minutes=minutes,
        latency=latency or [],
    )


def _minute(offset: int, total: int = 500, errors: int = 0, base: datetime | None = None) -> MinuteBucket:
    base = base or datetime(2026, 4, 30, tzinfo=timezone.utc)
    return MinuteBucket(
        minute_start=base + timedelta(minutes=offset),
        total_requests=total,
        error_requests=errors,
    )


class TestEvaluatorBasics:
    def test_clean_window_is_passing(self) -> None:
        eval_start = datetime(2026, 4, 30, tzinfo=timezone.utc)
        eval_end = eval_start + timedelta(minutes=60)
        minutes = [_minute(i, total=500, errors=0) for i in range(60)]
        target = _build_target()
        result = evaluate(target, _build_series(minutes, eval_start, eval_end))

        assert result.verdict == Verdict.PASSING
        assert result.downtime_minutes == 0
        assert result.monthly_uptime == 1.0
        assert result.error_budget_remaining_pct == 1.0

    def test_zero_traffic_is_insufficient_data(self) -> None:
        eval_start = datetime(2026, 4, 30, tzinfo=timezone.utc)
        eval_end = eval_start + timedelta(minutes=60)
        minutes = [_minute(i, total=0, errors=0) for i in range(60)]
        target = _build_target()
        result = evaluate(target, _build_series(minutes, eval_start, eval_end))

        assert result.verdict == Verdict.INSUFFICIENT_DATA
        assert result.eligible_minutes == 0


class TestDowntimeDetection:
    def test_minute_with_high_error_rate_counts_as_downtime(self) -> None:
        eval_start = datetime(2026, 4, 30, tzinfo=timezone.utc)
        eval_end = eval_start + timedelta(minutes=60)
        minutes = [_minute(i, total=500, errors=0) for i in range(60)]
        # Make minute 10 a downtime minute with 5% errors.
        minutes[10] = _minute(10, total=500, errors=25)
        target = _build_target(availability=0.99)
        result = evaluate(target, _build_series(minutes, eval_start, eval_end))

        assert result.downtime_minutes == 1
        assert len(result.downtime_periods) == 1
        assert result.downtime_periods[0].minute_count == 1

    def test_low_traffic_minute_is_not_downtime_even_with_errors(self) -> None:
        # 80 requests in a minute is below the 100-request floor in the SLA.
        # Even if every single request errors, that minute does not count.
        eval_start = datetime(2026, 4, 30, tzinfo=timezone.utc)
        eval_end = eval_start + timedelta(minutes=60)
        minutes = [_minute(i, total=500, errors=0) for i in range(60)]
        minutes[10] = _minute(10, total=80, errors=80)
        target = _build_target(availability=0.99)
        result = evaluate(target, _build_series(minutes, eval_start, eval_end))

        assert result.downtime_minutes == 0
        assert result.eligible_minutes == 59

    def test_consecutive_downtime_groups_into_one_period(self) -> None:
        eval_start = datetime(2026, 4, 30, tzinfo=timezone.utc)
        eval_end = eval_start + timedelta(minutes=60)
        minutes = [_minute(i, total=500, errors=0) for i in range(60)]
        for i in (10, 11, 12):
            minutes[i] = _minute(i, total=500, errors=20)
        target = _build_target(availability=0.99)
        result = evaluate(target, _build_series(minutes, eval_start, eval_end))

        assert result.downtime_minutes == 3
        assert len(result.downtime_periods) == 1
        assert result.downtime_periods[0].minute_count == 3

    def test_separate_downtime_bursts_produce_separate_periods(self) -> None:
        eval_start = datetime(2026, 4, 30, tzinfo=timezone.utc)
        eval_end = eval_start + timedelta(minutes=60)
        minutes = [_minute(i, total=500, errors=0) for i in range(60)]
        for i in (10, 11):
            minutes[i] = _minute(i, total=500, errors=20)
        for i in (40, 41, 42, 43):
            minutes[i] = _minute(i, total=500, errors=20)
        target = _build_target(availability=0.99)
        result = evaluate(target, _build_series(minutes, eval_start, eval_end))

        assert result.downtime_minutes == 6
        assert len(result.downtime_periods) == 2
        assert {p.minute_count for p in result.downtime_periods} == {2, 4}


class TestVerdictThresholds:
    def test_breach_below_sla_floor(self) -> None:
        # SLA floor is 99.95% in us-central1. A single hour with massive
        # errors should drop below that.
        eval_start = datetime(2026, 4, 30, tzinfo=timezone.utc)
        eval_end = eval_start + timedelta(minutes=60)
        minutes = [_minute(i, total=500, errors=0) for i in range(60)]
        for i in range(10, 25):
            minutes[i] = _minute(i, total=500, errors=20)
        target = _build_target(availability=0.999)
        result = evaluate(target, _build_series(minutes, eval_start, eval_end))

        assert result.verdict == Verdict.BREACHING
        assert result.credit_tier_pct > 0

    def test_warning_between_slo_and_sla(self) -> None:
        # SLO is 99.99%, SLA floor is 99.95%. One minute of downtime in 60
        # gives 98.33% uptime, well below SLO and below SLA.
        # We need a window where the gap exists. Use 1000 minutes.
        eval_start = datetime(2026, 4, 30, tzinfo=timezone.utc)
        eval_end = eval_start + timedelta(minutes=1000)
        minutes = [_minute(i, total=500, errors=0) for i in range(1000)]
        # 1 downtime minute in 1000 = 99.9% uptime.
        # SLO 99.99% would warn, SLA 99.95% would also breach.
        # To get only WARN, we need uptime between SLA and SLO. 1 minute in 10000.
        eval_end = eval_start + timedelta(minutes=10000)
        minutes = [_minute(i, total=500, errors=0) for i in range(10000)]
        minutes[100] = _minute(100, total=500, errors=20)
        target = _build_target(availability=0.9999)
        result = evaluate(target, _build_series(minutes, eval_start, eval_end))

        # 9999/10000 = 99.99%. SLO is 99.99%, so this is exactly at the line.
        # Add another downtime minute to push us between SLA (99.95%) and SLO.
        minutes[200] = _minute(200, total=500, errors=20)
        result = evaluate(target, _build_series(minutes, eval_start, eval_end))
        assert result.monthly_uptime < 0.9999
        assert result.monthly_uptime > 0.9995
        assert result.verdict == Verdict.WARNING


class TestLatency:
    def test_latency_breach_demotes_to_warning(self) -> None:
        eval_start = datetime(2026, 4, 30, tzinfo=timezone.utc)
        eval_end = eval_start + timedelta(minutes=60)
        minutes = [_minute(i, total=500, errors=0) for i in range(60)]
        target = _build_target(availability=0.99, latency_p99_ms=500)
        latency = [LatencySample(percentile=99, value_ms=900)]

        result = evaluate(target, _build_series(minutes, eval_start, eval_end, latency))
        assert result.verdict == Verdict.WARNING
        assert result.latency_p99_ms == 900

    def test_latency_within_threshold_stays_passing(self) -> None:
        eval_start = datetime(2026, 4, 30, tzinfo=timezone.utc)
        eval_end = eval_start + timedelta(minutes=60)
        minutes = [_minute(i, total=500, errors=0) for i in range(60)]
        target = _build_target(availability=0.99, latency_p99_ms=500)
        latency = [LatencySample(percentile=99, value_ms=300)]

        result = evaluate(target, _build_series(minutes, eval_start, eval_end, latency))
        assert result.verdict == Verdict.PASSING


class TestErrorBudget:
    def test_budget_consumption_matches_downtime(self) -> None:
        # Window: 1000 minutes. SLO 99.9% allows 1 minute of downtime.
        # Inject 1 minute of downtime: budget should be fully consumed.
        eval_start = datetime(2026, 4, 30, tzinfo=timezone.utc)
        eval_end = eval_start + timedelta(minutes=1000)
        minutes = [_minute(i, total=500, errors=0) for i in range(1000)]
        minutes[10] = _minute(10, total=500, errors=20)
        target = _build_target(availability=0.999)
        result = evaluate(target, _build_series(minutes, eval_start, eval_end))

        # SLO 99.9% over 1000 minutes = 1.0 minute = 60s budget
        assert result.error_budget_seconds == pytest.approx(60.0, abs=0.5)
        assert result.error_budget_consumed_seconds == 60
        assert result.error_budget_remaining_pct == pytest.approx(0.0, abs=0.01)


class TestRegionAwareSla:
    def test_mexico_region_uses_lower_floor(self) -> None:
        eval_start = datetime(2026, 4, 30, tzinfo=timezone.utc)
        eval_end = eval_start + timedelta(minutes=60)
        minutes = [_minute(i, total=500, errors=0) for i in range(60)]
        target = _build_target(availability=0.999, region="northamerica-south1")
        result = evaluate(target, _build_series(minutes, eval_start, eval_end))

        assert result.sla_floor == 0.999
        assert result.sla_variant == "non_gpu_mexico_stockholm"

    def test_standard_region_uses_default_floor(self) -> None:
        eval_start = datetime(2026, 4, 30, tzinfo=timezone.utc)
        eval_end = eval_start + timedelta(minutes=60)
        minutes = [_minute(i, total=500, errors=0) for i in range(60)]
        target = _build_target(availability=0.999, region="us-central1")
        result = evaluate(target, _build_series(minutes, eval_start, eval_end))

        assert result.sla_floor == 0.9995
        assert result.sla_variant == "non_gpu"
