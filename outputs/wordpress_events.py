"""
Publish aggregated shows to the WordPress site as VS Event List `event` posts.

VS Event List exposes no usable REST API (its CPT/meta aren't registered with
show_in_rest), so creation is delegated to the Tour Calendar plugin's
/publish-events endpoint, which calls native wp_insert_post()/update_post_meta()
server-side. This module gathers each act's fallback image + description from a
Google Drive folder and POSTs everything; the server decides per show whether to
skip (act + date already exists), copy image/body from an existing event of the
same act, or use the Drive fallback. Mirrors outputs/website.py for transport.
"""
import base64
import calendar
import io
import logging
import os
import re
from dataclasses import asdict
from datetime import date as _date

import requests

from config import (
    WORDPRESS_PUBLISH_EVENTS_URL,
    WORDPRESS_ASSETS_DRIVE_FOLDER_ID,
    WORDPRESS_DEFAULT_EVENT_TIME,
    OUTPUT_WEBSITE_SECRET,
)
from models import Show

log = logging.getLogger(__name__)

_IMAGE_MIME_PREFIX = "image/"

# Document formats we can turn into a description. Google Docs are exported to
# text/plain through the Drive API; everything else is downloaded raw and parsed
# locally (.docx via the stdlib, legacy .doc/.rtf via macOS `textutil`).
_GOOGLE_DOC_MIME = "application/vnd.google-apps.document"
_DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
_DOC_MIME = "application/msword"
_RTF_MIMES = ("application/rtf", "text/rtf")
_DESCRIPTION_MIMES = {
    "text/plain", "text/markdown", "text/x-markdown",
    _DOC_MIME, _DOCX_MIME, _GOOGLE_DOC_MIME, *_RTF_MIMES,
}
_DESCRIPTION_EXTENSIONS = (".txt", ".text", ".md", ".markdown", ".rtf", ".doc", ".docx")

# Placeholder/private shows that should never become public-facing events.
_PRIVATE_TBA_RE = re.compile(r"private event|on hold|\btba\b", re.IGNORECASE)


def _is_private_or_tba(show: Show) -> bool:
    """True for private holds / unannounced placeholders (venue or city)."""
    return bool(_PRIVATE_TBA_RE.search(f"{show.venue} {show.city}"))


def _one_month_cutoff() -> str:
    """ISO date one month from today, with the day clamped to the target month."""
    t = _date.today()
    year, month = (t.year + 1, 1) if t.month == 12 else (t.year, t.month + 1)
    day = min(t.day, calendar.monthrange(year, month)[1])
    return _date(year, month, day).isoformat()


def publish_events(shows: list[Show], dry_run: bool = False, limit: int = 0, one_month: bool = False) -> None:
    """
    Create a draft `event` post for every show not already on the site.

    Private/placeholder shows are always excluded. When one_month is set, only
    shows dated within the next month are considered. limit caps how many events
    the server creates (0 = unlimited) — mainly for testing. When dry_run is True
    the server plans the work and writes nothing; the summary of what *would* be
    created is logged so the operator can review before going live.
    """
    if not WORDPRESS_PUBLISH_EVENTS_URL:
        log.warning("WORDPRESS_PUBLISH_EVENTS_URL not set (and OUTPUT_WEBSITE_URL empty) — nothing to publish to.")
        return
    if not shows:
        log.warning("No shows to publish.")
        return

    # Always drop private holds and unannounced (TBA) placeholders.
    kept = [s for s in shows if not _is_private_or_tba(s)]
    if len(kept) != len(shows):
        log.info("Excluded %d private/TBA show(s).", len(shows) - len(kept))
    shows = kept

    if one_month:
        cutoff = _one_month_cutoff()
        before = len(shows)
        shows = [s for s in shows if s.date <= cutoff]
        log.info("One-month window (through %s): %d of %d shows.", cutoff, len(shows), before)

    if not shows:
        log.warning("No shows left to publish after filters.")
        return

    artists = sorted({s.artist for s in shows})
    assets = _load_drive_assets(artists)

    payload = {
        "dry_run": dry_run,
        "default_time": WORDPRESS_DEFAULT_EVENT_TIME,
        "limit": limit,
        "shows": [asdict(s) for s in shows],
        "assets": assets,
    }
    headers = {}
    if OUTPUT_WEBSITE_SECRET:
        headers["X-Tour-Secret"] = OUTPUT_WEBSITE_SECRET

    try:
        # Longer timeout than the ingest webhook: the server may sideload images.
        resp = requests.post(WORDPRESS_PUBLISH_EVENTS_URL, json=payload, headers=headers, timeout=120)
        resp.raise_for_status()
    except Exception as exc:
        log.error("publish-events request failed: %s", exc)
        return

    try:
        result = resp.json()
    except ValueError:
        log.error("publish-events returned non-JSON response: %s", resp.text[:500])
        return

    _log_summary(result, dry_run)


