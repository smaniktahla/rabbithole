import os
import json
import logging
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Optional

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

            docmost_id = None
            try:
                with open(filepath, encoding="utf-8") as f:
                    md_content = f.read()
                db.update_item(item_id, status_message="Syncing to DocMost...")
                docmost_id = upsert_page(title or "Unknown", md_content,
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
                docmost_page_id=docmost_id,
                processed_at=datetime.now().isoformat(),
                summary=(parsed.get("summary") or "")[:600],
                tags=json.dumps(parsed.get("tags", []))
            )
            logger.info(f"Done [{item_id}]: '{title}' -> {filepath}")

        except Exception as e:
            logger.error(f"Failed [{item_id}] {url}: {e}", exc_info=True)
            db.update_item(item_id, status="error", status_message=None,
                           error_message=str(e)[:500])


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
    url = normalize_url(req.url.strip())
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


@app.post("/api/library/{item_id}/sync-docmost")
def sync_docmost(item_id: int):
    item = db.get_item(item_id)
    if not item:
        raise HTTPException(404, "Not found")
    if not item.get("file_path") or not os.path.exists(item["file_path"]):
        raise HTTPException(400, "No file on disk to sync")
    with open(item["file_path"], encoding="utf-8") as f:
        md_content = f.read()
    page_id = upsert_page(
        item.get("title") or "Unknown",
        md_content,
        item.get("subject_area") or "misc"
    )
    if not page_id:
        raise HTTPException(500, "DocMost sync failed — check logs and DocMost config in Settings")
    db.update_item(item_id, docmost_page_id=page_id)
    return {"ok": True, "page_id": page_id}


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
