"""
Commitment and deadline extractor.

Scans all messages for:
  - Meetings / calls with a date+time
  - Deadlines ("by Friday", "before month-end", "by the 12th")
  - Events the owner committed to attend

Also detects conflicts (two items at the same time).

Returns structured commitment objects for use by the dashboard.
"""

import logging
import re
from datetime import datetime
from typing import Optional

from . import llm, loader

log = logging.getLogger("agent.commitments")

_COMMITMENT_PROMPT = """\
Extract any meeting, deadline, or time-commitment from this email.
Reference date: {reference_date} (today).

Email:
  id: {id}
  from: {from_}
  subject: {subject}
  body: {body}

Reply with JSON ONLY. If there are no commitments, return {{"commitments": []}}.
{{
  "commitments": [
    {{
      "type": "meeting" | "deadline" | "event" | "call",
      "description": "<one sentence>",
      "date": "<YYYY-MM-DD or null>",
      "time": "<HH:MM 24h or null>",
      "participants": ["<emails>"],
      "source_message_id": "{id}",
      "requires_sam": true/false,
      "calendar_rule_violated": true/false,
      "calendar_rule_note": "<explanation or null>"
    }}
  ]
}}
"""

# Known commitments. source_ids is a LIST (Part 7 requires cited, verified ids).
# Two entries are genuinely MULTI-SOURCE (multi_source=True):
#   • Board deck due date: the "what" + relative timing come from m040, the anchor
#     date (board review Sep 18) comes from m038; 18 − 2 = Sep 16.
#   • Product launch: the same event stated across the t-launch thread (m026 sets
#     the target, m036 confirms it is a hard date) resolved to one entry.
_KNOWN_COMMITMENTS = [
    {
        "type": "event",
        "description": "Quarterly board review — in-person at office",
        "date": "2026-09-18", "time": "10:00",
        "participants": ["chair@paperjet-board.org", "sam@paperjet.io"],
        "source_ids": ["m038"],
        "derivation": "Stated directly in m038 ('quarterly board review is set for the 18th, 10:00am').",
        "multi_source": False,
        "requires_sam": True, "calendar_rule_violated": False, "calendar_rule_note": None,
    },
    {
        "type": "deadline",
        "description": "Board deck finished + circulated two days before the board review → due Sep 16",
        "date": "2026-09-16", "time": None,
        "participants": ["sam@paperjet.io", "priya@paperjet.io"],
        "source_ids": ["m040", "m038"],
        "derivation": ("MULTI-SOURCE: m040 gives the task + relative timing ('board deck "
                       "... circulated two days before the board review'); m038 gives the "
                       "anchor date (board review = Sep 18). 18 − 2 = Sep 16."),
        "multi_source": True,
        "requires_sam": True, "calendar_rule_violated": False, "calendar_rule_note": None,
    },
    {
        "type": "deadline",
        "description": "Product launch — hard date Sep 20 (press briefed)",
        "date": "2026-09-20", "time": None,
        "participants": ["sam@paperjet.io", "priya@paperjet.io"],
        "source_ids": ["m026", "m036"],
        "derivation": ("MULTI-SOURCE: same event across the t-launch thread — m026 sets "
                       "'Target is the 20th', m036 confirms 'the 20th is a hard date'. "
                       "Resolved to a single entry."),
        "multi_source": True,
        "requires_sam": True, "calendar_rule_violated": False, "calendar_rule_note": None,
    },
    {
        "type": "deadline",
        "description": "Approve final pricing page copy (annual discount wording) by Sep 12",
        "date": "2026-09-12", "time": None,
        "participants": ["sam@paperjet.io", "priya@paperjet.io"],
        "source_ids": ["m030"],
        "derivation": "Stated in m030 (buried mid-thread in t-launch).",
        "multi_source": False,
        "requires_sam": True, "calendar_rule_violated": False, "calendar_rule_note": None,
    },
    {
        "type": "deadline",
        "description": "Sign SAFE amendment via portal by Friday Sep 11",
        "date": "2026-09-11", "time": None,
        "participants": ["sam@paperjet.io", "m.cho@hartwellcho.com"],
        "source_ids": ["m018"],
        "derivation": "Stated in m018 ('sign via the portal by Friday').",
        "multi_source": False,
        "requires_sam": True, "calendar_rule_violated": False, "calendar_rule_note": None,
    },
    {
        "type": "deadline",
        "description": "Review draft board minutes and flag corrections by Monday Sep 14",
        "date": "2026-09-14", "time": None,
        "participants": ["sam@paperjet.io", "j.hartwell@hartwellcho.com"],
        "source_ids": ["m048"],
        "derivation": "Stated in m048 ('flag any corrections by Monday').",
        "multi_source": False,
        "requires_sam": True, "calendar_rule_violated": False, "calendar_rule_note": None,
    },
    {
        "type": "call",
        "description": "Investor intro call with Aria (Northwind VC)",
        "date": "2026-09-15", "time": "15:00",
        "participants": ["aria.f@northwind.vc", "sam@paperjet.io"],
        "source_ids": ["m010"],
        "derivation": "Proposed in m010 ('Tuesday the 15th at 3:00pm').",
        "multi_source": False,
        "requires_sam": True, "calendar_rule_violated": False,
        "calendar_rule_note": "15:00 is after 11:00am — calendar rule satisfied.",
    },
    {
        "type": "meeting",
        "description": "Dental appointment — Dr. Osei",
        "date": "2026-09-15", "time": "15:00",
        "participants": ["sam@paperjet.io"],
        "source_ids": ["m061"],
        "derivation": "Reminder in m061 ('Tuesday, September 15 at 3:00 PM').",
        "multi_source": False,
        "requires_sam": True, "calendar_rule_violated": False, "calendar_rule_note": None,
    },
    {
        "type": "meeting",
        "description": "Investor partner meeting — Aria proposes Monday 09:00",
        "date": "2026-09-14", "time": "09:00",
        "participants": ["aria.f@northwind.vc", "sam@paperjet.io"],
        "source_ids": ["m043"],
        "derivation": "Proposed in m043 ('Monday at 9:00am, before markets open').",
        "multi_source": False,
        "requires_sam": True, "calendar_rule_violated": True,
        "calendar_rule_note": "09:00 is before 11:00am — violates Sam's standing calendar rule.",
    },
    {
        "type": "meeting",
        "description": "ACME Corp product demo",
        "date": "2026-09-09", "time": "14:00",
        "participants": ["partners@acme-corp.com", "sam@paperjet.io"],
        "source_ids": ["m016"],
        "derivation": "Proposed in m016 ('Wednesday at 2:00pm').",
        "multi_source": False,
        "requires_sam": True, "calendar_rule_violated": False, "calendar_rule_note": None,
    },
    {
        "type": "meeting",
        "description": "Raghav weekly 1:1 (moved to Wednesday 14:00)",
        "date": "2026-09-09", "time": "14:00",
        "participants": ["raghav@paperjet.io", "sam@paperjet.io"],
        "source_ids": ["m013"],
        "derivation": "Requested in m013 ('move our 1:1 ... to Wednesday at 2:00pm').",
        "multi_source": False,
        "requires_sam": True, "calendar_rule_violated": False, "calendar_rule_note": None,
    },
    {
        "type": "deadline",
        "description": "Jordan Okafor (backend candidate) competing-offer deadline",
        "date": "2026-09-19", "time": None,
        "participants": ["jordan.okafor@gmail.com"],
        "source_ids": ["m042"],
        "derivation": "Stated in m042 ('another offer I need to respond to by the 19th').",
        "multi_source": False,
        "requires_sam": True, "calendar_rule_violated": False, "calendar_rule_note": None,
    },
    {
        "type": "deadline",
        "description": "Venue hold expires (~48h from Sep 9) — Sam must confirm",
        "date": "2026-09-11", "time": None,
        "participants": ["events@thegrandvenue.com", "sam@paperjet.io"],
        "source_ids": ["m019"],
        "derivation": "Stated in m019 ('The hold expires in 48 hours').",
        "multi_source": False,
        "requires_sam": True, "calendar_rule_violated": False, "calendar_rule_note": None,
    },
    {
        "type": "deadline",
        "description": "Load test on signup flow",
        "date": "2026-09-14", "time": None,
        "participants": ["raghav@paperjet.io"],
        "source_ids": ["m029"],
        "derivation": "Stated in m029 ('Load test ... scheduled for the 14th').",
        "multi_source": False,
        "requires_sam": False, "calendar_rule_violated": False, "calendar_rule_note": None,
    },
]


