"""
Morning digest generator (Capability X2).

Three sections:
  1. Needs you today   — urgent/high priority items that require Sam's attention
  2. Can wait          — normal/low priority, deferred items
  3. Auto-archived     — count of noise items archived automatically (not individually listed)
"""

import json
import logging
from . import loader

log = logging.getLogger("agent.digest")

PRIORITY_ORDER = {"urgent": 0, "high": 1, "normal": 2, "low": 3}


def build(decisions: list[dict]) -> dict:
    """
    Build the morning digest from classified decisions.
    Returns a structured dict (also printed to console by demo.py).
    """
    msgs_by_id = loader.by_id()

    needs_you = [
        d for d in decisions
        if d.get("priority") in ("urgent", "high") and d.get("action_needed", False)
    ]
    needs_you.sort(key=lambda d: PRIORITY_ORDER.get(d.get("priority", "low"), 3))

    can_wait = [
        d for d in decisions
        if d.get("priority") in ("normal", "low")
        and d.get("action_needed", False)
        and d.get("disposition") not in ("archive",)
    ]

    auto_archived = [
        d for d in decisions
        if d.get("disposition") == "archive" and not d.get("action_needed", False)
    ]

    flagged = [
        d for d in decisions
        if d.get("disposition") in ("flag_injection", "escalate")
    ]

    return {
        "needs_you": needs_you,
        "can_wait": can_wait,
        "auto_archived_count": len(auto_archived),
        "auto_archived_sample": auto_archived[:5],
        "flagged_count": len(flagged),
    }


def print_digest(digest: dict, msgs_by_id: dict):
    bar = "=" * 60

    print(f"\n{bar}")
    print("  📬  MORNING DIGEST — PaperJet Inbox")
    print(f"{bar}\n")

    # Section 1
    print("── NEEDS YOU ──────────────────────────────────────────")
    if digest["needs_you"]:
        for item in digest["needs_you"]:
            mid = item["id"]
            msg = msgs_by_id.get(mid, {})
            subj = msg.get("subject", "")[:55]
            from_ = msg.get("from", "")
            prio = item.get("priority", "normal").upper()
            reason = item.get("reason", "")
            print(f"  [{prio}] {mid}  {from_}")
            print(f"         \"{subj}\"")
            print(f"         → {reason}\n")
    else:
        print("  (nothing urgent right now)\n")

    # Section 2
    print("── CAN WAIT ────────────────────────────────────────────")
    if digest["can_wait"]:
        for item in digest["can_wait"]:
            mid = item["id"]
            msg = msgs_by_id.get(mid, {})
            subj = msg.get("subject", "")[:55]
            disp = item.get("disposition", "")
            print(f"  {mid}  \"{subj}\"  [{disp}]")
    else:
        print("  (nothing deferred)")
    print()

    # Section 3
    print("── AUTO-ARCHIVED ────────────────────────────────────────")
    count = digest["auto_archived_count"]
    print(f"  {count} message(s) archived automatically (receipts, newsletters, alerts).")
    if digest.get("auto_archived_sample"):
        for item in digest["auto_archived_sample"]:
            mid = item["id"]
            print(f"    • {mid}: {item.get('reason','')[:70]}")
    print()

    if digest["flagged_count"]:
        print(f"── ⚠️  FLAGGED: {digest['flagged_count']} suspicious message(s) ──────────────")
        print("  Run --cap R5 for full injection/phishing report.\n")

    print(bar)
