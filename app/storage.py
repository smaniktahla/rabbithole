import os
import re
import logging
from datetime import datetime
from typing import Dict

from config_manager import load_config, get_subject_area_path

logger = logging.getLogger("rabbithole.storage")


def slugify(text: str) -> str:
    text = (text or "untitled").lower()
    text = re.sub(r'[^\w\s-]', '', text)
    text = re.sub(r'[\s_]+', '-', text).strip('-')
    return text[:60]


def write_markdown(url: str, title: str, channel: str, parsed: Dict,
                   transcript: str = None) -> str:
    """Write a structured markdown file. Returns the absolute path."""
    config = load_config()
    subject_area = parsed.get("subject_area", "misc")
    storage_path = get_subject_area_path(config, subject_area)
    os.makedirs(storage_path, exist_ok=True)

    date_str = datetime.now().strftime("%Y%m%d")
    slug = slugify(title)
    filepath = os.path.join(storage_path, f"{date_str}_{slug}.md")

    # Avoid collisions
    counter = 1
    while os.path.exists(filepath):
        filepath = os.path.join(storage_path, f"{date_str}_{slug}_{counter}.md")
        counter += 1

    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    tags = parsed.get("tags", [])
    title_safe = (title or "Unknown").replace('"', "'")

    lines = [
        "---",
        f'title: "{title_safe}"',
        f"url: {url}",
        f"channel: {channel or 'Unknown'}",
        f"processed: {now}",
        f"subject_area: {subject_area}",
        f"tags: [{', '.join(tags)}]",
        "---",
        "",
        f"# {title or 'Unknown Title'}",
        "",
        f"**Channel**: {channel or 'Unknown'} | **Processed**: {now}",
        f"**Source**: [{url}]({url})",
        "",
        "## Summary",
        "",
        parsed.get("summary", ""),
        "",
        "## Key Points",
        "",
    ]

    for point in parsed.get("key_points", []):
        lines.append(f"- {point}")

    quotes = parsed.get("quotes", [])
    if quotes:
        lines += ["", "## Notable Quotes", ""]
        for q in quotes:
            lines.append(f"> {q}")
            lines.append("")

    related = parsed.get("related_concepts", [])
    if related:
        lines += ["", "## Rabbit Holes", ""]
        for r in related:
            lines.append(f"- {r}")

    if tags:
        lines += ["", "## Tags", ""]
        lines.append(" ".join(f"`{t}`" for t in tags))

    if transcript:
        lines += ["", "## Full Transcript", ""]
        lines.append(transcript)

    lines.append("")

    with open(filepath, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    logger.info(f"Wrote: {filepath}")
    return filepath


def write_reddit_markdown(url: str, title: str, subreddit: str, author: str,
                          score: int, num_comments: int, parsed: Dict,
                          post_body: str = None, link_url: str = None,
                          comments_block: str = None) -> str:
    """Write a structured markdown file for a Reddit post. Returns the absolute path."""
    config = load_config()
    subject_area = parsed.get("subject_area", "misc")
    storage_path = get_subject_area_path(config, subject_area)
    os.makedirs(storage_path, exist_ok=True)

    date_str = datetime.now().strftime("%Y%m%d")
    slug = slugify(title)
    filepath = os.path.join(storage_path, f"{date_str}_{slug}.md")

    counter = 1
    while os.path.exists(filepath):
        filepath = os.path.join(storage_path, f"{date_str}_{slug}_{counter}.md")
        counter += 1

    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    tags = parsed.get("tags", [])
    title_safe = (title or "Untitled").replace('"', "'")

    lines = [
        "---",
        f'title: "{title_safe}"',
        f"url: {url}",
        f"subreddit: {subreddit or 'unknown'}",
        f"author: u/{author or 'unknown'}",
        f"score: {score or 0}",
        f"processed: {now}",
        f"subject_area: {subject_area}",
        f"tags: [{', '.join(tags)}]",
        "---",
        "",
        f"# {title or 'Untitled'}",
        "",
        f"**{subreddit or 'unknown'}** | **u/{author or 'unknown'}** | "
        f"**{score or 0} pts** | **{num_comments or 0} comments** | **Processed**: {now}",
        f"**Source**: [{url}]({url})",
        "",
        "## Summary",
        "",
        parsed.get("summary", ""),
        "",
        "## Key Points",
        "",
    ]

    for point in parsed.get("key_points", []):
        lines.append(f"- {point}")

    quotes = parsed.get("quotes", [])
    if quotes:
        lines += ["", "## Notable Quotes", ""]
        for q in quotes:
            lines.append(f"> {q}")
            lines.append("")

    related = parsed.get("related_concepts", [])
    if related:
        lines += ["", "## Rabbit Holes", ""]
        for r in related:
            lines.append(f"- {r}")

    if tags:
        lines += ["", "## Tags", ""]
        lines.append(" ".join(f"`{t}`" for t in tags))

    if link_url:
        lines += ["", "## Linked Content", "", f"[{link_url}]({link_url})"]

    if post_body:
        lines += ["", "## Original Post", ""]
        lines.append(post_body)

    if comments_block:
        lines += ["", "## Top Comments", ""]
        lines.append(comments_block)

    lines.append("")

    with open(filepath, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    logger.info(f"Wrote: {filepath}")
    return filepath
