import os
import re
import json
import logging
import random
import string
from typing import Optional

from config_manager import load_config

logger = logging.getLogger("rabbithole.docmost")

DOCMOST_URL = os.environ.get("DOCMOST_URL", "http://10.10.10.201:3121")

# ── Helpers ───────────────────────────────────────────────────────────────────

def _nanoid(size=21) -> str:
    """Generate a DocMost-style slug ID."""
    alphabet = string.ascii_letters + string.digits
    return "".join(random.choices(alphabet, k=size))


def _title_slug(title: str) -> str:
    """DocMost-style URL slug prefix for a page title."""
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return slug[:70].rstrip("-")


def _page_url(space_slug: str, title: str, slug_id: str) -> str:
    return f"{DOCMOST_URL}/s/{space_slug}/p/{_title_slug(title)}-{slug_id}"


def _get_conn(config: dict):
    import psycopg2
    import psycopg2.extras
    dm = config.get("docmost", {})
    host = dm.get("db_host", "10.10.10.201")
    password = dm.get("db_password", "")
    conn = psycopg2.connect(
        host=host, port=5432,
        dbname="docmost", user="docmost",
        password=password,
        connect_timeout=8
    )
    conn.cursor_factory = psycopg2.extras.RealDictCursor
    return conn


def _get_workspace_and_user(cur) -> tuple:
    """Fetch workspace_id and a creator_id from the DB."""
    cur.execute("SELECT id FROM workspaces LIMIT 1")
    row = cur.fetchone()
    workspace_id = str(row["id"]) if row else None

    cur.execute("SELECT id FROM users ORDER BY created_at ASC LIMIT 1")
    row = cur.fetchone()
    creator_id = str(row["id"]) if row else None

    return workspace_id, creator_id


# ── Markdown → TipTap JSON ────────────────────────────────────────────────────

def _inline(text: str) -> list:
    """Parse inline markdown into TipTap inline nodes."""
    if not text:
        return []
    result = []
    pattern = re.compile(
        r"(\*\*(.+?)\*\*)"           # bold
        r"|(\*(.+?)\*)"               # italic
        r"|(`(.+?)`)"                 # inline code
        r"|(\[(.+?)\]\((.+?)\))"     # link
    )
    last = 0
    for m in pattern.finditer(text):
        if m.start() > last:
            result.append({"type": "text", "text": text[last:m.start()]})
        if m.group(1):
            result.append({"type": "text", "text": m.group(2),
                           "marks": [{"type": "bold"}]})
        elif m.group(3):
            result.append({"type": "text", "text": m.group(4),
                           "marks": [{"type": "italic"}]})
        elif m.group(5):
            result.append({"type": "text", "text": m.group(6),
                           "marks": [{"type": "code"}]})
        elif m.group(7):
            result.append({"type": "text", "text": m.group(8),
                           "marks": [{"type": "link",
                                      "attrs": {"href": m.group(9),
                                                "target": "_blank"}}]})
        last = m.end()
    if last < len(text):
        result.append({"type": "text", "text": text[last:]})
    return result or [{"type": "text", "text": text}]


def _md_to_tiptap(md: str) -> dict:
    """Convert markdown string to TipTap/ProseMirror JSON."""
    nodes = []
    lines = md.split("\n")
    i = 0

    while i < len(lines):
        line = lines[i]

        # Skip YAML frontmatter block
        if i == 0 and line.strip() == "---":
            i += 1
            while i < len(lines) and lines[i].strip() != "---":
                i += 1
            i += 1
            continue

        # Heading
        m = re.match(r"^(#{1,6})\s+(.*)", line)
        if m:
            level = len(m.group(1))
            nodes.append({
                "type": "heading",
                "attrs": {"level": level},
                "content": _inline(m.group(2).strip())
            })
            i += 1
            continue

        # Fenced code block
        if line.startswith("```"):
            lang = line[3:].strip() or None
            i += 1
            code_lines = []
            while i < len(lines) and not lines[i].startswith("```"):
                code_lines.append(lines[i])
                i += 1
            i += 1
            nodes.append({
                "type": "codeBlock",
                "attrs": {"language": lang},
                "content": [{"type": "text", "text": "\n".join(code_lines)}]
            })
            continue

        # Blockquote
        if line.startswith("> "):
            text = line[2:]
            nodes.append({
                "type": "blockquote",
                "content": [{"type": "paragraph",
                             "content": _inline(text)}]
            })
            i += 1
            continue

        # Bullet list
        if re.match(r"^[-*+]\s+", line):
            items = []
            while i < len(lines) and re.match(r"^[-*+]\s+", lines[i]):
                text = re.sub(r"^[-*+]\s+", "", lines[i])
                items.append({
                    "type": "listItem",
                    "content": [{"type": "paragraph",
                                 "content": _inline(text)}]
                })
                i += 1
            nodes.append({"type": "bulletList", "content": items})
            continue

        # Ordered list
        if re.match(r"^\d+\.\s+", line):
            items = []
            while i < len(lines) and re.match(r"^\d+\.\s+", lines[i]):
                text = re.sub(r"^\d+\.\s+", "", lines[i])
                items.append({
                    "type": "listItem",
                    "content": [{"type": "paragraph",
                                 "content": _inline(text)}]
                })
                i += 1
            nodes.append({"type": "orderedList", "content": items})
            continue

        # Horizontal rule
        if re.match(r"^[-*_]{3,}\s*$", line):
            nodes.append({"type": "horizontalRule"})
            i += 1
            continue

        # Empty line
        if not line.strip():
            i += 1
            continue

        # Paragraph — consume until blank/block element
        para_lines = []
        while i < len(lines):
            l = lines[i]
            if (not l.strip()
                    or re.match(r"^#{1,6}\s", l)
                    or re.match(r"^[-*+]\s+", l)
                    or re.match(r"^\d+\.\s+", l)
                    or l.startswith("> ")
                    or l.startswith("```")
                    or re.match(r"^[-*_]{3,}\s*$", l)):
                break
            para_lines.append(l)
            i += 1
        if para_lines:
            text = " ".join(para_lines)
            nodes.append({"type": "paragraph", "content": _inline(text)})

    return {
        "type": "doc",
        "content": nodes or [{"type": "paragraph", "content": []}]
    }


