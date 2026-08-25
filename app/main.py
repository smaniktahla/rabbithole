import os
import re
import json
import logging
import shutil
import tempfile
import uuid
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Optional

import requests
from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import FastAPI, HTTPException, Request, UploadFile, File, Form
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import database as db
import reddit
from config_manager import load_config, save_config
from docmost import upsert_page, list_spaces
from email_poller import check_email
from parser import classify_and_parse, classify_and_parse_reddit
from storage import write_markdown, write_reddit_markdown
from transcriber import (get_transcript, is_smb_video_path, parse_smb_path,
                         smb_file_exists, fetch_smb_file, strip_wrapping_quotes,
                         title_from_filename, parent_folder_name, transcribe_local_file,
                         LOCAL_VIDEO_EXTENSIONS, UPLOADS_DIR, is_uploaded_file_path)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)-25s %(levelname)s %(message)s"
)
logger = logging.getLogger("rabbithole")

scheduler = BackgroundScheduler(timezone="America/New_York")
last_email_check: dict = {"time": None, "queued": 0}

ATQ_URL = os.environ.get("ATQ_URL", "http://10.10.10.226:8700")
ATQ_NOTIFY_TARGET = os.environ.get("ATQ_NOTIFY_TARGET", "hermes-ai2")
REELMEALS_URL = os.environ.get("REELMEALS_URL", "http://10.10.10.13:8092")
REELMEALS_INGEST_API_KEY = os.environ.get("REELMEALS_INGEST_API_KEY", "")


def notify(message: str):
    """Push a completion/error notification through ATQ's escalate_user
    task type, which Hermes already polls and delivers over WhatsApp."""
    try:
        requests.post(f"{ATQ_URL}/tasks", json={
            "type": "escalate_user",
            "instructions": message,
            "assigned_to": ATQ_NOTIFY_TARGET,
        }, timeout=5)
    except Exception as e:
        logger.warning(f"ATQ notify failed: {e}")


def _finalize_and_sync(item_id: int, url: str, title: str, channel: str,
                       parsed: dict, filepath: str, docmost_space_id: str = None):
    """Shared tail: sync to DocMost, mark done, and notify. Used by both the
    YouTube and Reddit processing paths once a markdown file has been written."""
    docmost_result = None
    try:
        with open(filepath, encoding="utf-8") as f:
            md_content = f.read()
        db.update_item(item_id, status_message="Syncing to DocMost...")
        docmost_result = upsert_page(title or "Unknown", md_content,
                                     parsed.get("subject_area", "misc"),
                                     space_id=docmost_space_id)
    except Exception as dm_err:
        logger.warning(f"DocMost upsert skipped: {dm_err}")

    db.update_item(
        item_id,
        status="done",
        status_message=None,
        title=title,
        channel=channel,
        subject_area=parsed.get("subject_area"),
        file_path=filepath,
        docmost_page_id=(docmost_result or {}).get("page_id"),
        docmost_url=(docmost_result or {}).get("url"),
        processed_at=datetime.now().isoformat(),
        summary=(parsed.get("summary") or "")[:600],
        tags=json.dumps(parsed.get("tags", []))
    )
    logger.info(f"Done [{item_id}]: '{title}' -> {filepath}")
    link = (docmost_result or {}).get("url")
    msg = f"🐰 RabbitHole done: \"{title}\" [{parsed.get('subject_area', 'misc')}]"
    if link:
        msg += f"\n{link}"
    notify(msg)


