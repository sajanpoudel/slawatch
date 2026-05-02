"""
Command-line interface.

Designed to be run from a developer laptop, a Docker container, or a
scheduled CI job. Exit codes are stable so cron and CI can react:

    0   all targets passing
    1   at least one target warning
    2   at least one target breaching the SLA floor
    3   configuration or runtime error
"""

from __future__ import annotations

import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

import click

from .config import Config, load_config
from .evaluator import TargetEvaluation, Verdict, evaluate
from .exceptions import ConfigError, SlaWatchError
from .metrics import CloudRunFetcher
from .reporters import render_json, render_markdown

EXIT_OK = 0
EXIT_WARNING = 1
EXIT_BREACH = 2
EXIT_ERROR = 3


@click.group()
@click.option("-v", "--verbose", is_flag=True, help="Enable debug logging.")
@click.pass_context
def cli(ctx: click.Context, verbose: bool) -> None:
    """slawatch: monitor SLA compliance for GCP services."""
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    ctx.ensure_object(dict)


@cli.command()
@click.option(
    "--config",
    "config_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    required=True,
    help="Path to the slawatch YAML config file.",
)
def check(config_path: Path) -> None:
    """Run a compliance check against all targets in the config."""
    try:
        config = load_config(config_path)
    except ConfigError as exc:
        click.echo(f"config error: {exc}", err=True)
        sys.exit(EXIT_ERROR)

    fetcher = CloudRunFetcher(project_id=config.project)
    eval_end = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    eval_start = eval_end - config.eval_window_delta()

    evaluations: list[TargetEvaluation] = []
    for target in config.targets:
        click.echo(f"evaluating {target.name} ({target.service} in {target.region})")
        try:
            series = fetcher.fetch(target, eval_start, eval_end)
        except SlaWatchError as exc:
            click.echo(f"  fetch failed: {exc}", err=True)
            sys.exit(EXIT_ERROR)
        evaluation = evaluate(target, series)
        evaluations.append(evaluation)
        click.echo(
            f"  verdict={evaluation.verdict.value} "
            f"uptime={evaluation.monthly_uptime * 100:.4f}% "
            f"slo={evaluation.slo_target * 100:.4f}% "
            f"sla={evaluation.sla_floor * 100:.4f}%"
        )

    _write_reports(config, evaluations)
    sys.exit(_decide_exit_code(config, evaluations))


def _write_reports(config: Config, evaluations: list[TargetEvaluation]) -> None:
    output_dir = Path(config.output.directory)
    output_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    base = output_dir / f"sla-{timestamp}"

    if "markdown" in config.output.formats:
        path = base.with_suffix(".md")
        path.write_text(render_markdown(config.project, evaluations), encoding="utf-8")
        click.echo(f"wrote {path}")
    if "json" in config.output.formats:
        path = base.with_suffix(".json")
        path.write_text(render_json(config.project, evaluations), encoding="utf-8")
        click.echo(f"wrote {path}")


def _decide_exit_code(config: Config, evaluations: list[TargetEvaluation]) -> int:
    if any(ev.verdict == Verdict.BREACHING for ev in evaluations):
        return EXIT_BREACH if config.fail_on_breach else EXIT_OK
    if any(ev.verdict == Verdict.WARNING for ev in evaluations):
        return EXIT_WARNING if config.fail_on_breach else EXIT_OK
    return EXIT_OK


@cli.command(name="validate")
@click.option(
    "--config",
    "config_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    required=True,
)
def validate_command(config_path: Path) -> None:
    """Validate a config file without making any API calls."""
    try:
        config = load_config(config_path)
    except ConfigError as exc:
        click.echo(f"config error: {exc}", err=True)
        sys.exit(EXIT_ERROR)
    click.echo(
        f"config OK: project={config.project}, "
        f"targets={len(config.targets)}, eval_window={config.eval_window}"
    )


def main() -> None:
    cli(prog_name="slawatch")


if __name__ == "__main__":
    main()
