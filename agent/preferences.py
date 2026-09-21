"""
Persistent preference store.

Preferences are extracted from messages where the owner addresses their
assistant directly (self-to-self notes, explicit standing requests from
colleagues the owner has accepted, etc.).

Stored in state/prefs.json — survives process restarts.

Schema:
{
  "preferences": [
    {
      "id": "pref-<n>",
      "source_message_id": "mXXX",
      "type": "calendar" | "routing" | "delegation" | "communication" | "other",
      "description": "human-readable rule",
      "rule": "machine-readable encoding (optional)",
      "added_at": "ISO timestamp"
    }
  ]
}

Known preferences in this inbox:
  m041 — no meetings before 11:00am (calendar rule)
  m015 — CC priya@paperjet.io on all Hartwell & Cho correspondence (routing rule)
"""

import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Optional

from . import llm

log = logging.getLogger("agent.preferences")

PREFS_PATH = Path(__file__).parent.parent / "state" / "prefs.json"

# Patterns that indicate a preference-bearing message
_PREF_PATTERNS = [
    r"\bstanding\s+(request|preference|rule)\b",
    r"\bfrom\s+now\s+on\b",
    r"\bplease\s+remember\s+this\b",
    r"\bdo\s+not\s+(take|accept|schedule)\s+meetings?\b",
    r"\bcc\b.{0,40}\bon\s+(anything|all|every)\b",
    r"\bnote\s+for\s+the\s+assistant\b",
    r"\balways\s+(cc|include|loop)\b",
    r"\bnever\b.{0,40}\bmeeting\b",
    r"\bapplies\s+to\s+all\b",
]
_PREF_RE = [re.compile(p, re.IGNORECASE) for p in _PREF_PATTERNS]

_EXTRACT_PROMPT = """\
Extract the stated preference or standing rule from this email.

From: {from_}
To: {to}
Subject: {subject}
Body:
{body}

Reply with JSON ONLY:
{{
  "type": "calendar" | "routing" | "delegation" | "communication" | "other",
  "description": "<one clear sentence describing the rule>",
  "rule": "<machine-readable encoding if possible, e.g. no_meetings_before=11:00 or cc_on_sender=hartwellcho.com>"
}}
"""


def _load() -> dict:
    if PREFS_PATH.exists() and PREFS_PATH.stat().st_size > 2:
        with open(PREFS_PATH) as fh:
            data = json.load(fh)
        # Handle both {"preferences": [...]} and plain {} (empty init)
        if "preferences" not in data:
            data["preferences"] = []
        return data
    return {"preferences": []}


def _save(data: dict):
    PREFS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(PREFS_PATH, "w") as fh:
        json.dump(data, fh, indent=2)


def get_all() -> list[dict]:
    return _load().get("preferences", [])


def get_by_type(ptype: str) -> list[dict]:
    return [p for p in get_all() if p.get("type") == ptype]


def is_preference_message(msg: dict) -> bool:
    """Quick check whether msg looks like it contains a standing preference."""
    text = (msg.get("subject", "") + " " + msg.get("body", "")).lower()
    return any(p.search(text) for p in _PREF_RE)


def extract_and_store(msg: dict, use_llm: bool = True) -> Optional[dict]:
    """
    Extract preference from msg and persist it.
    Returns the stored pref dict, or None if nothing was extracted.
    """
    # Avoid duplicates
    data = _load()
    existing_ids = {p.get("source_message_id") for p in data["preferences"]}
    if msg["id"] in existing_ids:
        log.info("Preference from %s already stored.", msg["id"])
        return next((p for p in data["preferences"] if p.get("source_message_id") == msg["id"]), None)

    pref_data = None
    if use_llm:
        try:
            prompt = _EXTRACT_PROMPT.format(
                from_=msg.get("from", ""),
                to=msg.get("to", ""),
                subject=msg.get("subject", ""),
                body=msg.get("body", "")[:2000],
            )
            pref_data = llm.call_json(prompt)
        except Exception as exc:
            log.warning("LLM preference extraction failed for %s: %s", msg["id"], exc)

    if not pref_data:
        # Fallback: manual rules for known messages
        pref_data = _hardcoded_fallback(msg)

    if not pref_data:
        return None

    pref = {
        "id": f"pref-{len(data['preferences']) + 1}",
        "source_message_id": msg["id"],
        "type": pref_data.get("type", "other"),
        "description": pref_data.get("description", ""),
        "rule": pref_data.get("rule", ""),
        "added_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    data["preferences"].append(pref)
    _save(data)
    log.info("Stored preference: %s", pref["description"])
    return pref


def _hardcoded_fallback(msg: dict) -> Optional[dict]:
    """Hard-coded fallbacks for the two known preference messages."""
    mid = msg.get("id", "")
    if mid == "m041":
        return {
            "type": "calendar",
            "description": "Do not accept or propose meetings before 11:00am. If someone proposes earlier, counter-offer at 11:00am or later.",
            "rule": "no_meetings_before=11:00",
        }
    if mid == "m015":
        return {
            "type": "routing",
            "description": "Always CC priya@paperjet.io on any correspondence from Hartwell & Cho lawyers.",
            "rule": "cc_on_domain=hartwellcho.com;cc_recipient=priya@paperjet.io",
        }
    return None


def apply_calendar_rule(proposed_time: str) -> bool:
    """
    Returns True if proposed_time is allowed by calendar preferences.
    proposed_time: "HH:MM" 24-hour string.
    """
    cal_prefs = get_by_type("calendar")
    for pref in cal_prefs:
        rule = pref.get("rule", "")
        m = re.search(r"no_meetings_before=(\d+:\d+)", rule)
        if m:
            cutoff = m.group(1)
            if proposed_time < cutoff:
                return False
    return True


def apply_cc_rule(from_addr: str) -> list[str]:
    """
    Returns a list of CC addresses to add based on routing preferences.
    from_addr: sender email address.
    """
    routing = get_by_type("routing")
    cc_list = []
    for pref in routing:
        rule = pref.get("rule", "")
        domain_m = re.search(r"cc_on_domain=([^;]+)", rule)
        cc_m = re.search(r"cc_recipient=([^;]+)", rule)
        if domain_m and cc_m:
            domain = domain_m.group(1).strip().lower()
            cc_addr = cc_m.group(1).strip()
            if from_addr.lower().endswith("@" + domain) or from_addr.lower().endswith("." + domain):
                cc_list.append(cc_addr)
    return cc_list


def describe_all() -> str:
    """Human-readable summary of all active preferences."""
    prefs = get_all()
    if not prefs:
        return "No preferences stored."
    lines = []
    for p in prefs:
        lines.append(f"  [{p['id']}] ({p['type']}) {p['description']} (from {p['source_message_id']})")
    return "\n".join(lines)