def _process_reddit_item(item_id: int, url: str, item: dict):
    db.update_item(item_id, status="processing",
                   status_message="Fetching Reddit post + comments...")
    logger.info(f"Processing [{item_id}] (reddit): {url}")

    content = reddit.get_reddit_content(url)
    if not content:
        db.update_item(item_id, status="error", status_message=None,
                       error_message="Could not fetch Reddit post (deleted, private, or unreachable)")
        notify(f"🐰 RabbitHole failed: {url} — could not fetch Reddit post")
        return

    title = content["title"]
    subreddit = content["subreddit"]
    comments_block = reddit.format_comments_block(content["comments"])

    db.update_item(item_id, title=title, channel=subreddit,
                   status_message=f"Post + {len(content['comments'])} comments fetched — sending to LLM...")

    parsed = classify_and_parse_reddit(
        url=content["permalink"],
        title=title,
        subreddit=subreddit,
        author=content["author"],
        score=content["score"],
        num_comments=content["num_comments"],
        post_body=content["selftext"],
        comments_block=comments_block,
        subject_area_override=item.get("subject_area")
    )

    db.update_item(item_id,
                   status_message=f"Writing markdown → {parsed.get('subject_area', 'misc')}...")

    filepath = write_reddit_markdown(
        content["permalink"], title, subreddit, content["author"],
        content["score"], content["num_comments"], parsed,
        post_body=content["selftext"], link_url=content["link_url"],
        comments_block=comments_block
    )

    _finalize_and_sync(item_id, content["permalink"], title, subreddit, parsed, filepath,
                       docmost_space_id=item.get("docmost_space_id"))


def _process_uploaded_item(item_id: int, path: str, item: dict):
    title = title_from_filename(path)

    if not os.path.isfile(path):
        # Retry on an upload whose temp copy was already discarded (a prior
        # run either finished successfully or hit a deterministic failure
        # that made retrying the same bytes pointless). There's no source
        # left to re-fetch from — unlike YouTube/SMB, an upload only ever
        # exists as long as we're actively processing it.
        db.update_item(item_id, status="error", status_message=None, title=title,
                       error_message="This upload was already discarded (processed or a "
                                     "non-retryable failure) — re-upload the file to try again.")
        logger.warning(f"[{item_id}] Retry on uploaded file with no temp copy left: {path}")
        return

    db.update_item(item_id, status="processing",
                   status_message="Transcribing uploaded file with Whisper (may take a while)...")
    logger.info(f"Processing [{item_id}] (uploaded file): {path}")

    upload_dir = os.path.dirname(path)
    # The uploaded temp file is deleted once we're done with it, so replace
    # the DB's `url` (currently that temp path) with something durable and
    # human-readable — scoped by item_id so same-named uploads stay unique.
    display_source = f"Uploaded file: {os.path.basename(path)} (#{item_id})"

    try:
        transcript, error_reason = transcribe_local_file(path)
        if not transcript:
            error_message = error_reason or "Whisper produced no transcript for this file"
            db.update_item(item_id, status="error", status_message=None,
                           error_message=error_message, title=title)
            notify(f"🐰 RabbitHole failed: {title} — {error_message}")
            # Deterministic — same bytes will fail Whisper the same way
            # again, so there's nothing a retry could gain from keeping this.
            shutil.rmtree(upload_dir, ignore_errors=True)
            return

        db.update_item(item_id, title=title,
                       status_message=f"Transcript ready ({len(transcript):,} chars) — sending to LLM...")

        parsed = classify_and_parse(
            url=display_source,
            title=title,
            channel="Uploaded",
            transcript=transcript,
            subject_area_override=item.get("subject_area")
        )

        db.update_item(item_id,
                       status_message=f"Writing markdown → {parsed.get('subject_area', 'misc')}...")

        filepath = write_markdown(display_source, title, "Uploaded", parsed, transcript=transcript)

        _finalize_and_sync(item_id, display_source, title, "Uploaded", parsed, filepath,
                           docmost_space_id=item.get("docmost_space_id"))
        # The uploaded temp file is deleted now that we're fully done with
        # it — swap the DB's `url` (currently that temp path) for something
        # durable and readable, since the path itself won't exist anymore.
        db.update_item(item_id, url=display_source)
        shutil.rmtree(upload_dir, ignore_errors=True)
    except Exception as e:
        # Transient failure (LLM call, DocMost sync, etc) — leave the
        # uploaded file in place so Retry can pick up from transcription
        # again without asking the user to re-upload.
        logger.error(f"[{item_id}] Upload transcribe failed: {e}", exc_info=True)
        db.update_item(item_id, status="error", status_message=None, error_message=str(e)[:500])
        notify(f"🐰 RabbitHole failed: {title} — {str(e)[:200]}")


