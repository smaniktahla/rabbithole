import imaplib
import email
import re
import logging
from typing import List, Optional

from config_manager import load_config
import database as db

logger = logging.getLogger("rabbithole.email")

YOUTUBE_RE = re.compile(
    r'(?:https?://)?(?:www\.)?(?:youtube\.com/watch\?(?:[^&\s]*&)*v=|youtu\.be/)([\w-]+)'
)
OVERRIDE_RE = re.compile(r'^\[([^\]]+)\]')


def extract_youtube_urls(text: str) -> List[str]:
    """Extract and normalize all YouTube URLs from text."""
    urls = []
    for m in YOUTUBE_RE.finditer(text):
        # Normalize to clean watch URL — drops si, feature, list, etc.
        urls.append(f"https://www.youtube.com/watch?v={m.group(1)}")
    return list(dict.fromkeys(urls))


def parse_subject_override(subject: str) -> Optional[str]:
    """Return [tag] prefix as subject area override, e.g. '[homelab] ...' -> 'homelab'."""
    m = OVERRIDE_RE.match(subject.strip())
    return m.group(1).strip().lower() if m else None


def get_email_body(msg) -> str:
    body = ""
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                try:
                    body += part.get_payload(decode=True).decode("utf-8", errors="replace")
                except Exception:
                    pass
    else:
        try:
            body = msg.get_payload(decode=True).decode("utf-8", errors="replace")
        except Exception:
            pass
    return body


def check_email() -> int:
    """Check inbox for YouTube links. Returns count of newly queued items."""
    config = load_config()

    # Prefer Gmail OAuth when a token is stored
    try:
        from gmail_oauth import is_connected, check_email_oauth
        if is_connected():
            return check_email_oauth()
    except Exception as e:
        logger.error(f"Gmail OAuth check failed: {e}", exc_info=True)

    # Fall back to IMAP + app password
    ecfg = config.get("email", {})

    if not ecfg.get("email_address") or not ecfg.get("app_password"):
        logger.debug("Email not configured, skipping check")
        return 0

    queued = 0
    try:
        mail = imaplib.IMAP4_SSL(ecfg["imap_server"], int(ecfg.get("imap_port", 993)))
        mail.login(ecfg["email_address"], ecfg["app_password"])
        mail.select("INBOX")

        _, msg_ids = mail.search(None, "UNSEEN")
        if not msg_ids or not msg_ids[0]:
            mail.logout()
            return 0

        for msg_id in msg_ids[0].split():
            try:
                _, msg_data = mail.fetch(msg_id, "(RFC822)")
                msg = email.message_from_bytes(msg_data[0][1])

                subject = msg.get("Subject", "")
                body = get_email_body(msg)
                full_text = f"{subject}\n{body}"

                urls = extract_youtube_urls(full_text)
                override = parse_subject_override(subject)

                for url in urls:
                    item_id = db.add_item(url, source="email", subject_area_override=override)
                    if item_id > 0:
                        logger.info(f"Queued from email: {url} (area override: {override})")
                        queued += 1

                mail.store(msg_id, "+FLAGS", "\\Seen")

            except Exception as e:
                logger.error(f"Error processing email {msg_id}: {e}")

        mail.logout()

    except Exception as e:
        logger.error(f"Email check failed: {e}")

    return queued
