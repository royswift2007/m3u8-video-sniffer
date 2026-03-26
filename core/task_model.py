"""
Task data models.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional
from urllib.parse import unquote, urlparse
import re


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
    quality_label: str = ""
    variants_listed: bool = False
    candidate_score: int = 0

    def __post_init__(self):
        if not self.title:
            self.title = self._extract_title()

    def _extract_title(self) -> str:
        """Build a reasonable default title from page title or URL."""
        if self.page_title:
            clean_title = re.sub(r'[<>:"/\\|?*]', "_", self.page_title)
            return clean_title[:100]

        try:
            path = urlparse(self.url).path
            filename = path.split("/")[-1]
            if filename:
                name = filename.rsplit(".", 1)[0]
                if name:
                    return unquote(name)
        except Exception:
            pass

        return "untitled_video"


@dataclass
class DownloadTask:
    """Download task entity."""

    url: str
    save_dir: str
    filename: str
    headers: dict
    status: str = "waiting"  # waiting, downloading, completed, failed, paused
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
    stop_reason: str = ""  # paused, cancelled, shutdown
    created_at: datetime = field(default_factory=datetime.now)
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None

    def get_status_display(self) -> str:
        """Return localized display status text."""
        status_map = {
            "waiting": "Waiting",
            "downloading": "Downloading",
            "completed": "Completed",
            "failed": "Failed",
            "paused": "Paused",
        }
        return status_map.get(self.status, self.status)