def _process_smb_item(item_id: int, raw_path: str, item: dict):
    db.update_item(item_id, status="processing", status_message="Resolving SMB path...")
    logger.info(f"Processing [{item_id}] (SMB file): {raw_path}")

    config = load_config()
    smb_cfg = config.get("smb", {})
    try:
        host, share, rel_path = parse_smb_path(raw_path, default_host=smb_cfg.get("default_host"))
    except ValueError as e:
        db.update_item(item_id, status="error", status_message=None, error_message=str(e))
        notify(f"🐰 RabbitHole failed: {raw_path} — {e}")
        return

    title = title_from_filename(rel_path)
    channel = parent_folder_name(rel_path)

    tmp_dir = tempfile.mkdtemp(prefix="rh_smb_")
    try:
        db.update_item(item_id, title=title, channel=channel,
                       status_message=f"Downloading over SMB from \\\\{host}\\{share}...")
        local_path = fetch_smb_file(host, share, rel_path, smb_cfg, tmp_dir)

        db.update_item(item_id, status_message="Transcribing with Whisper (may take a while)...")
        transcript, error_reason = transcribe_local_file(local_path)
        if not transcript:
            error_message = error_reason or "Whisper produced no transcript for this file"
            db.update_item(item_id, status="error", status_message=None, error_message=error_message)
            notify(f"🐰 RabbitHole failed: {raw_path} — {error_message}")
            return

        db.update_item(item_id,
                       status_message=f"Transcript ready ({len(transcript):,} chars) — sending to LLM...")

        parsed = classify_and_parse(
            url=raw_path,
            title=title,
            channel=channel or "Unknown",
            transcript=transcript,
            subject_area_override=item.get("subject_area")
        )

        db.update_item(item_id,
                       status_message=f"Writing markdown → {parsed.get('subject_area', 'misc')}...")

        filepath = write_markdown(raw_path, title, channel, parsed, transcript=transcript)

        _finalize_and_sync(item_id, raw_path, title, channel, parsed, filepath,
                           docmost_space_id=item.get("docmost_space_id"))
    except Exception as e:
        logger.error(f"[{item_id}] SMB fetch/transcribe failed: {e}", exc_info=True)
        db.update_item(item_id, status="error", status_message=None, error_message=str(e)[:500])
        notify(f"🐰 RabbitHole failed: {raw_path} — {str(e)[:200]}")
    finally:
        # Downloaded video is scratch data — discard it once we're done with
        # it (whether that ended in success or failure), never keep a copy.
        shutil.rmtree(tmp_dir, ignore_errors=True)


def _process_youtube_item(item_id: int, url: str, item: dict):
    db.update_item(item_id, status="processing",
                   status_message="Fetching transcript via yt-dlp...")
    logger.info(f"Processing [{item_id}]: {url}")

    transcript, title, channel, error_reason = get_transcript(url)
    if not transcript:
        error_message = error_reason or "Could not extract transcript or captions"
        db.update_item(item_id, status="error",
                       status_message=None,
                       error_message=error_message)
        notify(f"🐰 RabbitHole failed: {url} — {error_message}")
        return

    db.update_item(item_id,
                   title=title, channel=channel,
                   status_message=f"Transcript fetched ({len(transcript):,} chars) — sending to LLM...")

    parsed = classify_and_parse(
        url=url,
        title=title or "Unknown",
        channel=channel or "Unknown",
        transcript=transcript,
        subject_area_override=item.get("subject_area")
    )

    db.update_item(item_id,
                   status_message=f"Writing markdown → {parsed.get('subject_area', 'misc')}...")

    filepath = write_markdown(url, title, channel, parsed, transcript=transcript)

    _finalize_and_sync(item_id, url, title, channel, parsed, filepath,
                       docmost_space_id=item.get("docmost_space_id"))


