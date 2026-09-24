"""Offline tests: a scripted fake Gemini client drives the loop so the guard rules can be checked."""

import sys
from pathlib import Path
from types import SimpleNamespace as NS

import pytest
from google.genai import types

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import geo  # noqa: E402
import pipeline  # noqa: E402
import tools  # noqa: E402
from config import load_settings  # noqa: E402


def text(t):
    return types.Part(text=t)


def call(name, **args):
    return types.Part(function_call=types.FunctionCall(name=name, args=args))


def resp(*parts, finish="STOP"):
    return NS(
        candidates=[NS(content=types.Content(role="model", parts=list(parts)),
                       finish_reason=types.FinishReason[finish])],
        prompt_feedback=None,
        usage_metadata=NS(prompt_token_count=10, candidates_token_count=5, thoughts_token_count=3),
    )


class FakeClient:
    def __init__(self, responses):
        self.responses = list(responses)  # an Exception entry is raised instead of returned
        self.requests = []
        self.models = NS(generate_content=self._generate)

    def _generate(self, **kwargs):
        self.requests.append({**kwargs, "contents": list(kwargs["contents"])})  # snapshot: the list keeps growing
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


GOOD_POST = " ".join(["word"] * 150) + "\n\nWhat do you think? #Leadership #Hiring"
INDIA_NOTE = "We cut time-to-hire from 45 to 18 days by dropping the degree filter."


@pytest.fixture
def settings(tmp_path, monkeypatch):
    s = load_settings()
    s = s.__class__(**{**s.__dict__, "runs_dir": tmp_path, "telegram_bot_token": None, "telegram_chat_id": None})
    monkeypatch.setattr(tools, "google_rss_search", lambda q, region, _s: {
        "query": q, "region": region, "edition": f"{region}:en", "result_count": 1, "items": [{"title": "x"}]})
    return s


def run(settings, responses, note=INDIA_NOTE):
    client = FakeClient(responses)
    result = pipeline.LinkedInPipeline(settings, client=client, log=lambda *_: None).run(note)
    return result, client


def reply(client, turn=-1, i=0):
    """The i-th function response the model received in request `turn`."""
    return client.requests[turn]["contents"][-1].parts[i].function_response.response


# --- stage gate ------------------------------------------------------------------------------

def test_low_score_sends_feedback_and_stops(settings):
    result, client = run(settings, [resp(text("Vague.\nSCORE: 4"), call("send_telegram_message", message="Try..."))])
    assert result.outcome == "feedback" and result.score == 4
    assert len(client.requests) == 1  # no further model turns after delivery
    assert result.dry_run and not result.error


def test_rss_blocked_when_score_below_7(settings):
    result, client = run(settings, [
        resp(text("SCORE: 5"), call("google_rss_search", query="ai hiring", region="IN")),
        resp(call("send_telegram_message", message="Feedback")),
    ])
    assert result.rss_queries == [] and "Blocked" in reply(client)["error"]
    assert result.outcome == "feedback"


def test_tool_before_score_is_rejected(settings):
    result, client = run(settings, [
        resp(call("send_telegram_message", message="hi")),
        resp(text("SCORE: 3"), call("send_telegram_message", message="Feedback")),
    ])
    assert "SCORE" in reply(client)["error"]
    assert result.outcome == "feedback"


def test_high_score_full_path(settings):
    result, _ = run(settings, [
        resp(text("Specific.\n**SCORE: 8**\nThesis: ..."), call("google_rss_search", query="q", region="IN")),
        resp(call("send_telegram_message", message=GOOD_POST)),
    ])
    assert result.outcome == "post" and result.score == 8
    assert result.rss_queries == [{"query": "q", "region": "IN"}] and not result.warnings


def test_post_requires_research_first(settings):
    result, client = run(settings, [
        resp(text("SCORE: 9"), call("send_telegram_message", message=GOOD_POST)),
        resp(call("google_rss_search", query="q", region="IN")),
        resp(call("send_telegram_message", message=GOOD_POST)),
    ])
    assert "Stage 3" in reply(client, turn=1)["error"]
    assert result.outcome == "post"


def test_rss_capped_at_two_calls(settings):
    result, _ = run(settings, [
        resp(text("SCORE: 7"), call("google_rss_search", query="a", region="IN")),
        resp(call("google_rss_search", query="b", region="IN")),
        resp(call("google_rss_search", query="c", region="IN")),
        resp(call("send_telegram_message", message=GOOD_POST)),
    ])
    assert [q["query"] for q in result.rss_queries] == ["a", "b"]
    assert result.outcome == "post"


def test_bad_draft_gets_one_revision_then_accepted(settings):
    short = "Too short #a #b #c"
    result, client = run(settings, [
        resp(text("SCORE: 8"), call("google_rss_search", query="q", region="IN")),
        resp(call("send_telegram_message", message=short)),
        resp(call("send_telegram_message", message=short)),
    ])
    assert "hashtags" in reply(client, turn=2)["error"]
    assert result.telegram_message == short and result.warnings


def test_emoji_rejected_unless_in_input(settings):
    post = GOOD_POST + " 🚀"
    _, client = run(settings, [
        resp(text("SCORE: 8"), call("google_rss_search", query="q", region="IN")),
        resp(call("send_telegram_message", message=post)),
        resp(call("send_telegram_message", message=GOOD_POST)),
    ])
    assert "emoji" in reply(client, turn=2)["error"]
    result, client = run(settings, [
        resp(text("SCORE: 8"), call("google_rss_search", query="q", region="IN")),
        resp(call("send_telegram_message", message=post)),
    ], note=INDIA_NOTE + " 🚀")
    assert len(client.requests) == 2 and result.outcome == "post"


