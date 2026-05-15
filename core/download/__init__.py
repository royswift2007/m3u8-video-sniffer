"""``core.download`` package — split of the legacy ``core.download_manager``.

Task 25.1 of the ``security-stability-hardening`` spec breaks the
1600-line ``core/download_manager.py`` into a cohesive package:

* :mod:`core.download.manager`      — :class:`DownloadManager` orchestrator.
* :mod:`core.download.task_queue`   — :class:`TaskQueue` FIFO helper.
* :mod:`core.download.worker_pool`  — :class:`WorkerPool` thread management.
* :mod:`core.download.classifier`   — pure classification helpers.

For backwards compatibility, this ``__init__`` re-exports
:class:`DownloadManager` so both ``from core.download import
DownloadManager`` and the legacy ``from core.download_manager import
DownloadManager`` continue to work. The legacy
``core/download_manager.py`` module is kept as a thin shim that imports
from this package.
"""

from core.download.manager import DownloadManager

__all__ = ["DownloadManager"]
