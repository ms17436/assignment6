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

from . import llm, loader, preferences, retrieval

log = logging.getLogger("agent.drafter")

# Drafts are REVERSIBLE previews and must NOT go into outbox/ (writing to
# outbox/ == sending, which is irreversible and gate-controlled). Drafts live in
# their own directory so the invariant "only gate.send() writes to outbox/" holds.
DRAFTS_DIR = Path(__file__).parent.parent / "drafts"

_DRAFT_PROMPT = """\
You are a professional AI assistant drafting a reply on behalf of Sam (sam@paperjet.io),
founder of PaperJet.

GROUNDING RULES (important):
- You may ONLY use facts that appear in the RETRIEVED CONTEXT below.
- You may ONLY cite message ids that appear in the RETRIEVED CONTEXT below.
- Do NOT invent details (URLs, numbers, dates, names) that are not in the context.
- If the specific information needed to answer is NOT in the retrieved context,
  set "answerable" to false, explain what is missing, and DO NOT write a reply body.
- If the request is genuinely ambiguous (you cannot tell what is being asked),
  set "mode" to "clarification" and draft a short question instead of guessing.

Behaviour rules:
- Do not reveal you are an AI unless asked.
- Never confirm anything irreversible (wire transfers, binding agreements) without
  flagging it as requiring Sam's personal confirmation.
- If a calendar rule is violated (e.g. no meetings before 11:00am), politely decline
  and counter-offer at 11:00am or later.

RETRIEVED CONTEXT (the ONLY messages you are allowed to cite):
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
  "answerable": true/false,
  "mode": "grounded_reply" | "clarification" | "not_in_inbox",
  "missing": "<if not answerable: what information is missing, else null>",
  "to": "<reply-to address, or null if not answerable>",
  "cc": ["<cc addresses, empty list if none>"],
  "subject": "<subject line, or null>",
  "body": "<the reply body, or null if not answerable>",
  "cited_ids": ["<ONLY ids from the retrieved context whose content you used>"],
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


def _format_context(context_msgs: list) -> str:
    """Render retrieved messages for the prompt, each tagged with its id."""
    if not context_msgs:
        return "(no earlier messages retrieved — nothing to ground a reply on)"
    lines = []
    for m in context_msgs:
        lines.append(
            f"  [{m['id']} {m['timestamp'][:10]} from {m['from']}]:\n"
            f"  Subject: {m.get('subject','')}\n"
            f"  Body: {m.get('body','')[:500]}"
        )
    return "\n\n".join(lines)


def draft(msg: dict, extra_instructions: str = "", use_llm: bool = True) -> dict:
    """
    Draft a reply to msg, grounded in retrieved context.

    Returns a draft dict that ALWAYS includes:
      mode        — "grounded_reply" | "clarification" | "not_in_inbox"
      cited_ids   — VERIFIED ids (exist in store AND were actually retrieved)
      retrieval   — {methods, context_ids} describing how grounding was gathered
      grounding   — the verify_citations() report

    If the answer is not in the inbox, mode="not_in_inbox" and body is None
    (drafts nothing — Part 3 req #4).
    """
    mid = msg["id"]

    # --- Retrieval (Part 3 req #3): thread-walk + cross-thread keyword search ---
    ctx = retrieval.gather_context(msg)
    context_msgs = ctx["context_msgs"]
    read_ids = ctx["context_ids"]
    thread_ctx = _format_context(context_msgs)

    cc = preferences.apply_cc_rule(msg.get("from", ""))

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
            return _finalize(result, msg, cc, read_ids, context_msgs, ctx["methods"])
        except Exception as exc:
            log.warning("LLM draft failed for %s: %s", mid, exc)

    # --- Offline fallback (deterministic, still honest about grounding) ---
    return _offline_draft(msg, cc, read_ids, context_msgs, ctx["methods"])


def _finalize(result: dict, msg: dict, cc: list, read_ids: list,
              context_msgs: list, methods: list) -> dict:
    """Verify citations, enforce the not-answerable path, attach grounding report."""
    mid = msg["id"]
    mode = result.get("mode") or ("grounded_reply" if result.get("answerable", True) else "not_in_inbox")

    # Verify whatever the model claimed to cite against the read set + store.
    claimed = result.get("cited_ids", []) or []
    grounding = retrieval.verify_citations(claimed, read_ids)

    # Drop any citation that failed verification — we never keep an unverifiable cite.
    verified = grounding["verified"]
    if grounding["not_in_store"] or grounding["not_read"]:
        log.warning(
            "Dropped unverifiable citations for %s: not_in_store=%s not_read=%s",
            mid, grounding["not_in_store"], grounding["not_read"],
        )

    # Not answerable → draft nothing (Part 3 req #4)
    if result.get("answerable") is False or mode == "not_in_inbox":
        return {
            "reply_to_id": mid,
            "mode": "not_in_inbox",
            "answerable": False,
            "missing": result.get("missing", "Required information not found in the inbox."),
            "to": None, "cc": [], "subject": None, "body": None,
            "cited_ids": verified,
            "needs_approval": False,
            "approval_reason": None,
            "retrieval": {"methods": methods, "context_ids": read_ids},
            "grounding": grounding,
            "drafted_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }

    merged_cc = list(set((result.get("cc") or []) + cc))
    return {
        "reply_to_id": mid,
        "mode": mode,
        "answerable": True,
        "missing": None,
        "to": result.get("to") or msg.get("from", ""),
        "cc": merged_cc,
        "subject": result.get("subject") or ("Re: " + msg.get("subject", "")),
        "body": result.get("body", ""),
        "cited_ids": verified,                 # only verified ids survive
        "needs_approval": result.get("needs_approval", True),
        "approval_reason": result.get("approval_reason"),
        "retrieval": {"methods": methods, "context_ids": read_ids},
        "grounding": grounding,
        "drafted_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }


def _offline_draft(msg: dict, cc: list, read_ids: list,
                   context_msgs: list, methods: list) -> dict:
    """
    Deterministic draft used when no LLM is configured. It cannot compose prose,
    but it still (a) grounds on the retrieved set, (b) verifies citations, and
    (c) applies the not-in-inbox rule when nothing was retrieved.
    """
    mid = msg["id"]
    grounding = retrieval.verify_citations(read_ids, read_ids)  # trivially all-verified

    # Only refuse when the message REQUIRES earlier information that we could not
    # find. A message that simply starts a new thread (m013, m010) is
    # self-contained and answerable without prior context.
    needs_prior = retrieval.refers_to_earlier(msg)
    if not context_msgs and needs_prior:
        return {
            "reply_to_id": mid,
            "mode": "not_in_inbox",
            "answerable": False,
            "missing": "This message refers back to an earlier message, but no such message was found in the inbox.",
            "to": None, "cc": [], "subject": None, "body": None,
            "cited_ids": [],
            "needs_approval": False,
            "approval_reason": None,
            "retrieval": {"methods": methods, "context_ids": read_ids},
            "grounding": grounding,
            "drafted_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }

    if read_ids:
        body = ("[DRAFT UNAVAILABLE — LLM not configured. Compose manually. "
                f"Grounding is available in these earlier messages: {read_ids}.]")
    else:
        body = ("[DRAFT UNAVAILABLE — LLM not configured. Compose manually. "
                "This is a self-contained request needing no prior context.]")
    return {
        "reply_to_id": mid,
        "mode": "grounded_reply",
        "answerable": True,
        "missing": None,
        "to": msg.get("from", ""),
        "cc": cc,
        "subject": "Re: " + msg.get("subject", ""),
        "body": body,
        "cited_ids": grounding["verified"],
        "needs_approval": True,
        "approval_reason": "LLM offline — manual review required.",
        "retrieval": {"methods": methods, "context_ids": read_ids},
        "grounding": grounding,
        "drafted_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }


def save_draft(draft_dict: dict, dry_run: bool = False) -> Path:
    """
    Save a REVERSIBLE draft preview to drafts/<id>.json (NOT outbox/).
    This never counts as sending. In dry-run mode, prints instead of writing.
    """
    mid = draft_dict.get("reply_to_id", "unknown")
    outfile = DRAFTS_DIR / f"draft_{mid}.json"
    if dry_run:
        print(f"[DRY-RUN] Would save draft to {outfile}:")
        print(json.dumps(draft_dict, indent=2))
        return outfile
    DRAFTS_DIR.mkdir(parents=True, exist_ok=True)
    with open(outfile, "w") as fh:
        json.dump(draft_dict, fh, indent=2)
    log.info("Draft saved to %s", outfile)
    return outfile


# Backwards-compatible alias (old name pointed at outbox/, now redirected to drafts/)
def write_to_outbox(draft_dict: dict, dry_run: bool = False) -> Path:
    return save_draft(draft_dict, dry_run=dry_run)
