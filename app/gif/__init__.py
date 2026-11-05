"""GIF 子包。"""

from .assembler import encode_gif, normalize_frame
from .generator import (
    CancelFlag,
    GenerationCancelled,
    GenerationReport,
    GifGenerator,
    GifParams,
    parse_size,
)
from .planner import ClosureCheck, FrameDecision, PlanScript, Planner

__all__ = [
    "encode_gif",
    "normalize_frame",
    "CancelFlag",
    "GenerationCancelled",
    "GenerationReport",
    "GifGenerator",
    "GifParams",
    "parse_size",
    "ClosureCheck",
    "FrameDecision",
    "PlanScript",
    "Planner",
]
