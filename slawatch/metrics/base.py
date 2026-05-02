"""Abstract interface for service-specific metric fetchers."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime

from ..config import Target


@dataclass(frozen=True)
class MinuteBucket:
    """
    One minute of aggregated request data for a single resource.

    The Cloud Run SLA is defined in terms of one-minute Downtime Periods,
    so the minute is the natural unit of computation. Other services can
    use the same shape; if a service does not naturally bucket by request
    count, the fetcher fabricates one bucket per evaluation period and the
    evaluator handles it transparently.
    """

    minute_start: datetime
    total_requests: int
    error_requests: int

    @property
    def has_minimum_volume(self) -> bool:
        """Per the Cloud Run SLA, the floor is 100 valid requests per period."""
        return self.total_requests >= 100

    @property
    def error_ratio(self) -> float:
        if self.total_requests == 0:
            return 0.0
        return self.error_requests / self.total_requests


@dataclass(frozen=True)
class LatencySample:
    """One percentile measurement for a service over the evaluation window."""

    percentile: int
    value_ms: float


@dataclass(frozen=True)
class TargetSeries:
    """All raw data we need to evaluate a single target."""

    target_name: str
    eval_start: datetime
    eval_end: datetime
    minutes: list[MinuteBucket]
    latency: list[LatencySample]


class MetricFetcher(ABC):
    """Service-specific metric fetcher."""

    @abstractmethod
    def fetch(self, target: Target, eval_start: datetime, eval_end: datetime) -> TargetSeries:
        """Fetch the raw metric data for a target over the given window."""
