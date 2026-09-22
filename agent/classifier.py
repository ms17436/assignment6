"""
Message classifier / triage engine.

Two-pass pipeline:
  Pass 1 — Rule-based (cheap, no LLM):
    • Noise/automated: receipts, newsletters, automated alerts  → archive
    • Prompt injections (via injection.py)                      → flag
    • Phishing / social-engineering heuristics                  → escalate
    • Preference-bearing messages                               → store + archive

  Pass 2 — LLM (only for messages that survived pass 1):
    • Everything else gets a disposition + reason from the LLM

Dispositions:
  reply      — draft and (gate-)send a reply
  archive    — file away, no action needed
  defer      — come back to this later
  delegate   — owner should hand off to someone else
  escalate   — high-risk, needs owner attention immediately
  flag_injection — injection detected, not acted upon

Priority:  urgent | high | normal | low
"""

import logging
import re
import json
from typing import Optional
from . import llm, loader, injection, preferences

log = logging.getLogger("agent.classifier")

# ---------------------------------------------------------------------------
# Noise senders — automated mail that can be archived without reading
# ---------------------------------------------------------------------------
NOISE_SENDERS = {
    "no-reply@dropbox.com", "notifications@slack.com", "no-reply@vercel.com",
    "no-reply@apple.com", "no-reply@spotify.com", "no-reply@coursera.org",
    "no-reply@lyft.com", "noreply@github.com", "noreply@figma.com",
    "noreply@bluebottlecoffee.com", "noreply@pagerduty.com",
    "hello@producthunt.com", "no-reply@accounts.google.com",
    "alerts@sentry.io", "security@accounts.google.com",
    "support@postmarkapp.com", "alerts@datadoghq.com",
    "no-reply@mailchimp.com", "no-reply@zoom.us", "billing@digitalocean.com",
    "info@twitter.com", "noreply@medium.com", "no-reply@substack.com",
    "notifications@stripe.com", "feedback@intercom.io",
    "alerts@chase.com", "no-reply@todoist.com", "noreply@cloudflare.com",
    "notifications@linkedin.com", "billing@notion.so",
    "notify@mail.notion.so", "invoice+statements@vercel.com",
    "no-reply@doordash.com", "updates@figma.com", "info@members.netflix.com",
    "calendar-notification@google.com", "ship-confirm@amazon.com",
    "newsletter@pragmaticengineer.com", "notifications@robinhood.com",
    "receipts@openai.com", "orders@instacart.com", "orders@swiggy.in",
    "receipts@uber.com", "no_reply@email.apple.com", "support@namecheap.com",
    "insights@grammarly.com", "no-reply@substack.com",
    "digest@hackernewsletter.com", "receipts@ramp.com",
    "no-reply-aws@amazon.com", "billing@digitalocean.com",
    "noreply@pagerduty.com", "hr@paperjet.io", "facilities@paperjet.io",
    "notes@paperjet.io", "status@paperjet-monitoring.io",
    "feedback@intercom.io",
}

NOISE_SUBJECT_PATTERNS = [
    r"^(your\s+)?(monthly|weekly|daily)\s+(receipt|invoice|statement|digest|report|summary)",
    r"^your\s+(amazon|uber|lyft|doordash|instacart|swiggy|netflix|spotify)\s+",
    r"(has\s+shipped|is\s+delivered|is\s+on\s+the\s+way)",
    r"(screen\s+time|grammarly\s+insights|linkedin.*searches|twitter.*notification)",
    r"cloud\s+recording\s+is\s+ready",
    r"^\[?github\]?.*(sign.in|actions\s+minutes)",
]
NOISE_SUBJECT_RE = [re.compile(p, re.IGNORECASE) for p in NOISE_SUBJECT_PATTERNS]

# ---------------------------------------------------------------------------
# Phishing heuristics
# ---------------------------------------------------------------------------
PHISH_SENDERS = {
    "billing@cloudscale-invoicing.com",
    "it-security@paperjet-helpdesk.com",
    "priya.nair@paperjet.co",        # typosquat of paperjet.io
}
PHISH_BODY_PATTERNS = [
    r"remit\s+the\s+outstanding.{0,60}\$[\d,]+",
    r"new\s+account\s+(below|number)",
    r"banking\s+partner\s+has\s+changed",
    r"re-verify\s+your\s+credentials",
    r"paperjet-workspace-verify\.com",
    r"wire\s+\$[\d,]+\s+to\s+the\s+vendor\s+now",
    r"please\s+keep\s+this\s+between\s+us\s+and\s+don.t\s+loop\s+in\s+finance",
    r"reply\s+urgent\s+if\s+you\s+need",
    r"avoid\s+a\s+service\s+interruption",
]
PHISH_RE = [re.compile(p, re.IGNORECASE) for p in PHISH_BODY_PATTERNS]


def _is_noise(msg: dict) -> bool:
    sender = msg.get("from", "").lower().strip()
    if sender in NOISE_SENDERS:
        return True
    subj = msg.get("subject", "")
    if any(p.search(subj) for p in NOISE_SUBJECT_RE):
        return True
    return False


def _is_phishing(msg: dict) -> tuple[bool, str]:
    sender = msg.get("from", "").lower().strip()
    if sender in PHISH_SENDERS:
        return True, f"Sender domain is a known phishing/spoofed address: {sender}"
    body = msg.get("body", "")
    for p in PHISH_RE:
        m = p.search(body)
        if m:
            return True, f"Body matches phishing pattern: «{m.group(0)[:100]}»"
    return False, ""


