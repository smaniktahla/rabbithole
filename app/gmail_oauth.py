import os
import json
import logging
import base64
from typing import Optional

logger = logging.getLogger(rabbithole.gmail_oauth)

SCOPES = [https://www.googleapis.com/auth/gmail.modify]
TOKEN_PATH = /app/data/gmail_token.json


def _load_creds():
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request

    if not os.path.exists(TOKEN_PATH):
        return None
    with open(TOKEN_PATH) as f:
        data = json.load(f)
    creds = Credentials.from_authorized_user_info(data, SCOPES)
    if creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            _save_creds(creds)
        except Exception as e:
            logger.error(f"Token refresh failed: {e}")
            return None
    return creds if creds.valid else None


def _save_creds(creds) -> None:
    os.makedirs(os.path.dirname(TOKEN_PATH), exist_ok=True)
    with open(TOKEN_PATH, "w") as f:
        f.write(creds.to_json())


def is_connected() -> bool:
    return _load_creds() is not None


def _make_flow(client_id: str, client_secret: str, redirect_uri: str):
    from google_auth_oauthlib.flow import Flow
    return Flow.from_client_config(
        {"web": {
            "client_id": client_id,
            "client_secret": client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": [redirect_uri],
        }},
        scopes=SCOPES,
        redirect_uri=redirect_uri,
    )


def get_auth_url(client_id: str, client_secret: str, redirect_uri: str) -> str:
    flow = _make_flow(client_id, client_secret, redirect_uri)
    url, _ = flow.authorization_url(access_type="offline", prompt="consent")
    return url


def exchange_code(client_id: str, client_secret: str, redirect_uri: str, code: str) -> None:
    flow = _make_flow(client_id, client_secret, redirect_uri)
    flow.fetch_token(code=code)
    _save_creds(flow.credentials)


def disconnect() -> None:
    if os.path.exists(TOKEN_PATH):
        os.remove(TOKEN_PATH)


def check_email_oauth() -> int:
    """Poll Gmail for unread messages with YouTube links. Returns count queued."""
    import database as db
    from email_poller import extract_youtube_urls, parse_subject_override
    from googleapiclient.discovery import build

    creds = _load_creds()
    if not creds:
        logger.warning("Gmail OAuth: not connected")
        return 0

    service = build("gmail", "v1", credentials=creds)
    results = service.users().messages().list(
        userId="me", q="is:unread", maxResults=20
    ).execute()

    messages = results.get("messages", [])
    if not messages:
        return 0

    queued = 0
    for stub in messages:
        try:
            msg = service.users().messages().get(
                userId="me", id=stub["id"], format="full"
            ).execute()

            headers = {h["name"]: h["value"] for h in msg["payload"].get("headers", [])}
            subject = headers.get("Subject", "")
            body = _extract_body(msg["payload"])
            full_text = f"{subject}\n{body}"

            urls = extract_youtube_urls(full_text)
            override = parse_subject_override(subject)

            for url in urls:
                item_id = db.add_item(url, source="email", subject_area_override=override)
                if item_id > 0:
                    logger.info(f"Queued from Gmail: {url} (area: {override})")
                    queued += 1

            service.users().messages().modify(
                userId="me", id=stub["id"],
                body={"removeLabelIds": ["UNREAD"]}
            ).execute()

        except Exception as e:
            logger.error(f"Error processing Gmail message {stub['id']}: {e}")

    return queued


def _extract_body(payload) -> str:
    """Recursively extract text/plain body from Gmail message payload."""
    if payload.get("mimeType") == "text/plain":
        data = payload.get("body", {}).get("data", "")
        if data:
            return base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="replace")
    for part in payload.get("parts", []):
        result = _extract_body(part)
        if result:
            return result
    return ""
