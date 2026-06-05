import hashlib
from dataclasses import dataclass, field


@dataclass
class Show:
    artist: str
    date: str  # ISO 8601 date string, e.g. "2026-08-15"
    venue: str
    city: str
    region: str
    country: str
    ticket_url: str
    source: str  # which service provided this record
    raw_id: str = ""  # source-specific identifier for deduplication
    start_time: str = ""  # local start time "HH:MM" (24h), "" if unknown

    def dedup_key(self) -> str:
        """Stable hash used to deduplicate across sources."""
        raw = f"{self.artist}|{self.date}|{self.venue}|{self.city}"
        return hashlib.md5(raw.lower().encode()).hexdigest()
