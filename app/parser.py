import json
import logging
import re
from typing import Dict, Optional

from config_manager import load_config
from transcriber import truncate_transcript

logger = logging.getLogger("rabbithole.parser")

_SYSTEM = "You are a research assistant that analyzes video transcripts and returns structured JSON."

_PROMPT = """\
Analyze this video and produce a structured knowledge-base entry.

**Title**: {title}
**Channel**: {channel}
**URL**: {url}
**Available subject areas**: {subject_areas}

**Transcript** (may be truncated):
{transcript}

Return ONLY valid JSON — no markdown fences, no preamble:
{{
  "subject_area": "<one of the listed subject areas exactly, or 'misc' if none fit>",
  "summary": "<3-4 paragraph comprehensive summary of main ideas and conclusions>",
  "key_points": ["<specific actionable or informative insight>", ...],
  "quotes": ["<verbatim or near-verbatim notable quote>", ...],
  "tags": ["<lowercase-hyphenated-tag>", ...],
  "related_concepts": ["<rabbit hole worth exploring>", ...]
}}

Rules:
- subject_area: must exactly match one of the listed names, or be "misc"
- key_points: 5-10 items, each a full sentence
- quotes: 0-3 only if genuinely interesting; empty array is fine
- tags: 4-8 lowercase tags
- related_concepts: 3-5 ideas this video opens up
"""

_REDDIT_SYSTEM = "You are a research assistant that analyzes Reddit posts and their comment discussions and returns structured JSON."

_REDDIT_PROMPT = """\
Analyze this Reddit post and its top comments, then produce a structured knowledge-base entry.

**Title**: {title}
**Subreddit**: {subreddit}
**Author**: u/{author}
**Score**: {score} points | **Comments**: {num_comments}
**URL**: {url}
**Available subject areas**: {subject_areas}

**Original post** (may be truncated):
{post_body}

**Top comments** (sorted by score, may be truncated):
{comments}

Return ONLY valid JSON — no markdown fences, no preamble:
{{
  "subject_area": "<one of the listed subject areas exactly, or 'misc' if none fit>",
  "summary": "<3-4 paragraph comprehensive summary covering both the OP's post AND the most valuable insight, disagreement, or consensus that emerged in the comments>",
  "key_points": ["<specific actionable or informative insight, from the post or a standout comment>", ...],
  "quotes": ["<verbatim or near-verbatim notable line from the OP or a top comment, attributed inline like 'OP: ...' or 'u/username: ...'>", ...],
  "tags": ["<lowercase-hyphenated-tag>", ...],
  "related_concepts": ["<rabbit hole worth exploring>", ...]
}}

Rules:
- subject_area: must exactly match one of the listed names, or be "misc"
- key_points: 5-10 items, each a full sentence; draw from both post and comments, not just the OP
- quotes: 0-3 only if genuinely interesting; empty array is fine
- tags: 4-8 lowercase tags
- related_concepts: 3-5 ideas this thread opens up
"""


def _call_local(prompt: str, config: dict, system: str = _SYSTEM) -> str:
    from openai import OpenAI
    url = config.get("local_llm_url", "http://10.10.10.226:8080")
    model = config.get("local_llm_model", "gemma4:12b")
    client = OpenAI(base_url=f"{url.rstrip('/')}/v1", api_key="none")
    response = client.chat.completions.create(
        model=model,
        max_tokens=4096,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ]
    )
    return response.choices[0].message.content.strip()


def _call_anthropic(prompt: str, config: dict, system: str = _SYSTEM) -> str:
    import anthropic
    api_key = config.get("anthropic_api_key", "")
    if not api_key:
        raise ValueError("Anthropic API key not configured — set it in Settings.")
    client = anthropic.Anthropic(api_key=api_key)
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=4096,
        system=system,
        messages=[{"role": "user", "content": prompt}]
    )
    return response.content[0].text.strip()


def _run_llm(prompt: str, config: dict, system: str) -> str:
    provider = config.get("llm_provider", "local")
    if provider == "anthropic":
        return _call_anthropic(prompt, config, system)
    return _call_local(prompt, config, system)


def _clean_and_load_json(raw: str) -> Dict:
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)
    # Strip control characters that Gemma4 sometimes emits inside strings
    raw = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", raw)

    # Fix invalid JSON escapes (e.g., a single backslash not followed by a valid escape char)
    raw = re.sub(r"\\\\(?![\"\\\\/bfnrtu])", r"\\\\\\\\", raw)

    return json.loads(raw)


def _resolve_subject_area(result: Dict, subject_areas: list, subject_area_override: Optional[str]) -> Dict:
    if subject_area_override and subject_area_override in subject_areas:
        result["subject_area"] = subject_area_override

    if result.get("subject_area") not in set(subject_areas) | {"misc"}:
        result["subject_area"] = subject_areas[0] if subject_areas else "misc"

    return result


def classify_and_parse(
    url: str,
    title: str,
    channel: str,
    transcript: str,
    subject_area_override: Optional[str] = None
) -> Dict:
    config = load_config()

    subject_areas = [sa["name"] for sa in config.get("subject_areas", [])]
    if not subject_areas:
        subject_areas = ["misc"]

    prompt = _PROMPT.format(
        title=title or "Unknown",
        channel=channel or "Unknown",
        url=url,
        subject_areas=", ".join(subject_areas),
        transcript=truncate_transcript(transcript)
    )

    raw = _run_llm(prompt, config, _SYSTEM)
    result = _clean_and_load_json(raw)
    return _resolve_subject_area(result, subject_areas, subject_area_override)


def classify_and_parse_reddit(
    url: str,
    title: str,
    subreddit: str,
    author: str,
    score: int,
    num_comments: int,
    post_body: str,
    comments_block: str,
    subject_area_override: Optional[str] = None
) -> Dict:
    config = load_config()

    subject_areas = [sa["name"] for sa in config.get("subject_areas", [])]
    if not subject_areas:
        subject_areas = ["misc"]

    prompt = _REDDIT_PROMPT.format(
        title=title or "Untitled",
        subreddit=subreddit or "unknown",
        author=author or "unknown",
        score=score or 0,
        num_comments=num_comments or 0,
        url=url,
        subject_areas=", ".join(subject_areas),
        post_body=truncate_transcript(post_body or "(no post text — link post)"),
        comments=truncate_transcript(comments_block or "(no comments)")
    )

    raw = _run_llm(prompt, config, _REDDIT_SYSTEM)
    result = _clean_and_load_json(raw)
    return _resolve_subject_area(result, subject_areas, subject_area_override)