"""
Scheduling helper (supports Part 5).

Extracts a proposed meeting time from a message and evaluates it against the
owner's STORED calendar preferences (loaded from state/prefs.json). The point is
that the decline/counter-offer behaviour is driven entirely by the persisted
preference, not by any hard-coded per-message rule — so it genuinely changes
after a preference is stored and the process restarts.
"""

import re
from typing import Optional

from . import preferences

# "9:00am", "9 am", "2:00pm", "3pm", "11am", "14:00"
_TIME_RE = re.compile(
    r"\b(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\b", re.IGNORECASE
)

# Words near a time that indicate it is a meeting proposal
_MEETING_HINT = re.compile(
    r"\b(meet|meeting|call|slot|demo|1:1|one[- ]on[- ]one|catch up|sync|"
    r"at\s+\d|do\s+\w+day|available|work on your side)\b",
    re.IGNORECASE,
)


def extract_time(text: str) -> Optional[str]:
    """
    Return the first plausible clock time in `text` as 'HH:MM' (24h), or None.
    Prefers times with an am/pm marker or a colon (to avoid matching bare numbers
    like 'the 15th' or '$3,200').
    """
    for m in _TIME_RE.finditer(text):
        hour_s, minute_s, ampm = m.group(1), m.group(2), m.group(3)
        # Skip bare integers with neither colon nor am/pm (not a clock time)
        if not minute_s and not ampm:
            continue
        hour = int(hour_s)
        minute = int(minute_s or 0)
        ampm = (ampm or "").lower()
        if hour > 23 or minute > 59:
            continue
        if ampm == "pm" and hour < 12:
            hour += 12
        elif ampm == "am" and hour == 12:
            hour = 0
        return f"{hour:02d}:{minute:02d}"
    return None


def evaluate_meeting(msg: dict) -> dict:
    """
    Evaluate a scheduling request against STORED calendar preferences.

    Returns:
      {
        "proposed_time": "HH:MM" | None,
        "has_rule": bool,               # whether a calendar preference exists
        "allowed": bool,                # True if no rule or time satisfies it
        "counter_offer": "HH:MM" | None,
        "reason": str,
      }
    """
    text = msg.get("subject", "") + " " + msg.get("body", "")
    proposed = extract_time(text)

    cal_prefs = preferences.get_by_type("calendar")
    has_rule = bool(cal_prefs)

    if proposed is None:
        return {
            "proposed_time": None, "has_rule": has_rule, "allowed": True,
            "counter_offer": None,
            "reason": "No specific meeting time proposed.",
        }

    if not has_rule:
        return {
            "proposed_time": proposed, "has_rule": False, "allowed": True,
            "counter_offer": None,
            "reason": f"No calendar preference on file — {proposed} would be accepted.",
        }

    allowed = preferences.apply_calendar_rule(proposed)
    if allowed:
        return {
            "proposed_time": proposed, "has_rule": True, "allowed": True,
            "counter_offer": None,
            "reason": f"{proposed} satisfies the stored calendar rule.",
        }

    # Blocked — derive the counter-offer time from the rule
    cutoff = None
    for p in cal_prefs:
        m = re.search(r"no_meetings_before=(\d{2}:\d{2})", p.get("rule", ""))
        if m:
            cutoff = m.group(1)
            break
    return {
        "proposed_time": proposed, "has_rule": True, "allowed": False,
        "counter_offer": cutoff or "11:00",
        "reason": (f"{proposed} violates the stored calendar rule "
                   f"(no meetings before {cutoff or '11:00'}); counter-offer {cutoff or '11:00'}."),
    }
