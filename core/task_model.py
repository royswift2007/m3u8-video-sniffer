"""
Task data models.
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, ClassVar, Optional
from urllib.parse import unquote, urlparse
import re
import threading
import time

from utils.redact import redact_url
from utils.win_path import sanitize_title

_logger = logging.getLogger(__name__)


class InvalidTransition(Exception):
    """Raised when :meth:`DownloadTask.transition` receives an illegal state change.

    Stage 2 R11 (tasks.md 11.2) requires the :class:`DownloadTask` state
    machine to reject transitions such as ``completed -> downloading`` or any
    move out of the terminal ``removed`` state. Callers are expected to catch
    this exception and record a structured warning; silent fallthrough is a
    bug per R11.6.
    """

    def __init__(self, old: str, new: str, *, reason: Optional[str] = None) -> None:
        self.old = old
        self.new = new
        self.reason = reason
        detail = f"{old!r} -> {new!r}"
        if reason is not None:
            detail = f"{detail} (reason={reason!r})"
        super().__init__(detail)


@dataclass
class M3U8Resource:
    """Detected media resource from sniffer pipeline."""

    url: str
    headers: dict
    page_url: str
    timestamp: datetime = field(default_factory=datetime.now)
    title: str = ""
    page_title: str = ""
    variants: list = field(default_factory=list)
    is_variant: bool = False
    variant_info: Optional[dict] = None
    variant_parent_resource: Optional["M3U8Resource"] = None
    quality_label: str = ""
    variants_listed: bool = False
    candidate_score: int = 0
    selected_engine: Optional[str] = None

    def __post_init__(self):
        self.page_title = self._sanitize_title(self.page_title)
        # `title` participates in Windows path construction (via
        # DownloadTask.filename), so run it through the strong, reserved-name
        # and byte-budget aware sanitizer from utils.win_path. An idempotency
        # flag avoids re-sanitizing titles that already came from a
        # sanitized source (dataclass round-trips, copy.replace, etc.).
        # See tasks.md 12.2 / design.md Stage 2 R12.
        if not getattr(self, "_sanitized", False):
            # Unify both branches through `sanitize_title` so URL-derived
            # fallback titles (which only see the weaker display-level
            # `_sanitize_title`) also respect Windows reserved names,
            # trailing dot/space, and the 240-byte UTF-8 budget.
            raw = self.title if self.title else self._extract_title()
            self.title = sanitize_title(raw)
            object.__setattr__(self, "_sanitized", True)

    @staticmethod
    def _sanitize_title(title: str) -> str:
        """Sanitize a title for UI/display usage."""
        if not title:
            return ""
        clean_title = re.sub(r'[<>:"/\\|?*]', "_", title).strip()
        return clean_title[:100]

    def _extract_url_title(self) -> str:
        """Build a fallback title from the resource URL."""
        try:
            path = urlparse(self.url).path
            filename = path.split("/")[-1]
            if filename:
                name = filename.rsplit(".", 1)[0]
                if name:
                    return self._sanitize_title(unquote(name))
        except (ValueError, AttributeError):
            # Malformed URL (ValueError from urlparse on invalid IPv6) or a
            # non-string ``self.url``; fall through to the generic fallback.
            # URL intentionally not echoed — may contain tokens.
            _logger.debug("task_model: URL-title extraction skipped")

        return "untitled_video"

    @staticmethod
    def _strip_common_site_suffix(title: str) -> str:
        """Strip common site-brand suffixes before generic-title checks."""
        normalized = (title or "").strip().lower()
        if not normalized:
            return ""

        normalized = re.sub(r"_哔哩哔哩_bilibili$", "", normalized).strip(" _-")
        normalized = re.sub(
            r"\s*[-|_]\s*(youtube|tiktok|twitch|vimeo|dailymotion|facebook|instagram|twitter|x)\s*$",
            "",
            normalized,
        ).strip()
        return normalized

    def _title_rank(self, title: str) -> int:
        """Return a conservative quality rank for title replacement decisions."""
        normalized = self._sanitize_title(title)
        if not normalized:
            return 0

        normalized_lower = normalized.lower()
        fallback_url_title = (self._extract_url_title() or "").strip().lower()
        stripped_lower = self._strip_common_site_suffix(normalized_lower)

        generic_titles = {
            "untitled_video",
            "youtube",
            "bilibili",
            "tiktok",
            "douyin",
            "twitter",
            "x",
            "facebook",
            "instagram",
            "vimeo",
            "twitch",
            "youku",
            "iqiyi",
            "video",
            "watch",
            "play",
            "playlist",
            "index",
            "master",
            "home",
        }

        if normalized_lower == fallback_url_title or stripped_lower == fallback_url_title:
            return 1
        if normalized_lower in generic_titles or stripped_lower in generic_titles:
            return 1
        if re.fullmatch(r"youtube video \[[\w-]{6,}\]", normalized_lower):
            return 1

        return 2

    def is_better_title(self, candidate_title: str, current_title: Optional[str] = None) -> bool:
        """Decide whether candidate_title is safely better than current_title."""
        candidate = self._sanitize_title(candidate_title)
        current = self._sanitize_title(self.title if current_title is None else current_title)

        if not candidate:
            return False
        if not current:
            return True
        if candidate == current:
            return False

        return self._title_rank(candidate) > self._title_rank(current)

    def apply_page_title(self, page_title: str) -> bool:
        """Merge a page title into page_title/title using conservative replacement rules."""
        normalized = self._sanitize_title(page_title)
        if not normalized:
            return False

        changed = False
        if not self.page_title or self.is_better_title(normalized, self.page_title):
            self.page_title = normalized
            changed = True

        if not self.title or self.is_better_title(normalized, self.title):
            # self.title is path-bound; route through the strong Windows-safe
            # sanitizer in addition to the display-level _sanitize_title above.
            # See tasks.md 12.2.
            self.title = sanitize_title(normalized)
            changed = True

        return changed

    def _extract_title(self) -> str:
        """Build a reasonable default title from page title or URL."""
        if self.page_title:
            return self.page_title

        return self._extract_url_title()


@dataclass
class DownloadTask:
    """Download task entity.

    Stage 2 R11 (tasks.md 11.2) introduced a per-task ``threading.RLock`` and
    an explicit state machine guarded by :meth:`transition`. Cross-thread
    access to the volatile fields (``status``/``stop_requested``/
    ``stop_reason``/``process``/``retry_count``/``error_message``) must occur
    under ``with task.lock:``. Direct attribute assignment remains supported
    to keep the success path backwards compatible while external callers are
    migrated to :meth:`transition` wave by wave. Illegal state moves (e.g.
    ``completed -> downloading``) raise :class:`InvalidTransition` per
    R11.5 / R11.6.
    """

    url: str
    save_dir: str
    filename: str
    headers: dict
    status: str = "waiting"  # waiting, downloading, completed, failed, paused, removed
    progress: float = 0.0
    speed: str = ""
    engine: str = ""
    error_message: str = ""
    downloaded_size: str = ""
    selected_variant: Optional[dict] = None
    master_url: Optional[str] = None
    media_url: Optional[str] = None
    candidate_scores: Optional[dict] = None
    process: Optional[object] = None
    retry_count: int = 0
    max_retries: int = 0
    stop_requested: bool = False
    stop_reason: str = ""  # paused, cancelled, shutdown, removed, engine_switch
    created_at: datetime = field(default_factory=datetime.now)
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None

    # ------------------------------------------------------------------
    # Engine pid-ownership metadata (security-stability-hardening R30.1,
    # tasks.md 27.1)
    # ------------------------------------------------------------------
    #
    # ``_pid`` mirrors ``process.pid`` at spawn time so that downstream
    # kill paths can still identify the engine even after ``process`` has
    # been cleared (e.g. by :meth:`transition` on an ``engine_switch``).
    # ``_expected_engine_name`` records the engine image name the manager
    # believes lives at that pid (e.g. ``"yt-dlp"``); the kill helpers
    # pass it into :func:`engines.base_engine.kill_process_tree` as the
    # ``expected_name`` guard so a recycled pid cannot be terminated by
    # mistake.
    #
    # Both fields are optional. Engines that don't yet populate them get
    # the historic (unguarded) kill behaviour. Consumers reset the fields
    # atomically via :meth:`transition` (see ``_clear_engine_binding``).
    _pid: Optional[int] = None
    _expected_engine_name: Optional[str] = None

    # --- State machine ------------------------------------------------------
    #
    # The keys below are the status strings actually used across
    # ``core/download_manager.py`` and the UI layer (``ui/download_queue.py``,
    # ``ui/main_window.py``). The transition table is the authoritative list
    # of allowed moves; any status pair absent from the mapping is treated as
    # illegal and rejected via :class:`InvalidTransition`.
    #
    # Notes:
    #   * ``waiting -> failed`` is required by ``DownloadManager.add_task``
    #     which flips a freshly queued task to ``failed`` on engine selection
    #     errors, and by ``shutdown`` which demotes waiting tasks to failed.
    #   * ``failed -> waiting`` covers the manual retry path through
    #     ``_retry_task``/``add_task``; bumping ``retry_count`` is the
    #     responsibility of the caller.
    #   * ``completed`` and ``removed`` are terminal; no outbound edges.
    #   * ``downloading -> waiting`` covers the internal backoff retry loop
    #     inside ``DownloadManager._execute_task``.
    #   * Self-transitions (e.g. ``paused -> paused``) are allowed only when a
    #     ``reason`` annotation is attached (see :meth:`transition`); this
    #     lets callers refresh ``stop_reason`` without state churn.
    _TRANSITIONS: ClassVar[dict[str, frozenset[str]]] = {
        "waiting": frozenset({"downloading", "paused", "failed", "removed"}),
        "downloading": frozenset({"waiting", "paused", "completed", "failed", "removed"}),
        "paused": frozenset({"waiting", "downloading", "failed", "removed"}),
        "failed": frozenset({"waiting", "removed"}),
        "completed": frozenset({"removed"}),
        "removed": frozenset(),
    }

    # Fields whose writes must be serialized behind ``self.lock`` once the
    # manager side adopts :meth:`transition`. Documented here (rather than as
    # __slots__) so call-sites can grep the canonical list.
    _LOCKED_FIELDS: ClassVar[tuple[str, ...]] = (
        "status",
        "stop_requested",
        "stop_reason",
        "process",
        "retry_count",
        "error_message",
    )

    def __post_init__(self) -> None:
        # ``threading.RLock`` (not ``Lock``) so a single thread can re-enter
        # :meth:`transition` safely from helpers that already hold the lock
        # (e.g. a snapshot emitter). The attribute is assigned via
        # ``object.__setattr__`` so that future migration to
        # ``@dataclass(frozen=True)`` stays straightforward.
        object.__setattr__(self, "lock", threading.RLock())

    # --- Locked helpers -----------------------------------------------------

    def _read_status(self) -> str:
        with self.lock:
            return self.status

    def _set_fields_locked(self, **fields: Any) -> None:
        """Set one or more volatile fields atomically.

        This is an internal helper used by :meth:`transition` and by the
        module-local success path; external callers should prefer
        :meth:`transition` for status changes. Only the fields listed in
        :attr:`_LOCKED_FIELDS` are accepted; anything else raises
        ``AttributeError`` to surface migration mistakes loudly.
        """
        invalid = [k for k in fields if k not in self._LOCKED_FIELDS]
        if invalid:
            raise AttributeError(
                f"DownloadTask._set_fields_locked refused non-locked field(s): {invalid!r}"
            )
        with self.lock:
            for key, value in fields.items():
                object.__setattr__(self, key, value)

    def transition(self, new_status: str, *, reason: Optional[str] = None) -> None:
        """Move the task to ``new_status`` under ``self.lock``.

        Args:
            new_status: Target status string. Must be present in
                :attr:`_TRANSITIONS`.
            reason: Optional ``stop_reason`` annotation stored alongside the
                new status; used for ``paused``/``cancelled``/``shutdown``/
                ``engine_switch``/``removed``/``other`` (see
                ``utils.errors.StopReason``).

        Raises:
            InvalidTransition: When the move is not listed in
                :attr:`_TRANSITIONS`. Same-state transitions are permitted
                only when ``reason`` is provided; this allows the manager to
                re-annotate a paused task with a different ``stop_reason``
                without an intermediate state.

        Side effects:
            * On any transition into ``downloading`` the ``stop_requested``
              flag is cleared, matching the behaviour of
              ``DownloadManager._reset_task_runtime`` which resets the flag
              when a task leaves the queue.
            * ``process`` is never mutated here; terminal transitions leave
              the process handle for the caller to drain/close, matching the
              current ``core/download_manager.py`` finalization path.
        """
        with self.lock:
            old = self.status
            allowed = self._TRANSITIONS.get(old, frozenset())

            if new_status == old:
                # Only permit idempotent re-entry when the caller is
                # annotating a new reason; a blind no-op is silently skipped
                # to match the documented "accept-or-raise" contract.
                if reason is None:
                    return
                # Re-entering a terminal state is still forbidden even with a
                # reason: once completed/removed, the reason is frozen.
                if old in {"completed", "removed"}:
                    raise InvalidTransition(old, new_status, reason=reason)
            elif new_status not in allowed:
                # ``failed -> downloading`` is explicitly excluded: the retry
                # path must go ``failed -> waiting -> downloading`` which
                # bumps ``retry_count`` on the way through ``add_task``.
                raise InvalidTransition(old, new_status, reason=reason)

            # Apply the new state (and optional reason) atomically.
            object.__setattr__(self, "status", new_status)
            if reason is not None:
                object.__setattr__(self, "stop_reason", reason)

            # security-stability-hardening R30.2/R30.3 (tasks.md 27.1): an
            # engine-switch transition must drop the pid-ownership binding
            # atomically under ``self.lock`` so that any subsequent kill
            # path sees a fresh slate. Leaving ``_pid`` /
            # ``_expected_engine_name`` behind would risk killing a recycled
            # pid, and leaving ``process`` behind would risk double-kills
            # or stale handle reads.
            if reason is not None:
                reason_value = (
                    reason.value if hasattr(reason, "value") else str(reason)
                )
                # Compare against the canonical ``StopReason.ENGINE_SWITCH``
                # string literal so callers can pass either the enum or the
                # plain ``"engine_switch"`` literal.
                if reason_value == "engine_switch":
                    object.__setattr__(self, "process", None)
                    object.__setattr__(self, "_pid", None)
                    object.__setattr__(self, "_expected_engine_name", None)

            if new_status == "downloading":
                # Clear any pending stop request when we (re-)enter the
                # running state. ``stop_reason`` is preserved unless the
                # caller explicitly overwrote it above, matching
                # ``_reset_task_runtime``.
                object.__setattr__(self, "stop_requested", False)

    def get_status_display(self) -> str:
        """Return localized display status text."""
        from utils.i18n import TR
        with self.lock:
            current = self.status
        return TR(f"status_{current}")


# ---------------------------------------------------------------------------
# security-stability-hardening R11 / R29 — TaskSnapshot
# ---------------------------------------------------------------------------
#
# ``TaskSnapshot`` is the immutable, cross-thread-safe view of a
# :class:`DownloadTask` that is emitted toward UI consumers
# (``MainWindow.task_update_received``). It exists so the UI thread
# never reads volatile task fields while a worker thread is mid-write,
# and so any future telemetry pipeline can serialize the snapshot
# without risking a mutation race (see design 2.3 / data models / R29).
#
# Fields are kept deliberately minimal - only what the UI already
# displays today plus the machine-parsable ``stop_reason`` / ``error``
# annotations used by the Stage 3 classifier (R18). ``url_masked`` goes
# through :func:`utils.redact.redact_url` so sensitive query values
# (token / sign / signature / auth) never leak to the UI layer or any
# downstream log consumer (R3 / R6).
#
# The snapshot is a ``@dataclass(frozen=True)``; ``from_task`` takes
# ``task.lock`` for the duration of field reads so the snapshot is
# internally consistent even when workers mutate the task concurrently
# (R11.4 / R11.7).


@dataclass(frozen=True)
class TaskSnapshot:
    """Immutable, UI-safe view of a :class:`DownloadTask`.

    Field contract (see design §2.3 / §Data Models):

    * ``task_id``      -- stable identifier used to dedupe snapshots in
      the UI tree (currently ``str(id(task))`` to match
      ``ui/download_queue.py`` bookkeeping; callers MUST NOT depend on
      the specific format).
    * ``url_masked``   -- ``redact_url(task.url)`` so sensitive query
      values never reach the UI / telemetry layer.
    * ``title``        -- ``task.filename`` (already Windows-sanitized
      by ``utils.win_path.sanitize_title`` per R12).
    * ``engine``       -- current engine name (e.g. ``"n_m3u8dl_re"``);
      empty string before engine selection completes.
    * ``status``       -- one of the statuses accepted by
      :attr:`DownloadTask._TRANSITIONS`.
    * ``progress``     -- 0..100 percentage (matches the scale used by
      ``DownloadTask.progress`` today).
    * ``speed_bps``    -- best-effort integer bytes/sec. When the engine
      only reports a human-readable speed string this falls back to 0
      and the UI keeps the legacy display via the raw-task path.
    * ``stop_reason``  -- raw ``task.stop_reason`` string (matches the
      ``utils.errors.StopReason`` enum values; empty string when no
      stop has been requested).
    * ``error``        -- latest ``task.error_message`` (already
      redacted by upstream engine logging; unchanged here to keep
      backwards compatibility with existing classifier fallbacks).
    * ``updated_at``   -- Unix timestamp of the moment the snapshot was
      materialized; emitted as a float for precision and JSON-friendly
      serialization.
    """

    task_id: str
    url_masked: str
    title: str
    engine: str
    status: str
    progress: float
    speed_bps: int
    stop_reason: Optional[str]
    error: Optional[str]
    updated_at: float

    # Field ordering used by :meth:`to_dict`. Keeping a dedicated tuple
    # (instead of relying on ``dataclasses.fields``) makes the on-wire
    # contract explicit and stable: the order below is the serialization
    # order promised to downstream consumers (logging / telemetry /
    # future JSON upload per R29.3). Reordering here is a breaking
    # change; adding a new field must also update the lint contract in
    # ``scripts/lint_main_window_slots.py``.
    _SERIALIZED_FIELDS: ClassVar[tuple[str, ...]] = (
        "task_id",
        "url_masked",
        "title",
        "engine",
        "status",
        "progress",
        "speed_bps",
        "stop_reason",
        "error",
        "updated_at",
    )

    def to_dict(self) -> dict[str, Any]:
        """Return a stable, JSON-friendly ``dict`` view of the snapshot.

        security-stability-hardening R29.3 — the snapshot must serialize
        deterministically so logs / telemetry / future upload pipelines
        see the same key set and ordering across versions. Key order
        follows :attr:`_SERIALIZED_FIELDS`; value coercion keeps the
        output trivially JSON-encodable:

        * ``task_id`` / ``url_masked`` / ``title`` / ``engine`` /
          ``status`` are emitted as-is (all already ``str``).
        * ``progress`` / ``updated_at`` stay ``float``.
        * ``speed_bps`` stays ``int``.
        * ``stop_reason`` / ``error`` fall back to empty strings when
          ``None`` so downstream JSON schemas don't need to special-case
          nulls. The snapshot is already the redacted view, so there is
          no additional redaction step here.

        Callers MUST treat the returned ``dict`` as read-only; the
        snapshot itself remains a frozen dataclass and is the source of
        truth.
        """

        return {
            "task_id": self.task_id,
            "url_masked": self.url_masked,
            "title": self.title,
            "engine": self.engine,
            "status": self.status,
            "progress": float(self.progress),
            "speed_bps": int(self.speed_bps),
            "stop_reason": self.stop_reason or "",
            "error": self.error or "",
            "updated_at": float(self.updated_at),
        }

    @classmethod
    def from_task(cls, task: "DownloadTask") -> "TaskSnapshot":
        """Materialize a snapshot by reading ``task`` atomically under its lock.

        Acquiring ``task.lock`` ensures the volatile fields listed in
        :attr:`DownloadTask._LOCKED_FIELDS` are captured consistently
        even when a worker thread is mid-``transition``. The URL is
        redacted via :func:`utils.redact.redact_url` before leaving the
        lock so downstream consumers never see the raw value.
        """

        with task.lock:
            raw_url = task.url or ""
            status = task.status
            progress = float(task.progress or 0.0)
            speed_bps = _coerce_speed_bps(task.speed)
            stop_reason = task.stop_reason or None
            error = task.error_message or None
            engine = task.engine or ""
            title = task.filename or ""
            # ``task_id`` uses ``id(task)`` today because neither
            # ``DownloadTask`` nor ``DownloadQueuePanel`` carries a
            # stable UUID yet; stringifying keeps the contract explicit
            # and forward-compatible with a future dedicated field.
            task_id = str(id(task))

        return cls(
            task_id=task_id,
            url_masked=redact_url(raw_url),
            title=title,
            engine=engine,
            status=status,
            progress=progress,
            speed_bps=speed_bps,
            stop_reason=stop_reason,
            error=error,
            updated_at=time.time(),
        )


# ---------------------------------------------------------------------------
# speed coercion helper
# ---------------------------------------------------------------------------
#
# ``DownloadTask.speed`` is a human-readable string assembled by the
# various engines (e.g. ``"1.2MB/s"``, ``"523 KB/s"``, ``""``). For the
# snapshot we prefer an integer bytes/sec so telemetry consumers can
# aggregate without re-parsing; we fall back to 0 whenever the string
# is missing or not parseable, matching the best-effort contract
# documented on :class:`TaskSnapshot.speed_bps`.

_SPEED_UNITS: dict[str, int] = {
    "": 1,
    "b": 1,
    "b/s": 1,
    "bps": 1,
    "k": 1024,
    "kb": 1024,
    "kb/s": 1024,
    "kbps": 1024,
    "kib/s": 1024,
    "m": 1024 * 1024,
    "mb": 1024 * 1024,
    "mb/s": 1024 * 1024,
    "mbps": 1024 * 1024,
    "mib/s": 1024 * 1024,
    "g": 1024 * 1024 * 1024,
    "gb": 1024 * 1024 * 1024,
    "gb/s": 1024 * 1024 * 1024,
    "gbps": 1024 * 1024 * 1024,
    "gib/s": 1024 * 1024 * 1024,
}

_SPEED_RE = re.compile(
    r"^\s*([0-9]*\.?[0-9]+)\s*([A-Za-z/]*)\s*$"
)


def _coerce_speed_bps(speed: Any) -> int:
    """Return a best-effort integer bytes/sec for ``speed``.

    Accepts the following shapes:

    * ``int`` / ``float``  -- assumed already in bytes/sec.
    * ``str``              -- ``"1.2MB/s"`` / ``"523 KB/s"`` / ...
    * anything else        -- returns 0.

    Unknown suffixes degrade gracefully to 0 so a malformed engine
    string never crashes the snapshot path.
    """

    if speed is None:
        return 0
    if isinstance(speed, bool):
        # ``bool`` is a subclass of ``int`` but is never a valid speed.
        return 0
    if isinstance(speed, (int, float)):
        try:
            return max(0, int(speed))
        except (OverflowError, ValueError):
            return 0
    if not isinstance(speed, str):
        return 0

    match = _SPEED_RE.match(speed)
    if not match:
        return 0
    number_part, unit_part = match.group(1), match.group(2).lower()
    try:
        number = float(number_part)
    except ValueError:
        return 0
    multiplier = _SPEED_UNITS.get(unit_part)
    if multiplier is None:
        # Unknown suffix: keep the scalar so very small numbers are not
        # silently zeroed, but avoid amplifying via an unknown unit.
        multiplier = 1
    try:
        return max(0, int(number * multiplier))
    except (OverflowError, ValueError):
        return 0
