"""
GCP service-level agreement (SLA) values published by Google.

These figures are the contractual floor that Google commits to. The team's own
SLO (the warning threshold) is configured per target in the YAML config and
is generally tighter than the SLA so the team gets paged before Google owes
them a credit.

Sources:
- Cloud Run SLA: https://cloud.google.com/run/sla
- Cloud Storage SLA: https://cloud.google.com/storage/sla
- Compute Engine SLA: https://cloud.google.com/compute/sla

Last verified: see VERIFIED_AT below. SLA documents change. The verification
date is part of every report so it's clear how stale the catalog is relative
to the run.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

VERIFIED_AT: date = date(2026, 4, 30)


@dataclass(frozen=True)
class SlaTier:
    """One row of a published SLA table."""

    service: str
    variant: str
    monthly_uptime_floor: float
    notes: str = ""


# Cloud Run SLA. The 99.95% floor applies to the standard non-GPU service in
# every region except Mexico and Stockholm, which fall back to 99.9%.
CLOUD_RUN: dict[str, SlaTier] = {
    "default": SlaTier(
        service="cloud_run",
        variant="non_gpu",
        monthly_uptime_floor=0.9995,
        notes="Standard Cloud Run service in regions other than Mexico and Stockholm.",
    ),
    "non_gpu_mexico_stockholm": SlaTier(
        service="cloud_run",
        variant="non_gpu_mexico_stockholm",
        monthly_uptime_floor=0.999,
        notes="Cloud Run service in Mexico (northamerica-south1) and Stockholm (europe-north2).",
    ),
    "gpu_zonal_redundancy": SlaTier(
        service="cloud_run",
        variant="gpu_zonal_redundancy",
        monthly_uptime_floor=0.9995,
        notes="Cloud Run GPU service with zonal redundancy enabled.",
    ),
    "gpu_no_zonal_redundancy": SlaTier(
        service="cloud_run",
        variant="gpu_no_zonal_redundancy",
        monthly_uptime_floor=0.995,
        notes="Cloud Run GPU service without zonal redundancy.",
    ),
}


# Financial credit thresholds for Cloud Run non-GPU in standard regions. Each
# tuple is (lower_bound_inclusive, upper_bound_exclusive, credit_percent).
# A monthly uptime in [0.99, 0.9995) earns a 10% credit, and so on.
CLOUD_RUN_CREDIT_TIERS: tuple[tuple[float, float, int], ...] = (
    (0.99, 0.9995, 10),
    (0.95, 0.99, 25),
    (0.0, 0.95, 50),
)


# Regions that fall under the lower SLA floor. Anything not in this set uses
# the default 99.95% number.
LOWER_SLA_REGIONS: frozenset[str] = frozenset(
    {
        "northamerica-south1",
        "europe-north2",
    }
)


def cloud_run_floor_for_region(region: str, gpu: bool = False, zonal_redundancy: bool = True) -> SlaTier:
    """
    Return the published Cloud Run SLA tier for a given region and configuration.

    The SLA structure has three axes:
    - GPU vs non-GPU
    - Zonal redundancy on or off (only matters for GPU)
    - Standard region vs Mexico/Stockholm

    The Mexico/Stockholm exception only applies to non-GPU and GPU-with-zonal
    services. GPU-without-zonal is a flat 99.5% everywhere.
    """
    if gpu and not zonal_redundancy:
        return CLOUD_RUN["gpu_no_zonal_redundancy"]
    if gpu and zonal_redundancy:
        return CLOUD_RUN["gpu_zonal_redundancy"]
    if region in LOWER_SLA_REGIONS:
        return CLOUD_RUN["non_gpu_mexico_stockholm"]
    return CLOUD_RUN["default"]


def credit_tier_for_uptime(monthly_uptime: float) -> int:
    """
    Return the financial credit percentage that applies to a given uptime.

    Returns 0 if the service met the SLA. Uses the Cloud Run non-GPU table
    in standard regions; the same shape is used for the other Cloud Run
    variants with different breakpoints, but 10/25/50 are the canonical
    numbers and serve as a reasonable default for reporting purposes.
    """
    for lower, upper, credit in CLOUD_RUN_CREDIT_TIERS:
        if lower <= monthly_uptime < upper:
            return credit
    return 0
