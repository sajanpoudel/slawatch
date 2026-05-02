"""Configuration model for slawatch."""

from __future__ import annotations

import re
from datetime import timedelta
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

from .exceptions import ConfigError

DURATION_PATTERN = re.compile(r"^(\d+)\s*(s|m|h|d)$")


def parse_duration(value: str) -> timedelta:
    """Parse '5m', '1h', '7d', '30d' into a timedelta."""
    match = DURATION_PATTERN.match(value.strip().lower())
    if not match:
        raise ValueError(f"invalid duration {value!r}, expected formats like 5m, 1h, 7d, 30d")
    amount = int(match.group(1))
    unit = match.group(2)
    if unit == "s":
        return timedelta(seconds=amount)
    if unit == "m":
        return timedelta(minutes=amount)
    if unit == "h":
        return timedelta(hours=amount)
    return timedelta(days=amount)


class SloConfig(BaseModel):
    """
    The team's internal target. Distinct from the SLA floor.

    availability: a fraction (0.999 = three nines).
    latency_p99_ms: optional. If set, the evaluator also checks p99 latency.
    """

    availability: float = Field(..., ge=0.0, le=1.0)
    latency_p99_ms: float | None = Field(default=None, ge=0)


class SlaOverride(BaseModel):
    """
    Optional metadata about the deployment shape. Used to look up the right
    contractual SLA floor.
    """

    gpu: bool = False
    zonal_redundancy: bool = True


class Target(BaseModel):
    """One thing to monitor."""

    name: str
    kind: Literal["cloud_run"]
    service: str
    region: str
    slo: SloConfig
    sla: SlaOverride = Field(default_factory=SlaOverride)
    revision: str | None = Field(
        default=None,
        description="Optional. Restrict the query to a single revision.",
    )

    @field_validator("name")
    @classmethod
    def _validate_name(cls, value: str) -> str:
        if not value or len(value) > 128:
            raise ValueError("target name must be 1 to 128 characters")
        return value


class OutputConfig(BaseModel):
    """Where reports go."""

    formats: list[Literal["markdown", "json"]] = Field(default_factory=lambda: ["markdown"])
    directory: str = "./reports"


class Config(BaseModel):
    """Top-level config."""

    project: str
    eval_window: str = "30d"
    targets: list[Target]
    output: OutputConfig = Field(default_factory=OutputConfig)
    fail_on_breach: bool = Field(
        default=True,
        description=(
            "When true, the CLI exits with a non-zero status if any target is "
            "below its SLO. This is what makes the tool useful inside a "
            "scheduled CI job."
        ),
    )

    @field_validator("targets")
    @classmethod
    def _validate_targets(cls, value: list[Target]) -> list[Target]:
        if not value:
            raise ValueError("at least one target is required")
        names = [target.name for target in value]
        if len(set(names)) != len(names):
            raise ValueError("target names must be unique")
        return value

    @model_validator(mode="after")
    def _validate_window(self) -> Config:
        # Validate that the duration parses, even though we keep it as a string
        # in the model for readability in dumps.
        parse_duration(self.eval_window)
        return self

    def eval_window_delta(self) -> timedelta:
        return parse_duration(self.eval_window)


def load_config(path: str | Path) -> Config:
    """Load and validate a YAML config file."""
    config_path = Path(path)
    if not config_path.is_file():
        raise ConfigError(f"config file not found: {config_path}")

    try:
        with config_path.open("r", encoding="utf-8") as handle:
            raw = yaml.safe_load(handle)
    except yaml.YAMLError as exc:
        raise ConfigError(f"failed to parse YAML in {config_path}: {exc}") from exc

    if not isinstance(raw, dict):
        raise ConfigError(f"config must be a mapping at the top level, got {type(raw).__name__}")

    try:
        return Config.model_validate(raw)
    except ValidationError as exc:
        raise ConfigError(f"config validation failed:\n{exc}") from exc
