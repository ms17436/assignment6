"""
X5 — Tone matching (Tier B).

Classifies the relationship with a correspondent and picks a tone register, then
(optionally) drafts a reply in that register. A lawyer gets formal; an old friend
gets warm and casual; an investor gets crisp and professional.

Relationship detection is deterministic (by sender domain / known contacts);
the tone-adjusted draft uses the LLM when available.
"""

import logging
import re
from typing import Optional

from . import loader, llm, drafter

log = logging.getLogger("agent.tone")

# (relationship, tone, matcher) — first match wins
_RELATIONSHIP_RULES = [
    ("lawyer",    "formal, precise, no emojis, acknowledge deadlines explicitly",
     lambda a, m: a.endswith("@hartwellcho.com")),
    ("board",     "formal, concise, deferential to the board's time",
     lambda a, m: a.endswith("@paperjet-board.org")),
    ("investor",  "crisp, confident, professional; respect their time",
     lambda a, m: a.endswith("@northwind.vc")),
    ("press",     "professional and careful; nothing off the record; short",
     lambda a, m: a.endswith("@techbrief.news")),
    ("candidate", "warm, encouraging, professional; respect their timeline",
     lambda a, m: "jordan.okafor" in a or "backend role" in (m.get("subject","").lower())),
    ("old_friend","warm, casual, first-name, a little informal; no corporate tone",
     lambda a, m: a.endswith("@oldfriends.net")),
    ("vendor",    "polite, businesslike, transactional",
     lambda a, m: a.endswith("@zenboard.io") or a.endswith("@thegrandvenue.com")),
    ("colleague", "friendly-professional, direct, first-name, low ceremony",
     lambda a, m: a.endswith("@paperjet.io")),
]


def classify_relationship(msg: dict) -> tuple[str, str]:
    """Return (relationship, tone_guidance)."""
    addr = msg.get("from", "").lower()
    for rel, tone, matcher in _RELATIONSHIP_RULES:
        try:
            if matcher(addr, msg):
                return rel, tone
        except Exception:
            continue
    return "unknown", "neutral, professional, safe default"


def draft_with_tone(msg: dict, use_llm: bool = True) -> dict:
    """Classify relationship + draft a reply in the matching tone."""
    rel, tone = classify_relationship(msg)
    instruction = (f"The correspondent relationship is '{rel}'. "
                   f"Write in this tone: {tone}.")
    draft = drafter.draft(msg, extra_instructions=instruction, use_llm=use_llm)
    draft["relationship"] = rel
    draft["tone_guidance"] = tone
    return draft
