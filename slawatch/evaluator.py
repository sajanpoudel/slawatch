"""
Compliance evaluator.

Converts minute-by-minute Cloud Monitoring request data into a compliance
verdict using Google's contractual SLA definition:

    Monthly Uptime % = (total minutes - downtime minutes) / total minutes

A minute is Downtime when error rate > 1% with at least 100 valid requests.
Three thresholds are tracked: SLA floor (contractual), SLO target (internal,
stricter), and the error budget (time remaining before SLO breach).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from enum import Enum

from .config import Target
from .metrics import LatencySample, MinuteBucket, TargetSeries
from .sla_catalog import (
    SlaTier,
    cloud_run_floor_for_region,
    credit_tier_for_uptime,
)

# Cloud Run SLA threshold for "Downtime" within a one-minute period.
DOWNTIME_ERROR_RATE = 0.01

# Cloud Run SLA minimum number of valid requests required for a measurement
# period to count toward Error Rate computations.
MIN_VALID_REQUESTS_PER_PERIOD = 100


class Verdict(str, Enum):
    PASSING = "passing"
    WARNING = "warning"
    BREACHING = "breaching"
    INSUFFICIENT_DATA = "insufficient_data"


@dataclass
class DowntimePeriod:
    """A consecutive run of one or more downtime minutes."""

    start: datetime
    end: datetime
    minute_count: int

    def to_dict(self) -> dict:
        return {
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "minute_count": self.minute_count,
        }


@dataclass
class TargetEvaluation:
    """The full compliance picture for a single target."""

    target_name: str
    service: str
    region: str
    eval_start: datetime
    eval_end: datetime
    total_minutes: int
    eligible_minutes: int
    downtime_minutes: int
    monthly_uptime: float
    sla_floor: float
    sla_variant: str
    slo_target: float
    error_budget_seconds: float
    error_budget_consumed_seconds: float
    error_budget_remaining_pct: float
    downtime_periods: list[DowntimePeriod] = field(default_factory=list)
    latency_p99_ms: float | None = None
    latency_p99_threshold_ms: float | None = None
    verdict: Verdict = Verdict.PASSING
    credit_tier_pct: int = 0
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        data = asdict(self)
        data["eval_start"] = self.eval_start.isoformat()
        data["eval_end"] = self.eval_end.isoformat()
        data["downtime_periods"] = [period.to_dict() for period in self.downtime_periods]
        data["verdict"] = self.verdict.value
        return data


def evaluate(target: Target, series: TargetSeries) -> TargetEvaluation:
    """Run the full compliance computation for one target."""
    sla_tier = cloud_run_floor_for_region(
        region=target.region,
        gpu=target.sla.gpu,
        zonal_redundancy=target.sla.zonal_redundancy,
    )
    if target.slo.availability < sla_tier.monthly_uptime_floor:
        # The team is targeting something looser than what Google itself
        # promises. This is almost certainly a config mistake.
        note = (
            f"SLO target {target.slo.availability:.4f} is below the published "
            f"SLA floor {sla_tier.monthly_uptime_floor:.4f}. The SLO should be "
            f"stricter than the SLA, not looser."
        )
    else:
        note = ""

    downtime_minutes, downtime_periods, eligible_minutes = _compute_downtime(series.minutes)
    total_minutes = max(int((series.eval_end - series.eval_start).total_seconds() // 60), 1)
    monthly_uptime = (total_minutes - downtime_minutes) / total_minutes

    latency_value, latency_breach = _evaluate_latency(series.latency, target.slo.latency_p99_ms)

    verdict = _decide_verdict(
        monthly_uptime=monthly_uptime,
        sla_floor=sla_tier.monthly_uptime_floor,
        slo_target=target.slo.availability,
        latency_breach=latency_breach,
        eligible_minutes=eligible_minutes,
    )

    error_budget_total_seconds = max((1 - target.slo.availability) * total_minutes * 60, 0.0)
    error_budget_consumed_seconds = downtime_minutes * 60
    if error_budget_total_seconds > 0:
        remaining_pct = max(
            (error_budget_total_seconds - error_budget_consumed_seconds) / error_budget_total_seconds,
            0.0,
        )
    else:
        remaining_pct = 0.0

    notes: list[str] = []
    if note:
        notes.append(note)

    evaluation = TargetEvaluation(
        target_name=target.name,
        service=target.service,
        region=target.region,
        eval_start=series.eval_start,
        eval_end=series.eval_end,
        total_minutes=total_minutes,
        eligible_minutes=eligible_minutes,
        downtime_minutes=downtime_minutes,
        monthly_uptime=monthly_uptime,
        sla_floor=sla_tier.monthly_uptime_floor,
        sla_variant=sla_tier.variant,
        slo_target=target.slo.availability,
        error_budget_seconds=error_budget_total_seconds,
        error_budget_consumed_seconds=error_budget_consumed_seconds,
        error_budget_remaining_pct=remaining_pct,
        downtime_periods=downtime_periods,
        latency_p99_ms=latency_value,
        latency_p99_threshold_ms=target.slo.latency_p99_ms,
        verdict=verdict,
        credit_tier_pct=credit_tier_for_uptime(monthly_uptime) if monthly_uptime < sla_tier.monthly_uptime_floor else 0,
        notes=notes,
    )
    return evaluation


def _compute_downtime(
    minutes: list[MinuteBucket],
) -> tuple[int, list[DowntimePeriod], int]:
    """
    Walk the minute buckets and identify Downtime Periods per the SLA.

    Returns (downtime_minute_count, downtime_periods, eligible_minute_count).

    Eligible minutes are those with enough traffic to count under the SLA's
    "minimum 100 valid requests" rule. The SLA does not count low-traffic
    minutes against you, which is the right behaviour for off-hours services.
    """
    downtime_count = 0
    eligible_count = 0
    periods: list[DowntimePeriod] = []
    current_start: datetime | None = None
    current_count = 0
    last_minute: datetime | None = None

    for bucket in minutes:
        is_eligible = bucket.has_minimum_volume
        if is_eligible:
            eligible_count += 1
        is_down = is_eligible and bucket.error_ratio > DOWNTIME_ERROR_RATE

        if is_down:
            downtime_count += 1
            if current_start is None or _gap_too_large(last_minute, bucket.minute_start):
                if current_start is not None:
                    periods.append(_close_period(current_start, last_minute, current_count))
                current_start = bucket.minute_start
                current_count = 1
            else:
                current_count += 1
            last_minute = bucket.minute_start
        else:
            if current_start is not None:
                periods.append(_close_period(current_start, last_minute, current_count))
                current_start = None
                current_count = 0
            last_minute = bucket.minute_start

    if current_start is not None and last_minute is not None:
        periods.append(_close_period(current_start, last_minute, current_count))

    return downtime_count, periods, eligible_count


def _close_period(start: datetime, last_minute: datetime, count: int) -> DowntimePeriod:
    end = last_minute + timedelta(minutes=1)
    return DowntimePeriod(start=start, end=end, minute_count=count)


def _gap_too_large(last_minute: datetime | None, current_minute: datetime) -> bool:
    """A new period starts whenever there's a gap of more than one minute."""
    if last_minute is None:
        return True
    return (current_minute - last_minute) > timedelta(minutes=1)


