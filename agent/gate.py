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
) -> str:
    """
    Present the proposed action to the user and return the DECISION as a string:
      "dry_run"  — dry-run mode: shown, not performed
      "approved" — human said yes (or AUTO_APPROVE=1)
      "rejected" — human said no

    This function only decides. The caller performs the effect and writes the
    consolidated trace record, so every gated decision is logged exactly once
    with proposal + decision + outcome (Part 4 req #4).
    """
    if dry_run:
        print(f"\n[DRY-RUN] Would {action.upper()}: {description}")
        if action == "send":
            print(f"  To:      {payload.get('to', '')}")
            cc = payload.get("cc", [])
            if cc:
                print(f"  CC:      {', '.join(cc)}")
            print(f"  Subject: {payload.get('subject', '')}")
            body = payload.get("body") or ""
            print(f"  Body snippet: {body[:200]}{'...' if len(body) > 200 else ''}")
        elif action == "delete":
            print(f"  Message: {payload.get('id','')} — {payload.get('subject','')}")
        return "dry_run"

    if os.environ.get("AUTO_APPROVE") == "1":
        log.warning("AUTO_APPROVE=1 — auto-approving %s. Testing only.", action)
        return "approved"

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
            log.info("GATE: %s approved by user.", action)
            return "approved"
        if ans in ("n", "no", ""):
            log.info("GATE: %s rejected by user.", action)
            return "rejected"
        print("Please enter 'y' or 'n'.")


def _log_gate(action: str, proposed: dict, decision: str, outcome: str,
              outfile: Optional[str] = None):
    """Write ONE consolidated record: what was proposed, decided, and what happened."""
    _append_trace({
        "cap": "R3",
        "type": "gate",
        "action": action,
        "proposed": proposed,
        "human_decision": decision,   # dry_run | approved | rejected
        "outcome": outcome,           # sent | deleted | not_performed
        "outfile": outfile,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
    })


def send(draft: dict, dry_run: bool = False) -> bool:
    """
    Gate + send. Writing to outbox/ IS sending; this is the ONLY function that
    does it. One file per message: outbox/sent_<message_id>.json.
    Returns True iff the message was actually written to outbox.
    """
    mid = draft.get("reply_to_id", "unknown")
    proposed = {
        "to": draft.get("to", ""),
        "cc": draft.get("cc", []),
        "subject": draft.get("subject", ""),
        "reply_to_id": mid,
        "cited_ids": draft.get("cited_ids", []),
        "body_len": len(draft.get("body") or ""),
    }

    decision = require_approval(
        action="send",
        description=f"Send reply to {draft.get('to','')} re: {draft.get('subject','')}",
        payload=draft,
        dry_run=dry_run,
    )

    if decision != "approved":
        _log_gate("send", proposed, decision, outcome="not_performed")
        return False

    OUTBOX_DIR.mkdir(parents=True, exist_ok=True)
    outfile = OUTBOX_DIR / f"sent_{mid}.json"   # deterministic: one file per message
    with open(outfile, "w") as fh:
        json.dump({**draft, "sent_at": time.strftime("%Y-%m-%dT%H:%M:%S")}, fh, indent=2)
    _log_gate("send", proposed, decision, outcome="sent", outfile=str(outfile))
    print(f"[SENT] Written to {outfile}")
    return True


def delete(msg: dict, dry_run: bool = False) -> bool:
    """
    Gate + delete. Irreversible in this design (see manifest): the mock store has
    no trash, so a delete cannot be undone. Writes a tombstone to outbox/.
    Returns True iff the delete was performed.
    """
    proposed = {"id": msg.get("id", ""), "subject": msg.get("subject", "")}

    decision = require_approval(
        action="delete",
        description=f"Delete message {msg.get('id','')}: {msg.get('subject','')}",
        payload=msg,
        dry_run=dry_run,
    )

    if decision != "approved":
        _log_gate("delete", proposed, decision, outcome="not_performed")
        return False

    OUTBOX_DIR.mkdir(parents=True, exist_ok=True)
    tombstone = OUTBOX_DIR / f"deleted_{msg['id']}.json"
    with open(tombstone, "w") as fh:
        json.dump({"deleted_id": msg["id"], "deleted_at": time.strftime("%Y-%m-%dT%H:%M:%S")}, fh)
    _log_gate("delete", proposed, decision, outcome="deleted", outfile=str(tombstone))
    print(f"[DELETED] Tombstone written for {msg['id']}")
    return True


def audit_outbox() -> dict:
    """
    Verify the outbox invariant: it may contain ONLY files this gate wrote
    (sent_*.json / deleted_*.json), and at most one sent file per message.
    Returns a report dict.
    """
    if not OUTBOX_DIR.exists():
        return {"ok": True, "sent": [], "deleted": [], "unexpected": []}
    sent, deleted, unexpected = [], [], []
    for f in sorted(OUTBOX_DIR.iterdir()):
        if f.name.startswith("sent_") and f.suffix == ".json":
            sent.append(f.name)
        elif f.name.startswith("deleted_") and f.suffix == ".json":
            deleted.append(f.name)
        elif f.name == ".gitkeep":
            continue
        else:
            unexpected.append(f.name)
    return {"ok": not unexpected, "sent": sent, "deleted": deleted, "unexpected": unexpected}
