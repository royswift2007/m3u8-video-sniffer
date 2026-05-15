"""
Stage 4 smoke: engine ``BaseEngine.spawn`` migration audit
(task 29.2 / 30.1 / R37.1, R37.2).

The R37 goal is that every concrete download engine routes its
:class:`subprocess.Popen` calls through :meth:`engines.base_engine.BaseEngine.spawn`
(or, for FFmpeg, through the :class:`_BaseEngineAdapter` that delegates to
``BaseEngine.spawn``). This smoke performs two complementary checks, both
offline and synchronous:

1. **Static AST scan.** Walks every engine module under ``engines/`` and
   flags direct ``subprocess.Popen(...)`` call sites outside of
   ``base_engine.py`` and the FFmpeg internal adapter. The idea is that
   the Windows ``creationflags`` / ``close_fds`` / stdout=PIPE boilerplate
   must live in exactly one place, so a stray ``subprocess.Popen`` inside
   ``aria2_engine.py`` / ``ytdlp_engine.py`` / ``streamlink_engine.py`` /
   ``n_m3u8dl_re.py`` is a regression signal.

2. **Runtime monkey-patch probe.** For each engine's primary download
   method we monkey-patch ``BaseEngine.spawn`` to record every call,
   short-circuiting before a real process is created, then invoke the
   engine's download entry on a synthetic task. Any engine that bypasses
   ``spawn`` and calls ``subprocess.Popen`` directly will either never
   hit the patched recorder or will crash when ``spawn`` is not used;
   both outcomes are surfaced as a failure.

Exits 0 on pass, 1 on any violation.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path
from typing import Iterable

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENGINES_DIR = PROJECT_ROOT / "engines"

# Modules that are *allowed* to call ``subprocess.Popen`` directly because
# they implement the shared spawn infrastructure (``base_engine.py``) or
# are thin adapter shims that delegate straight back into it.
ALLOWED_POPEN_FILES: frozenset[str] = frozenset(
    {
        "base_engine.py",
        # ``ffmpeg_processor.py`` routes through ``self._base.spawn`` via its
        # ``_BaseEngineAdapter``; the Popen calls inside that file, if any,
        # live on the shared helper and are reachable only through spawn().
        # We still gate them with the AST check below: the file must not
        # introduce a *new* Popen that bypasses the adapter.
    }
)


# ----------------------------------------------------------------------
# 1. AST scan for rogue subprocess.Popen call sites
# ----------------------------------------------------------------------


def _is_subprocess_popen_call(node: ast.Call) -> bool:
    """Return True if ``node`` is a ``subprocess.Popen(...)`` invocation.

    Matches both the fully-qualified ``subprocess.Popen(...)`` form and
    the bare ``Popen(...)`` form following ``from subprocess import Popen``.
    """

    func = node.func
    if isinstance(func, ast.Attribute):
        if func.attr != "Popen":
            return False
        value = func.value
        if isinstance(value, ast.Name) and value.id == "subprocess":
            return True
    if isinstance(func, ast.Name) and func.id == "Popen":
        return True
    return False


def scan_engine_files() -> list[tuple[Path, int, str]]:
    """Return every disallowed ``subprocess.Popen`` call site under ``engines/``."""

    findings: list[tuple[Path, int, str]] = []
    for path in sorted(ENGINES_DIR.rglob("*.py")):
        if path.name in ALLOWED_POPEN_FILES:
            continue
        if any(part == "__pycache__" for part in path.parts):
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (OSError, SyntaxError) as exc:
            findings.append((path, 0, f"<parse failed: {exc}>"))
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and _is_subprocess_popen_call(node):
                snippet = ast.unparse(node)[:120]
                findings.append((path, node.lineno, snippet))
    return findings


# ----------------------------------------------------------------------
# 2. Runtime probe — every engine's download path must touch BaseEngine.spawn
# ----------------------------------------------------------------------


class _SpawnRecorder:
    """Shared call recorder used to patch :meth:`BaseEngine.spawn`."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[str, ...]]] = []

    def make_stub(self, engine_name: str):
        recorder = self

        def _stub(self, argv, *args, **kwargs):  # noqa: ARG001
            argv_tuple = tuple(str(x) for x in argv)
            recorder.calls.append((engine_name, argv_tuple))
            # Deliberately raise so the engine's download method bails
            # out immediately without touching real I/O. The runtime
            # probe only cares that ``spawn`` was called; what happens
            # afterward is out of scope for this migration audit.
            raise _SpawnCalled(engine_name, argv_tuple)

        return _stub


class _SpawnCalled(RuntimeError):
    """Sentinel signaling that ``BaseEngine.spawn`` was invoked as expected."""

    def __init__(self, engine: str, argv: tuple[str, ...]) -> None:
        super().__init__(f"spawn invoked for {engine} with argv[0]={argv[:1]}")
        self.engine = engine
        self.argv = argv