def _evaluate_latency(samples: list[LatencySample], threshold_ms: float | None) -> tuple[float | None, bool]:
    """
    Return the observed p99 (or None if not configured) and whether it
    breaches the threshold.
    """
    if threshold_ms is None:
        return None, False
    if not samples:
        return None, False
    # If the API returned multiple points (rare, since we use one big alignment
    # window), take the worst.
    p99_values = [sample.value_ms for sample in samples if sample.percentile == 99]
    if not p99_values:
        return None, False
    observed = max(p99_values)
    return observed, observed > threshold_ms


def _decide_verdict(
    monthly_uptime: float,
    sla_floor: float,
    slo_target: float,
    latency_breach: bool,
    eligible_minutes: int,
) -> Verdict:
    """
    Map the numbers to a single user-facing status.

    BREACHING        below the SLA floor (Google owes a credit)
    WARNING          below the SLO but above the SLA, or latency breach
    PASSING          everything green
    INSUFFICIENT_DATA  not enough traffic to make a call

    The latency breach demotes a passing service to warning, but never to
    breaching, because the contractual SLA is availability-only. Latency is a
    team-defined SLO, not a vendor commitment.
    """
    if eligible_minutes == 0:
        return Verdict.INSUFFICIENT_DATA
    if monthly_uptime < sla_floor:
        return Verdict.BREACHING
    if monthly_uptime < slo_target or latency_breach:
        return Verdict.WARNING
    return Verdict.PASSING
