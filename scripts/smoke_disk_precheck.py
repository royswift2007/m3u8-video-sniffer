"""
Stage 3 smoke: ComponentUpdateInstaller disk pre-check (R17.2, R25.1).

Stages a 100 MB fake component under a temporary directory, monkey-patches
``shutil.disk_usage`` on the installer module so only 10 MB of free space is
reported, then asserts:

* ``install_staged_update`` returns ``success=False`` with
  ``code="insufficient_disk"``.
* ``bin/`` (the sandboxed target parent) is **not** touched — neither the
  target file nor any ``.bak`` / ``*.update-tmp-*`` siblings are created.
* The structured ``details`` payload carries ``need`` / ``free`` /
  ``required`` bytes so the UI can render the actionable error (R17.2).

A sanity scenario with plenty of free space confirms that the pre-check
allows the install through when the disk has enough headroom, ensuring the
smoke does not silently pass because the pre-check path was skipped.

Runs headless in <3s. Exits 0 on pass; non-zero on any deviation.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import hashlib  # noqa: E402

from core import component_update_installer as cui  # noqa: E402
from core.component_update_downloader import ComponentDownloadStageResult  # noqa: E402
from core.component_update_installer import ComponentUpdateInstaller  # noqa: E402
from core.component_update_models import ComponentVersionInfo  # noqa: E402
from core.dependency_manifest import DependencyEntry, DependencyManifest  # noqa: E402


# ---------------------------------------------------------------------------
# Test doubles mirroring ``scripts/smoke_component_update_install.py`` so the
# two smokes stay consistent. We keep them local rather than importing to
# avoid a circular smoke→smoke dependency.
# ---------------------------------------------------------------------------


class _FakeVersionProbe:
    def probe(self, entry: DependencyEntry) -> ComponentVersionInfo:
        return ComponentVersionInfo(
            component_id=entry.id,
            label=entry.label,
            path=str(entry.path),
            exists=entry.path.exists(),
            version=None,
            raw_output=None,
            error=None,
        )


def _write_manifest(path: Path, target_relative: str) -> DependencyManifest:
    payload = {
        "required": [
            {
                "id": "fake_tool",
                "label": "Fake Tool",
                "path": target_relative.replace("\\", "/"),
                "version": {
                    "command": ["{path}", "--version"],
                    "regex": r"(?P<version>\d+\.\d+\.\d+)",
                    "normalize": "semantic",
                    "timeout": 5,
                },
                "update": {
                    "enabled": True,
                    "release_source": "direct",
                    "latest_url": "https://example.invalid/fake_tool.exe",
                    "asset_pattern": "fake_tool.exe",
                    "version_source": "url",
                    "install_strategy": "replace_file",
                    "requires_process_free": True,
                },
            }
        ],
        "recommended": [],
        "optional": [],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return DependencyManifest(path)


def _stage_100mb(tmp_root: Path) -> ComponentDownloadStageResult:
    """Write a 100 MB sparse-ish staged file and return the download result.

    100 MB × 1 byte writes would be slow; we use a single ``truncate`` to
    create a file of the right size without paying for page writes. On
    Windows / POSIX this produces a sparse or allocated file depending on
    the filesystem; either way ``stat().st_size`` reports the logical
    length that ``_check_disk_space`` consumes.
    """

    staging_dir = tmp_root / "component_updates" / "fake_tool" / "run" / "staging"
    staging_dir.mkdir(parents=True, exist_ok=True)
    staged = staging_dir / "fake_tool.exe"
    size = 100 * 1024 * 1024  # 100 MiB
    with open(staged, "wb") as fh:
        fh.truncate(size)
    # The sha256 of an all-zero 100 MiB buffer is deterministic; we only
    # need *some* valid digest so the pre-install re-verification step
    # does not short-circuit before the disk pre-check runs. Computing
    # the hash streamingly keeps peak memory bounded.
    h = hashlib.sha256()
    with open(staged, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return ComponentDownloadStageResult(
        component_id="fake_tool",
        success=True,
        code="ok",
        message="staged",
        staging_dir=str(staging_dir),
        staged_exe_path=str(staged),
        asset_name="fake_tool.exe",
        bytes_downloaded=size,
        sha256=h.hexdigest(),
        weak_validation=False,
    )


# ---------------------------------------------------------------------------
# Patch helpers.
# ---------------------------------------------------------------------------


def _patched_disk_usage(free_bytes: int):
    """Return a ``shutil.disk_usage``-compatible callable reporting ``free_bytes``.

    The installer reads ``.free`` from the result; other attributes mirror a
    plausible 500 GB drive so nothing crashes if future code reads
    ``total`` / ``used``.
    """

    def _impl(_path):
        return SimpleNamespace(
            total=500 * 1024 * 1024 * 1024,
            used=500 * 1024 * 1024 * 1024 - free_bytes,
            free=free_bytes,
        )

    return _impl


# ---------------------------------------------------------------------------
# Scenarios.
# ---------------------------------------------------------------------------


def assert_insufficient_disk_rejects_without_touching_target() -> None:
    original = cui.shutil.disk_usage
    with tempfile.TemporaryDirectory() as tmp:
        tmp_root = Path(tmp)
        target = tmp_root / "sandbox_bin" / "fake_tool.exe"
        manifest = _write_manifest(tmp_root / "deps.json", str(target))
        stage = _stage_100mb(tmp_root)
        entry = manifest.get_update_enabled_entries(include_recommended=False)[0]

        installer = ComponentUpdateInstaller(
            backup_root=tmp_root / "component_backups",
            version_probe=_FakeVersionProbe(),
        )

        # 10 MiB free → far below the 100 MiB staged size × 1.2 margin.
        cui.shutil.disk_usage = _patched_disk_usage(10 * 1024 * 1024)  # type: ignore[assignment]
        try:
            result = installer.install_staged_update(
                stage, entry, expected_version="2.0.0"
            )
        finally:
            cui.shutil.disk_usage = original  # type: ignore[assignment]

    assert not result.success, result
    assert result.code == "insufficient_disk", result
    assert result.details is not None, result
    assert result.details.get("need") == 100 * 1024 * 1024, result.details
    assert result.details.get("free") == 10 * 1024 * 1024, result.details
    assert result.details.get("required") == int(100 * 1024 * 1024 * 1.2), result.details

    # R17.2 invariant: ``bin/`` must stay pristine. The sandboxed target
    # parent should not contain the target, any ``.bak`` sibling, or a
    # same-volume replace temp.
    parent = target.parent
    if parent.exists():
        leftovers = list(parent.iterdir())
        assert not leftovers, f"sandbox_bin must stay empty, found: {leftovers}"


def assert_sufficient_disk_proceeds_past_precheck() -> None:
    """Mirror scenario: 500 MiB free → install advances past the disk check.

    The install then fails with ``staged_exe_invalid`` because the staged
    file is sparse and its stat reports 100 MiB but the installer considers
    the staged signature valid; we only care that the failure reason is
    *not* ``insufficient_disk``. This ensures our pre-check shim is
    actually exercising the code path we claim.
    """

    original = cui.shutil.disk_usage
    with tempfile.TemporaryDirectory() as tmp:
        tmp_root = Path(tmp)
        target = tmp_root / "sandbox_bin" / "fake_tool.exe"
        manifest = _write_manifest(tmp_root / "deps.json", str(target))
        stage = _stage_100mb(tmp_root)
        entry = manifest.get_update_enabled_entries(include_recommended=False)[0]

        installer = ComponentUpdateInstaller(
            backup_root=tmp_root / "component_backups",
            version_probe=_FakeVersionProbe(),
        )

        # 500 MiB free → well above the 100 MiB × 1.2 requirement.
        cui.shutil.disk_usage = _patched_disk_usage(500 * 1024 * 1024)  # type: ignore[assignment]
        try:
            result = installer.install_staged_update(
                stage, entry, expected_version="2.0.0"
            )
        finally:
            cui.shutil.disk_usage = original  # type: ignore[assignment]

    # The install may succeed or fail downstream (version probe / replace
    # semantics); what matters is that the disk pre-check did not short-
    # circuit the pipeline with ``insufficient_disk``.
    assert result.code != "insufficient_disk", result


# ---------------------------------------------------------------------------
# Entry point.
# ---------------------------------------------------------------------------


def run() -> None:
    checks = (
        assert_insufficient_disk_rejects_without_touching_target,
        assert_sufficient_disk_proceeds_past_precheck,
    )
    for check in checks:
        check()
        print(f"PASS {check.__name__}")
    print("disk precheck smoke passed")


if __name__ == "__main__":
    run()
