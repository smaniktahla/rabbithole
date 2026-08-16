# 🐇 RabbitHole

A self-hosted YouTube + Reddit knowledge capture tool. Drop a link (or forward an email), get a structured summary, key points, and tags — saved to markdown on disk and optionally synced to [DocMost](https://docmost.com) (DocMost sync requires some custom code). Great for ingesting info from YouTube videos or Reddit threads to use for AI RAG or just adding to your own notes without having to watch/read the whole thing. Let the AI take the notes for you!
<img width="1577" height="1181" alt="image" src="https://github.com/user-attachments/assets/6996d036-12c5-4891-a928-05ce2528b818" />

## What it does

- **Captures YouTube videos** — paste a URL manually or forward an email containing a YouTube link
- **Captures Reddit threads** — paste a post URL and get a summary of the OP plus the highest-signal comments, not just the top post
- **Transcribes and summarizes** using an LLM (local via llama.cpp/Ollama, or Anthropic Claude)
- **Classifies** each item into a primary category, writes structured markdown to disk, and generates searchable tags
- **Syncs to DocMost** — optionally pushes each note into a DocMost wiki space via direct Postgres
- **Library view** — search, filter by category, reclassify items, edit tags, and bulk-update multiple items at once
<img width="1560" height="1182" alt="image" src="https://github.com/user-attachments/assets/00b8129e-f545-482d-a835-98b0c55a8649" />

## Stack

- **Backend**: FastAPI + APScheduler, SQLite for item state
- **Frontend**: Single-page vanilla JS, no build step
- **LLM**: Local (llama.cpp / Ollama OpenAI-compatible endpoint) or Anthropic API
- **Email**: Gmail via IMAP app password or OAuth
- **DocMost**: Direct Postgres write (no API key needed)

## Quick start

```bash
git clone https://github.com/smaniktahla/rabbithole.git
cd rabbithole
docker compose up -d
```

Open `http://localhost:8095`.

> The `/mnt` volume mount in `docker-compose.yml` is specific to a TrueNAS/NFS setup. Change it to wherever you want markdown files written.

## Configuration

Everything is configured through the **Settings** tab in the UI — no env file needed.

| Setting | Description |
|---|---|
| **LLM Provider** | `local` (llama.cpp/Ollama) or `anthropic` |
| **Local LLM URL** | Base URL for OpenAI-compatible endpoint, e.g. `http://10.0.0.1:8080` |
| **Local LLM Model** | Model name to pass in requests, e.g. `gemma4:12b` |
| **Anthropic API Key** | `sk-ant-...` key if using Claude |
| **Default Storage Path** | Root folder for markdown files, e.g. `/mnt/documents/RabbitHole` |
| **Primary Categories** | Named categories mapped to storage subfolders |
| **Gmail / IMAP** | App password or OAuth credentials for email polling |
| **Reddit API** (optional) | Client ID/secret if you have working OAuth credentials; not required — see [Reddit capture](#reddit-capture) |
| **DocMost** | Postgres host, password, and Space ID |

## Email capture

Two modes:

- **IMAP app password** — polls a Gmail inbox for emails tagged `[RH]` in the subject line
- **Gmail OAuth** — connect via the Settings tab; same `[RH]` subject tag triggers capture

Forward any email containing a YouTube or Reddit URL with `[RH]` in the subject and it gets queued automatically.

## Reddit capture

Reddit closed self-serve OAuth app registration in Nov 2025 ("Responsible Builder Policy"), so RabbitHole doesn't depend on Reddit API credentials at all. A plain HTTP request to `.json` gets blocked (403) even with a normal User-Agent, but a *browser session with real cookies* is allowed through — so RabbitHole uses a headless Chromium (Playwright) to load the post's real page first, then fetches the `.json` data from that same session. No Reddit account, app, or credentials needed.

- The **Reddit API** card in Settings is still there and used automatically *if* you happen to have working OAuth credentials (e.g. from before the policy change) — it's tried first as a fast path, then falls back to the headless-browser method. Most people can leave it blank.
- Paste any post URL (`reddit.com/r/.../comments/...` or a `redd.it/...` short link) into the capture box.

RabbitHole pulls the OP's post plus the top comments (score-filtered, one reply level deep) and asks the LLM to summarize both the original post and the most valuable discussion/consensus from the thread — not just the headline post.

The headless-browser fallback needs Chromium bundled in the image (`playwright install --with-deps chromium` in the Dockerfile), which adds real build time and image size — expect a slower first build after pulling this change.

## Library features

- **Search** across titles, summaries, tags, and channel names
- **Filter** by primary category
- **Reclassify** any item — change its category or edit tags inline from the item modal
- **Bulk select** — check multiple items and apply a new category or add a tag to all of them at once; type a new category name to create it on the fly
- **DocMost sync** — items without a DocMost page show a "Sync to DocMost" button; already-synced items re-sync automatically when reclassified

## Markdown output

Each captured video produces a file like `20260615_video-title.md`:

```markdown
---
title: "Video Title"
url: https://youtube.com/watch?v=...
channel: Channel Name
processed: 2026-06-15 10:30
subject_area: ai-ml
tags: [ai-agents, llm, tool-use]
---

# Video Title

## Summary
...

## Key Points
- ...

## Tags
`ai-agents` `llm` `tool-use`

## Full Transcript
...
```

A captured Reddit thread instead has `subreddit`/`author`/`score` frontmatter and `## Original Post` / `## Top Comments` sections in place of channel/transcript.

## Ports & volumes

| | |
|---|---|
| Port | `8095` |
| Data (SQLite + config) | Docker volume `rabbithole-data` → `/app/data` |
| Markdown storage | Host path via volume mount |

## License

MIT
