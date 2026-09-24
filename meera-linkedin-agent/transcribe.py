"""Voice note -> text, using Gemini.

Tries the dedicated transcription model first, then the general models in settings.models
(prompted to transcribe) if it's unavailable.
"""

from google import genai
from google.genai import errors, types

from config import Settings

TRANSCRIBE_MODEL = "gemini-3.5-transcribe"
# Names the speech model tends to mishear ("Claude Code" -> "Claude Goda"). Extend via TRANSCRIBE_VOCABULARY.
VOCABULARY = ["Claude", "Claude Code", "Codex", "ChatGPT", "OpenAI", "Anthropic", "Gemini", "n8n", "Gumloop",
              "Cursor", "Zapier", "Make.com", "LangChain", "Copilot", "Perplexity", "LinkedIn", "SaaS", "GenAI"]
PROMPT = (
    "Transcribe this voice note verbatim. Keep the speaker's own words, including filler and "
    "Hindi/English mixing (write Hindi words in Latin script). Output only the transcript. "
    "If there is no intelligible speech, output nothing."
)


def _text_of(response) -> str:
    """Collect transcript text; the transcription model returns `audio_transcription` parts."""
    chunks = []
    for candidate in response.candidates or []:
        for part in (candidate.content.parts if candidate.content else None) or []:
            if part.audio_transcription and part.audio_transcription.text:
                chunks.append(part.audio_transcription.text)
            elif part.text and not part.thought:
                chunks.append(part.text)
    return " ".join(c.strip() for c in chunks if c.strip())


def transcribe(audio: bytes, mime_type: str, settings: Settings, client: genai.Client) -> tuple[str, str]:
    """Return (transcript, model used). Raises the last API error if every model fails."""
    audio_part = types.Part.from_bytes(data=audio, mime_type=mime_type)
    vocabulary = VOCABULARY + list(settings.transcribe_vocabulary)
    asr_config = types.GenerateContentConfig(
        audio_transcription_config=types.AudioTranscriptionConfig(custom_vocabulary=vocabulary))
    prompt = f"{PROMPT} Terms that may come up: {', '.join(vocabulary)}."
    attempts = [(TRANSCRIBE_MODEL, [audio_part], asr_config)] + \
               [(m, [audio_part, prompt], None) for m in settings.models]
    last_error = None
    for model, contents, config in attempts:
        try:
            response = client.models.generate_content(model=model, contents=contents, config=config)
            return _text_of(response), model
        except errors.APIError as e:
            last_error = e
    raise last_error
