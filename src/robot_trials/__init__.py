"""人形机器人结构化试验数据的基础组件。"""

from .contracts import Observation, Protocol, ValidationError
from .analysis import ALGORITHM_VERSION, analyze, bootstrap_mean_interval
from .diffing import diff_protocols
from .numeric import NumericSummary, WilsonInterval
from .service import TrialService

__all__ = [
    "NumericSummary",
    "Observation",
    "Protocol",
    "ValidationError",
    "WilsonInterval",
    "ALGORITHM_VERSION",
    "TrialService",
    "analyze",
    "bootstrap_mean_interval",
    "diff_protocols",
]

__version__ = "0.1.0"
