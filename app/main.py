import os
import re
import json
import logging
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Optional

import requests
from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import database as db
from config_manager import load_config, save_config
from docmost import upsert_page
from email_poller import check_email
from parser import classify_and_parse
from storage import write_markdown
from transcriber import get_transcript

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


def process_queue():
    items = db.get_queued_items()
    if not items:
        return
    for item in items:
        item_id = item["id"]
        url = item["url"]
        try:
            db.update_item(item_id, status="processing",
                           status_message="Fetching transcript via yt-dlp...")
            logger.info(f"Processing [{item_id}]: {url}")

            transcript, title, channel = get_transcript(url)
            if not transcript:
                db.update_item(item_id, status="error",
                               status_message=None,
                               error_message="Could not extract transcript or captions")
                notify(f"🐰 RabbitHole failed: {url} — could not extract transcript or captions")
                continue

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

            docmost_result = None
            try:
                with open(filepath, encoding="utf-8") as f:
                    md_content = f.read()
                db.update_item(item_id, status_message="Syncing to DocMost...")
                docmost_result = upsert_page(title or "Unknown", md_content,
                                             parsed.get("subject_area", "misc"))
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
    url = normalize_url(raw)
    if not extract_video_id(url):
        raise HTTPException(400, "Could not find a YouTube video ID in that URL")
    item_id = db.add_item(url, source="manual",
                          subject_area_override=req.subject_area or None)
    if item_id == -1:
        # Already exists — if it errored, re-queue it
        with db.get_conn() as conn:
            row = conn.execute("SELECT id, status FROM items WHERE url = ?", (url,)).fetchone()
        if row and row["status"] == "error":
            db.update_item(row["id"], status="queued", error_message=None)
            return {"id": row["id"], "message": "Re-queued for processing"}
        raise HTTPException(409, "URL is already in the library")
    return {"id": item_id, "message": "Queued — will process within ~1 minute"}


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
            result = upsert_page(item.get("title") or "Unknown", md_content, area)
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
        item.get("subject_area") or "misc"
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
