"""The two tools the pipeline exposes to Gemini: Google News RSS search and Telegram delivery."""

import html
import json
import re
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

from google.genai import types

import geo
from config import Settings


def tool_declarations(scope: geo.SearchScope) -> list[types.Tool]:
    """Function declarations for one run. The `region` enum only lists the regions this note allows."""
    return [types.Tool(function_declarations=[
        types.FunctionDeclaration(
            name="google_rss_search",
            description=(
                "Fetch recent news articles and data from Google News RSS. Returns up to ~8 recent items "
                "with title, source, publish date, link and snippet. Call only in Stage 3, and only when "
                "the Stage 1 score is 7 or higher."
            ),
            parameters_json_schema={
                "type": "object",
                "properties": {
                    "query": {"type": "string",
                              "description": "One precise, SEO-friendly search query targeting recent data/news."},
                    "region": {"type": "string", "enum": scope.allowed_regions,
                               "description": "Where to search: IN (India, the default), a country code "
                                              "mentioned in the note, or GLOBAL."},
                },
                "required": ["query", "region"],
            },
        ),
        types.FunctionDeclaration(
            name="send_telegram_message",
            description=(
                "Deliver a message to Meera's Telegram chat. Call exactly once per run, at the end of the "
                "stage that terminates the pipeline: Stage 2 feedback, or the Stage 4 final post draft only."
            ),
            parameters_json_schema={
                "type": "object",
                "properties": {"message": {"type": "string", "description": "The exact text to send."}},
                "required": ["message"],
            },
        ),
    ])]


_TAG_RE = re.compile(r"<[^>]+>")
USER_AGENT = "Mozilla/5.0 (meera-linkedin-agent)"


def google_rss_search(query: str, region: str, settings: Settings) -> dict:
    """Query Google News RSS for `region` and return a compact list of recent items."""
    params, country_hint = geo.rss_params(region)
    search = f"{query} {country_hint}" if country_hint and country_hint.lower() not in query.lower() else query
    url = f"https://news.google.com/rss/search?{urllib.parse.urlencode({'q': search, **params})}"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=20) as resp:
        root = ET.fromstring(resp.read())

    items = []
    for item in root.iter("item"):
        title = item.findtext("title", "").strip()
        source = item.findtext("source", "").strip()
        snippet = re.sub(r"\s+", " ", _TAG_RE.sub(" ", html.unescape(item.findtext("description", "")))).strip()
        entry = {"title": title, "source": source, "published": item.findtext("pubDate", "").strip(),
                 "link": item.findtext("link", "").strip()}
        if snippet and not snippet.startswith(title.rsplit(" - ", 1)[0]):
            entry["snippet"] = snippet[:300]  # Google News snippets usually just repeat the title
        items.append(entry)
        if len(items) >= settings.rss_max_items:
            break
    return {"query": search, "region": region, "edition": params["ceid"], "result_count": len(items),
            "items": items}


TELEGRAM_LIMIT = 4096


def send_telegram_message(message: str, settings: Settings, chat_id: str | None = None) -> dict:
    """Send `message` to `chat_id` (default: the owner chat). Without credentials, runs as a dry run."""
    chat_id = chat_id or settings.telegram_chat_id
    if not (settings.telegram_bot_token and chat_id):
        return {"delivered": False, "dry_run": True,
                "note": "No Telegram bot token or chat id; message printed locally."}

    # Telegram caps a single message at 4096 characters; split on paragraph boundaries if needed.
    chunks, current = [], ""
    for para in message.split("\n"):
        candidate = f"{current}\n{para}" if current else para
        if len(candidate) > TELEGRAM_LIMIT and current:
            chunks.append(current)
            current = para
        else:
            current = candidate
    chunks.append(current)

    url = f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage"
    message_ids = []
    for chunk in chunks:
        body = json.dumps({"chat_id": chat_id, "text": chunk[:TELEGRAM_LIMIT]}).encode()
        req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=20) as resp:
            payload = json.loads(resp.read())
        if not payload.get("ok"):
            raise RuntimeError(f"Telegram API error: {payload.get('description')}")
        message_ids.append(payload["result"]["message_id"])
    return {"delivered": True, "dry_run": False, "message_ids": message_ids}
