import re
import logging
import urllib.request
import urllib.parse
from typing import Optional, Tuple

logger = logging.getLogger("rabbithole.transcriber")

YOUTUBE_ID_RE = re.compile(r'(?:youtube\.com/watch\?(?:[^&\s]*&)*v=|youtu\.be/)([\w-]+)')


def extract_video_id(url: str) -> Optional[str]:
    m = YOUTUBE_ID_RE.search(url)
    return m.group(1) if m else None


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

    # Strategy 1: youtube-transcript-api
    try:
        from youtube_transcript_api import YouTubeTranscriptApi
        segs = YouTubeTranscriptApi.get_transcript(video_id, languages=["en", "en-US", "en-GB"])
        text = " ".join(s["text"] for s in segs)
        text = re.sub(r'\s+', ' ', text).strip()
        title, channel = _get_metadata(url)
        logger.info(f"[{video_id}] Transcript via youtube-transcript-api ({len(text)} chars)")
        return text, title, channel
    except Exception as e:
        logger.warning(f"[{video_id}] youtube-transcript-api failed: {e}")

    # Strategy 2: yt-dlp with auto-generated captions
    try:
        import yt_dlp
        ydl_opts = {
            "skip_download": True,
            "quiet": True,
            "no_warnings": True,
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
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
        with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "skip_download": True}) as ydl:
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