def _log_summary(result: dict, dry_run: bool) -> None:
    created = result.get("created", []) or []
    skipped = result.get("skipped", []) or []
    would = result.get("would_create", []) or []
    errors = result.get("errors", []) or []

    n_exists = sum(1 for s in skipped if s.get("reason") == "exists")
    n_nocontent = sum(1 for s in skipped if s.get("reason") == "no_content")

    if dry_run:
        log.info(
            "DRY RUN — %d would be created; skipped %d (already exist) + %d (no image/body).",
            len(would), n_exists, n_nocontent,
        )
        for p in would:
            log.info(
                "  + %s | %s | %s | link=%s | body=%s image=%s",
                p.get("date", ""), p.get("title", ""), p.get("location", ""),
                p.get("link", "") or "(none)", p.get("body_source", ""), p.get("image_source", ""),
            )
    else:
        log.info(
            "Published — %d created; skipped %d (already exist) + %d (no image/body).",
            len(created), n_exists, n_nocontent,
        )
        for c in created:
            log.info("  + #%s %s | %s", c.get("id", ""), c.get("artist", ""), c.get("date", ""))

    for s in skipped:
        log.debug(
            "  = skip [%s] %s | %s (matched '%s')",
            s.get("reason", ""), s.get("date", ""), s.get("artist", ""), s.get("matched_title", ""),
        )
    for e in errors:
        log.error("  ! error %s | %s: %s", e.get("date", ""), e.get("artist", ""), e.get("error", ""))
    if errors:
        log.warning("%d show(s) failed to publish — see errors above.", len(errors))


# ---------------------------------------------------------------------------- #
#  Google Drive asset loading                                                  #
# ---------------------------------------------------------------------------- #


def _norm(name: str) -> str:
    """Match folder names to artist names leniently (lowercase, alnum only)."""
    return "".join(c for c in name.lower() if c.isalnum())


def _is_description_file(f: dict) -> bool:
    """True for any supported description asset: txt, md, rtf, doc, docx, Google Doc."""
    name = str(f.get("name", "")).lower()
    mime = str(f.get("mimeType", ""))
    return mime in _DESCRIPTION_MIMES or name.endswith(_DESCRIPTION_EXTENSIONS)


def _lines_to_paragraphs(text: str) -> str:
    """One line per paragraph -> blank-line-separated paragraphs.

    Used for formats whose extractors emit a single line per paragraph (legacy
    Office via textutil, Google Docs export). The PHP cleanup treats blank lines
    as paragraph breaks and joins single newlines, so we hand it that contract.
    Plain .txt is deliberately NOT run through this — it may be hard-wrapped, and
    the PHP cleanup unwraps it correctly on its own.
    """
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = [ln.strip() for ln in text.split("\n")]
    return "\n\n".join(ln for ln in lines if ln)


def _docx_to_text(data: bytes) -> str:
    """Extract paragraph text from a .docx (Office Open XML) using only the stdlib."""
    import zipfile
    from xml.etree import ElementTree as ET

    ns = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            xml = zf.read("word/document.xml")
        root = ET.fromstring(xml)
    except Exception as exc:
        log.warning("Could not parse .docx: %s", exc)
        return ""

    paragraphs = []
    for p in root.iter(f"{ns}p"):
        parts = []
        for node in p.iter():
            if node.tag == f"{ns}t":
                parts.append(node.text or "")
            elif node.tag == f"{ns}tab":
                parts.append("\t")
            elif node.tag in (f"{ns}br", f"{ns}cr"):
                parts.append(" ")
        line = "".join(parts).strip()
        if line:
            paragraphs.append(line)
    return "\n\n".join(paragraphs)


def _textutil_to_text(data: bytes, suffix: str) -> str:
    """Convert a legacy .doc / .rtf to text via macOS `textutil`. '' (with a warning)
    if textutil is unavailable (non-macOS) or conversion fails."""
    import shutil
    import subprocess
    import tempfile

    if not shutil.which("textutil"):
        log.warning("Cannot extract %s — 'textutil' (macOS) not found; convert it to .docx or .txt.", suffix)
        return ""
    tmp_path = ""
    try:
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tf:
            tf.write(data)
            tmp_path = tf.name
        proc = subprocess.run(
            ["textutil", "-convert", "txt", "-stdout", tmp_path],
            capture_output=True, timeout=30,
        )
        if proc.returncode != 0:
            log.warning("textutil failed for %s: %s", suffix, proc.stderr.decode("utf-8", "replace")[:200])
            return ""
        return _lines_to_paragraphs(proc.stdout.decode("utf-8", "replace"))
    except Exception as exc:
        log.warning("textutil error for %s: %s", suffix, exc)
        return ""
    finally:
        if tmp_path:
            try:
                os.remove(tmp_path)
            except OSError:
                pass


def _extract_description(data: bytes, name: str, mime: str) -> str:
    """Turn raw asset bytes into plain text based on type. '' if unsupported/empty.

    Google Docs are handled by the caller (exported to text/plain via Drive), not
    here. Output is normalized so the PHP cleanup sees blank-line-separated
    paragraphs, except plain .txt/.md which pass through raw for the cleanup to
    unwrap (it may be hard-wrapped).
    """
    name_l = str(name).lower()
    if mime == _DOCX_MIME or name_l.endswith(".docx"):
        return _docx_to_text(data) or _textutil_to_text(data, ".docx")
    if mime == _DOC_MIME or name_l.endswith(".doc"):
        return _textutil_to_text(data, ".doc")
    if mime in _RTF_MIMES or name_l.endswith(".rtf"):
        return _textutil_to_text(data, ".rtf")
    # text/plain, markdown, or anything else small enough to be text.
    return data.decode("utf-8", errors="replace")


