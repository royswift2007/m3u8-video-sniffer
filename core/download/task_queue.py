"""Thread-safe FIFO queue for ``DownloadTask`` instances.

This module replaces the use of ``queue.Queue`` inside
``DownloadManager`` so that no caller has to reach into private fields
such as ``.mutex``, ``.queue``, ``.unfinished_tasks``, ``.all_tasks_done``
or ``.not_full`` to inspect or mutate the queue.

Design lineage: ``security-stability-hardening`` spec, Requirement 11
(Stage 2 / P1-3 / P1-4) and the Design document section 2.3.

Container layout:

* ``_items``   — a ``collections.deque`` providing O(1) append/popleft.
* ``_index``   — a ``dict[str, DownloadTask]`` providing O(1) lookup
                 and removal by task id.
* ``_lock``    — a reentrant lock protecting every mutation and read.

The two structures are always mutated together under the lock so the
external invariant ``len(_items) == len(_index)`` holds between
operations.

The module is deliberately self-contained and does **not** import
``core.download_manager`` or ``core.task_model`` at runtime; the
``DownloadTask`` type is referenced via ``TYPE_CHECKING`` only so that
this module can be imported from unit tests and from
``core.download_manager`` without creating a cycle.
"""

from __future__ import annotations

import threading
from collections import deque
from typing import TYPE_CHECKING, Callable, Iterator, List, Optional

if TYPE_CHECKING:  # pragma: no cover - import only for type checkers
    from core.task_model import DownloadTask


__all__ = ["TaskQueue", "default_task_key"]


def default_task_key(task: "DownloadTask") -> str:
    """Derive a stable string key for ``task``.

    Prefers an explicit ``task.task_id`` attribute (introduced by
    Requirement 11 / Task 11.2). Falls back to a key derived from the
    Python object identity so this module remains usable before the
    ``DownloadTask`` model grows a ``task_id`` field.
    """

    tid = getattr(task, "task_id", None)
    if tid:
        return str(tid)
    return f"task-{id(task):x}"


class TaskQueue:
    """Minimal FIFO queue of ``DownloadTask`` with O(1) id lookup.

    All public methods are safe to call concurrently. Ordering is FIFO:
    ``pop_ready()`` returns tasks in the order they were ``put``.
    """

    def __init__(
        self,
        *,
        key_fn: Optional[Callable[["DownloadTask"], str]] = None,
    ) -> None:
        self._lock = threading.RLock()
        self._items: "deque[DownloadTask]" = deque()
        self._index: dict = {}
        self._key_fn: Callable[["DownloadTask"], str] = key_fn or default_task_key

    # ------------------------------------------------------------------
    # Mutation
    # ------------------------------------------------------------------
    def put(self, task: "DownloadTask") -> str:
        """Append ``task`` to the tail of the queue.

        Returns the key used to index ``task``. If a task with the same
        key is already queued the call is an idempotent no-op and the
        existing key is returned; this preserves the semantics of
        ``DownloadManager._is_task_queued`` without exposing queue
        internals.
        """

        key = self._key_fn(task)
        with self._lock:
            if key in self._index:
                return key
            self._items.append(task)
            self._index[key] = task
        return key

    def pop_ready(self) -> Optional["DownloadTask"]:
        """Pop the head task, or return ``None`` if the queue is empty.

        The task is removed from both the deque and the index atomically.
        """

        with self._lock:
            if not self._items:
                return None
            task = self._items.popleft()
            key = self._key_fn(task)
            # ``pop`` is used instead of ``del`` to tolerate keys that
            # have drifted (e.g. a late ``task_id`` assignment); the
            # deque remains the source of truth for ordering.
            self._index.pop(key, None)
            return task

    def remove(self, task_id: str) -> bool:
        """Remove the task whose key equals ``task_id``.

        Returns ``True`` if a task was removed, ``False`` if no such id
        was queued. O(1) for the index; O(n) for the deque ``remove``
        but bounded by the queue length which is already small.
        """

        with self._lock:
            task = self._index.pop(task_id, None)
            if task is None:
                return False
            try:
                self._items.remove(task)
            except ValueError:
                # Should not happen under the lock, but if the deque is
                # out of sync we still consider the removal successful
                # from the index point of view.
                pass
            return True

    def clear(self) -> List["DownloadTask"]:
        """Drain the queue and return the list of tasks in FIFO order.

        Returning the drained tasks lets callers (such as
        ``DownloadManager.shutdown``) reason about what was still
        pending without having to snapshot then clear.
        """

        with self._lock:
            drained = list(self._items)
            self._items.clear()
            self._index.clear()
            return drained

    # ------------------------------------------------------------------
    # Inspection
    # ------------------------------------------------------------------
    def snapshot(self) -> List["DownloadTask"]:
        """Return a shallow copy of the queued tasks, head first.

        The returned list is owned by the caller and may be mutated
        freely; the underlying queue is not affected.
        """

        with self._lock:
            return list(self._items)

    def size(self) -> int:
        """Return the current number of queued tasks."""

        with self._lock:
            return len(self._items)

    def contains(self, task_id: str) -> bool:
        """Return ``True`` iff a task with id ``task_id`` is queued."""

        with self._lock:
            return task_id in self._index

    # ------------------------------------------------------------------
    # Dunder helpers
    # ------------------------------------------------------------------
    def __len__(self) -> int:
        return self.size()

    def __contains__(self, task_id: object) -> bool:
        if not isinstance(task_id, str):
            return False
        return self.contains(task_id)

    def __iter__(self) -> Iterator["DownloadTask"]:
        # Iterate over a snapshot so the caller can mutate the queue
        # while iterating without triggering ``RuntimeError``.
        return iter(self.snapshot())

    def __bool__(self) -> bool:
        return self.size() > 0

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"TaskQueue(size={self.size()})"
