"""The 4-stage pipeline: Gemini drives the stages, this module enforces the rules in code.

The system prompt tells the model how to behave; `PipelineGuard` makes the important rules hold
even if the model slips:
  * google_rss_search only after a Stage 1 score >= 7, at most 2 calls (original + one retry)
  * search region is India unless the note mentions another country or an international angle
  * send_telegram_message exactly once per run; the run ends as soon as it succeeds
  * Stage 4 drafts must meet the length / hashtag / emoji rules (one revision request, then accepted)
"""

import json
import re
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

from google import genai
from google.genai import errors, types

import geo
import tools
from config import SYSTEM_PROMPT, Settings

MAX_TURNS = 8
MAX_RSS_CALLS = 2
FALLBACK_STATUS = {429, 500, 503}  # quota / overload: try the next model in settings.models

SCORE_RE = re.compile(r"^[\s*_#>`-]*SCORE[\s*_`]*:[\s*_`]*(\d{1,2})\b", re.IGNORECASE | re.MULTILINE)
HASHTAG_RE = re.compile(r"(?<!\w)#\w+")
EMOJI_RE = re.compile(
    "[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0001F000-\U0001F2FF\U00002B00-\U00002BFF️]"
)
# Finish reasons that mean the model's output was blocked or broken rather than completed.
BLOCKED = {"SAFETY", "RECITATION", "BLOCKLIST", "PROHIBITED_CONTENT", "SPII", "OTHER"}


def word_count(text: str) -> int:
    return len(re.findall(r"\b[\w'’-]+\b", text))


@dataclass
class RunResult:
    input_text: str
    source: str = "text"          # "text" | "voice"
    chat_id: str | None = None    # where the reply goes (None: the owner chat from settings)
    model: str | None = None
    allowed_regions: list[str] = field(default_factory=list)
    score: int | None = None
    outcome: str = "failed"  # "feedback" | "post" | "failed"
    telegram_message: str | None = None
    delivered: bool = False
    dry_run: bool = False
    rss_queries: list[dict] = field(default_factory=list)  # [{"query", "region"}]
    reasoning: list[str] = field(default_factory=list)      # the model's visible stage-by-stage text
    warnings: list[str] = field(default_factory=list)
    error: str | None = None
    usage: dict = field(default_factory=lambda: {"prompt_tokens": 0, "output_tokens": 0, "thinking_tokens": 0})
    started_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


class ToolRejected(Exception):
    """Raised when a tool call breaks a pipeline rule; the message goes back to the model as an error."""


class PipelineGuard:
    def __init__(self, input_text: str, scope: geo.SearchScope, result: RunResult):
        self.input_text = input_text
        self.scope = scope
        self.result = result
        self.rss_calls = 0
        self.telegram_sent = False
        self.draft_rejections = 0

    def observe_text(self, text: str) -> None:
        scores = SCORE_RE.findall(text)
        if scores and self.result.score is None:
            self.result.score = max(1, min(10, int(scores[-1])))

    def _require_score(self) -> int:
        if self.result.score is None:
            raise ToolRejected("Write your Stage 1 justification and the `SCORE: N` line before any tool call.")
        return self.result.score

    def check_rss(self, region: str) -> None:
        if self.telegram_sent:
            raise ToolRejected("The pipeline already terminated (Telegram message sent). Stop.")
        score = self._require_score()
        if score < 7:
            raise ToolRejected(f"Blocked: Stage 1 score is {score} (<7). Stage 3 is not allowed. "
                               "Send Stage 2 feedback via send_telegram_message and stop.")
        if self.rss_calls >= MAX_RSS_CALLS:
            raise ToolRejected("Search limit reached (original + one reformulated retry). Proceed to Stage 4 "
                               "with what you have, noting if no supporting data was found. Do not invent data.")
        if region not in self.scope.allowed_regions:
            raise ToolRejected(f"Region {region!r} isn't allowed for this note. Meera's note doesn't mention it, "
                               f"so search one of: {', '.join(self.scope.allowed_regions)} (India, IN, by default).")

    def check_telegram(self, message: str) -> None:
        if self.telegram_sent:
            raise ToolRejected("send_telegram_message was already called for this run. Stop.")
        score = self._require_score()
        if not message.strip():
            raise ToolRejected("Message is empty.")
        if score < 7:
            return  # Stage 2 feedback: free-form
        if self.rss_calls == 0:
            raise ToolRejected("Score >= 7 requires Stage 3 first: state the core thesis and call google_rss_search.")

        problems = []
        words = word_count(message)
        if not 100 <= words <= 200:
            problems.append(f"length is {words} words (must be 100-200)")
        hashtags = HASHTAG_RE.findall(message)
        if len(hashtags) > 2:
            problems.append(f"{len(hashtags)} hashtags (max 2)")
        if EMOJI_RE.search(message) and not EMOJI_RE.search(self.input_text):
            problems.append("contains emoji, but Meera's input had none")
        if not problems:
            return
        if self.draft_rejections == 0:
            self.draft_rejections += 1
            raise ToolRejected("Draft not sent. Fix and call send_telegram_message again with the revised post "
                               "only: " + "; ".join(problems) + ".")
        self.result.warnings.append("Draft sent after one revision despite: " + "; ".join(problems))


