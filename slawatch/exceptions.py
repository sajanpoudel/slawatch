"""Exception types used across the package."""

from __future__ import annotations


class SlaWatchError(Exception):
    """Base class for all errors raised by slawatch."""


class ConfigError(SlaWatchError):
    """Raised when the YAML config is invalid or unparseable."""


class MetricFetchError(SlaWatchError):
    """Raised when Cloud Monitoring fails to return data after retries."""


