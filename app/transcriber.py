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


def _authenticated_session():
    """A requests.Session carrying the exported YouTube cookies, if present."""
    import http.cookiejar
    import requests

    session = requests.Session()
    if os.path.isfile(YOUTUBE_COOKIES_PATH):
        jar = http.cookiejar.MozillaCookieJar(YOUTUBE_COOKIES_PATH)
        jar.load(ignore_discard=True, ignore_expires=True)
        session.cookies.update(jar)
    return session


def _scratch_cookiefile() -> Optional[str]:
    """
    A disposable copy of the master cookie file for yt-dlp to use.

    yt-dlp treats `cookiefile` as read-write and rewrites it after every run —
    and its writer silently drops HttpOnly cookies, which is exactly the auth
    cookies (SID, APISID, SAPISID, LOGIN_INFO, __Secure-1PSID, ...) that make
    the session logged-in. Handing it the master file corrupts it within one
    call. Copy to a throwaway temp path each time instead.
    """
    if not os.path.isfile(YOUTUBE_COOKIES_PATH):
        return None
    import shutil
    import tempfile
    fd, path = tempfile.mkstemp(suffix=".txt", prefix="ytcookies_")
    os.close(fd)
    shutil.copyfile(YOUTUBE_COOKIES_PATH, path)
    return path


def _ydl_opts(cookiefile: Optional[str] = None, **extra) -> dict:
    # remote_components pulls yt-dlp's JS challenge-solver script (run via the
    # Deno runtime baked into the image) — without it YouTube's "n" signature
    # challenge can't be solved and only image formats resolve.
    opts = {
        "skip_download": True,
        "quiet": True,
        "no_warnings": True,
        "remote_components": {"ejs:github"},
        **extra,
    }
    if cookiefile:
        opts["cookiefile"] = cookiefile
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
        fetched = YouTubeTranscriptApi(http_client=_authenticated_session()).fetch(
            video_id, languages=["en", "en-US", "en-GB"]
        )
        text = " ".join(s.text for s in fetched)
        text = re.sub(r'\s+', ' ', text).strip()
        title, channel = _get_metadata(url)
        logger.info(f"[{video_id}] Transcript via youtube-transcript-api ({len(text)} chars)")
        return text, title, channel
    except Exception as e:
        logger.warning(f"[{video_id}] youtube-transcript-api failed: {e}")

    # Strategy 2: yt-dlp with existing (creator or auto-generated) captions
    title, channel = None, None
    cookiefile = _scratch_cookiefile()
    try:
        import yt_dlp
        with yt_dlp.YoutubeDL(_ydl_opts(cookiefile=cookiefile)) as ydl:
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

    except Exception as e:
        logger.error(f"[{video_id}] yt-dlp caption lookup failed: {e}")
    finally:
        if cookiefile:
            os.unlink(cookiefile)

    # Strategy 3: no captions exist at all — transcribe the audio with Whisper
    text = _transcribe_with_whisper(url, video_id)
    if text:
        return text, title, channel

    # Last resort: video description
    if title is None:
        title, channel = _get_metadata(url)
    return None, title, channel


def _get_metadata(url: str) -> Tuple[Optional[str], Optional[str]]:
    cookiefile = _scratch_cookiefile()
    try:
        import yt_dlp
        with yt_dlp.YoutubeDL(_ydl_opts(cookiefile=cookiefile)) as ydl:
            info = ydl.extract_info(url, download=False)
            return info.get("title"), info.get("uploader") or info.get("channel")
    except Exception:
        return None, None
    finally:
        if cookiefile:
            os.unlink(cookiefile)


WHISPER_MODEL = os.environ.get("WHISPER_MODEL", "base")
_whisper_model = None


def _get_whisper_model():
    global _whisper_model
    if _whisper_model is None:
        from faster_whisper import WhisperModel
        logger.info(f"Loading Whisper model '{WHISPER_MODEL}' (first use, may take a moment)")
        _whisper_model = WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8")
    return _whisper_model


def _transcribe_with_whisper(url: str, video_id: str) -> Optional[str]:
    """Downloads audio only and runs local speech-to-text — last-resort path
    for videos with no creator or auto-generated captions at all."""
    import shutil
    import tempfile

    cookiefile = _scratch_cookiefile()
    tmp_dir = tempfile.mkdtemp(prefix="ytaudio_")
    try:
        import yt_dlp
        audio_path = os.path.join(tmp_dir, f"{video_id}.mp3")
        opts = _ydl_opts(
            cookiefile=cookiefile,
            format="bestaudio/best",
            outtmpl=os.path.join(tmp_dir, "%(id)s.%(ext)s"),
            postprocessors=[{
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "64",
            }],
        )
        opts["skip_download"] = False
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.extract_info(url, download=True)

        if not os.path.isfile(audio_path):
            logger.warning(f"[{video_id}] Whisper: no audio file produced")
            return None

        model = _get_whisper_model()
        segments, _info = model.transcribe(audio_path, language="en")
        text = " ".join(seg.text.strip() for seg in segments)
        text = re.sub(r'\s+', ' ', text).strip()
        if text:
            logger.info(f"[{video_id}] Transcript via Whisper ({len(text)} chars)")
            return text
        return None

    except Exception as e:
        logger.error(f"[{video_id}] Whisper transcription failed: {e}")
        return None
    finally:
        if cookiefile:
            os.unlink(cookiefile)
        shutil.rmtree(tmp_dir, ignore_errors=True)


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
