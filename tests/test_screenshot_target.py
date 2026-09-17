"""Risk tests: screenshots and actions cannot change targets or replay input."""
import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from qcu.common.types import Action, LayerResult
from qcu.layers.screenshot_fallback import ScreenshotFallbackLayer


@pytest.fixture
def session(monkeypatch, tmp_path):
    from qcu import session as store
    monkeypatch.setattr(store, "SESSION_PATH", tmp_path / "session.json")
    monkeypatch.setenv("QCU_HOME", str(tmp_path))
    return store.new(context="desktop", layer="desktop_ax")


class Desktop:
    _target_scope = {"pid": 12, "app": "Test", "window_id": 34, "window_title": "Fixture"}
    def __init__(self):
        self.calls = []
    def _screenshot(self, path):
        Path(path).write_bytes(b"target PNG")
        return LayerResult(True, "desktop_ax", data={"target": dict(self._target_scope),
            "coordinate_origin": {"x": 100, "y": 200}, "coordinate_scale": 2.0})
    def _click(self, *args):
        raise AssertionError("fallback must use backend action contract")
    def act(self, action):
        self.calls.append(action)
        return LayerResult(True, "desktop_ax", dispatch_state="sent", outcome="unknown")


@pytest.fixture
def desktop(monkeypatch, session):
    from qcu.layers import runtime
    from qcu import platforms
    d = Desktop()
    requested = []
    def layer(name):
        requested.append(name)
        assert name == "desktop_ax", "desktop fallback requested a browser"
        return d
    monkeypatch.setattr(platforms, "desktop_backend_name", lambda: "desktop_ax")
    monkeypatch.setattr(runtime, "get_layer", layer)
    d.requested = requested
    return d


def test_desktop_capture_and_click_never_load_web(desktop):
    layer = ScreenshotFallbackLayer()
    obs = layer.observe()
    result = layer.act(Action("click", {"x": 120, "y": 230}))
    assert obs.context == "desktop" and obs.routing_meta["captured"]
    assert result.dispatch_state == "sent" and result.outcome == "unknown"
    assert len(desktop.calls) == 1
    assert set(desktop.requested) == {"desktop_ax"}


def test_desktop_missing_screenshot_does_not_try_browser(desktop):
    desktop._screenshot = None
    obs = ScreenshotFallbackLayer().observe()
    assert not obs.routing_meta["captured"]
    assert obs.routing_meta["reason"] == "missing_capability"
    assert set(desktop.requested) == {"desktop_ax"}


def test_explicit_coordinates_cannot_override_bound_window(desktop):
    result = ScreenshotFallbackLayer().act(Action("click", {"x": 120, "y": 230, "window": "Other"}))
    assert result.dispatch_state == "not_sent"
    assert result.data["reason"] == "target_mismatch"
    assert desktop.calls == []


def test_native_ref_cannot_be_reinterpreted_as_coordinates(desktop):
    result = ScreenshotFallbackLayer().act(Action("click", {"ref": "ref_1", "x": 120, "y": 230}))
    assert result.dispatch_state == "not_sent" and not desktop.calls


def test_grounding_transforms_pixels_to_bound_window_points(desktop, monkeypatch):
    from qcu.layers import screenshot_fallback as mod
    monkeypatch.setattr(mod, "get_grounding", lambda name: SimpleNamespace(ground=lambda *a, **k:
        SimpleNamespace(coords=[{"x": 40, "y": 60}])))
    result = ScreenshotFallbackLayer().act(Action("click", {"grounding": "local", "query": "Save"}))
    assert result.dispatch_state == "sent"
    assert desktop.calls[0].params["x"] == 120
    assert desktop.calls[0].params["y"] == 230


def test_grounding_target_change_aborts_before_dispatch(desktop, monkeypatch):
    from qcu.layers import screenshot_fallback as mod
    def ground(*a, **k):
        desktop._target_scope = {**desktop._target_scope, "window_id": 99}
        return SimpleNamespace(coords=[{"x": 40, "y": 60}])
    monkeypatch.setattr(mod, "get_grounding", lambda name: SimpleNamespace(ground=ground))
    result = ScreenshotFallbackLayer().act(Action("click", {"grounding": "local"}))
    assert result.dispatch_state == "not_sent"
    assert result.data["reason"] == "target_changed"
    assert desktop.calls == []