def process_queue():
    items = db.get_queued_items()
    if not items:
        return
    for item in items:
        item_id = item["id"]
        url = item["url"]
        try:
            if reddit.is_reddit_url(url):
                _process_reddit_item(item_id, url, item)
            elif is_uploaded_file_path(url):
                _process_uploaded_item(item_id, url, item)
            elif is_smb_video_path(url):
                _process_smb_item(item_id, url, item)
            else:
                _process_youtube_item(item_id, url, item)

        except Exception as e:
            logger.error(f"Failed [{item_id}] {url}: {e}", exc_info=True)
            db.update_item(item_id, status="error", status_message=None,
                           error_message=str(e)[:500])
            notify(f"🐰 RabbitHole failed: {url} — {str(e)[:200]}")


def run_email_check():
    global last_email_check
    count = check_email()
    last_email_check = {"time": datetime.now().isoformat(), "queued": count}
    if count:
        logger.info(f"Email check: {count} new items queued")


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    config = load_config()
    interval = config.get("email", {}).get("check_interval_minutes", 5)
    scheduler.add_job(run_email_check, "interval", minutes=interval,
                      id="email_check", replace_existing=True)
    scheduler.add_job(process_queue, "interval", minutes=1,
                      id="process_queue", replace_existing=True)
    scheduler.start()
    logger.info("RabbitHole started — email check every %d min", interval)
    yield
    scheduler.shutdown(wait=False)


app = FastAPI(title="RabbitHole", lifespan=lifespan)


class SubmitRequest(BaseModel):
    url: str
    subject_area: Optional[str] = None
    docmost_space_id: Optional[str] = None


@app.post("/api/submit")
def submit_url(req: SubmitRequest):
    from transcriber import normalize_url, extract_video_id
    raw = req.url.strip()
    # Some share sheets (Android, browser extensions) attach a video title
    # or extra text alongside the link rather than sending a bare URL —
    # pull the first http(s) URL out of whatever we were given.
    match = re.search(r"https?://\S+", raw)
    if match:
        raw = match.group(0)

    if reddit.is_reddit_url(raw):
        if not reddit.extract_post_id(raw):
            raise HTTPException(400, "That's a Reddit URL but not a specific post — link to a post's comments page")
        url = reddit.normalize_reddit_url(raw)
    elif is_smb_video_path(raw):
        config = load_config()
        smb_cfg = config.get("smb", {})
        if not smb_cfg.get("username") or not smb_cfg.get("password"):
            raise HTTPException(400, "SMB credentials aren't configured — set Username/Password "
                                     "(and Default Host, for local drive-letter paths) in Settings")
        url = strip_wrapping_quotes(raw)
        try:
            host, share, rel_path = parse_smb_path(url, default_host=smb_cfg.get("default_host"))
        except ValueError as e:
            raise HTTPException(400, str(e))
        try:
            found = smb_file_exists(host, share, rel_path, smb_cfg)
        except Exception as e:
            raise HTTPException(400, f"Could not reach \\\\{host}\\{share}: {e}")
        if not found:
            raise HTTPException(400, f"File not found on the share: \\\\{host}\\{share}\\{rel_path}")
    else:
        url = normalize_url(raw)
        if not extract_video_id(url):
            raise HTTPException(400, "Could not find a YouTube video ID or Reddit post in that URL, "
                                     "and it's not a local/SMB video path")

    item_id = db.add_item(url, source="manual",
                          subject_area_override=req.subject_area or None,
                          docmost_space_id=req.docmost_space_id or None)
    if item_id == -1:
        # Already exists — if it errored, re-queue it
        with db.get_conn() as conn:
            row = conn.execute("SELECT id, status FROM items WHERE url = ?", (url,)).fetchone()
        if row and row["status"] == "error":
            db.update_item(row["id"], status="queued", error_message=None)
            return {"id": row["id"], "message": "Re-queued for processing"}
        raise HTTPException(409, "URL is already in the library")
    return {"id": item_id, "message": "Queued — will process within ~1 minute"}


