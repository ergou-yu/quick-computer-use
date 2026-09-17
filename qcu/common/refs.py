"""Native handles extend web's observation-generation refs with live scope.

Refs are opaque public tokens, not durable locators. A new backend process must
observe again; serializing a token never serializes its native handle.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any


class NativeRefRegistry:
    def __init__(self, backend: str) -> None:
        self.backend = backend
        self.lifecycle = uuid.uuid4().hex
        self.generation = 0
        self.target: dict[str, Any] = {}
        self._target_key = ""

    @staticmethod
    def _key(target: dict[str, Any]) -> str:
        data = json.dumps(target, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return hashlib.sha256(data.encode()).hexdigest()[:20]

    def begin(self, target: dict[str, Any]) -> dict[str, Any]:
        self.generation += 1
        self.target = dict(target)
        self._target_key = self._key(self.target)
        return self.scope

    @property
    def scope(self) -> dict[str, Any]:
        return {"backend": self.backend, "lifecycle": self.lifecycle,
                "target": dict(self.target), "observation_generation": self.generation}

    def issue(self, index: int) -> str:
        if not self._target_key:
            raise RuntimeError("begin an observation before issuing native refs")
        return (f"{self.backend}:{self.lifecycle}:{self._target_key}:"
                f"obs_{self.generation}:ref_{index}")

    def validate(self, ref: str, target: dict[str, Any] | None = None) -> tuple[bool, str]:
        if not isinstance(ref, str):
            return False, "invalid_ref"
        parts = ref.split(":")
        if len(parts) != 5:
            return False, "legacy_ref_requires_observe"
        backend, lifecycle, target_key, generation, index = parts
        if backend != self.backend:
            return False, "ref_backend_mismatch"
        if lifecycle != self.lifecycle:
            return False, "ref_lifecycle_expired"
        if target_key != self._target_key or (target is not None and target_key != self._key(target)):
            return False, "ref_target_mismatch"
        if generation != f"obs_{self.generation}":
            return False, "stale_ref"
        if not index.startswith("ref_") or not index[4:].isdigit():
            return False, "invalid_ref"
        return True, "current"

    def invalidate(self) -> None:
        self.lifecycle = uuid.uuid4().hex
        self.generation = 0
        self.target = {}
        self._target_key = ""
