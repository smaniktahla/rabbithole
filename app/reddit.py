import json
import re
import time
import logging
from typing import Dict, Optional
from urllib.parse import urlparse

import requests

logger = logging.getLogger("rabbithole.reddit")

_USER_AGENT = "RabbitHole/1.0 (personal knowledge-base bot)"
_BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")

# Cached app-only OAuth token. Reddit closed self-serve script-app registration
# in Nov 2025 ("Responsible Builder Policy"), so this path is mostly dead —
# kept as a fast first attempt in case credentials are ever configured (e.g.
# Reddit reopens access, or an existing pre-Nov-2025 app is available).
_token_cache = {"access_token": None, "expires_at": 0.0}

# Reddit blocks *cold* (no session cookie) requests to .json endpoints —
# confirmed via testing: a plain HTTP request gets 403 even with a realistic
# User-Agent, but a headless browser that first loads the real HTML page
# (establishing normal session cookies) and then fetches .json from that same
# session gets 200. This is the reliable fallback when OAuth isn't available.

# Matches .../r/<subreddit>/comments/<post_id>/<slug?>/... or bare .../comments/<post_id>/...
_POST_PATH_RE = re.compile(r"(?:/r/([\w-]+))?/comments/([a-z0-9]+)(?:/([^/?#]+))?")

MIN_COMMENT_SCORE = 2
MAX_COMMENTS = 20
MAX_REPLY_DEPTH = 1


def is_reddit_url(url: str) -> bool:
    host = urlparse(url.strip()).netloc.lower().removeprefix("www.")
    return host in {"reddit.com", "old.reddit.com", "np.reddit.com", "redd.it", "new.reddit.com", "amp.reddit.com"}


def normalize_reddit_url(url: str) -> str:
    """Normalize any Reddit post URL variant to https://www.reddit.com/r/<sub>/comments/<id>/<slug>/"""
    url = url.strip()
    parsed = urlparse(url)
    host = parsed.netloc.lower().removeprefix("www.")

    if host == "redd.it":
        post_id = parsed.path.strip("/")
        if post_id:
            return f"https://www.reddit.com/comments/{post_id}/"
        return url

    m = _POST_PATH_RE.search(parsed.path)
    if not m:
        return url
    subreddit, post_id, slug = m.group(1), m.group(2), m.group(3) or ""
    if subreddit:
        return f"https://www.reddit.com/r/{subreddit}/comments/{post_id}/{slug}"
    return f"https://www.reddit.com/comments/{post_id}/{slug}"


def extract_post_id(url: str) -> Optional[str]:
    parsed = urlparse(url.strip())
    host = parsed.netloc.lower().removeprefix("www.")
    if host == "redd.it":
        pid = parsed.path.strip("/")
        return pid or None
    m = _POST_PATH_RE.search(parsed.path)
    return m.group(2) if m else None


def _get_oauth_token(config: dict) -> Optional[str]:
    rd = config.get("reddit", {})
    client_id = rd.get("client_id", "")
    client_secret = rd.get("client_secret", "")
    if not client_id or not client_secret:
        return None

    now = time.time()
    if _token_cache["access_token"] and now < _token_cache["expires_at"]:
        return _token_cache["access_token"]

    try:
        resp = requests.post(
            "https://www.reddit.com/api/v1/access_token",
            auth=(client_id, client_secret),
            data={"grant_type": "client_credentials"},
            headers={"User-Agent": _USER_AGENT},
            timeout=15
        )
        resp.raise_for_status()
        data = resp.json()
        _token_cache["access_token"] = data["access_token"]
        _token_cache["expires_at"] = now + data.get("expires_in", 3600) - 60
        return _token_cache["access_token"]
    except Exception as e:
        logger.error(f"Reddit OAuth token fetch failed: {e}")
        return None


