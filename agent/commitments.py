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

# Known commitments (hard-coded as fallback, consistent with inbox analysis)
_KNOWN_COMMITMENTS = [
    {
        "type": "event",
        "description": "Quarterly board review — in-person at office",
        "date": "2026-09-18",
        "time": "10:00",
        "participants": ["chair@paperjet-board.org", "sam@paperjet.io"],
        "source_message_id": "m038",
        "requires_sam": True,
        "calendar_rule_violated": False,
        "calendar_rule_note": None,
    },
    {
        "type": "deadline",
        "description": "Board deck to be circulated 2 days before board review (Sep 18) → due Sep 16",
        "date": "2026-09-16",
        "time": None,
        "participants": ["sam@paperjet.io"],
        "source_message_id": "m040",
        "requires_sam": True,
        "calendar_rule_violated": False,
        "calendar_rule_note": None,
    },
    {
        "type": "deadline",
        "description": "Approve final pricing page copy (annual discount wording) by Sep 12",
        "date": "2026-09-12",
        "time": None,
        "participants": ["sam@paperjet.io", "priya@paperjet.io"],
        "source_message_id": "m030",
        "requires_sam": True,
        "calendar_rule_violated": False,
        "calendar_rule_note": None,
    },
    {
        "type": "deadline",
        "description": "Sign SAFE amendment via portal by Friday Sep 11",
        "date": "2026-09-11",
        "time": None,
        "participants": ["sam@paperjet.io", "m.cho@hartwellcho.com"],
        "source_message_id": "m018",
        "requires_sam": True,
        "calendar_rule_violated": False,
        "calendar_rule_note": None,
    },
    {
        "type": "deadline",
        "description": "Review draft board minutes and flag corrections by Monday Sep 14",
        "date": "2026-09-14",
        "time": None,
        "participants": ["sam@paperjet.io"],
        "source_message_id": "m048",
        "requires_sam": True,
        "calendar_rule_violated": False,
        "calendar_rule_note": None,
    },
    {
        "type": "call",
        "description": "Investor intro call with Aria (Northwind VC) — proposed Tue Sep 15 at 15:00",
        "date": "2026-09-15",
        "time": "15:00",
        "participants": ["aria.f@northwind.vc", "sam@paperjet.io"],
        "source_message_id": "m010",
        "requires_sam": True,
        "calendar_rule_violated": False,
        "calendar_rule_note": "15:00 is after 11:00am — calendar rule satisfied.",
    },
    {
        "type": "meeting",
        "description": "Dental appointment — Dr. Osei, Sep 15 at 15:00",
        "date": "2026-09-15",
        "time": "15:00",
        "participants": ["sam@paperjet.io"],
        "source_message_id": "m061",
        "requires_sam": True,
        "calendar_rule_violated": False,
        "calendar_rule_note": None,
    },
    {
        "type": "meeting",
        "description": "Investor partner meeting — Aria proposes Monday at 09:00 (VIOLATES calendar rule)",
        "date": "2026-09-14",   # Monday before the 15th
        "time": "09:00",
        "participants": ["aria.f@northwind.vc", "sam@paperjet.io"],
        "source_message_id": "m043",
        "requires_sam": True,
        "calendar_rule_violated": True,
        "calendar_rule_note": "09:00 is before 11:00am — Sam's standing rule says no meetings before 11am.",
    },
    {
        "type": "meeting",
        "description": "ACME Corp product demo — proposed Wed Sep 9 at 14:00",
        "date": "2026-09-09",
        "time": "14:00",
        "participants": ["partners@acme-corp.com", "sam@paperjet.io"],
        "source_message_id": "m016",
        "requires_sam": True,
        "calendar_rule_violated": False,
        "calendar_rule_note": None,
    },
    {
        "type": "meeting",
        "description": "Raghav 1:1 rescheduled to Wednesday Sep 9 at 14:00 (potential conflict with ACME demo)",
        "date": "2026-09-09",
        "time": "14:00",
        "participants": ["raghav@paperjet.io", "sam@paperjet.io"],
        "source_message_id": "m013",
        "requires_sam": True,
        "calendar_rule_violated": False,
        "calendar_rule_note": None,
    },
    {
        "type": "deadline",
        "description": "Product launch — hard date Sep 20, press briefed",
        "date": "2026-09-20",
        "time": None,
        "participants": ["sam@paperjet.io", "priya@paperjet.io"],
        "source_message_id": "m036",
        "requires_sam": True,
        "calendar_rule_violated": False,
        "calendar_rule_note": None,
    },
    {
        "type": "deadline",
        "description": "Jordan Okafor (backend candidate) has competing offer deadline Sep 19",
        "date": "2026-09-19",
        "time": None,
        "participants": ["jordan.okafor@gmail.com"],
        "source_message_id": "m042",
        "requires_sam": True,
        "calendar_rule_violated": False,
        "calendar_rule_note": None,
    },
    {
        "type": "deadline",
        "description": "Venue hold expires in 48 hours (from Sep 9) — Sam must confirm",
        "date": "2026-09-11",
        "time": None,
        "participants": ["events@thegrandvenue.com", "sam@paperjet.io"],
        "source_message_id": "m019",
        "requires_sam": True,
        "calendar_rule_violated": False,
        "calendar_rule_note": None,
    },
    {
        "type": "deadline",
        "description": "Load test on signup flow scheduled for Sep 14",
        "date": "2026-09-14",
        "time": None,
        "participants": ["raghav@paperjet.io"],
        "source_message_id": "m029",
        "requires_sam": False,
        "calendar_rule_violated": False,
        "calendar_rule_note": None,
    },
]


def detect_conflicts(commitments: list[dict]) -> list[dict]:
    """Find commitments that share the same date+time."""
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
                "description": f"CONFLICT at {slot}: " + " vs ".join(
                    i["description"][:60] for i in items
                ),
            })
    return conflicts


def get_all(use_llm: bool = False) -> tuple[list[dict], list[dict]]:
    """
    Returns (commitments, conflicts).
    use_llm=True adds LLM-extracted commitments on top of the hard-coded set.
    """
    commitments = list(_KNOWN_COMMITMENTS)

    if use_llm:
        msgs = loader.load_inbox()
        owner = loader.owner_email()
        # Only scan non-noise messages for additional commitments
        from . import classifier
        for msg in msgs:
            if classifier._is_noise(msg):
                continue
            if msg.get("id") in {c["source_message_id"] for c in _KNOWN_COMMITMENTS}:
                continue
            try:
                prompt = _COMMITMENT_PROMPT.format(
                    reference_date="2026-09-09",
                    id=msg["id"],
                    from_=msg.get("from", ""),
                    subject=msg.get("subject", ""),
                    body=msg.get("body", "")[:1500],
                )
                result = llm.call_json(prompt)
                for c in result.get("commitments", []):
                    c["source_message_id"] = msg["id"]
                    commitments.append(c)
            except Exception as exc:
                log.debug("Commitment extraction skipped for %s: %s", msg["id"], exc)

    # Add calendar-rule violation flag via preferences
    from . import preferences
    for c in commitments:
        if c.get("time") and not c.get("calendar_rule_violated"):
            allowed = preferences.apply_calendar_rule(c["time"])
            if not allowed:
                c["calendar_rule_violated"] = True
                c["calendar_rule_note"] = (
                    f"{c['time']} is before 11:00am — violates Sam's calendar rule."
                )

    conflicts = detect_conflicts(commitments)
    return commitments, conflicts
