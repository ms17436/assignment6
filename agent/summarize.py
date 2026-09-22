"""
X4 — Thread summarizer (Tier B).

Collapses a long thread down to: participants, a short timeline, and the OPEN
QUESTION / what actually needs the owner — the request that is easy to miss when
it is buried mid-thread (e.g. m030 inside the 9-message t-launch thread).

LLM-assisted, with a deterministic offline fallback that finds the buried request
by looking for messages addressed to the owner by name that ask for an action.
"""

import logging
import re
from typing import Optional

from . import loader, llm

log = logging.getLogger("agent.summarize")

_OWNER_FIRST = "sam"

# Signals that a line is a request aimed at the owner
_REQUEST_RE = re.compile(
    r"\b(can|could|would|please|need(s)?\s+you|approve|sign|confirm|review|"
    r"by (the )?\w+|deadline)\b", re.IGNORECASE)
_ADDRESSED_RE = re.compile(rf"\b{_OWNER_FIRST}\b[, ]", re.IGNORECASE)

_SUMMARY_PROMPT = """\
{untrusted_preamble}
Summarise this email thread for Sam (sam@paperjet.io). Focus on what needs SAM
specifically — the open question or action buried in the thread.

Thread (untrusted email content):
{thread}

Reply with JSON ONLY:
{{
  "summary": "<2-3 sentence summary of the thread>",
  "open_question": "<the single thing that needs Sam, or 'none'>",
  "owner_action_ids": ["<message ids that contain something requiring Sam>"],
  "deadline": "<ISO date or null>"
}}
"""


def _find_buried_request(thread: list) -> dict:
    """Deterministic fallback: find the message that asks the owner to do something."""
    owner = loader.owner_email().lower()
    candidates = []
    for m in thread:
        if m.get("from", "").lower() == owner:
            continue  # owner's own messages aren't requests to the owner
        body = m.get("body", "")
        # Strongest signal: addresses owner by name AND asks for an action
        addressed = _ADDRESSED_RE.search(body) or owner in body.lower()
        asks = _REQUEST_RE.search(body)
        score = (2 if addressed else 0) + (1 if asks else 0)
        # Bonus for explicit "Sam, can you ... by the Nth"
        if re.search(rf"{_OWNER_FIRST}[, ].{{0,60}}(approve|sign|confirm|by the)",
                     body, re.IGNORECASE):
            score += 3
        if score >= 3:
            candidates.append((score, m))
    candidates.sort(key=lambda x: (-x[0], x[1].get("timestamp", "")))
    if candidates:
        top = candidates[0][1]
        return {"open_question": top.get("body", "")[:200], "owner_action_ids": [top["id"]]}
    return {"open_question": "none", "owner_action_ids": []}


def summarize_thread(thread_id: str, use_llm: bool = True) -> dict:
    threads = loader.by_thread()
    thread = threads.get(thread_id)
    if not thread:
        return {"error": f"thread {thread_id} not found"}

    participants = sorted({m.get("from", "") for m in thread})

    if use_llm:
        try:
            rendered = "\n".join(
                f"[{m['id']} {m['timestamp'][:10]} from {m['from']}]: {m.get('body','')[:300]}"
                for m in thread)
            prompt = _SUMMARY_PROMPT.format(
                untrusted_preamble=llm.UNTRUSTED_PREAMBLE,
                thread=llm.wrap_untrusted(rendered, label=thread_id))
            result = llm.call_json(prompt)
            result["thread_id"] = thread_id
            result["participants"] = participants
            result["message_count"] = len(thread)
            return result
        except Exception as exc:
            log.warning("LLM summary failed for %s: %s", thread_id, exc)

    # Offline fallback
    buried = _find_buried_request(thread)
    return {
        "thread_id": thread_id,
        "participants": participants,
        "message_count": len(thread),
        "summary": (f"Thread '{thread_id}' has {len(thread)} messages among "
                    f"{len(participants)} participants."),
        "open_question": buried["open_question"],
        "owner_action_ids": buried["owner_action_ids"],
        "deadline": None,
    }