@app.post("/api/submit-file")
async def submit_file(file: UploadFile = File(...),
                      subject_area: Optional[str] = Form(None),
                      docmost_space_id: Optional[str] = Form(None)):
    ext = os.path.splitext(file.filename or "")[1].lower()
    if ext not in LOCAL_VIDEO_EXTENSIONS:
        raise HTTPException(400, f"Not a recognized video extension: {ext or '(none)'}")

    dest_dir = os.path.join(UPLOADS_DIR, uuid.uuid4().hex)
    os.makedirs(dest_dir, exist_ok=True)
    dest_path = os.path.join(dest_dir, os.path.basename(file.filename))

    try:
        with open(dest_path, "wb") as out:
            shutil.copyfileobj(file.file, out, length=4 * 1024 * 1024)
    finally:
        await file.close()

    item_id = db.add_item(dest_path, source="upload",
                          subject_area_override=subject_area or None,
                          docmost_space_id=docmost_space_id or None)
    if item_id == -1:
        shutil.rmtree(dest_dir, ignore_errors=True)
        raise HTTPException(409, "That file is already queued or in the library")
    return {"id": item_id, "message": "Uploaded — will process within ~1 minute"}


@app.get("/api/library")
def get_library(limit: int = 20, offset: int = 0,
                subject_area: str = None, search: str = None,
                include_active: bool = False):
    items = db.get_items(limit=limit, offset=offset,
                         subject_area=subject_area, search=search,
                         include_active=include_active)
    return {"items": items}


@app.get("/api/library/{item_id}")
def get_item(item_id: int):
    item = db.get_item(item_id)
    if not item:
        raise HTTPException(404, "Not found")
    return item


@app.post("/api/library/{item_id}/retry")
def retry_item(item_id: int):
    item = db.get_item(item_id)
    if not item:
        raise HTTPException(404, "Not found")
    if item["status"] != "error":
        raise HTTPException(400, "Item is not in error state")
    db.update_item(item_id, status="queued", error_message=None, status_message=None)
    return {"ok": True}


@app.delete("/api/library/{item_id}")
def delete_item(item_id: int):
    db.delete_item(item_id)
    return {"ok": True}


@app.get("/api/docmost/spaces")
def get_docmost_spaces():
    return {"spaces": list_spaces()}


@app.get("/api/stats")
def get_stats():
    return db.get_stats()


@app.get("/api/status")
def get_status():
    jobs = [{"id": j.id, "next_run": str(j.next_run_time)}
            for j in scheduler.get_jobs()]
    return {
        "scheduler_running": scheduler.running,
        "last_email_check": last_email_check,
        "jobs": jobs
    }


@app.post("/api/check-email")
def trigger_email_check():
    run_email_check()
    return last_email_check


@app.post("/api/process-now")
def trigger_process():
    process_queue()
    return {"ok": True}


@app.get("/api/config")
def get_config():
    config = load_config()
    safe = json.loads(json.dumps(config))
    if safe.get("email", {}).get("app_password"):
        safe["email"]["app_password"] = "••••••••"
    if safe.get("anthropic_api_key"):
        safe["anthropic_api_key"] = "sk-ant-..." + safe["anthropic_api_key"][-4:]
    if safe.get("docmost", {}).get("db_password"):
        safe["docmost"]["db_password"] = "••••••••"
    if safe.get("gmail_oauth", {}).get("client_secret"):
        safe["gmail_oauth"]["client_secret"] = "••••••••"
    if safe.get("reddit", {}).get("client_secret"):
        safe["reddit"]["client_secret"] = "••••••••"
    if safe.get("smb", {}).get("password"):
        safe["smb"]["password"] = "••••••••"
    return safe


@app.post("/api/config")
def update_config(new_config: dict):
    current = load_config()
    if new_config.get("email", {}).get("app_password", "").startswith("•"):
        new_config.setdefault("email", {})["app_password"] = \
            current.get("email", {}).get("app_password", "")
    if new_config.get("anthropic_api_key", "").startswith("sk-ant-..."):
        new_config["anthropic_api_key"] = current.get("anthropic_api_key", "")
    # Preserve docmost password if incoming is masked or empty (fields were collapsed/hidden)
    incoming_pw = new_config.get("docmost", {}).get("db_password", "")
    if not incoming_pw or incoming_pw.startswith("•"):
        saved_pw = current.get("docmost", {}).get("db_password", "")
        if saved_pw:
            new_config.setdefault("docmost", {})["db_password"] = saved_pw
    incoming_rd_secret = new_config.get("reddit", {}).get("client_secret", "")
    if not incoming_rd_secret or incoming_rd_secret.startswith("•"):
        saved_secret = current.get("reddit", {}).get("client_secret", "")
        if saved_secret:
            new_config.setdefault("reddit", {})["client_secret"] = saved_secret
    incoming_smb_pw = new_config.get("smb", {}).get("password", "")
    if not incoming_smb_pw or incoming_smb_pw.startswith("•"):
        saved_smb_pw = current.get("smb", {}).get("password", "")
        if saved_smb_pw:
            new_config.setdefault("smb", {})["password"] = saved_smb_pw
    save_config(new_config)
    try:
        interval = int(new_config.get("email", {}).get("check_interval_minutes", 5))
        scheduler.reschedule_job("email_check", trigger="interval", minutes=interval)
    except Exception as e:
        logger.warning(f"Could not reschedule email check: {e}")
    return {"ok": True}


