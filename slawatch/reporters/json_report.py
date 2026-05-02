"""JSON report generation. Useful for piping into dashboards or BigQuery."""

from __future__ import annotations

import json
from datetime import datetime, timezone

from ..evaluator import TargetEvaluation
from ..sla_catalog import VERIFIED_AT


def render_json(project: str, evaluations: list[TargetEvaluation]) -> str:
    """Render a JSON compliance report."""
    payload = {
        "project": project,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "sla_catalog_verified_at": VERIFIED_AT.isoformat(),
        "evaluations": [ev.to_dict() for ev in evaluations],
    }
    return json.dumps(payload, indent=2, default=str)
