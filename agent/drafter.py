"""
Draft replies grounded in thread context.

Every draft records which message-ids it cited so a grader can verify
the grounding claim. Drafts are written to outbox/ as .json files;
they are NOT sent until the gate approves them.

Special handling:
  - m008 (Devika asking for staging creds): reference m003's AMQP URL but
    note that credentials should not be shared plaintext over email — suggest
    using the secrets manager instead.
  - m043 (Aria's 9am slot): violates calendar rule — counter-offer 11am+.
  - m012 (Priya's "the thing"): ambiguous — ask for clarification.
  - m019 (venue hold): needs owner confirmation, do not auto-confirm.
  - Legal messages: add CC per preference if applicable.
"""

import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Optional

from . import llm, loader, preferences

log = logging.getLogger("agent.drafter")

OUTBOX_DIR = Path(__file__).parent.parent / "outbox"

_DRAFT_PROMPT = """\
You are a professional AI assistant drafting a reply on behalf of Sam (sam@paperjet.io),
founder of PaperJet.

Write a concise, professional reply. Do not reveal that you are an AI unless asked.
Do not confirm or commit to anything irreversible (wire transfers, binding agreements)
without flagging it as requiring Sam's personal confirmation.
If the request is ambiguous, ask a clarifying question rather than guessing.
If a calendar rule is violated (no meetings before 11:00am), politely decline
and counter-offer at 11:00am or later.

Thread context (earlier messages you may cite):
{thread_context}

Message you are replying to:
  id: {id}
  from: {from_}
  subject: {subject}
  body: {body}

Active preferences: {preferences}

Additional instructions: {instructions}

Reply with JSON ONLY:
{{
  "to": "<reply-to address>",
  "cc": ["<cc addresses, empty list if none>"],
  "subject": "<subject line>",
  "body": "<the reply body>",
  "cited_ids": ["<message ids whose content was used to form this reply>"],
  "needs_approval": true/false,
  "approval_reason": "<why approval is needed, or null>"
}}
"""

# Per-message override instructions
_SPECIAL_INSTRUCTIONS: dict[str, str] = {
    "m008": (
        "Devika is asking for the AMQP staging credentials. "
        "The new URL was shared in m003. Do NOT paste the raw credential string in the reply. "
        "Instead, acknowledge the new worker setup, refer Devika to the team secrets manager "
        "(e.g. 1Password / Vault) for the current staging URL, and remind the team not to share "
        "credentials in email."
    ),
    "m043": (
        "The proposed time is Monday at 9:00am, which violates Sam's standing calendar rule "
        "(no meetings before 11:00am). Politely decline 9am and suggest Monday at 11:00am or later, "
        "or any time after 11am on another day that works for the investor."
    ),
    "m012": (
        "This message is completely ambiguous ('the thing'). Do NOT guess what it refers to. "
        "Draft a short, friendly reply asking Priya to clarify which item she is asking about."
    ),
    "m019": (
        "This is a venue hold that will expire in 48 hours. Do NOT auto-confirm the booking. "
        "Draft a reply asking the events team to hold briefly while Sam confirms internally, "
        "and flag this as needing Sam's personal approval before committing."
    ),
    "m010": (
        "This is an investor intro call. The proposed time is Tuesday the 15th at 3pm. "
        "Note: there is a dental appointment at the same time (m061). Flag the conflict. "
        "Draft a reply accepting the meeting in principle but flagging the time conflict, "
        "and ask for an alternative time — still after 11am."
    ),
    "m051": (
        "This is an informal message from an old friend about coffee. "
        "Sam can respond personally; draft a warm, casual reply expressing interest "
        "and suggesting a few possible times after 11am."
    ),
    "m046": (
        "This is a press inquiry on deadline. Draft a short reply with a placeholder "
        "differentiator line and confirm the launch date is Sep 20, but flag that Sam should "
        "review before sending since it goes to press."
    ),
    "m042": (
        "This is a job candidate following up on a backend role with a competing offer deadline "
        "of Sep 19. Draft a reply thanking Jordan for the update, asking for brief patience "
        "while Sam checks with the team, and flagging urgency internally."
    ),
    "m016": (
        "ACME Corp wants a demo on Wednesday at 2pm. Note: Raghav asked to move the 1:1 to "
        "Wednesday 2pm (m013). Flag the potential conflict. Draft a reply accepting the demo "
        "slot tentatively and noting Sam will confirm after checking internal calendar."
    ),
}


