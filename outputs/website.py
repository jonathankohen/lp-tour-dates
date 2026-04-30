import logging
from dataclasses import asdict

import requests

from config import OUTPUT_WEBSITE_URL
from models import Show

log = logging.getLogger(__name__)


def write_website(shows: list[Show]) -> None:
    """
    POST the shows JSON to a webhook/API endpoint on the destination website.
    Expects the endpoint to accept { "shows": [...] } and return 2xx.
    """
    if not OUTPUT_WEBSITE_URL:
        return
    payload = {"shows": [asdict(s) for s in shows]}
    try:
        resp = requests.post(OUTPUT_WEBSITE_URL, json=payload, timeout=15)
        resp.raise_for_status()
        log.info("Posted %d shows to %s", len(shows), OUTPUT_WEBSITE_URL)
    except Exception as exc:
        log.error("Website output error: %s", exc)
