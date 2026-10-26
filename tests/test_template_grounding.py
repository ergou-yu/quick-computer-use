"""Real local image matching; fake desktop boundaries are identified separately."""
import struct
import subprocess
import sys

import pytest

cv2 = pytest.importorskip("cv2")
np = pytest.importorskip("numpy")

from qcu.common.types import Action
from qcu.grounding import get
from qcu.grounding.template import TemplateError, TemplateGrounder
from qcu.layers.screenshot_fallback import ScreenshotFallbackLayer
from tests.test_screenshot_target import desktop, session


def png(image):
    return cv2.imencode('.png', image)[1].tobytes()


@pytest.fixture
def scene(tmp_path):
    rng = np.random.default_rng(47)
    icon = rng.integers(0, 256, (20, 24, 3), dtype=np.uint8)
    path = tmp_path/'icon.png'
    path.write_bytes(png(icon))
    image = np.full((160, 240, 3), 100, dtype=np.uint8)
    image[40:60, 60:84] = icon
    return image, icon, path


def test_exact_match_and_scale_search(scene):
    image, icon, path = scene
    result = TemplateGrounder(template_path=str(path)).ground(png(image))
    assert len(result.coords) == 1 and result.coords[0]['bbox'] == [60, 40, 24, 20]
    enlarged = np.full_like(image, 100)
    enlarged[50:90, 70:118] = cv2.resize(icon, (48, 40))
    result = TemplateGrounder(template_path=str(path), scales=[1, 1.5, 2]).ground(png(enlarged))
    assert len(result.coords) == 1 and result.coords[0]['bbox'] == [70, 50, 48, 40]


def test_duplicate_icons_stay_ambiguous_even_with_max_one(scene):
    image, icon, path = scene
    image[90:110, 160:184] = icon
    result = TemplateGrounder(template_path=str(path)).ground(png(image), max_results=1)
    assert len(result.coords) == 2


def test_missing_icon_is_not_a_guess(scene):
    image, _, path = scene
    image[:] = 0
    assert TemplateGrounder(template_path=str(path)).ground(png(image)).coords == []


@pytest.mark.parametrize('color', [(20, 20, 20), (0, 0, 255)])
def test_constant_templates_are_rejected(scene, color):
    image, icon, path = scene
    icon[:] = color
    path.write_bytes(png(icon))
    with pytest.raises(TemplateError, match='spatial detail'):
        TemplateGrounder(template_path=str(path)).ground(png(image))


def test_transparent_templates_are_rejected(scene):
    image, icon, path = scene
    rgba = np.dstack([icon, np.zeros(icon.shape[:2], np.uint8)])
    path.write_bytes(png(rgba))
    with pytest.raises(TemplateError, match='opaque'):
        TemplateGrounder(template_path=str(path)).ground(png(image))


@pytest.mark.parametrize('options', [{'threshold': float('nan')}, {'scales': []}, {'scales': [float('inf')]}, {'margin': -1}])
def test_invalid_configuration_fails_before_capture(options):
    with pytest.raises(TemplateError):
        TemplateGrounder(**options)


def test_declared_huge_png_never_decodes(scene):
    image, _, path = scene
    huge = bytearray(png(image))
    huge[16:24] = struct.pack('>II', 100000, 100000)
    with pytest.raises(TemplateError, match='dimensions'):
        TemplateGrounder(template_path=str(path)).ground(bytes(huge))


def test_template_import_stays_off_semantic_path():
    code = "import sys; import qcu.layers.desktop_uia; from qcu.grounding import available; assert 'template' in available(); assert 'cv2' not in sys.modules; assert 'numpy' not in sys.modules"
    assert subprocess.run([sys.executable, '-c', code], capture_output=True).returncode == 0


def test_bound_fallback_rechecks_and_keeps_dispatch_unknown(desktop, scene):
    image, _, path = scene
    captures=[]
    original = desktop._screenshot
    def capture(output):
        result = original(output)
        from pathlib import Path
        Path(output).write_bytes(png(image))
        captures.append(output)
        return result
    desktop._screenshot = capture
    result = ScreenshotFallbackLayer().act(Action('click', {'grounding': 'template', 'template_path': str(path)}))
    assert len(captures) == 2 and len(desktop.calls) == 1
    assert desktop.calls[0].params['x'] == 136 and desktop.calls[0].params['y'] == 225
    assert result.dispatch_state == 'sent' and result.outcome == 'unknown'
    assert result.data['grounding']['rechecked']


@pytest.mark.parametrize('change', ['move', 'duplicate', 'disappear'])
def test_internal_layout_changes_never_dispatch(desktop, scene, change):
    image, icon, path = scene
    count=0
    original = desktop._screenshot
    def capture(output):
        nonlocal count
        count += 1
        result = original(output)
        if count == 2:
            if change != 'duplicate': image[:] = 100
            if change != 'disappear': image[90:110, 160:184] = icon
        from pathlib import Path
        Path(output).write_bytes(png(image))
        return result
    desktop._screenshot = capture
    result = ScreenshotFallbackLayer().act(Action('click', {'grounding': 'template', 'template_path': str(path)}))
    assert result.dispatch_state == 'not_sent' and desktop.calls == []
    assert result.data['reason'] == dict(move='target_changed', duplicate='ambiguous', disappear='locate_failed')[change]


@pytest.mark.parametrize('kind,params', [('type', {}), ('click', {'x': 1, 'y': 2})])
def test_template_cannot_silently_ignore_its_localization_contract(desktop, kind, params):
    result = ScreenshotFallbackLayer().act(Action(kind, dict(params, grounding='template')))
    assert result.dispatch_state == 'not_sent' and result.data['reason'] == 'invalid_template'
    assert desktop.calls == []


def test_optional_dependency_absence_is_explicit(desktop, scene, monkeypatch):
    _, _, path = scene
    monkeypatch.setitem(sys.modules, 'cv2', None)
    result = ScreenshotFallbackLayer().act(Action('click', {'grounding': 'template', 'template_path': str(path)}))
    assert result.dispatch_state == 'not_sent' and result.data['reason'] == 'missing_capability'
    assert desktop.calls == []
