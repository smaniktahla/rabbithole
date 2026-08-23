import os
import re
import logging
import urllib.request
import urllib.parse
from typing import Optional, Tuple
from urllib.parse import parse_qs, urlparse

logger = logging.getLogger("rabbithole.transcriber")

YOUTUBE_ID_RE = re.compile(r'^[A-Za-z0-9_-]{11}$')

# Exported via a browser extension (e.g. "Get cookies.txt LOCALLY") and dropped
# into the persistent data volume. Optional — only used if present, to get
# yt-dlp past YouTube's "Sign in to confirm you're not a bot" wall.
YOUTUBE_COOKIES_PATH = os.environ.get("YOUTUBE_COOKIES_PATH", "/app/data/youtube_cookies.txt")


def _ydl_opts(**extra) -> dict:
    opts = {"skip_download": True, "quiet": True, "no_warnings": True, **extra}
    if os.path.isfile(YOUTUBE_COOKIES_PATH):
        opts["cookiefile"] = YOUTUBE_COOKIES_PATH
    return opts


def extract_video_id(url: str) -> Optional[str]:
    parsed = urlparse(url.strip())
    host = parsed.netloc.lower().removeprefix("www.")
    path_parts = [part for part in parsed.path.split("/") if part]

    candidate = None
    if host == "youtu.be" and path_parts:
        candidate = path_parts[0]
    elif host in {"youtube.com", "m.youtube.com", "music.youtube.com"}:
        if parsed.path == "/watch":
            candidate = parse_qs(parsed.query).get("v", [None])[0]
        elif len(path_parts) >= 2 and path_parts[0] in {"shorts", "embed", "live"}:
            candidate = path_parts[1]

    if candidate and YOUTUBE_ID_RE.fullmatch(candidate):
        return candidate
    return None


def normalize_url(url: str) -> str:
    """
    Normalize any YouTube URL variant to https://www.youtube.com/watch?v=VIDEO_ID
    Strips si, feature, pp, list, index, t and other tracking/playlist params.
    """
    video_id = extract_video_id(url)
    if video_id:
        return f"https://www.youtube.com/watch?v={video_id}"
    return url


def get_transcript(url: str) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """
    Returns (transcript_text, title, channel).
    Normalizes URL first, then tries youtube-transcript-api, then yt-dlp.
    """
    url = normalize_url(url)
    video_id = extract_video_id(url)
    if not video_id:
        logger.error(f"Could not extract video ID from: {url}")
        return None, None, None

    # Strategy 1: youtube-transcript-api (v1.x instance API — no more classmethods)
    try:
        from youtube_transcript_api import YouTubeTranscriptApi
        fetched = YouTubeTranscriptApi().fetch(video_id, languages=["en", "en-US", "en-GB"])
        text = " ".join(s.text for s in fetched)
        text = re.sub(r'\s+', ' ', text).strip()
        title, channel = _get_metadata(url)
        logger.info(f"[{video_id}] Transcript via youtube-transcript-api ({len(text)} chars)")
        return text, title, channel
    except Exception as e:
        logger.warning(f"[{video_id}] youtube-transcript-api failed: {e}")

    # Strategy 2: yt-dlp with auto-generated captions
    try:
        import yt_dlp
        with yt_dlp.YoutubeDL(_ydl_opts()) as ydl:
            info = ydl.extract_info(url, download=False)
            title = info.get("title")
            channel = info.get("uploader") or info.get("channel")

            all_subs = {}
            all_subs.update(info.get("subtitles") or {})
            all_subs.update(info.get("automatic_captions") or {})

            for lang in ["en", "en-US", "en-orig"]:
                if lang in all_subs:
                    for fmt in all_subs[lang]:
                        if fmt.get("ext") in ("vtt", "json3"):
                            try:
                                with urllib.request.urlopen(fmt["url"], timeout=15) as resp:
                                    raw = resp.read().decode("utf-8")
                                text = _parse_vtt(raw) if fmt["ext"] == "vtt" else _parse_json3(raw)
                                if text:
                                    logger.info(f"[{video_id}] Transcript via yt-dlp ({lang}, {fmt['ext']})")
                                    return text, title, channel
                            except Exception as sub_e:
                                logger.warning(f"[{video_id}] Sub download failed: {sub_e}")

            # Last resort: description
            desc = info.get("description", "")
            if desc:
                logger.warning(f"[{video_id}] No transcript found, using description")
                return desc[:8000], title, channel

            return None, title, channel

    except Exception as e:
        logger.error(f"[{video_id}] yt-dlp failed: {e}")
        return None, None, None


def _get_metadata(url: str) -> Tuple[Optional[str], Optional[str]]:
    try:
        import yt_dlp
        with yt_dlp.YoutubeDL(_ydl_opts()) as ydl:
            info = ydl.extract_info(url, download=False)
            return info.get("title"), info.get("uploader") or info.get("channel")
    except Exception:
        return None, None


def _parse_vtt(vtt: str) -> str:
    lines = []
    for line in vtt.split("\n"):
        line = line.strip()
        if not line or "-->" in line or line.startswith("WEBVTT") or line.isdigit():
            continue
        clean = re.sub(r'<[^>]+>', '', line)
        if clean:
            lines.append(clean)
    return " ".join(lines)


def _parse_json3(raw: str) -> str:
    import json
    try:
        data = json.loads(raw)
        words = []
        for event in data.get("events", []):
            for seg in event.get("segs", []):
                words.append(seg.get("utf8", ""))
        return re.sub(r'\s+', ' ', "".join(words)).strip()
    except Exception:
        return ""


def truncate_transcript(text: str, max_chars: int = 14000) -> str:
    if len(text) <= max_chars:
        return text
    half = max_chars // 2
    return text[:half] + "\n\n[...transcript truncated...]\n\n" + text[-(max_chars - half):]