def _load_drive_assets(artists: list[str]) -> dict:
    """
    For each act, return {artist: {image_b64, image_filename, description}} pulled
    from a per-act subfolder of WORDPRESS_ASSETS_DRIVE_FOLDER_ID. Acts without a
    folder (or without assets) are simply omitted — the server then falls back to
    an existing event of the same act, or creates the event without them.
    """
    if not WORDPRESS_ASSETS_DRIVE_FOLDER_ID:
        log.info("WORDPRESS_ASSETS_DRIVE_FOLDER_ID not set — skipping Drive assets (server will use existing events as templates).")
        return {}

    try:
        from googleapiclient.discovery import build  # type: ignore
        from googleapiclient.http import MediaIoBaseDownload  # type: ignore
        from google.oauth2 import service_account  # type: ignore
    except ImportError:
        log.warning("google-api-python-client not installed — skipping Drive assets.")
        return {}

    creds_path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "")
    if not creds_path:
        log.warning("GOOGLE_APPLICATION_CREDENTIALS not set — skipping Drive assets.")
        return {}

    list_kwargs = {
        "supportsAllDrives": True,
        "includeItemsFromAllDrives": True,
        "pageSize": 1000,
    }

    # Build the client and list the act subfolders. Drive assets are an optional
    # fallback, so any failure here (API disabled, folder not shared, auth) must
    # degrade gracefully — the server can still template off existing events.
    try:
        scopes = ["https://www.googleapis.com/auth/drive.readonly"]
        creds = service_account.Credentials.from_service_account_file(creds_path, scopes=scopes)
        service = build("drive", "v3", credentials=creds)

        # Map every act subfolder by normalized name so internal artist names that
        # differ slightly from folder names still match.
        folders = service.files().list(
            q=f"'{WORDPRESS_ASSETS_DRIVE_FOLDER_ID}' in parents "
              "and mimeType='application/vnd.google-apps.folder' and trashed=false",
            fields="files(id,name)",
            **list_kwargs,
        ).execute().get("files", [])
    except Exception as exc:
        log.warning("Could not access Google Drive assets (%s) — continuing without them.", exc)
        return {}
    folder_by_norm = {_norm(f["name"]): f for f in folders}

    def _download(file_id: str, export_mime: str | None = None) -> bytes:
        # Google-native files (Docs) must be exported; uploaded files use get_media.
        if export_mime:
            request = service.files().export_media(fileId=file_id, mimeType=export_mime)
        else:
            request = service.files().get_media(fileId=file_id)
        buf = io.BytesIO()
        downloader = MediaIoBaseDownload(buf, request)
        done = False
        while not done:
            _status, done = downloader.next_chunk()
        return buf.getvalue()

    assets: dict = {}
    for artist in artists:
        folder = folder_by_norm.get(_norm(artist))
        if not folder:
            continue

        try:
            files = service.files().list(
                q=f"'{folder['id']}' in parents and trashed=false",
                fields="files(id,name,mimeType)",
                **list_kwargs,
            ).execute().get("files", [])
        except Exception as exc:
            log.warning("Could not list Drive folder for %s: %s", artist, exc)
            continue

        image = next((f for f in files if str(f.get("mimeType", "")).startswith(_IMAGE_MIME_PREFIX)), None)
        # Each artist folder holds just two files — an image and a document — so take
        # the first supported document, whatever it's named (txt, md, rtf, doc, docx,
        # Google Doc).
        desc = next((f for f in files if _is_description_file(f)), None)

        entry: dict = {}
        if image:
            try:
                entry["image_b64"] = base64.b64encode(_download(image["id"])).decode("ascii")
                entry["image_filename"] = image["name"]
            except Exception as exc:
                log.warning("Could not download image for %s: %s", artist, exc)
        if desc:
            try:
                dmime = str(desc.get("mimeType", ""))
                dname = str(desc.get("name", ""))
                if dmime == _GOOGLE_DOC_MIME:
                    text = _lines_to_paragraphs(_download(desc["id"], export_mime="text/plain").decode("utf-8", errors="replace"))
                else:
                    text = _extract_description(_download(desc["id"]), dname, dmime)
                text = (text or "").strip()
                if text:
                    entry["description"] = text
                else:
                    log.warning("Description file '%s' for %s produced no text.", dname, artist)
            except Exception as exc:
                log.warning("Could not read description for %s: %s", artist, exc)

        if entry:
            assets[artist] = entry
            log.info("Drive assets for %s: image=%s description=%s",
                     artist, "yes" if "image_b64" in entry else "no", "yes" if "description" in entry else "no")

    if not assets:
        log.info("No Drive assets matched any act (server will rely on existing events as templates).")
    return assets