class LinkedInPipeline:
    def __init__(self, settings: Settings, client: genai.Client | None = None, log=print):
        self.settings = settings
        self.client = client or genai.Client(
            api_key=settings.gemini_api_key,
            http_options=types.HttpOptions(retry_options=types.HttpRetryOptions(attempts=2)),
        )
        self.log = log

    def _generate(self, contents: list, scope: geo.SearchScope, result: RunResult):
        config = types.GenerateContentConfig(
            system_instruction=SYSTEM_PROMPT,
            tools=tools.tool_declarations(scope),
            # We run the tools ourselves so every call passes through PipelineGuard.
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
            tool_config=types.ToolConfig(
                function_calling_config=types.FunctionCallingConfig(mode=types.FunctionCallingConfigMode.AUTO)),
        )
        # Once a model has answered, the run stays on it (its thought signatures are in `contents`).
        candidates = [result.model] if result.model else list(self.settings.models)
        for i, model in enumerate(candidates):
            try:
                response = self.client.models.generate_content(model=model, contents=contents, config=config)
                result.model = model
                return response
            except errors.APIError as e:
                if e.code not in FALLBACK_STATUS or i == len(candidates) - 1:
                    raise
                self.log(f"[model] {model} unavailable ({e.code}); trying {candidates[i + 1]}")
                result.warnings.append(f"{model} unavailable ({e.code})")

    def _execute(self, call: types.FunctionCall, guard: PipelineGuard, result: RunResult) -> types.Part:
        name, args = call.name, dict(call.args or {})
        try:
            if name == "google_rss_search":
                query = str(args.get("query", "")).strip()
                region = str(args.get("region") or guard.scope.default_region).strip().upper()
                if not query:
                    raise ToolRejected("query is required.")
                guard.check_rss(region)
                guard.rss_calls += 1
                result.rss_queries.append({"query": query, "region": region})
                self.log(f"\n[tool] google_rss_search({query!r}, region={region})")
                response = tools.google_rss_search(query, region, self.settings)
                self.log(f"[tool]   -> {response['result_count']} items from {response['edition']}")
            elif name == "send_telegram_message":
                message = str(args.get("message", ""))
                guard.check_telegram(message)
                self.log("\n[tool] send_telegram_message")
                response = tools.send_telegram_message(message, self.settings, result.chat_id)
                guard.telegram_sent = True
                result.telegram_message = message
                result.delivered = response["delivered"]
                result.dry_run = response["dry_run"]
                result.outcome = "post" if (result.score or 0) >= 7 else "feedback"
            else:
                raise ToolRejected(f"Unknown tool {name!r}.")
        except ToolRejected as e:
            self.log(f"\n[guard] rejected {name}: {e}")
            response = {"error": str(e)}
        except Exception as e:  # network / Telegram failures go back to the model, and into the run log
            self.log(f"\n[tool] {name} failed: {e}")
            result.warnings.append(f"{name} failed: {e}")
            response = {"error": f"Tool error: {e}"}
        return types.Part(function_response=types.FunctionResponse(id=call.id, name=name, response=response))

    def run(self, input_text: str, chat_id: str | None = None, source: str = "text") -> RunResult:
        scope = geo.detect_scope(input_text)
        result = RunResult(input_text=input_text, source=source, chat_id=chat_id,
                           allowed_regions=scope.allowed_regions)
        guard = PipelineGuard(input_text, scope, result)
        contents = [types.Content(role="user", parts=[types.Part(text=(
            f"<transcribed_input>\n{input_text.strip()}\n</transcribed_input>\n\n"
            f"<search_scope>\nToday's date: {datetime.now():%d %B %Y} (search for recent data, not past years).\n"
            f"{scope.describe()}\n</search_scope>"))])]
        self.log(f"[scope] {scope.describe()}")
        nudged = False

        try:
            for _ in range(MAX_TURNS):
                response = self._generate(contents, scope, result)
                usage = response.usage_metadata
                if usage:
                    result.usage["prompt_tokens"] += usage.prompt_token_count or 0
                    result.usage["output_tokens"] += usage.candidates_token_count or 0
                    result.usage["thinking_tokens"] += usage.thoughts_token_count or 0

                if not response.candidates:
                    block = response.prompt_feedback.block_reason if response.prompt_feedback else "unknown"
                    result.error = f"Gemini blocked the request ({block})."
                    break
                candidate = response.candidates[0]
                finish = candidate.finish_reason.name if candidate.finish_reason else "STOP"
                parts = (candidate.content.parts if candidate.content else None) or []

                for part in parts:
                    if part.text and not part.thought and part.text.strip():
                        self.log(part.text)
                        result.reasoning.append(part.text)
                        guard.observe_text(part.text)

                calls = [p.function_call for p in parts if p.function_call]
                if not calls and finish in BLOCKED:
                    result.error = f"Gemini stopped the response ({finish})."
                    break
                if candidate.content:
                    # Append the model turn unchanged: it carries thought signatures Gemini needs back.
                    contents.append(candidate.content)

                if calls:
                    # Every function call gets a response, all in one user turn.
                    replies = [self._execute(c, guard, result) for c in calls]
                    if guard.telegram_sent:
                        break  # delivery terminates the pipeline; no further model turns
                    contents.append(types.Content(role="user", parts=replies))
                    continue

                if finish == "MAX_TOKENS":
                    result.error = "Response hit the output token limit before finishing."
                    break
                # The model ended its turn without delivering anything: remind it once.
                if not guard.telegram_sent and not nudged:
                    nudged = True
                    contents.append(types.Content(role="user", parts=[types.Part(text=(
                        "The pipeline has not terminated yet. Finish the current stage and call "
                        "send_telegram_message exactly once, as your instructions specify."))]))
                    continue
                break
            else:
                result.error = f"Stopped after {MAX_TURNS} model turns without delivery."
        except errors.APIError as e:
            result.error = f"Gemini API error {e.code}: {e.message}"

        if not guard.telegram_sent and not result.error:
            result.error = "Pipeline ended without sending a Telegram message."
        self._save(result)
        return result

    def _save(self, result: RunResult) -> None:
        if not self.settings.save_runs:  # Vercel: one JSON line in the function logs instead
            print("[run] " + json.dumps(asdict(result), ensure_ascii=False))
            return
        self.settings.runs_dir.mkdir(exist_ok=True)
        path = self.settings.runs_dir / f"{time.strftime('%Y%m%d-%H%M%S')}-{result.outcome}.json"
        path.write_text(json.dumps(asdict(result), indent=2, ensure_ascii=False), encoding="utf-8")
