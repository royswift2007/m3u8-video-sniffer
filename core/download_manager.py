"""Backwards-compatible shim for :mod:`core.download`.

Task 25.1 of the ``security-stability-hardening`` spec split the
original ``DownloadManager`` across the :mod:`core.download` package
(``manager.py`` / ``task_queue.py`` / ``worker_pool.py`` /
``classifier.py``). This module remains to preserve legacy import
paths such as ``from core.download_manager import DownloadManager``
and must not contain any standalone logic. New code should import
directly from ``core.download``.
"""

from core.download import DownloadManager

__all__ = ["DownloadManager"]
