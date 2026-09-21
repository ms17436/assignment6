"""
Approval gate for irreversible actions.

Every call to send() or delete() MUST pass through this gate.
No other code path can trigger those effects.

Modes:
  interactive  — prompts y/n at the terminal (default)
  dry_run      — prints what would happen, writes nothing
  auto_approve — for testing only; requires AUTO_APPROVE=1 env var

The gate also logs every decision to state/trace.jsonl.

Design rationale (from manifest):
  A prompt injection or miscategorised message can influence a draft,
  but it cannot reach send() without passing require_approval().
  The gate is the single choke-point for irreversible effects.
"""

import json
import logging
import os
import time
from pathlib import Path
from typing import Literal, Optional

log = logging.getLogger("agent.gate")

TRACE_PATH = Path(__file__).parent.parent / "state" / "trace.jsonl"
OUTBOX_DIR = Path(__file__).parent.parent / "outbox"

ActionKind = Literal["send", "delete"]


def _append_trace(event: dict):
    TRACE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(TRACE_PATH, "a") as fh:
        fh.write(json.dumps(event) + "\n")


def require_approval(
    action: ActionKind,
    description: str,
    payload: dict,
    dry_run: bool = False,
) -> bool:
    """
    Present the proposed action to the user and get approval.

    Returns True if approved, False if rejected.
    In dry_run mode always returns False (no effect, no prompt).
    """
    event_base = {
        "cap": "R3",
        "type": "gate",
        "action": action,
        "description": description,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }

    if dry_run:
        print(f"\n[DRY-RUN] Would {action.upper()}: {description}")
        if action == "send":
            print(f"  To:      {payload.get('to', '')}")
            print(f"  CC:      {', '.join(payload.get('cc', []))}")
            print(f"  Subject: {payload.get('subject', '')}")
            print(f"  Body snippet: {payload.get('body', '')[:200]}...")
        _append_trace({**event_base, "decision": "dry_run_skipped"})
        return False

    # Auto-approve mode (tests only)
    if os.environ.get("AUTO_APPROVE") == "1":
        log.warning("AUTO_APPROVE=1 — auto-approving %s. Do not use in production.", action)
        _append_trace({**event_base, "decision": "auto_approved"})
        return True

    # Interactive prompt
    print(f"\n{'='*60}")
    print(f"APPROVAL REQUIRED: {action.upper()}")
    print(f"{'='*60}")
    print(f"Description: {description}")
    if action == "send":
        print(f"To:          {payload.get('to', '')}")
        cc = payload.get("cc", [])
        if cc:
            print(f"CC:          {', '.join(cc)}")
        print(f"Subject:     {payload.get('subject', '')}")
        print(f"\n--- Body ---\n{payload.get('body', '')}\n--- End body ---")
        cited = payload.get("cited_ids", [])
        if cited:
            print(f"Cites messages: {cited}")
    elif action == "delete":
        print(f"Message ID:  {payload.get('id', '')}")
        print(f"Subject:     {payload.get('subject', '')}")

    while True:
        ans = input("\nApprove? [y/N] ").strip().lower()
        if ans in ("y", "yes"):
            _append_trace({**event_base, "decision": "approved"})
            log.info("GATE: %s approved by user.", action)
            return True
        if ans in ("n", "no", ""):
            _append_trace({**event_base, "decision": "rejected"})
            log.info("GATE: %s rejected by user.", action)
            return False
        print("Please enter 'y' or 'n'.")


def send(draft: dict, dry_run: bool = False) -> bool:
    """
    Gate + send (write to outbox/).
    Returns True if the message was actually written to outbox.
    """
    approved = require_approval(
        action="send",
        description=f"Send reply to {draft.get('to','')} re: {draft.get('subject','')}",
        payload=draft,
        dry_run=dry_run,
    )
    if not approved:
        return False

    OUTBOX_DIR.mkdir(parents=True, exist_ok=True)
    mid = draft.get("reply_to_id", "unknown")
    outfile = OUTBOX_DIR / f"sent_{mid}_{int(time.time())}.json"
    with open(outfile, "w") as fh:
        json.dump({**draft, "sent_at": time.strftime("%Y-%m-%dT%H:%M:%S")}, fh, indent=2)
    _append_trace({
        "cap": "R3",
        "type": "gate",
        "action": "send",
        "decision": "sent",
        "outfile": str(outfile),
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
    })
    print(f"[SENT] Written to {outfile}")
    return True


def delete(msg: dict, dry_run: bool = False) -> bool:
    """
    Gate + delete (marks message as deleted in decisions.json).
    We have no actual store to delete from, so this writes a tombstone.
    Returns True if approved.
    """
    approved = require_approval(
        action="delete",
        description=f"Delete message {msg['id']}: {msg.get('subject','')}",
        payload=msg,
        dry_run=dry_run,
    )
    if not approved:
        return False

    tombstone = OUTBOX_DIR / f"deleted_{msg['id']}.json"
    OUTBOX_DIR.mkdir(parents=True, exist_ok=True)
    with open(tombstone, "w") as fh:
        json.dump({"deleted_id": msg["id"], "deleted_at": time.strftime("%Y-%m-%dT%H:%M:%S")}, fh)
    _append_trace({
        "cap": "R3",
        "type": "gate",
        "action": "delete",
        "decision": "deleted",
        "message_id": msg["id"],
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
    })
    print(f"[DELETED] Tombstone written for {msg['id']}")
    return True