def test_nudge_when_model_stops_without_delivery(settings):
    result, client = run(settings, [
        resp(text("SCORE: 2")),
        resp(call("send_telegram_message", message="Feedback")),
    ])
    assert "not terminated" in client.requests[1]["contents"][-1].parts[0].text
    assert result.outcome == "feedback"


def test_safety_block_is_reported(settings):
    result, _ = run(settings, [resp(finish="SAFETY")])
    assert result.outcome == "failed" and "SAFETY" in result.error


def test_falls_back_to_next_model_on_overload(settings):
    from google.genai import errors
    overloaded = errors.ServerError(503, {"error": {"code": 503, "message": "high demand"}})
    result, client = run(settings, [
        overloaded,
        resp(text("SCORE: 8"), call("google_rss_search", query="q", region="IN")),
        resp(call("send_telegram_message", message=GOOD_POST)),
    ])
    models = [r["model"] for r in client.requests]
    assert models == [settings.models[0], settings.models[1], settings.models[1]]  # stays on the fallback
    assert result.model == settings.models[1] and result.outcome == "post"


# --- search region ---------------------------------------------------------------------------

def test_foreign_region_rejected_when_note_mentions_no_country(settings):
    result, client = run(settings, [
        resp(text("SCORE: 8"), call("google_rss_search", query="q", region="US")),
        resp(call("google_rss_search", query="q", region="IN")),
        resp(call("send_telegram_message", message=GOOD_POST)),
    ])
    assert "isn't allowed" in reply(client, turn=1)["error"]
    assert result.rss_queries == [{"query": "q", "region": "IN"}]


def test_mentioned_country_allowed(settings):
    note = "Opening our Singapore office showed me hiring there takes twice as long."
    result, client = run(settings, [
        resp(text("SCORE: 8"), call("google_rss_search", query="q", region="SG")),
        resp(call("send_telegram_message", message=GOOD_POST)),
    ], note=note)
    assert result.rss_queries == [{"query": "q", "region": "SG"}]
    region_enum = client.requests[0]["config"].tools[0].function_declarations[0] \
        .parameters_json_schema["properties"]["region"]["enum"]
    assert region_enum == ["IN", "SG", "GLOBAL"]


def test_missing_region_defaults_to_scope(settings):
    result, _ = run(settings, [
        resp(text("SCORE: 8"), call("google_rss_search", query="q")),
        resp(call("send_telegram_message", message=GOOD_POST)),
    ])
    assert result.rss_queries == [{"query": "q", "region": "IN"}]


def test_request_shape(settings):
    _, client = run(settings, [resp(text("SCORE: 2"), call("send_telegram_message", message="x"))])
    req = client.requests[0]
    assert req["model"] == settings.models[0]
    assert req["config"].automatic_function_calling.disable is True
    prompt = req["contents"][0].parts[0].text
    assert "<transcribed_input>" in prompt and 'region="IN"' in prompt


@pytest.mark.parametrize("note, allowed, default", [
    ("Structured interviews cut our bad hires by half.", ["IN"], "IN"),
    ("Our Bengaluru team shipped 3x faster after async standups.", ["IN"], "IN"),
    ("Let us talk about what worked for us in West Bengal.", ["IN"], "IN"),
    ("Unlike the US, Indian startups hire generalists early.", ["IN", "US", "GLOBAL"], "IN"),
    ("Our Dubai expansion taught me visas take 3 months.", ["IN", "AE", "GLOBAL"], "AE"),
    ("Remote work is now a global talent market.", ["IN", "GLOBAL"], "GLOBAL"),
    ("Comparing Germany and Japan factory automation.", ["IN", "DE", "JP", "GLOBAL"], "GLOBAL"),
])
def test_detect_scope(note, allowed, default):
    scope = geo.detect_scope(note)
    assert scope.allowed_regions == allowed and scope.default_region == default


def test_rss_params():
    assert geo.rss_params("IN") == ({"hl": "en-IN", "gl": "IN", "ceid": "IN:en"}, None)
    assert geo.rss_params("AE") == ({"hl": "en-US", "gl": "US", "ceid": "US:en"}, "United Arab Emirates")
    assert geo.rss_params("GLOBAL") == ({"hl": "en-US", "gl": "US", "ceid": "US:en"}, None)


# --- delivery target + voice transcription ---------------------------------------------------

def test_reply_goes_to_sender_chat(settings, monkeypatch):
    sent = {}
    monkeypatch.setattr(tools, "send_telegram_message",
                        lambda msg, _s, chat_id=None: sent.update(chat_id=chat_id) or {"delivered": True, "dry_run": False})
    client = FakeClient([resp(text("SCORE: 3"), call("send_telegram_message", message="More context?"))])
    result = pipeline.LinkedInPipeline(settings, client=client, log=lambda *_: None).run(
        "hi", chat_id="12345", source="voice")
    assert sent["chat_id"] == "12345" and result.source == "voice" and result.delivered


def test_transcribe_reads_transcription_parts_and_falls_back(settings):
    from google.genai import errors
    import transcribe as tr
    transcript = NS(candidates=[NS(content=types.Content(role="model", parts=[
        types.Part(audio_transcription=types.Transcription(text="Last quarter we rebuilt our lead report."))]))])
    client = FakeClient([errors.ServerError(503, {"error": {"code": 503, "message": "busy"}}), transcript])
    text_out, model = tr.transcribe(b"audio", "audio/ogg", settings, client)
    assert text_out == "Last quarter we rebuilt our lead report."
    assert [r["model"] for r in client.requests] == [tr.TRANSCRIBE_MODEL, settings.models[0]]
    assert model == settings.models[0]