def _runtime_probe() -> list[str]:
    """Invoke each engine's download entrypoint and assert spawn was hit.

    Returns a list of failure messages; empty list means pass.

    The probe is intentionally shallow: it does not exercise progress
    parsing or process cleanup — those are covered by dedicated Stage 2/3
    tests. All we need here is a guarantee that every engine's download
    path funnels through ``BaseEngine.spawn``.
    """

    # Import lazily so a syntax error in the project surfaces through the
    # AST scan above with a clearer message before we crash here.
    from engines.base_engine import BaseEngine  # noqa: PLC0415
    from core.task_model import DownloadTask, M3U8Resource  # noqa: PLC0415

    recorder = _SpawnRecorder()
    failures: list[str] = []

    # (engine_module_path, engine_class_name, download_method_name, argv_marker)
    # The argv marker is optional; it's only used in diagnostic output.
    engine_specs: Iterable[tuple[str, str, str]] = (
        ("engines.n_m3u8dl_re", "NM3u8DlReEngine", "download"),
        ("engines.aria2_engine", "Aria2Engine", "download"),
        ("engines.streamlink_engine", "StreamlinkEngine", "download"),
        ("engines.ytdlp_engine", "YtdlpEngine", "download"),
    )

    # Swap spawn globally for the duration of the probe. Tests running in
    # parallel within the same interpreter would race here, but stage_gate
    # invokes smoke scripts as separate subprocesses so isolation holds.
    original_spawn = BaseEngine.spawn

    try:
        for module_path, class_name, method_name in engine_specs:
            try:
                module = __import__(module_path, fromlist=[class_name])
            except ImportError as exc:
                failures.append(f"{module_path}: import failed: {exc}")
                continue
            engine_cls = getattr(module, class_name, None)
            if engine_cls is None:
                failures.append(f"{module_path}: class {class_name!r} not found")
                continue

            # Each engine needs a unique binary path for log tagging.
            try:
                engine = engine_cls(binary_path=f"C:/fake/{class_name}.exe")
            except TypeError:
                # Some engines may require alternate constructor kwargs
                # (e.g. ``ffmpeg_path``); retry with a positional fallback.
                try:
                    engine = engine_cls(f"C:/fake/{class_name}.exe")
                except Exception as exc:
                    failures.append(
                        f"{module_path}:{class_name}: instantiation failed: {exc}"
                    )
                    continue

            # Rebind spawn on this instance's class so subclass overrides
            # (if any) still route through the stub.
            BaseEngine.spawn = recorder.make_stub(class_name)  # type: ignore[method-assign]

            resource = M3U8Resource(
                url="https://example.invalid/playlist.m3u8",
                title=f"{class_name}-probe",
                headers={},
            )
            task = DownloadTask(
                resource=resource,
                download_dir=str(PROJECT_ROOT / "build" / "_spawn_probe"),
                filename=f"{class_name}-probe.mp4",
            )

            method = getattr(engine, method_name, None)
            if method is None:
                failures.append(
                    f"{module_path}:{class_name}: missing method {method_name!r}"
                )
                continue

            pre_call_count = len(
                [c for c in recorder.calls if c[0] == class_name]
            )

            try:
                method(task, lambda *a, **kw: None)
            except _SpawnCalled:
                pass  # Expected — sentinel raised after spawn was recorded.
            except Exception as exc:  # noqa: BLE001 - diagnostic collection
                # A non-sentinel exception is acceptable *only if* spawn
                # was actually called before the failure; otherwise the
                # engine bailed out on argv construction and we cannot
                # prove the spawn route.
                post = len([c for c in recorder.calls if c[0] == class_name])
                if post == pre_call_count:
                    failures.append(
                        f"{class_name}.{method_name} raised before calling "
                        f"BaseEngine.spawn: {type(exc).__name__}: {exc}"
                    )
                    continue

            post_call_count = len(
                [c for c in recorder.calls if c[0] == class_name]
            )
            if post_call_count == pre_call_count:
                failures.append(
                    f"{class_name}.{method_name} completed without calling "
                    "BaseEngine.spawn (direct subprocess.Popen?)"
                )
    finally:
        BaseEngine.spawn = original_spawn  # type: ignore[method-assign]

    return failures


# ----------------------------------------------------------------------
# Entry point
# ----------------------------------------------------------------------


def main() -> int:
    ast_findings = scan_engine_files()
    if ast_findings:
        print(
            f"[smoke_engine_spawn_migration] FAIL: {len(ast_findings)} "
            "disallowed subprocess.Popen call site(s):",
            flush=True,
        )
        for path, lineno, snippet in ast_findings:
            rel = path.relative_to(PROJECT_ROOT)
            print(f"  - {rel}:{lineno}: {snippet}", flush=True)
        return 1

    # Ensure the project root is on sys.path so the engines import cleanly
    # whether or not the stage gate runner set PYTHONPATH for us.
    root_str = str(PROJECT_ROOT)
    if root_str not in sys.path:
        sys.path.insert(0, root_str)

    runtime_failures = _runtime_probe()
    if runtime_failures:
        print(
            f"[smoke_engine_spawn_migration] FAIL: {len(runtime_failures)} "
            "runtime probe violation(s):",
            flush=True,
        )
        for msg in runtime_failures:
            print(f"  - {msg}", flush=True)
        return 1

    print(
        "[smoke_engine_spawn_migration] OK: all engines route through "
        "BaseEngine.spawn and no rogue subprocess.Popen call sites exist",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
