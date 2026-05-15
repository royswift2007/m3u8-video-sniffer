"""
Read-only local component version probing.
"""

from __future__ import annotations

import os
import re
import subprocess
from concurrent.futures import ThreadPoolExecutor, TimeoutError

from core.component_update_models import ComponentVersionInfo
from core.dependency_manifest import DependencyEntry


class ComponentVersionProbe:
    """Run configured local version commands safely and return status objects."""

    def __init__(self, timeout_default: int = 8, timeout_cap: int = 8):
        self.timeout_default = max(1, int(timeout_default))
        self.timeout_cap = max(1, int(timeout_cap))

    def probe(self, entry: DependencyEntry) -> ComponentVersionInfo:
        """Probe one component version without raising operational errors to callers."""
        target_path = entry.path
        path_text = str(target_path)
        if not target_path.exists():
            return ComponentVersionInfo(
                component_id=entry.id,
                label=entry.label,
                path=path_text,
                exists=False,
                error="component file is missing",
            )
        if entry.version is None:
            return ComponentVersionInfo(
                component_id=entry.id,
                label=entry.label,
                path=path_text,
                exists=True,
                error="version probe is not configured",
            )

        try:
            command = self._build_command(entry)
            timeout = self._bounded_timeout(entry.version.timeout)
            return_code, stdout, stderr = self._run_version_command_with_hard_timeout(command, timeout)
            output = "\n".join(part for part in (stdout, stderr) if part).strip()
            version = self._normalize_version(
                self._parse_version(output, entry.version.regex),
                entry.version.normalize,
            )
            error = None
            if not output and return_code == 0:
                version = "available"
                error = "version command returned no output"
            elif version is None:
                error = "version output did not match regex"
            elif return_code != 0:
                error = f"version command exited with code {return_code}"
            return ComponentVersionInfo(
                component_id=entry.id,
                label=entry.label,
                path=path_text,
                exists=True,
                version=version,
                raw_output=output or None,
                error=error,
            )
        except subprocess.TimeoutExpired:
            return ComponentVersionInfo(
                component_id=entry.id,
                label=entry.label,
                path=path_text,
                exists=True,
                error="version command timed out",
            )
        except Exception as exc:
            return ComponentVersionInfo(
                component_id=entry.id,
                label=entry.label,
                path=path_text,
                exists=True,
                error=f"version probe failed: {exc}",
            )

    def _bounded_timeout(self, configured_timeout: int | None) -> int:
        """Return timeout for one probe.

        Manifest-provided timeouts are trusted per-component overrides. The
        fallback timeout remains capped so components without explicit metadata
        cannot accidentally block refresh for too long.
        """
        if configured_timeout is not None:
            return max(1, int(configured_timeout))
        return max(1, min(int(self.timeout_default), self.timeout_cap))

    def _build_command(self, entry: DependencyEntry) -> list[str]:
        """Replace {path} placeholders with the absolute component path."""
        if entry.version is None:
            return [str(entry.path)]
        path_text = str(entry.path)
        command = [part.replace("{path}", path_text) for part in entry.version.command]
        return command or [path_text]

    def _run_version_command_with_hard_timeout(self, command: list[str], timeout: int) -> tuple[int, str, str]:
        """Run the version command behind a hard caller-side timeout guard."""
        executor = ThreadPoolExecutor(max_workers=1)
        future = executor.submit(self._run_version_command, command, timeout)
        try:
            return future.result(timeout=timeout + 1)
        except TimeoutError as exc:
            future.cancel()
            raise subprocess.TimeoutExpired(command, timeout) from exc
        finally:
            executor.shutdown(wait=future.done(), cancel_futures=True)

    def _run_version_command(self, command: list[str], timeout: int) -> tuple[int, str, str]:
        """Execute a version command with captured output and no console popup on Windows."""
        creationflags = 0
        startupinfo = None
        if os.name == "nt":
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        completed = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
            creationflags=creationflags,
            startupinfo=startupinfo,
        )
        return completed.returncode, completed.stdout or "", completed.stderr or ""

    def _parse_version(self, output: str, regex: str) -> str | None:
        """Extract version using a configured regex."""
        match = re.search(regex, output or "", flags=re.MULTILINE)
        if not match:
            return None
        if "version" in match.groupdict():
            return match.group("version")
        if match.groups():
            return match.group(1)
        return match.group(0)

    def _normalize_version(self, version: str | None, normalize: str | None) -> str | None:
        """Normalize common version prefixes without enforcing comparison semantics."""
        if version is None:
            return None
        normalized = version.strip()
        if normalize in (None, "strip_v", "semantic", "date"):
            normalized = normalized.lstrip("vV")
        return normalized or None
