"""Overlay installation must fail closed before touching an unknown runtime."""
import hashlib
import importlib.util
from pathlib import Path

import pytest

PATH = Path(__file__).resolve().parents[2] / 'modules/savant_security/savant_patches/apply_patches.py'


@pytest.fixture
def overlay(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location('patch_loader_under_test', PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    package = tmp_path / 'savant'
    patches = tmp_path / 'patches'
    (package / 'deepstream').mkdir(parents=True)
    patches.mkdir()
    target = package / 'deepstream/pipeline.py'
    target.write_text('upstream')
    patch = patches / 'pipeline.py'
    patch.write_text('patched')
    addition = patches / 'decoded_frame_guard.py'
    addition.write_text('guard')
    md5 = lambda text: hashlib.md5(text.encode()).hexdigest()
    monkeypatch.setattr(module, 'PATCH_DIR', patches)
    monkeypatch.setattr(module, '_locate_savant_package', lambda: package)
    monkeypatch.setattr(module, 'PATCHES', (('deepstream/pipeline.py', 'pipeline.py', md5('upstream'), md5('patched')),))
    monkeypatch.setattr(module, 'UPGRADE_BASELINES', {'deepstream/pipeline.py': {md5('legacy')}})
    monkeypatch.setattr(module, 'ADDITIONS', (('deepstream/decoded_frame_guard.py', 'decoded_frame_guard.py', md5('guard')),))
    monkeypatch.delenv('SAVANT_PATCH_ENABLED', raising=False)
    monkeypatch.delenv('SAVANT_PATCH_ENFORCE', raising=False)
    return module, package, patches, target


@pytest.mark.parametrize('before', ['upstream', 'legacy', 'patched'])
def test_known_runtime_upgrade_is_complete_and_idempotent(overlay, before):
    module, package, patches, target = overlay
    target.write_text(before)
    assert module.main() == 0
    assert target.read_text() == 'patched'
    assert (package / 'deepstream/decoded_frame_guard.py').read_text() == 'guard'
    assert module.main() == 0


def test_unknown_framework_prevents_partial_install(overlay):
    module, package, patches, target = overlay
    target.write_text('unknown')
    assert module.main() == 1
    assert target.read_text() == 'unknown'
    assert not (package / 'deepstream/decoded_frame_guard.py').exists()


@pytest.mark.parametrize('damage', ['missing_guard', 'changed_guard', 'changed_pipeline', 'existing_unknown_guard'])
def test_incomplete_or_unknown_guard_prevents_pipeline_replacement(overlay, damage):
    module, package, patches, target = overlay
    if damage == 'missing_guard':
        (patches / 'decoded_frame_guard.py').unlink()
    elif damage == 'changed_guard':
        (patches / 'decoded_frame_guard.py').write_text('unexpected')
    elif damage == 'changed_pipeline':
        (patches / 'pipeline.py').write_text('unexpected')
    else:
        (package / 'deepstream/decoded_frame_guard.py').write_text('unexpected')
    assert module.main() == 1
    assert target.read_text() == 'upstream'


def test_checkout_matches_every_declared_checksum():
    spec = importlib.util.spec_from_file_location('patch_loader_checksums', PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for _, name, _, checksum in module.PATCHES:
        assert module._md5(module.PATCH_DIR / name) == checksum
    for _, name, checksum in module.ADDITIONS:
        assert module._md5(module.PATCH_DIR / name) == checksum
