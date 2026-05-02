"""Tests for config loading and validation."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from slawatch.config import load_config, parse_duration
from slawatch.exceptions import ConfigError


def write_config(tmp_path: Path, content: str) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(content, encoding="utf-8")
    return path


def test_minimal_valid_config(tmp_path: Path) -> None:
    config_path = write_config(
        tmp_path,
        """
project: test-project
targets:
  - name: api
    kind: cloud_run
    service: api
    region: us-central1
    slo:
      availability: 0.999
""",
    )
    config = load_config(config_path)
    assert config.project == "test-project"
    assert len(config.targets) == 1
    assert config.targets[0].name == "api"
    assert config.eval_window == "30d"


def test_duplicate_target_names_rejected(tmp_path: Path) -> None:
    config_path = write_config(
        tmp_path,
        """
project: test-project
targets:
  - name: api
    kind: cloud_run
    service: api
    region: us-central1
    slo:
      availability: 0.999
  - name: api
    kind: cloud_run
    service: api2
    region: us-central1
    slo:
      availability: 0.999
""",
    )
    with pytest.raises(ConfigError, match="unique"):
        load_config(config_path)


def test_invalid_yaml_raises_config_error(tmp_path: Path) -> None:
    config_path = write_config(tmp_path, "{ this: is: not: yaml")
    with pytest.raises(ConfigError):
        load_config(config_path)


def test_missing_file_raises_config_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "nope.yaml")


def test_no_targets_rejected(tmp_path: Path) -> None:
    config_path = write_config(
        tmp_path,
        """
project: test-project
targets: []
""",
    )
    with pytest.raises(ConfigError):
        load_config(config_path)


def test_invalid_eval_window_rejected(tmp_path: Path) -> None:
    config_path = write_config(
        tmp_path,
        """
project: test-project
eval_window: forever
targets:
  - name: api
    kind: cloud_run
    service: api
    region: us-central1
    slo:
      availability: 0.999
""",
    )
    with pytest.raises(ConfigError):
        load_config(config_path)


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("60s", timedelta(seconds=60)),
        ("5m", timedelta(minutes=5)),
        ("1h", timedelta(hours=1)),
        ("7d", timedelta(days=7)),
        ("30d", timedelta(days=30)),
    ],
)
def test_parse_duration(raw: str, expected: timedelta) -> None:
    assert parse_duration(raw) == expected


def test_parse_duration_rejects_garbage() -> None:
    with pytest.raises(ValueError):
        parse_duration("five days")
