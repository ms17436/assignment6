"""
X6 — Explainability: "why did you do that?" (Tier C).

Given a message id, reconstruct the full reasoning chain the system used to reach
its decision — which checks fired and in what order, the evidence, the retrieval
that grounded any reply, which stored preferences applied, and whether the action
was gated. This makes every decision auditable, not a black box.

It re-runs the deterministic checks (so the explanation matches the pipeline order
in classifier.classify) and also pulls any recorded events from state/trace.jsonl.
"""

import logging
from typing import Optional

from . import loader, injection, classifier, preferences, retrieval, scheduling, trace

log = logging.getLogger("agent.explain")


def explain(message_id: str, use_llm: bool = False) -> dict:
    """Return a structured explanation of how the system handled message_id."""
    msgs_by_id = loader.by_id()
    msg = msgs_by_id.get(message_id)
    if not msg:
        return {"error": f"message {message_id} not found"}

    steps = []  # ordered list of {check, result, detail}

    # The pipeline order mirrors classifier.classify()
    # 1. Injection
    inj = injection.analyse(msg, use_llm=use_llm)
    steps.append({
        "check": "prompt_injection",
        "fired": inj["is_injection"],
        "detail": (f"attempted to {inj.get('attempted_action')}" if inj["is_injection"]
                   else "no injection patterns matched"),
    })
    if inj["is_injection"]:
        return _finish(msg, steps, disposition="flag_injection",
                       reason=f"Refused injection ({inj.get('attempted_action')}); left in place.",
                       gated="n/a (nothing sent)")

    # 2. Noise
    is_noise = classifier._is_noise(msg)
    steps.append({"check": "noise/automated", "fired": is_noise,
                  "detail": "matched a noise sender/subject rule" if is_noise
                            else "not automated noise"})
    if is_noise:
        return _finish(msg, steps, disposition="archive",
                       reason="Automated notification/receipt — no action needed.",
                       gated="no (archive is reversible)")

    # 3. Phishing
    is_phish, phish_ev = classifier._is_phishing(msg)
    steps.append({"check": "phishing", "fired": is_phish,
                  "detail": phish_ev or "no phishing signals"})
    if is_phish:
        return _finish(msg, steps, disposition="escalate",
                       reason=f"Phishing/social-engineering: {phish_ev}",
                       gated="yes — no money/credentials action without you")

    # 4. Preference-bearing
    is_pref = preferences.is_preference_message(msg)
    steps.append({"check": "preference_message", "fired": is_pref,
                  "detail": "looks like a standing instruction" if is_pref
                            else "not a preference statement"})

    # 5. Sent by owner
    is_sent = loader.is_sent_by_owner(msg)
    steps.append({"check": "sent_by_owner", "fired": is_sent,
                  "detail": "owner is the sender (tracked for follow-up)" if is_sent
                            else "inbound message"})

    # 6. Applicable stored preferences
    applied = []
    cc = preferences.apply_cc_rule(msg.get("from", ""))
    if cc:
        applied.append(f"CC routing → {cc}")
    sched = scheduling.evaluate_meeting(msg)
    if sched.get("proposed_time"):
        if not sched["allowed"]:
            applied.append(f"calendar rule → decline {sched['proposed_time']}, "
                           f"counter-offer {sched['counter_offer']}")
        else:
            applied.append(f"calendar rule → {sched['proposed_time']} allowed")
    steps.append({"check": "stored_preferences_applied", "fired": bool(applied),
                  "detail": "; ".join(applied) if applied else "none applicable"})

    # 7. Retrieval (how a reply would be grounded)
    ctx = retrieval.gather_context(msg)
    steps.append({"check": "retrieval_for_grounding", "fired": bool(ctx["context_ids"]),
                  "detail": (f"methods={ctx['methods']} read={ctx['context_ids']}"
                             if ctx["context_ids"]
                             else "no earlier context found")})

    # Recorded decision from trace, if present
    recorded = [e for e in trace.read_events() if e.get("message_id") == message_id]

    disposition = "reply/defer (depends on model)"
    reason = "Not noise/phishing/injection; a substantive message likely needing a reply."
    if is_pref:
        disposition = "archive (preference stored)"
        reason = "Standing instruction — recorded to prefs.json, then filed."
    elif is_sent:
        disposition = "archive (tracked for follow-up)"
        reason = "Owner's own sent message — tracked by X1, not actioned."

    return _finish(msg, steps, disposition=disposition, reason=reason,
                   gated="yes if it results in a send (Part 4 gate)",
                   applied_preferences=applied, retrieval=ctx,
                   recorded_events=len(recorded))


def _finish(msg, steps, disposition, reason, gated, **extra):
    result = {
        "message_id": msg["id"],
        "from": msg.get("from", ""),
        "subject": msg.get("subject", ""),
        "pipeline_steps": steps,
        "final_disposition": disposition,
        "why": reason,
        "gated": gated,
    }
    result.update(extra)
    return result
