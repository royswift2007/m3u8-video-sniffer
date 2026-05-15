"""
Offline smoke checks for staged component update installation.

No UI, no real network, and no writes to the repository's real bin directory are
performed. All targets, staged executables, and backups live under a temporary
directory created by this script.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from dataclasses import replace
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.component_update_downloader import ComponentDownloadStageResult
from core.component_update_installer import ComponentUpdateInstaller
from core.component_update_models import ComponentVersionInfo
from core.dependency_manifest import DependencyEntry, DependencyManifest


class FakeVersionProbe:
    def __init__(self, versions: list[str | None] | None = None, errors: list[str | None] | None = None):
        self.versions = list(versions or [])
        self.errors = list(errors or [])
        self.calls: list[str] = []

    def probe(self, entry: DependencyEntry) -> ComponentVersionInfo:
        self.calls.append(entry.id)
        exists = entry.path.exists()
        version = self.versions.pop(0) if self.versions else _read_version(entry.path)
        error = self.errors.pop(0) if self.errors else None
        return ComponentVersionInfo(
            component_id=entry.id,
            label=entry.label,
            path=str(entry.path),
            exists=exists,
            version=version,
            raw_output=version,
            error=error,
        )


def _exe_bytes(version: str) -> bytes:
    return f"MZ fake exe version={version}\n".encode("utf-8")


def _read_version(path: Path) -> str | None:
    if not path.exists():
        return None
    marker = "version="
    text = path.read_text(encoding="utf-8", errors="replace")
    if marker not in text:
        return "available"
    return text.split(marker, 1)[1].splitlines()[0].strip() or None


def _manifest(path: Path, target_relative: str, *, post_install_version_required: bool | None = None) -> DependencyManifest:
    version_spec = {
        "command": ["{path}", "--version"],
        "regex": r"(?P<version>\d+\.\d+\.\d+)",
        "normalize": "semantic",
        "timeout": 5,
    }
    update_spec = {
        "enabled": True,
        "release_source": "direct",
        "latest_url": "https://example.invalid/fake_tool.exe",
        "asset_pattern": "fake_tool.exe",
        "version_source": "url",
        "install_strategy": "replace_file",
        "requires_process_free": True,
    }
    if post_install_version_required is not None:
        version_spec["post_install_required"] = post_install_version_required
        update_spec["post_install_version_required"] = post_install_version_required
    payload = {
        "required": [
            {
                "id": "fake_tool",
                "label": "Fake Tool",
                "path": target_relative.replace("\\", "/"),
                "version": version_spec,
                "update": update_spec,
            }
        ],
        "recommended": [],
        "optional": [],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return DependencyManifest(path)


def _entry(manifest: DependencyManifest) -> DependencyEntry:
    return manifest.get_update_enabled_entries(include_recommended=False)[0]


def _stage(tmp_root: Path, version: str = "2.0.0", component_id: str = "fake_tool") -> ComponentDownloadStageResult:
    staging_dir = tmp_root / "component_updates" / component_id / "run" / "staging"
    staging_dir.mkdir(parents=True, exist_ok=True)
    staged = staging_dir / "fake_tool.exe"
    payload = _exe_bytes(version)
    staged.write_bytes(payload)
    import hashlib as _hashlib
    digest = _hashlib.sha256(payload).hexdigest()
    return ComponentDownloadStageResult(
        component_id=component_id,
        success=True,
        code="ok",
        message="staged",
        staging_dir=str(staging_dir),
        staged_exe_path=str(staged),
        asset_name="fake_tool.exe",
        bytes_downloaded=staged.stat().st_size,
        sha256=digest,
        weak_validation=False,
    )


def _installer(tmp_root: Path, probe: FakeVersionProbe | None = None, replace_func=None) -> ComponentUpdateInstaller:
    return ComponentUpdateInstaller(
        backup_root=tmp_root / "component_backups",
        version_probe=probe or FakeVersionProbe(),
        replace_func=replace_func,
    )


def _assert_under(child: Path | str | None, parent: Path) -> None:
    assert child, "expected path"
    resolved_child = Path(child).resolve()
    resolved_parent = parent.resolve()
    assert resolved_child == resolved_parent or resolved_parent in resolved_child.parents, (resolved_child, resolved_parent)


def assert_first_install_without_target() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_root = Path(tmp)
        target = tmp_root / "sandbox_bin" / "fake_tool.exe"
        manifest = _manifest(tmp_root / "deps.json", str(target))
        result = _installer(tmp_root).install_staged_update(_stage(tmp_root, "2.0.0"), _entry(manifest), expected_version="2.0.0")
        assert result.success, result
        assert result.code == "ok", result
        assert target.exists(), result
        assert target.read_bytes() == _exe_bytes("2.0.0"), result
        assert result.backup_path is None, result
        _assert_under(result.target_path, tmp_root)
        assert not (PROJECT_ROOT / "bin" / "fake_tool.exe").exists(), "real bin must not be written"


def assert_existing_target_backup_replace_success() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_root = Path(tmp)
        target = tmp_root / "sandbox_bin" / "fake_tool.exe"
        target.parent.mkdir(parents=True)
        target.write_bytes(_exe_bytes("1.0.0"))
        manifest = _manifest(tmp_root / "deps.json", str(target))
        result = _installer(tmp_root).install_staged_update(_stage(tmp_root, "2.0.0"), _entry(manifest), expected_version="2.0.0")
        assert result.success, result
        assert result.old_version == "1.0.0", result
        assert result.new_version == "2.0.0", result
        assert result.backup_path, result
        backup = Path(result.backup_path)
        assert backup.exists(), result
        assert "fake_tool" in backup.name and "1.0.0" in backup.name, backup.name
        assert backup.read_bytes() == _exe_bytes("1.0.0"), result
        assert target.read_bytes() == _exe_bytes("2.0.0"), result
        _assert_under(backup, tmp_root / "component_backups")


def assert_staged_exe_missing_objectized() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_root = Path(tmp)
        target = tmp_root / "sandbox_bin" / "fake_tool.exe"
        manifest = _manifest(tmp_root / "deps.json", str(target))
        stage = _stage(tmp_root, "2.0.0")
        Path(stage.staged_exe_path or "").unlink()
        result = _installer(tmp_root).install_staged_update(stage, _entry(manifest), expected_version="2.0.0")
        assert not result.success, result
        assert result.code == "staged_exe_missing", result
        assert not target.exists(), "target must not be created when staged exe is missing"


def assert_post_check_failure_triggers_rollback() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_root = Path(tmp)
        target = tmp_root / "sandbox_bin" / "fake_tool.exe"
        target.parent.mkdir(parents=True)
        target.write_bytes(_exe_bytes("1.0.0"))
        manifest = _manifest(tmp_root / "deps.json", str(target))
        probe = FakeVersionProbe(versions=["1.0.0", "2.0.0"], errors=[None, "forced post-check failure"])
        result = _installer(tmp_root, probe=probe).install_staged_update(_stage(tmp_root, "2.0.0"), _entry(manifest), expected_version="2.0.0")
        assert not result.success, result
        assert result.code == "post_check_failed", result
        assert result.rollback_attempted, result
        assert result.rollback_success is True, result
        assert target.read_bytes() == _exe_bytes("1.0.0"), result


def assert_streamlink_optional_post_check_warning_does_not_rollback() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_root = Path(tmp)
        target = tmp_root / "sandbox_bin" / "fake_tool.exe"
        target.parent.mkdir(parents=True)
        target.write_bytes(_exe_bytes("1.0.0"))
        manifest = _manifest(tmp_root / "deps.json", str(target), post_install_version_required=False)
        probe = FakeVersionProbe(versions=["1.0.0", None], errors=[None, "forced streamlink probe failure"])
        result = _installer(tmp_root, probe=probe).install_staged_update(_stage(tmp_root, "2.0.0"), _entry(manifest), expected_version="2.0.0")
        assert result.success, result
        assert result.code == "ok_with_warning", result
        assert result.warning == "forced streamlink probe failure", result
        assert result.post_check_error == "forced streamlink probe failure", result
        assert not result.rollback_attempted, result
        assert target.read_bytes() == _exe_bytes("2.0.0"), result
        assert result.backup_path and Path(result.backup_path).read_bytes() == _exe_bytes("1.0.0"), result



def assert_required_post_check_failure_still_rolls_back() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_root = Path(tmp)
        target = tmp_root / "sandbox_bin" / "fake_tool.exe"
        target.parent.mkdir(parents=True)
        target.write_bytes(_exe_bytes("1.0.0"))
        manifest = _manifest(tmp_root / "deps.json", str(target), post_install_version_required=True)
        probe = FakeVersionProbe(versions=["1.0.0", None], errors=[None, "forced required probe failure"])
        result = _installer(tmp_root, probe=probe).install_staged_update(_stage(tmp_root, "2.0.0"), _entry(manifest), expected_version="2.0.0")
        assert not result.success, result
        assert result.code == "post_check_failed", result
        assert result.rollback_attempted and result.rollback_success is True, result
        assert target.read_bytes() == _exe_bytes("1.0.0"), result



def assert_replace_failed_objectized_and_backup_exists() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_root = Path(tmp)
        target = tmp_root / "sandbox_bin" / "fake_tool.exe"
        target.parent.mkdir(parents=True)
        target.write_bytes(_exe_bytes("1.0.0"))
        manifest = _manifest(tmp_root / "deps.json", str(target))
        attempted_sources: list[Path] = []

        def fail_replace(source: Path, destination: Path) -> None:
            attempted_sources.append(Path(source))
            raise PermissionError("simulated permission failure")

        result = _installer(tmp_root, replace_func=fail_replace).install_staged_update(_stage(tmp_root, "2.0.0"), _entry(manifest), expected_version="2.0.0")
        assert not result.success, result
        assert result.code == "permission_denied", result
        assert "administrator privileges" in result.message, result
        assert result.backup_path and Path(result.backup_path).exists(), result
        assert Path(result.backup_path).read_bytes() == _exe_bytes("1.0.0"), result
        assert target.read_bytes() == _exe_bytes("1.0.0"), result
        assert attempted_sources and attempted_sources[0].parent == target.parent, attempted_sources
        assert not list(target.parent.glob("*.update-tmp-*.exe")), "same-volume temp must be cleaned after replace failure"


def assert_cross_root_stage_to_target_uses_same_volume_temp_success() -> None:
    with tempfile.TemporaryDirectory() as stage_tmp, tempfile.TemporaryDirectory() as target_tmp:
        stage_root = Path(stage_tmp)
        target_root = Path(target_tmp)
        target = target_root / "sandbox_bin" / "fake_tool.exe"
        manifest = _manifest(target_root / "deps.json", str(target))
        staged_path = Path(_stage(stage_root, "2.0.0").staged_exe_path or "")
        replace_calls: list[tuple[Path, Path]] = []

        def assert_same_volume_replace(source: Path, destination: Path) -> None:
            replace_calls.append((Path(source), Path(destination)))
            if Path(source).resolve() == staged_path.resolve():
                raise OSError(17, "simulated cross-drive replace failure")
            os.replace(source, destination)

        result = _installer(target_root, replace_func=assert_same_volume_replace).install_staged_update(
            _stage(stage_root, "2.0.0"),
            _entry(manifest),
            expected_version="2.0.0",
        )
        assert result.success, result
        assert target.read_bytes() == _exe_bytes("2.0.0"), result
        assert len(replace_calls) == 1, replace_calls
        temp_source, destination = replace_calls[0]
        assert destination == target.resolve(), replace_calls
        assert temp_source.parent == target.parent.resolve(), replace_calls
        assert temp_source.name.startswith("fake_tool.update-tmp-"), temp_source
        assert not temp_source.exists(), "same-volume temp must be removed by successful os.replace"
        assert not list(target.parent.glob("*.update-tmp-*.exe")), "same-volume temp leftovers"


def assert_rollback_uses_same_volume_temp_and_cleans() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_root = Path(tmp)
        target = tmp_root / "sandbox_bin" / "fake_tool.exe"
        target.parent.mkdir(parents=True)
        target.write_bytes(_exe_bytes("1.0.0"))
        manifest = _manifest(tmp_root / "deps.json", str(target))
        probe = FakeVersionProbe(versions=["1.0.0", "2.0.0"], errors=[None, "forced post-check failure"])
        replace_calls: list[tuple[Path, Path]] = []

        def tracking_replace(source: Path, destination: Path) -> None:
            replace_calls.append((Path(source), Path(destination)))
            os.replace(source, destination)

        result = _installer(tmp_root, probe=probe, replace_func=tracking_replace).install_staged_update(
            _stage(tmp_root, "2.0.0"),
            _entry(manifest),
            expected_version="2.0.0",
        )
        assert not result.success, result
        assert result.rollback_attempted and result.rollback_success is True, result
        assert target.read_bytes() == _exe_bytes("1.0.0"), result
        # R17.4 flow: (1) target → target.bak sibling (short-lived rollback
        # point), (2) install same-volume temp → target, (3) post-check fails
        # → rollback from backup_root same-volume temp → target.
        assert len(replace_calls) == 3, replace_calls
        sibling_source, sibling_destination = replace_calls[0]
        install_source, install_destination = replace_calls[1]
        rollback_source, rollback_destination = replace_calls[2]
        assert sibling_source == target.resolve(), replace_calls
        assert sibling_destination.name == target.name + ".bak", replace_calls
        assert sibling_destination.parent == target.parent.resolve(), replace_calls
        assert install_destination == target.resolve(), replace_calls
        assert rollback_destination == target.resolve(), replace_calls
        assert install_source.parent == target.parent.resolve(), replace_calls
        assert rollback_source.parent == target.parent.resolve(), replace_calls
        assert not list(target.parent.glob("*.update-tmp-*.exe")), "same-volume temp leftovers after rollback"
        # The short-lived ``.bak`` sibling is intentionally kept for startup
        # cleanup (R17.4); it sits next to the restored target.
        assert (target.parent / (target.name + ".bak")).exists(), "sibling .bak kept for next-startup cleanup"


def assert_target_mismatch_rejected_before_write() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_root = Path(tmp)
        target = tmp_root / "sandbox_bin" / "fake_tool.exe"
        manifest = _manifest(tmp_root / "deps.json", str(target))
        wrong_target = tmp_root / "other" / "fake_tool.exe"
        installer = ComponentUpdateInstaller(
            backup_root=tmp_root / "component_backups",
            version_probe=FakeVersionProbe(),
            target_path_resolver=lambda entry: wrong_target,
        )
        result = installer.install_staged_update(_stage(tmp_root, "2.0.0"), _entry(manifest), expected_version="2.0.0")
        assert not result.success, result
        assert result.code == "target_path_mismatch", result
        assert not target.exists(), result
        assert not wrong_target.exists(), result


def assert_staged_outside_staging_rejected() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_root = Path(tmp)
        target = tmp_root / "sandbox_bin" / "fake_tool.exe"
        manifest = _manifest(tmp_root / "deps.json", str(target))
        stage = _stage(tmp_root, "2.0.0")
        outside = tmp_root / "outside.exe"
        outside.write_bytes(_exe_bytes("2.0.0"))
        stage = replace(stage, staged_exe_path=str(outside))
        result = _installer(tmp_root).install_staged_update(stage, _entry(manifest), expected_version="2.0.0")
        assert not result.success, result
        assert result.code == "staging_path_escape", result
        assert not target.exists(), result


def assert_staging_tampered_rejects_mismatch() -> None:
    """Tampering the staged file between download and install must not touch bin."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_root = Path(tmp)
        target = tmp_root / "sandbox_bin" / "fake_tool.exe"
        target.parent.mkdir(parents=True)
        target.write_bytes(_exe_bytes("1.0.0"))
        manifest = _manifest(tmp_root / "deps.json", str(target))
        stage = _stage(tmp_root, "2.0.0")
        # Tamper with staged file AFTER the download recorded its digest.
        Path(stage.staged_exe_path or "").write_bytes(_exe_bytes("2.0.0") + b"\x00tampered")
        result = _installer(tmp_root).install_staged_update(stage, _entry(manifest), expected_version="2.0.0")
        assert not result.success, result
        assert result.code == "staging_tampered", result
        # bin must still hold the pre-install binary, untouched.
        assert target.read_bytes() == _exe_bytes("1.0.0"), result


