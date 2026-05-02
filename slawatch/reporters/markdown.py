"""Markdown report generation."""

from __future__ import annotations

from datetime import datetime, timezone

from ..evaluator import TargetEvaluation, Verdict
from ..sla_catalog import VERIFIED_AT


VERDICT_LABEL = {
    Verdict.PASSING: "PASS",
    Verdict.WARNING: "WARN",
    Verdict.BREACHING: "BREACH",
    Verdict.INSUFFICIENT_DATA: "NODATA",
}


def render_markdown(project: str, evaluations: list[TargetEvaluation]) -> str:
    """Render a markdown compliance report."""
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    breach_count = sum(1 for ev in evaluations if ev.verdict == Verdict.BREACHING)
    warn_count = sum(1 for ev in evaluations if ev.verdict == Verdict.WARNING)
    pass_count = sum(1 for ev in evaluations if ev.verdict == Verdict.PASSING)
    nodata_count = sum(1 for ev in evaluations if ev.verdict == Verdict.INSUFFICIENT_DATA)

    lines: list[str] = []
    lines.append(f"# SLA compliance report")
    lines.append("")
    lines.append(f"- Project: `{project}`")
    lines.append(f"- Generated: {now}")
    lines.append(f"- SLA catalog last verified: {VERIFIED_AT.isoformat()}")
    lines.append(
        f"- Targets evaluated: {len(evaluations)} "
        f"({pass_count} passing, {warn_count} warning, {breach_count} breaching, "
        f"{nodata_count} insufficient data)"
    )
    lines.append("")

    lines.append("## Summary")
    lines.append("")
    lines.append("| Status | Target | Service | Region | Uptime | SLA floor | SLO | Budget left |")
    lines.append("|--------|--------|---------|--------|--------|-----------|-----|-------------|")
    for ev in evaluations:
        lines.append(
            "| {status} | {name} | {service} | {region} | {uptime} | {sla} | {slo} | {budget} |".format(
                status=VERDICT_LABEL[ev.verdict],
                name=ev.target_name,
                service=ev.service,
                region=ev.region,
                uptime=_fmt_pct(ev.monthly_uptime),
                sla=_fmt_pct(ev.sla_floor),
                slo=_fmt_pct(ev.slo_target),
                budget=_fmt_pct(ev.error_budget_remaining_pct),
            )
        )
    lines.append("")

    for ev in evaluations:
        lines.extend(_render_target_section(ev))

    return "\n".join(lines).rstrip() + "\n"


def _render_target_section(ev: TargetEvaluation) -> list[str]:
    lines: list[str] = []
    lines.append(f"## {ev.target_name} [{VERDICT_LABEL[ev.verdict]}]")
    lines.append("")
    lines.append(f"- Service: `{ev.service}` in `{ev.region}`")
    lines.append(
        f"- Window: {ev.eval_start.isoformat(timespec='minutes')} "
        f"to {ev.eval_end.isoformat(timespec='minutes')} "
        f"({ev.total_minutes} minutes)"
    )
    lines.append(f"- Eligible minutes (>={100} valid requests): {ev.eligible_minutes}")
    lines.append(f"- Downtime minutes: {ev.downtime_minutes}")
    lines.append(f"- Monthly uptime: **{_fmt_pct(ev.monthly_uptime)}**")
    lines.append(f"- SLA floor (variant `{ev.sla_variant}`): {_fmt_pct(ev.sla_floor)}")
    lines.append(f"- SLO target: {_fmt_pct(ev.slo_target)}")
    lines.append(
        f"- Error budget: {_fmt_seconds(ev.error_budget_consumed_seconds)} consumed "
        f"of {_fmt_seconds(ev.error_budget_seconds)} "
        f"({_fmt_pct(ev.error_budget_remaining_pct)} remaining)"
    )

    if ev.latency_p99_threshold_ms is not None:
        if ev.latency_p99_ms is not None:
            lines.append(
                f"- p99 latency: {ev.latency_p99_ms:.0f} ms "
                f"(threshold {ev.latency_p99_threshold_ms:.0f} ms)"
            )
        else:
            lines.append(f"- p99 latency: not enough data to compute")

    if ev.credit_tier_pct > 0:
        lines.append(
            f"- Financial credit tier: {ev.credit_tier_pct}% of monthly bill for this region. "
            f"Customer must request the credit within 30 days."
        )

    if ev.downtime_periods:
        lines.append("")
        lines.append("### Downtime periods")
        lines.append("")
        lines.append("| Start | End | Minutes |")
        lines.append("|-------|-----|---------|")
        for period in ev.downtime_periods:
            lines.append(
                "| {start} | {end} | {count} |".format(
                    start=period.start.isoformat(timespec="minutes"),
                    end=period.end.isoformat(timespec="minutes"),
                    count=period.minute_count,
                )
            )

    if ev.notes:
        lines.append("")
        lines.append("### Notes")
        lines.append("")
        for note in ev.notes:
            lines.append(f"- {note}")

    lines.append("")
    return lines


def _fmt_pct(value: float) -> str:
    """Render a fraction (0.9995) as a percentage string with four decimals."""
    return f"{value * 100:.4f}%"


def _fmt_seconds(seconds: float) -> str:
    """Render a duration in a humane way."""
    if seconds < 60:
        return f"{seconds:.0f}s"
    if seconds < 3600:
        return f"{seconds / 60:.1f}m"
    if seconds < 86400:
        return f"{seconds / 3600:.1f}h"
    return f"{seconds / 86400:.1f}d"
