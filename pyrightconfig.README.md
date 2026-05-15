# `pyrightconfig.json` rationale

security-stability-hardening task 26.3 / Requirement 29.2.

The sibling `pyrightconfig.json` configures `pyright` as a CI-time type checker
for the **TaskSnapshot typed channel** only. Its purpose is narrow and
deliberate:

- Keep `MainWindow.task_update_received(snapshot: TaskSnapshot) -> None`
  honest — any caller that tries to emit a raw `core.task_model.DownloadTask`
  into the `TaskSnapshot`-typed choke points (`_emit_task_snapshot`,
  `_on_task_snapshot`, or `task_update_received` itself) must fail
  `pyright` at authoring time, not at runtime inside the defensive
  `isinstance` guard.
- Mirror the rule enforced at AST level by
  `scripts/lint_main_window_slots.py`, giving us two independent signals
  for the same contract (`Requirement 29 AC-2`).

## Why the scope is four files

`pyright` over the entire repository would be drowned out by:

- PyQt's dynamic `pyqtSignal` / `pyqtSlot` metaclass (no `.pyi` exist for
  all `PyQt6.QtCore` members we use).
- Legacy unparameterized `dict` / `list` annotations on `DownloadTask`
  and `M3U8Resource`.
- Integrations that rely on `subprocess.Popen` stdout as `bytes | str`
  depending on platform.

None of those are relevant to Requirement 29. Limiting `include` to the
four files that implement the `TaskSnapshot` contract keeps the signal
high and the noise zero:

- `core/task_model.py` — the `TaskSnapshot` frozen dataclass and
  `DownloadTask` owning type, so the two are seen as distinct.
- `ui/main_window.py` — the `_emit_task_snapshot` typed alias plus the
  `task_update_received` slot.
- `ui/main_window_actions.py` — currently the only split module that
  hands tasks/snapshots around (kept in scope for future refactors).
- `ui/main_window_sniff_flow.py` — same reason as above.

## Why `typeCheckingMode = "basic"`

Strict mode would force us to annotate every PyQt attribute (e.g.
`self.browser`, `self.download_queue`, `self.resource_panel`) and every
dynamic state-machine field; that is out of scope for 26.3. The three
errors we want — `reportArgumentType`, `reportGeneralTypeIssues`,
`reportIncompatibleMethodOverride` — are promoted to `error` explicitly,
which is sufficient to catch a bare `DownloadTask` at the argument
position of a `TaskSnapshot`-typed callable.

## How to run locally

```bash
pip install pyright
pyright
```

`pyright` reads `pyrightconfig.json` automatically from the repo root.
Expected output: `0 errors, 0 warnings, 0 informations` against the four
files listed above.

## Widening scope

Widening `include` to the full repository is a follow-up, not part of
26.3. When that happens, do it file-by-file (not all at once) and expect
to suppress unrelated `reportUnknownMemberType` / `reportUnknownVariableType`
findings either by adding `.pyi` stubs or by keeping them at `none` as
this file already does.