def _fetch_via_browser(warm_url: str, json_url: str) -> Optional[dict]:
    """Load the post's real HTML page in a headless browser first (to pick up
    normal session cookies), then fetch the .json endpoint from that same
    browser session via an in-page fetch()."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        logger.error("playwright not installed — cannot use browser fallback")
        return None

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            try:
                page = browser.new_page(user_agent=_BROWSER_UA)
                page.goto(warm_url, wait_until="domcontentloaded", timeout=25000)
                page.wait_for_timeout(1000)
                result = page.evaluate(
                    """async (url) => {
                        const r = await fetch(url, {headers: {"Accept": "application/json"}});
                        return {status: r.status, body: await r.text()};
                    }""",
                    json_url
                )
            finally:
                browser.close()
    except Exception as e:
        logger.error(f"Browser fetch failed for {json_url}: {e}")
        return None

    if result["status"] != 200:
        logger.error(f"Browser fetch got HTTP {result['status']} for {json_url}")
        return None

    try:
        return json.loads(result["body"])
    except Exception as e:
        logger.error(f"Browser fetch returned non-JSON body: {e}")
        return None


def _flatten_comments(children, depth=0, out=None):
    if out is None:
        out = []
    for child in children or []:
        if child.get("kind") != "t1":
            continue
        data = child.get("data", {})
        body = (data.get("body") or "").strip()
        author = data.get("author") or "[deleted]"
        score = data.get("score") or 0
        if not body or body in ("[deleted]", "[removed]") or author == "AutoModerator":
            continue
        out.append({
            "author": author,
            "score": score,
            "body": body,
            "depth": depth,
        })
        replies = data.get("replies")
        if depth < MAX_REPLY_DEPTH and isinstance(replies, dict):
            _flatten_comments(replies.get("data", {}).get("children"), depth + 1, out)
    return out


def get_reddit_content(url: str) -> Optional[Dict]:
    """
    Fetch a Reddit post + its top comments via Reddit's public JSON API.
    Returns a dict with post metadata, selftext/link, and a ranked comment list,
    or None if the post could not be fetched.
    """
    url = normalize_reddit_url(url)
    post_id = extract_post_id(url)
    if not post_id:
        logger.error(f"Could not extract post ID from: {url}")
        return None

    from config_manager import load_config
    config = load_config()
    token = _get_oauth_token(config)

    path = urlparse(url).path.rstrip("/")
    payload = None

    if token:
        try:
            resp = requests.get(
                f"https://oauth.reddit.com{path}.json?limit=200&sort=top&raw_json=1",
                headers={"User-Agent": _USER_AGENT, "Authorization": f"Bearer {token}"},
                timeout=20
            )
            resp.raise_for_status()
            payload = resp.json()
        except Exception as e:
            logger.warning(f"[{post_id}] OAuth fetch failed, falling back: {e}")

    plain_json_url = f"https://www.reddit.com{path}.json?limit=200&sort=top&raw_json=1"

    if payload is None:
        try:
            resp = requests.get(plain_json_url, headers={"User-Agent": _USER_AGENT}, timeout=20)
            resp.raise_for_status()
            payload = resp.json()
        except Exception as e:
            logger.warning(f"[{post_id}] Anonymous fetch failed, trying browser: {e}")

    if payload is None:
        payload = _fetch_via_browser(url, plain_json_url)

    if payload is None:
        logger.error(f"[{post_id}] All Reddit fetch strategies failed")
        return None

    if not isinstance(payload, list) or len(payload) < 1:
        logger.error(f"[{post_id}] Unexpected Reddit response shape")
        return None

    post_children = payload[0].get("data", {}).get("children", [])
    if not post_children:
        logger.error(f"[{post_id}] No post data returned (deleted/private/removed?)")
        return None

    post = post_children[0].get("data", {})
    if post.get("removed_by_category") or post.get("selftext") == "[removed]":
        logger.warning(f"[{post_id}] Post appears to be removed")

    comments = []
    if len(payload) > 1:
        comment_children = payload[1].get("data", {}).get("children", [])
        comments = _flatten_comments(comment_children)

    comments = [c for c in comments if c["score"] >= MIN_COMMENT_SCORE]
    comments.sort(key=lambda c: c["score"], reverse=True)
    comments = comments[:MAX_COMMENTS]

    is_self = post.get("is_self", True)
    link_url = None if is_self else post.get("url")

    return {
        "post_id": post_id,
        "title": post.get("title") or "Untitled",
        "subreddit": f"r/{post.get('subreddit', 'unknown')}",
        "author": post.get("author") or "[deleted]",
        "score": post.get("score", 0),
        "num_comments": post.get("num_comments", 0),
        "selftext": (post.get("selftext") or "").strip(),
        "link_url": link_url,
        "permalink": f"https://www.reddit.com{post.get('permalink', '')}" if post.get("permalink") else url,
        "comments": comments,
    }


def format_comments_block(comments) -> str:
    if not comments:
        return "(no comments above the score threshold)"
    lines = []
    for c in comments:
        indent = "  " * c["depth"]
        lines.append(f"{indent}[{c['score']} pts] u/{c['author']}: {c['body']}")
    return "\n\n".join(lines)