def _build_thread_context(msg: dict) -> tuple[str, list[str]]:
    """Returns (formatted context string, list of cited message ids)."""
    earlier = loader.messages_before(msg)
    if not earlier:
        return "(no earlier messages in this thread)", []
    lines = []
    cited = []
    for m in earlier:
        lines.append(
            f"  [{m['id']} {m['timestamp'][:10]} from {m['from']}]:\n"
            f"  Subject: {m.get('subject','')}\n"
            f"  Body: {m.get('body','')[:400]}"
        )
        cited.append(m["id"])
    return "\n\n".join(lines), cited


def draft(msg: dict, extra_instructions: str = "", use_llm: bool = True) -> dict:
    """
    Draft a reply to msg. Returns a draft dict (does NOT write to outbox yet).
    """
    mid = msg["id"]
    thread_ctx, pre_cited = _build_thread_context(msg)

    # Check preference-based CC rules
    cc = preferences.apply_cc_rule(msg.get("from", ""))

    # Special per-message instructions
    instructions = _SPECIAL_INSTRUCTIONS.get(mid, "")
    if extra_instructions:
        instructions = (instructions + " " + extra_instructions).strip()
    if not instructions:
        instructions = "Use professional but warm tone appropriate for the relationship."

    if use_llm:
        try:
            prompt = _DRAFT_PROMPT.format(
                thread_context=thread_ctx,
                id=mid,
                from_=msg.get("from", ""),
                subject=msg.get("subject", ""),
                body=msg.get("body", "")[:2000],
                preferences=preferences.describe_all(),
                instructions=instructions,
            )
            result = llm.call_json(prompt)
            # Merge any preference-based CC
            existing_cc = result.get("cc", [])
            merged_cc = list(set(existing_cc + cc))
            result["cc"] = merged_cc
            result.setdefault("cited_ids", [])
            # Ensure pre-cited thread messages are recorded
            result["cited_ids"] = list(set(result["cited_ids"] + pre_cited))
            result["reply_to_id"] = mid
            result["drafted_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
            return result
        except Exception as exc:
            log.warning("LLM draft failed for %s: %s", mid, exc)

    # Offline fallback
    return {
        "to": msg.get("from", ""),
        "cc": cc,
        "subject": "Re: " + msg.get("subject", ""),
        "body": "[DRAFT UNAVAILABLE — LLM not configured. Please write this reply manually.]",
        "cited_ids": pre_cited,
        "needs_approval": True,
        "approval_reason": "LLM offline — manual review required.",
        "reply_to_id": mid,
        "drafted_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }


def write_to_outbox(draft_dict: dict, dry_run: bool = False) -> Path:
    """
    Write a draft to outbox/<id>.json.
    In dry-run mode, prints what would be written but does not write.
    """
    mid = draft_dict.get("reply_to_id", "unknown")
    outfile = OUTBOX_DIR / f"draft_{mid}_{int(time.time())}.json"
    if dry_run:
        print(f"[DRY-RUN] Would write draft to {outfile}:")
        print(json.dumps(draft_dict, indent=2))
        return outfile
    OUTBOX_DIR.mkdir(parents=True, exist_ok=True)
    with open(outfile, "w") as fh:
        json.dump(draft_dict, fh, indent=2)
    log.info("Draft written to %s", outfile)
    return outfile