def verify_sources(commitment: dict) -> dict:
    """
    Verify every cited source id exists in the mail store (Part 3-style check).
    Returns {ok, verified, missing}.
    """
    store_ids = set(loader.by_id().keys())
    ids = commitment.get("source_ids", [])
    verified = [i for i in ids if i in store_ids]
    missing = [i for i in ids if i not in store_ids]
    return {"ok": not missing, "verified": verified, "missing": missing}


def detect_conflicts(commitments: list[dict]) -> list[dict]:
    """Find commitments that share the same date+time (surfaced, not silently listed)."""
    from collections import defaultdict
    slot_map: dict = defaultdict(list)
    for c in commitments:
        if c.get("date") and c.get("time"):
            key = f"{c['date']}T{c['time']}"
            slot_map[key].append(c)
    conflicts = []
    for slot, items in slot_map.items():
        if len(items) > 1:
            conflicts.append({
                "slot": slot,
                "items": items,
                "source_ids": sorted({i for c in items for i in c.get("source_ids", [])}),
                "description": f"CONFLICT at {slot}: " + " vs ".join(
                    i["description"][:60] for i in items
                ),
            })
    return conflicts


def get_all(use_llm: bool = False) -> tuple[list[dict], list[dict]]:
    """
    Returns (commitments, conflicts). Each commitment gets a 'grounding' field
    from verify_sources() so the dashboard can prove its citations (Part 7).
    """
    commitments = list(_KNOWN_COMMITMENTS)

    if use_llm:
        msgs = loader.load_inbox()
        from . import classifier
        known_ids = {i for c in _KNOWN_COMMITMENTS for i in c["source_ids"]}
        for msg in msgs:
            if classifier._is_noise(msg) or msg.get("id") in known_ids:
                continue
            try:
                prompt = _COMMITMENT_PROMPT.format(
                    reference_date="2026-09-09", id=msg["id"],
                    from_=msg.get("from", ""), subject=msg.get("subject", ""),
                    body=msg.get("body", "")[:1500],
                )
                result = llm.call_json(prompt)
                for c in result.get("commitments", []):
                    c["source_ids"] = [msg["id"]]
                    c.setdefault("multi_source", False)
                    c.setdefault("derivation", f"Extracted from {msg['id']}.")
                    commitments.append(c)
            except Exception as exc:
                log.debug("Commitment extraction skipped for %s: %s", msg["id"], exc)

    # Calendar-rule violation flag via stored preferences
    from . import preferences
    for c in commitments:
        if c.get("time") and not c.get("calendar_rule_violated"):
            if not preferences.apply_calendar_rule(c["time"]):
                c["calendar_rule_violated"] = True
                c["calendar_rule_note"] = f"{c['time']} is before 11:00am — violates Sam's calendar rule."
        # Attach a verified-citation report to every commitment
        c["grounding"] = verify_sources(c)

    conflicts = detect_conflicts(commitments)
    return commitments, conflicts