class ReclassifyRequest(BaseModel):
    subject_area: Optional[str] = None
    tags: Optional[list] = None


def _rewrite_markdown_metadata(filepath: str, subject_area: str = None, tags: list = None):
    """Update subject_area and tags in a markdown file's frontmatter and ## Tags section."""
    if not os.path.exists(filepath):
        return
    with open(filepath, encoding="utf-8") as f:
        content = f.read()

    # Rewrite frontmatter fields
    if subject_area is not None:
        content = re.sub(r"^subject_area:.*$", f"subject_area: {subject_area}", content, flags=re.MULTILINE)
    if tags is not None:
        tag_str = f"[{', '.join(tags)}]"
        content = re.sub(r"^tags:.*$", f"tags: {tag_str}", content, flags=re.MULTILINE)
        # Rewrite the ## Tags body section if present
        new_tag_line = " ".join(f"`{t}`" for t in tags) if tags else ""
        if re.search(r"^## Tags\s*$", content, flags=re.MULTILINE):
            content = re.sub(
                r"(^## Tags\s*\n\n?).*?(\n(?=##|\Z))",
                lambda m: m.group(1) + new_tag_line + m.group(2),
                content, flags=re.MULTILINE | re.DOTALL
            )

    with open(filepath, "w", encoding="utf-8") as f:
        f.write(content)


@app.patch("/api/library/{item_id}")
def reclassify_item(item_id: int, req: ReclassifyRequest):
    item = db.get_item(item_id)
    if not item:
        raise HTTPException(404, "Not found")

    updates = {}
    if req.subject_area is not None:
        updates["subject_area"] = req.subject_area
    if req.tags is not None:
        updates["tags"] = json.dumps(req.tags)

    if updates:
        db.update_item(item_id, **updates)

    if item.get("file_path") and os.path.exists(item["file_path"]):
        _rewrite_markdown_metadata(
            item["file_path"],
            subject_area=req.subject_area,
            tags=req.tags
        )

    # Re-sync to DocMost if already synced
    if item.get("docmost_page_id") and item.get("file_path") and os.path.exists(item["file_path"]):
        fp = item["file_path"]
        if req.subject_area is not None or req.tags is not None:
            _rewrite_markdown_metadata(fp, subject_area=req.subject_area, tags=req.tags)
        with open(fp, encoding="utf-8") as f:
            md_content = f.read()
        area = req.subject_area if req.subject_area is not None else item.get("subject_area", "misc")
        try:
            result = upsert_page(item.get("title") or "Unknown", md_content, area,
                                 space_id=item.get("docmost_space_id"))
            if result:
                db.update_item(item_id, docmost_page_id=result["page_id"], docmost_url=result["url"])
        except Exception as e:
            logger.warning(f"DocMost re-sync skipped after reclassify: {e}")

    return {"ok": True}


@app.post("/api/library/{item_id}/sync-docmost")
def sync_docmost(item_id: int):
    item = db.get_item(item_id)
    if not item:
        raise HTTPException(404, "Not found")
    if not item.get("file_path") or not os.path.exists(item["file_path"]):
        raise HTTPException(400, "No file on disk to sync")
    with open(item["file_path"], encoding="utf-8") as f:
        md_content = f.read()
    result = upsert_page(
        item.get("title") or "Unknown",
        md_content,
        item.get("subject_area") or "misc",
        space_id=item.get("docmost_space_id")
    )
    if not result:
        raise HTTPException(500, "DocMost sync failed — check logs and DocMost config in Settings")
    db.update_item(item_id, docmost_page_id=result["page_id"], docmost_url=result["url"])
    return {"ok": True, "page_id": result["page_id"], "url": result["url"]}


