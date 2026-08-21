"""Common types and helpers."""

from qcu.common.normalize import (
    NORMALIZED_ROLES,
    clean_text,
    from_aria,
    from_ax,
    from_uia,
    normalize,
)
from qcu.common.types import (
    ACTION_TYPES,
    DEFAULT_CRITICAL_ACTIONS,
    Action,
    Element,
    LayerResult,
    Observation,
    Rect,
)

__all__ = [
    "ACTION_TYPES",
    "DEFAULT_CRITICAL_ACTIONS",
    "Action",
    "Element",
    "LayerResult",
    "NORMALIZED_ROLES",
    "Observation",
    "Rect",
    "clean_text",
    "from_aria",
    "from_ax",
    "from_uia",
    "normalize",
]