def assert_staging_tampered_without_expected_sha_in_strong_mode() -> None:
    """If a strong-validation download result carries no digest, the install refuses."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_root = Path(tmp)
        target = tmp_root / "sandbox_bin" / "fake_tool.exe"
        target.parent.mkdir(parents=True)
        target.write_bytes(_exe_bytes("1.0.0"))
        manifest = _manifest(tmp_root / "deps.json", str(target))
        stage = _stage(tmp_root, "2.0.0")
        # Clear the digest while keeping weak_validation=False; the installer
        # must not silently trust the staged file in this configuration.
        stage = replace(stage, sha256=None, weak_validation=False)
        result = _installer(tmp_root).install_staged_update(stage, _entry(manifest), expected_version="2.0.0")
        assert not result.success, result
        assert result.code == "staging_tampered", result
        assert target.read_bytes() == _exe_bytes("1.0.0"), result


def assert_staging_weak_validation_skips_reverify() -> None:
    """Weak-validated download results bypass re-verify (missing_checksum is downloader's gate)."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_root = Path(tmp)
        target = tmp_root / "sandbox_bin" / "fake_tool.exe"
        manifest = _manifest(tmp_root / "deps.json", str(target))
        stage = _stage(tmp_root, "2.0.0")
        stage = replace(stage, sha256=None, weak_validation=True)
        result = _installer(tmp_root).install_staged_update(stage, _entry(manifest), expected_version="2.0.0")
        assert result.success, result
        assert target.read_bytes() == _exe_bytes("2.0.0"), result


class _InMemoryStateStore:
    """Minimal state store stub exposing the subset used by the installer."""

    def __init__(self, state: dict[str, Any]):
        self._state = state

    def get_component_state(self, component_id: str) -> dict[str, Any]:
        return dict(self._state.get(component_id, {}))


def assert_staging_tampered_uses_state_json_fallback_digest() -> None:
    """When the download result has no digest but state.json does, use the stored digest."""
    import hashlib as _hashlib
    from typing import Any
    with tempfile.TemporaryDirectory() as tmp:
        tmp_root = Path(tmp)
        target = tmp_root / "sandbox_bin" / "fake_tool.exe"
        target.parent.mkdir(parents=True)
        target.write_bytes(_exe_bytes("1.0.0"))
        manifest = _manifest(tmp_root / "deps.json", str(target))
        stage = _stage(tmp_root, "2.0.0")
        expected = _hashlib.sha256(_exe_bytes("2.0.0")).hexdigest()
        state_store = _InMemoryStateStore({"fake_tool": {"last_download": {"sha256": expected}}})
        stage_without_digest = replace(stage, sha256=None, weak_validation=False)
        installer = ComponentUpdateInstaller(
            backup_root=tmp_root / "component_backups",
            version_probe=FakeVersionProbe(),
            state_store=state_store,
        )
        result = installer.install_staged_update(stage_without_digest, _entry(manifest), expected_version="2.0.0")
        assert result.success, result
        assert target.read_bytes() == _exe_bytes("2.0.0"), result

        # Tampering the staged file while state.json still holds the old digest
        # must be rejected without touching bin.
        target.write_bytes(_exe_bytes("1.0.0"))
        stage_tampered = _stage(tmp_root, "2.0.0")
        Path(stage_tampered.staged_exe_path or "").write_bytes(_exe_bytes("2.0.0") + b"tamper")
        stage_tampered = replace(stage_tampered, sha256=None, weak_validation=False)
        installer2 = ComponentUpdateInstaller(
            backup_root=tmp_root / "component_backups",
            version_probe=FakeVersionProbe(),
            state_store=state_store,
        )
        result = installer2.install_staged_update(stage_tampered, _entry(manifest), expected_version="2.0.0")
        assert not result.success, result
        assert result.code == "staging_tampered", result
        assert target.read_bytes() == _exe_bytes("1.0.0"), result


def run() -> None:
    checks = [
        assert_first_install_without_target,
        assert_existing_target_backup_replace_success,
        assert_staged_exe_missing_objectized,
        assert_post_check_failure_triggers_rollback,
        assert_streamlink_optional_post_check_warning_does_not_rollback,
        assert_required_post_check_failure_still_rolls_back,
        assert_replace_failed_objectized_and_backup_exists,
        assert_cross_root_stage_to_target_uses_same_volume_temp_success,
        assert_rollback_uses_same_volume_temp_and_cleans,
        assert_target_mismatch_rejected_before_write,
        assert_staged_outside_staging_rejected,
        assert_staging_tampered_rejects_mismatch,
        assert_staging_tampered_without_expected_sha_in_strong_mode,
        assert_staging_weak_validation_skips_reverify,
        assert_staging_tampered_uses_state_json_fallback_digest,
    ]
    for check in checks:
        check()
        print(f"PASS {check.__name__}")
    print("component update install smoke passed")


if __name__ == "__main__":
    run()