@app.post("/api/library/{item_id}/send-to-reelmeals")
def send_to_reelmeals(item_id: int):
    item = db.get_item(item_id)
    if not item:
        raise HTTPException(404, "Not found")
    if not item.get("file_path") or not os.path.exists(item["file_path"]):
        raise HTTPException(400, "No file on disk to pull a transcript from")
    if not REELMEALS_INGEST_API_KEY:
        raise HTTPException(500, "REELMEALS_INGEST_API_KEY is not configured")

    with open(item["file_path"], encoding="utf-8") as f:
        md_content = f.read()
    marker = "## Full Transcript\n\n"
    idx = md_content.find(marker)
    if idx == -1:
        raise HTTPException(400, "No transcript found in this item — it may have been processed from a description only")
    transcript = md_content[idx + len(marker):].strip()

    try:
        resp = requests.post(
            f"{REELMEALS_URL}/api/ingest/transcript",
            headers={"x-api-key": REELMEALS_INGEST_API_KEY},
            json={"url": item["url"], "title": item.get("title") or "", "transcript": transcript},
            # Local LLM extraction on a full transcript can run several
            # minutes (reasoning models "think" before answering) — give it
            # real headroom rather than timing out mid-extraction.
            timeout=300
        )
    except requests.RequestException as e:
        raise HTTPException(502, f"Could not reach ReelMeals: {e}")

    if not resp.ok:
        detail = resp.json().get("detail", resp.text) if resp.headers.get("content-type", "").startswith("application/json") else resp.text
        raise HTTPException(resp.status_code, f"ReelMeals: {detail}")

    result = resp.json()
    recipe_url = f"{REELMEALS_URL}/?recipe={result['slug']}"
    db.update_item(item_id, reelmeals_url=recipe_url)
    return {"ok": True, "slug": result["slug"], "cached": result.get("cached", False), "url": recipe_url}


@app.get("/api/library/{item_id}/file")
def get_item_file(item_id: int):
    item = db.get_item(item_id)
    if not item or not item.get("file_path"):
        raise HTTPException(404, "File not found")
    fp = item["file_path"]
    if not os.path.exists(fp):
        raise HTTPException(404, f"File does not exist on disk: {fp}")
    return FileResponse(fp, media_type="text/markdown",
                        filename=os.path.basename(fp))




@app.get("/api/oauth/gmail/status")
def gmail_oauth_status():
    from gmail_oauth import is_connected
    return {"connected": is_connected()}


@app.get("/api/oauth/gmail/auth-url")
def gmail_oauth_auth_url():
    config = load_config()
    oauth_cfg = config.get("gmail_oauth", {})
    if not oauth_cfg.get("client_id") or not oauth_cfg.get("client_secret"):
        raise HTTPException(400, "Gmail OAuth credentials not configured — save Client ID and Secret in Settings first")
    from gmail_oauth import get_auth_url
    return {"url": get_auth_url(oauth_cfg["client_id"], oauth_cfg["client_secret"])}


class OAuthExchangeRequest(BaseModel):
    code_or_url: str


@app.post("/api/oauth/gmail/exchange")
def gmail_oauth_exchange(req: OAuthExchangeRequest):
    config = load_config()
    oauth_cfg = config.get("gmail_oauth", {})
    from gmail_oauth import exchange_code
    exchange_code(oauth_cfg["client_id"], oauth_cfg["client_secret"], req.code_or_url)
    return {"ok": True}


@app.post("/api/oauth/gmail/disconnect")
def gmail_oauth_disconnect():
    from gmail_oauth import disconnect
    disconnect()
    return {"ok": True}


app.mount("/static", StaticFiles(directory="/app/static"), name="static")


@app.get("/")
def index():
    return FileResponse("/app/static/index.html")
