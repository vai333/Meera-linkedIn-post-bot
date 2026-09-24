# Meera's LinkedIn Agent

Turns Meera's voice-transcribed notes into either **encouraging feedback** (score ≤ 6) or a
**researched LinkedIn post draft** (score ≥ 7), delivered to her Telegram. She can send **text or voice notes** to @Fractionless_bot. Runs on Google Gemini.

```
voice note ──► transcribe (gemini-3.5-transcribe)
                    │
text note ─────────►├─► detect search region (India unless she mentions elsewhere)
                    └─► Stage 1: score (SCORE: N)
           │
           ├─ ≤6 ─► Stage 2: feedback ──► send_telegram_message ──► stop
           │
           └─ ≥7 ─► Stage 3: thesis + google_rss_search(query, region)  (max 2)
                        └─► Stage 4: draft post ──► send_telegram_message ──► stop
```

## Search region

| Meera's note mentions | Allowed regions | Default |
|---|---|---|
| No country, no international angle | `IN` only | India |
| One other country ("our Dubai office") | `IN`, that country, `GLOBAL` | that country |
| India + another country ("unlike the US, Indian startups…") | `IN`, `US`, `GLOBAL` | India |
| International wording ("global", "overseas", "Europe", "APAC"…) | `IN`, `GLOBAL` | Global |
| Several other countries | `IN`, each country, `GLOBAL` | Global |

Detection is keyword-based in [geo.py](geo.py) (country names, demonyms, major cities). It runs in code before the model sees the note, so Gemini can't widen the search: the `region` parameter only accepts the allowed values, and the guard rejects anything else.

Google News has English editions for IN, US, GB, CA, AU, NZ, SG, MY, PH, PK, NG, KE, ZA, IE and IL. Any other country (for example UAE, Germany or Japan) is searched on the US-English edition with the country name added to the query. `GLOBAL` also uses the US-English edition.

## How it works

| File | Role |
|---|---|
| `prompts/system_prompt.md` | Your pipeline spec, plus the `SCORE: N` line and the region rules |
| `geo.py` | Works out which regions the note allows |
| `pipeline.py` | Gemini function-calling loop + `PipelineGuard` (rules enforced in code) |
| `tools.py` | `google_rss_search` (Google News RSS) and `send_telegram_message` (Bot API) |
| `app.py` | **Vercel entry point**: Telegram webhook at `POST /api/telegram`, health check at `/` |
| `telegram_bot.py` | Message handling shared by the webhook and the local bot (text, voice, commands) |
| `bot.py` | Local alternative to Vercel: long polling from your machine |
| `set_webhook.py` | Points the bot at a deployment (or shows / removes the webhook) |
| `cli.py` | Run the pipeline once from the command line |
| `transcribe.py` | Voice note → text (dedicated transcription model, general models as fallback) |

**Rules enforced in code**, not just in the prompt (a broken call is rejected and the model is told why):
- No tool call until the `SCORE: N` line is written
- Search is blocked when the score is below 7, and capped at 2 calls. The region must be one the note allows
- A score ≥ 7 post can't be sent before research has run
- `send_telegram_message` runs exactly once, and the run ends immediately after it
- A Stage 4 draft outside 100–200 words, with more than 2 hashtags, or with emoji Meera didn't use gets sent back for one revision
- **Model fallback:** if a Gemini model is overloaded (503) or out of quota (429), the run moves to the next model in `GEMINI_MODELS`

Every run is saved as JSON in `runs/` (model, score, regions, searches, reasoning, message, token usage).

## Setup

```bash
cd meera-linkedin-agent
/opt/homebrew/bin/python3.13 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
cp .env.example .env   # fill in GEMINI_API_KEY, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, TELEGRAM_WEBHOOK_SECRET
```

Without a Telegram bot token the app runs in **dry-run mode**: the message is printed, not sent.

The free Gemini tier only covers Flash models. With billing enabled, put `gemini-pro-latest` first in `GEMINI_MODELS` for stronger writing.

**Pairing:** leave `TELEGRAM_CHAT_ID` blank and start `bot.py`. The first person to message the bot privately becomes its owner, and their chat ID is saved to `.env`. Messages from anyone else are ignored. To re-pair, clear `TELEGRAM_CHAT_ID` and restart.

**Transcription vocabulary:** product names like "Claude Code" and "n8n" are built in. Add Meera's own (company, colleagues, clients) to `TRANSCRIBE_VOCABULARY` in `.env`.

## Run locally

```bash
.venv/bin/python cli.py "Ran our first skills-based hiring round last month - dropped the degree filter and 3 of our 5 best hires came from non-CS backgrounds."
.venv/bin/python bot.py        # listen on Telegram
.venv/bin/python -m pytest -q  # offline tests (fake Gemini client, no API calls)
```


## Deploy to Vercel

The bot runs as one Python function (`app.py`, FastAPI). Telegram calls it for each message, and it processes the note within the request. `vercel.json` gives it up to 300 s, the Hobby plan maximum; a note usually takes 20–60 s.

1. **Log in and deploy**
   ```bash
   npx vercel login
   npx vercel deploy --prod
   ```
2. **Add environment variables** (Vercel dashboard → project → Settings → Environment Variables, Production). Copy the values from `.env`:
   `GEMINI_API_KEY`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `TELEGRAM_WEBHOOK_SECRET`. Optional: `GEMINI_MODELS`, `TRANSCRIBE_VOCABULARY`.
   Then redeploy (`npx vercel deploy --prod`) so the function picks them up.
3. **Check** `https://<your-project>.vercel.app/` shows `"ready": true`.
4. **Stop `bot.py`** if it's running, then point Telegram at Vercel:
   ```bash
   .venv/bin/python set_webhook.py https://<your-project>.vercel.app
   ```
5. Message the bot. Logs: Vercel dashboard → project → Logs (each run is logged as one `[run] {...}` JSON line).

**Protections on the webhook:** requests without the right `X-Telegram-Bot-Api-Secret-Token` get a 401. Only `TELEGRAM_CHAT_ID` is served. A message Telegram re-sends after a slow reply is skipped when it reaches the same warm instance. There's no shared storage, so a re-send that lands on a different instance could be processed twice.

**Switching back to local:** `bot.py` removes the webhook when it starts; run `set_webhook.py` again to return to Vercel. `set_webhook.py --info` shows the webhook status and Telegram's last delivery error.
