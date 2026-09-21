"""
Follow-up tracker (Capability X1).

Finds messages that Sam sent but received no reply to within N days,
then drafts a polite chase message.

Algorithm:
  1. Collect all messages sent BY the owner (sam@paperjet.io).
  2. For each, check if the same thread contains any reply arriving
     AFTER the sent message.
  3. If not, and if N days have passed since sending, flag as unanswered.
  4. Draft a chase for each flagged message.
"""

import logging
import time
from datetime import datetime, timezone
from typing import Optional

from . import loader, drafter, llm

log = logging.getLogger("agent.followup")

DEFAULT_DAYS = 3  # flag after this many days with no reply

_CHASE_PROMPT = """\
Sam (sam@paperjet.io) sent this message {days} days ago but received no reply.
Draft a brief, polite follow-up (2-3 sentences) asking if the recipient received
the original message and if there is an update.

Original sent message:
  To: {to}
  Subject: {subject}
  Body: {body}

Reply with JSON ONLY:
{{
  "to": "{to}",
  "cc": [],
  "subject": "Re: {subject_escaped}",
  "body": "<chase message body>",
  "cited_ids": ["{mid}"],
  "needs_approval": true,
  "approval_reason": "Follow-up message — approve before sending."
}}
"""


def find_unanswered(days: int = DEFAULT_DAYS, reference_date: Optional[datetime] = None) -> list[dict]:
    """
    Return list of {message, days_waiting} for sent messages with no reply.
    """
    owner = loader.owner_email()
    msgs = loader.load_inbox()
    threads = loader.by_thread()

    if reference_date is None:
        # Use the latest message timestamp as 'now' (inbox is a snapshot)
        last_ts = max(m["timestamp"] for m in msgs)
        reference_date = datetime.fromisoformat(last_ts)

    unanswered = []
    for msg in msgs:
        if msg.get("from", "").lower() != owner.lower():
            continue
        # Self-notes are not outbound emails
        if msg.get("to", "").lower() == owner.lower():
            continue

        sent_dt = datetime.fromisoformat(msg["timestamp"])
        days_waiting = (reference_date - sent_dt).days
        if days_waiting < days:
            continue

        # Check for any reply in the thread after this message
        thread = threads.get(msg.get("thread_id", msg["id"]), [msg])
        has_reply = any(
            m["timestamp"] > msg["timestamp"] and m.get("from", "").lower() != owner.lower()
            for m in thread
        )
        if not has_reply:
            unanswered.append({
                "message": msg,
                "days_waiting": days_waiting,
                "thread_id": msg.get("thread_id", msg["id"]),
            })

    return unanswered


def draft_chase(item: dict, use_llm: bool = True) -> dict:
    """Draft a chase reply for an unanswered sent message."""
    msg = item["message"]
    days = item["days_waiting"]
    mid = msg["id"]
    subj = msg.get("subject", "")

    if use_llm:
        try:
            prompt = _CHASE_PROMPT.format(
                days=days,
                to=msg.get("to", ""),
                subject=subj,
                body=msg.get("body", "")[:800],
                subject_escaped=subj.replace('"', '\\"'),
                mid=mid,
            )
            result = llm.call_json(prompt)
            result["reply_to_id"] = mid
            result["drafted_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
            return result
        except Exception as exc:
            log.warning("Chase draft failed for %s: %s", mid, exc)

    # Fallback
    return {
        "to": msg.get("to", ""),
        "cc": [],
        "subject": f"Re: {subj}",
        "body": (
            f"Hi, just following up on my message from {days} days ago — "
            f"wanted to check if you received it and if there is any update. Thanks!"
        ),
        "cited_ids": [mid],
        "needs_approval": True,
        "approval_reason": "Follow-up chase — review before sending.",
        "reply_to_id": mid,
        "drafted_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }


def run(days: int = DEFAULT_DAYS, use_llm: bool = True) -> list[dict]:
    """
    Main entry point for X1. Returns list of {message_id, days_waiting, draft}.
    """
    unanswered = find_unanswered(days=days)
    results = []
    for item in unanswered:
        chase = draft_chase(item, use_llm=use_llm)
        results.append({
            "message_id": item["message"]["id"],
            "subject": item["message"].get("subject", ""),
            "sent_to": item["message"].get("to", ""),
            "days_waiting": item["days_waiting"],
            "draft": chase,
        })
    return results
