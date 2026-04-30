import json
import logging
from dataclasses import asdict
from datetime import datetime, timezone

from config import OUTPUT_JSON_PATH
from models import Show

log = logging.getLogger(__name__)


def write_json(shows: list[Show]) -> None:
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "shows": [asdict(s) for s in shows],
    }
    with open(OUTPUT_JSON_PATH, "w") as f:
        json.dump(payload, f, indent=2)
    log.info("Wrote %d shows to %s", len(shows), OUTPUT_JSON_PATH)
