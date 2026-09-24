"""
Model-provider configuration, read from environment variables.

A local `.env` file (copy `.env.example`) is loaded first if present; real
environment variables always win over values in `.env`. Nothing here is
hardcoded to a provider: with no variables set, the system runs offline.

    LLM_PROVIDER     ollama | gemini | openai | offline | auto   (default: auto)
    LLM_MODEL        model name; blank = provider default
    LLM_OFFLINE      1 = force offline heuristics (same as --no-llm)
    LLM_CALL_DELAY   seconds between model calls (default 4, i.e. <= 15 per minute)
    LLM_MAX_RETRIES  retries on HTTP 429 (default 5)
    GEMINI_API_KEY
    OPENAI_API_KEY, OPENAI_BASE_URL, OPENAI_MODEL
    OLLAMA_HOST, OLLAMA_MODEL
"""

import os
from pathlib import Path

ENV_FILE = Path(__file__).parent / ".env"

DEFAULT_MODEL = {
    "ollama": "qwen2.5:1.5b",
    "gemini": "gemini-1.5-flash",
    "openai": "gpt-4o-mini",
}


def _load_dotenv(path: Path = ENV_FILE) -> None:
    if not path.exists():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip().removeprefix("export ").strip()
        value = value.strip().strip('"').strip("'")
        if key:
            os.environ.setdefault(key, value)


_load_dotenv()


def get(name: str, default: str = "") -> str:
    """Read a setting at call time, so runtime flags like --no-llm take effect."""
    return os.environ.get(name, default)


def provider() -> str:
    return get("LLM_PROVIDER", "auto").strip().lower() or "auto"


def model() -> str:
    return get("LLM_MODEL").strip()


def offline() -> bool:
    return get("LLM_OFFLINE", "0") == "1"


def call_delay() -> float:
    return float(get("LLM_CALL_DELAY", "4"))


def max_retries() -> int:
    return int(get("LLM_MAX_RETRIES", "5"))