# ── Spaces ────────────────────────────────────────────────────────────────────

def list_spaces() -> list:
    """
    Return [{"id":..., "name":..., "slug":...}, ...] for all spaces in the
    configured DocMost Postgres DB. Used to populate workspace pickers in
    the UI. Returns [] on failure/disabled.
    """
    config = load_config()
    dm = config.get("docmost", {})
    if not dm.get("db_password"):
        return []
    try:
        conn = _get_conn(config)
        cur = conn.cursor()
        cur.execute("SELECT id, name, slug FROM spaces ORDER BY name ASC")
        rows = cur.fetchall()
        conn.close()
        return [{"id": str(r["id"]), "name": r["name"], "slug": r["slug"]} for r in rows]
    except Exception as e:
        logger.error(f"DocMost list_spaces error: {e}", exc_info=True)
        return []


# ── Main upsert ───────────────────────────────────────────────────────────────

def upsert_page(title: str, md_content: str, subject_area: str,
                space_id: Optional[str] = None) -> Optional[dict]:
    """
    Write or update a DocMost page via direct Postgres.
    Returns {"page_id": ..., "url": ...} on success, None on failure/disabled.
    `space_id` overrides the configured default space when given.
    """
    config = load_config()
    dm = config.get("docmost", {})

    if not dm.get("enabled"):
        return None
    if not dm.get("db_password"):
        logger.warning("DocMost enabled but db_password not set")
        return None

    space_id = space_id or dm.get("space_id", "0196753b-62a3-7d2b-8d23-473d8bd58bff")
    page_title = f"[{subject_area}] {title}"
    tiptap = _md_to_tiptap(md_content)
    content_json = json.dumps(tiptap)
    plain_text = title + "\n" + md_content  # used for tsv, DocMost trigger handles tsv

    try:
        conn = _get_conn(config)
        cur = conn.cursor()

        workspace_id, creator_id = _get_workspace_and_user(cur)
        if not workspace_id or not creator_id:
            logger.error("Could not find workspace or user in DocMost DB")
            conn.close()
            return None

        cur.execute("SELECT slug FROM spaces WHERE id = %s", (space_id,))
        space_row = cur.fetchone()
        space_slug = space_row["slug"] if space_row else space_id

        # Check for existing page with same title in this space
        cur.execute(
            "SELECT id, slug_id FROM pages WHERE space_id = %s AND title = %s "
            "AND deleted_at IS NULL LIMIT 1",
            (space_id, page_title)
        )
        existing = cur.fetchone()

        if existing:
            page_id = str(existing["id"])
            slug_id = existing["slug_id"]
            cur.execute(
                """UPDATE pages SET
                     content = %s::jsonb,
                     text_content = %s,
                     last_updated_by_id = %s,
                     updated_at = now()
                   WHERE id = %s""",
                (content_json, plain_text, creator_id, page_id)
            )
            logger.info(f"DocMost updated page {page_id}: {page_title!r}")
        else:
            slug_id = _nanoid()
            cur.execute(
                """INSERT INTO pages
                     (slug_id, title, content, text_content,
                      space_id, workspace_id, creator_id, last_updated_by_id)
                   VALUES (%s, %s, %s::jsonb, %s, %s, %s, %s, %s)
                   RETURNING id""",
                (slug_id, page_title, content_json, plain_text,
                 space_id, workspace_id, creator_id, creator_id)
            )
            row = cur.fetchone()
            page_id = str(row["id"])
            logger.info(f"DocMost created page {page_id}: {page_title!r}")

        conn.commit()
        conn.close()
        return {"page_id": page_id, "url": _page_url(space_slug, page_title, slug_id)}

    except Exception as e:
        logger.error(f"DocMost Postgres error: {e}", exc_info=True)
        return None
