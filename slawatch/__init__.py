"""slawatch: GCP SLA compliance monitoring."""

__version__ = "0.1.0"

from .config import Config, Target, load_config
from .evaluator import TargetEvaluation, Verdict, evaluate

__all__ = [
    "Config",
    "Target",
    "TargetEvaluation",
    "Verdict",
    "evaluate",
    "load_config",
    "__version__",
]