def test_ambiguous_grounding_is_not_arbitrarily_chosen(desktop, monkeypatch):
    from qcu.layers import screenshot_fallback as mod
    monkeypatch.setattr(mod, "get_grounding", lambda name: SimpleNamespace(ground=lambda *a, **k:
        SimpleNamespace(coords=[{"x": 1, "y": 2}, {"x": 3, "y": 4}])))
    result = ScreenshotFallbackLayer().act(Action("click", {"grounding": "local"}))
    assert result.data["reason"] == "ambiguous" and not desktop.calls


def test_desktop_dispatch_exception_is_unknown_and_not_replayed(desktop):
    def partial(action):
        desktop.calls.append(action)
        raise TimeoutError("lost acknowledgement")
    desktop.act = partial
    result = ScreenshotFallbackLayer().act(Action("click", {"x": 120, "y": 230}))
    assert result.dispatch_state == "unknown"
    assert result.data["retry_safe"] is False
    assert len(desktop.calls) == 1


def test_no_session_cannot_launch_browser(monkeypatch, tmp_path):
    from qcu import session as store
    from qcu.layers import runtime
    monkeypatch.setattr(store, "SESSION_PATH", tmp_path / "missing.json")
    monkeypatch.setattr(runtime, "get_layer", lambda name: pytest.fail("must not create any backend"))
    assert ScreenshotFallbackLayer().observe().routing_meta["reason"] == "target_unbound"


def test_web_requires_connected_page_without_launch(session, monkeypatch):
    from qcu import session as store
    from qcu.layers import runtime
    store.patch(context="web", layer="web_a11y")
    web = SimpleNamespace(_page=None, _loop=None, _ensure_browser=lambda **k: pytest.fail("must not launch browser"))
    monkeypatch.setattr(runtime, "get_layer", lambda name: web if name == "web_a11y" else pytest.fail("wrong backend"))
    result = ScreenshotFallbackLayer().act(Action("click", {"x": 10, "y": 20}))
    assert result.dispatch_state == "not_sent"
    assert result.data["reason"] == "missing_capability"


def test_web_dispatch_exception_never_uses_desktop(session, monkeypatch):
    from qcu import session as store
    from qcu.layers import runtime
    store.patch(context="web", layer="web_a11y")
    loop = asyncio.new_event_loop()
    calls = []
    async def evaluate(script):
        return 1234
    async def click(action):
        calls.append(action)
        raise TimeoutError("delivered; no reply")
    page = SimpleNamespace(is_closed=lambda: False, url="http://fixture/", evaluate=evaluate)
    web = SimpleNamespace(_page=page, _loop=loop, _act_async=click)
    monkeypatch.setattr(runtime, "get_layer", lambda name: web if name == "web_a11y" else pytest.fail("wrong backend"))
    try:
        result = ScreenshotFallbackLayer().act(Action("double_click", {"x": 10, "y": 20}))
        assert result.dispatch_state == "unknown" and len(calls) == 1
        assert calls[0].type == "double_click"
    finally:
        loop.close()


def test_grounding_window_movement_invalidates_pixel_location(desktop, monkeypatch):
    from qcu.layers import screenshot_fallback as mod
    geometry = {"X": 100, "Y": 200, "Width": 300, "Height": 200}
    desktop._target_window = object()
    desktop._window_identity = lambda *a: {"window_id": 34, "bounds": dict(geometry)}
    def ground(*a, **k):
        geometry["X"] += 20
        return SimpleNamespace(coords=[{"x": 40, "y": 60}])
    monkeypatch.setattr(mod, "get_grounding", lambda name: SimpleNamespace(ground=ground))
    result = ScreenshotFallbackLayer().act(Action("click", {"grounding": "local"}))
    assert result.dispatch_state == "not_sent"
    assert result.data["reason"] == "target_changed" and not desktop.calls
