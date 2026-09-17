"""Truncation helpers shared across desktop layers.

Why: Chromium-class browsers expose 400+ interactive elements per window. A
naive observe() returns the whole tree as ~30-50 KB of JSON, drowning the
high-signal ``web_app`` probe at the top of the raw_tree and forcing the
CLI / handler code to truncate from above. The CLI already accepts
``--limit / --offset / --compact`` flags; this module gives the desktop
layers the same cheap pagination we get on web.
"""

from __future__ import annotations

from typing import Any, Optional

from qcu.common.types import Element


def apply_pagination_and_compact(
    elements: list[Element],
    *,
    limit: Optional[int] = None,
    offset: int = 0,
    compact: bool = False,
    role_filter: Optional[str] = None,
    name_query: Optional[str] = None,
) -> tuple[list[Element], dict[str, Any]]:
    """Apply role filter, name substring, offset/limit, and compact to the
    list. Returns ``(sliced, meta)`` where meta has ``n_returned``,
    ``n_total`` (post-filter, pre-pagination), ``n_interactive`` (pre-filter),
    and ``tree_truncated`` (whether limit/offset cut anything).
    """
    n_interactive = len(elements)
    # Pre-filter: role
    filtered = elements
    if role_filter:
        filtered = [e for e in filtered if e.role == role_filter]
    # Pre-filter: name substring
    if name_query:
        q = name_query.lower()
        filtered = [
            e for e in filtered
            if (e.name and q in e.name.lower())
            or (e.value and q in str(e.value).lower())
        ]
    n_total = len(filtered)

    # Pagination
    start = max(0, offset)
    end: Optional[int] = None
    if limit is not None and limit > 0:
        end = start + limit
    sliced = filtered[start:end] if end is not None else filtered[start:]

    truncated = (n_total > len(sliced)) or start > 0

    # Compact mode: drop null/bulky fields per element. We rebuild Element
    # objects with only the fields the caller needs to plan action (ref +
    # role + name + value if set). Bounds are kept — they're small and the
    # coordinate fallback path needs them.
    if compact:
        sliced = [
            Element(
                ref=e.ref,
                role=e.role,
                name=e.name,           # name/url usually what the LLM matches by
                value=e.value if e.value else None,
                bounds=e.bounds,
                enabled=e.enabled,
                focused=e.focused,
                properties={k: v for k, v in e.properties.items()
                            if k in {"checked", "selected", "expanded", "pressed", "readonly", "required"}},
            )
            for e in sliced
        ]

    meta = {
        "n_interactive": n_interactive,
        "n_total": n_total,
        "n_returned": len(sliced),
        "tree_truncated": truncated,
    }
    return sliced, meta
