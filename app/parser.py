import json
import logging
import re
from typing import Dict, Optional

import anthropic
from config_manager import load_config
from transcriber import truncate_transcript

logger = logging.getLogger("rabbithole.parser")

_SYSTEM = "You are a research assistant that analyzes YouTube video transcripts and returns structured JSON."

_PROMPT = """\
Analyze this YouTube video and produce a structured knowledge-base entry.

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


def classify_and_parse(
    url: str,
    title: str,
    channel: str,
    transcript: str,
    subject_area_override: Optional[str] = None
) -> Dict:
    config = load_config()
    api_key = config.get("anthropic_api_key", "")
    if not api_key:
        raise ValueError("Anthropic API key not configured — set it in Settings.")

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

    client = anthropic.Anthropic(api_key=api_key)
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=2000,
        system=_SYSTEM,
        messages=[{"role": "user", "content": prompt}]
    )

    raw = response.content[0].text.strip()
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)

    result = json.loads(raw)

    # Subject-line email override takes precedence
    if subject_area_override and subject_area_override in subject_areas:
        result["subject_area"] = subject_area_override

    # Validate
    if result.get("subject_area") not in set(subject_areas) | {"misc"}:
        result["subject_area"] = subject_areas[0] if subject_areas else "misc"

    return result