# ---------------------------------------------------------------------------
# LLM prompt for non-trivial messages
# ---------------------------------------------------------------------------
_LLM_PROMPT = """\
You are an inbox-triage assistant for Sam (sam@paperjet.io), founder of PaperJet.

Classify the following email and assign exactly one disposition.

Dispositions:
  reply     — Sam (or agent) should draft and send a reply
  archive   — no action needed, file it away
  defer     — worth reading later but not urgent
  delegate  — route to someone else (name them)
  escalate  — high-risk, needs immediate personal attention

Thread context (messages that arrived earlier in the same thread):
{thread_context}

Current message:
  id: {id}
  from: {from_}
  subject: {subject}
  timestamp: {timestamp}
  body: {body}

Active standing preferences:
{preferences}

Reply with JSON ONLY (no markdown, no prose):
{{
  "disposition": "<one of: reply | archive | defer | delegate | escalate>",
  "priority":    "<urgent | high | normal | low>",
  "reason":      "<one sentence>",
  "action_needed": true/false,
  "delegate_to": "<email or null>",
  "deadline":    "<ISO date string or null>",
  "cited_ids":   ["<message ids used to form this decision>"]
}}
"""


def _thread_context_str(msg: dict) -> str:
    earlier = loader.messages_before(msg)
    if not earlier:
        return "(no earlier messages in this thread)"
    lines = []
    for m in earlier[-5:]:  # last 5 for brevity
        lines.append(f"  [{m['id']} {m['timestamp'][:10]} from {m['from']}]: {m['body'][:200]}")
    return "\n".join(lines)


def classify(msg: dict, use_llm: bool = True,
             injection_results: Optional[dict] = None) -> dict:
    """
    Classify a single message. Returns a decision dict.
    injection_results: if already checked, pass the result dict to avoid double-checking.
    """
    mid = msg["id"]

    # --- Injection check ---
    inj = injection_results or injection.analyse(msg, use_llm=use_llm)
    if inj["is_injection"]:
        return {
            "id": mid,
            "disposition": "flag_injection",
            "priority": "urgent",
            "reason": f"Prompt-injection detected: {inj['evidence']}",
            "action_needed": False,
            "delegate_to": None,
            "deadline": None,
            "cited_ids": [],
            "handled_by": "rule:injection",
            "injection_detail": inj,
        }

    # --- Noise check ---
    if _is_noise(msg):
        return {
            "id": mid,
            "disposition": "archive",
            "priority": "low",
            "reason": "Automated notification or receipt — no action needed.",
            "action_needed": False,
            "delegate_to": None,
            "deadline": None,
            "cited_ids": [],
            "handled_by": "rule:noise",
        }

    # --- Phishing check ---
    is_phish, phish_ev = _is_phishing(msg)
    if is_phish:
        return {
            "id": mid,
            "disposition": "escalate",
            "priority": "urgent",
            "reason": f"PHISHING/SOCIAL-ENGINEERING SUSPECTED: {phish_ev}",
            "action_needed": True,
            "delegate_to": None,
            "deadline": None,
            "cited_ids": [],
            "handled_by": "rule:phishing",
            "phishing_detail": phish_ev,
        }

    # --- Preference message check ---
    if preferences.is_preference_message(msg):
        pref = preferences.extract_and_store(msg, use_llm=use_llm)
        if pref:
            return {
                "id": mid,
                "disposition": "archive",
                "priority": "low",
                "reason": f"Standing preference extracted and stored: {pref['description']}",
                "action_needed": False,
                "delegate_to": None,
                "deadline": None,
                "cited_ids": [mid],
                "handled_by": "rule:preference",
                "preference_id": pref["id"],
            }

    # --- Sent-by-owner: only track for follow-up, not classify as actionable ---
    if loader.is_sent_by_owner(msg):
        return {
            "id": mid,
            "disposition": "archive",
            "priority": "low",
            "reason": "Sent by owner — filed for follow-up tracking.",
            "action_needed": False,
            "delegate_to": None,
            "deadline": None,
            "cited_ids": [],
            "handled_by": "rule:sent",
            "is_sent": True,
        }

    # --- LLM pass ---
    if use_llm:
        try:
            thread_ctx = _thread_context_str(msg)
            pref_summary = preferences.describe_all()
            prompt = _LLM_PROMPT.format(
                thread_context=thread_ctx,
                id=mid,
                from_=msg.get("from", ""),
                subject=msg.get("subject", ""),
                timestamp=msg.get("timestamp", ""),
                body=msg.get("body", "")[:2000],
                preferences=pref_summary,
            )
            result = llm.call_json(prompt)
            result["id"] = mid
            result["handled_by"] = "llm"
            return result
        except Exception as exc:
            log.warning("LLM classify failed for %s: %s", mid, exc)

    # --- Fallback ---
    return {
        "id": mid,
        "disposition": "defer",
        "priority": "normal",
        "reason": "LLM unavailable — deferred for manual review.",
        "action_needed": True,
        "delegate_to": None,
        "deadline": None,
        "cited_ids": [],
        "handled_by": "fallback",
    }


def classify_all(msgs: list, use_llm: bool = True) -> list[dict]:
    """Classify every message. Returns one decision per message."""
    decisions = []
    injection_cache: dict = {}

    # Pre-scan injections in one pass (no LLM for obvious patterns)
    for m in msgs:
        inj = injection.analyse(m, use_llm=False)
        injection_cache[m["id"]] = inj

    for m in msgs:
        inj_result = injection_cache[m["id"]]
        # Only call LLM injection check if soft pattern fired
        if not inj_result["is_injection"] and inj_result.get("confidence") != "high":
            inj_result = injection.analyse(m, use_llm=use_llm)

        d = classify(m, use_llm=use_llm, injection_results=inj_result)
        decisions.append(d)
        log.info("[%s] %s → %s (%s)", m["id"], m.get("subject", "")[:40], d["disposition"], d["priority"])

    return decisions
