#!/usr/bin/env python3
"""
PaperJet Inbox Agent — demo entry point.

Usage:
  python demo.py --cap R1              # triage every message
  python demo.py --cap R2 --msg m008  # draft a grounded reply to a specific message
  python demo.py --cap R3 --dry-run   # show what would be sent, write nothing
  python demo.py --cap R4             # extract + persist standing preferences
  python demo.py --cap R5             # detect and report all prompt injections
  python demo.py --cap R6             # generate dashboard.html + dashboard.json
  python demo.py --cap X1             # follow-up tracker
  python demo.py --cap X2             # morning digest
  python demo.py --all                # run all capabilities in order

Environment variables:
  GEMINI_API_KEY   — Google Gemini API key
  OPENAI_API_KEY   — OpenAI-compatible key (set OPENAI_BASE_URL for local models)
  LLM_OFFLINE=1    — disable LLM, use heuristics only
  LLM_CALL_DELAY   — seconds between LLM calls (default 2)
  AUTO_APPROVE=1   — auto-approve all gate prompts (testing only)
"""

import argparse
import json
import logging
import os
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("demo")

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent))

from agent import loader, classifier, injection, preferences, drafter, gate, trace
from agent import followup, digest as digest_mod, dashboard as dash_mod, commitments


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _save_decisions(decisions: list[dict]):
    state_path = Path(__file__).parent / "state" / "decisions.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    with open(state_path, "w") as fh:
        json.dump(decisions, fh, indent=2)


def _load_decisions() -> list[dict]:
    state_path = Path(__file__).parent / "state" / "decisions.json"
    if state_path.exists() and state_path.stat().st_size > 2:
        with open(state_path) as fh:
            return json.load(fh)
    return []


# ---------------------------------------------------------------------------
# Capabilities
# ---------------------------------------------------------------------------

def cap_r1(use_llm: bool = True):
    """R1 — Zero the inbox: assign every message exactly one disposition."""
    print("\n" + "=" * 60)
    print("R1: Zero the Inbox")
    print("=" * 60)

    from agent import llm as llm_mod
    llm_mod.reset_call_count()

    msgs = loader.load_inbox()
    decisions = classifier.classify_all(msgs, use_llm=use_llm)
    _save_decisions(decisions)

    # Print table
    print(f"\n{'ID':<8}{'PRIORITY':<10}{'DISPOSITION':<20}{'REASON'}")
    print("-" * 90)
    for d in decisions:
        mid = d.get("id", "")
        prio = d.get("priority", "normal")
        disp = d.get("disposition", "")
        reason = d.get("reason", "")[:60]
        print(f"{mid:<8}{prio:<10}{disp:<20}{reason}")

    # Tally by disposition
    from collections import Counter
    tally = Counter(d.get("disposition") for d in decisions)
    print("\n── Dispositions ────────────────────────")
    for disp, count in sorted(tally.items()):
        print(f"  {disp:<25} {count}")

    # Routing breakdown: how each disposition was reached (rule vs model)
    route = Counter(d.get("handled_by", "unknown") for d in decisions)
    rule_routed = sum(c for k, c in route.items() if k.startswith("rule"))
    llm_routed = route.get("llm", 0)
    print("\n── Routing (how the decision was made) ──")
    for k, c in sorted(route.items()):
        print(f"  {k:<25} {c}")

    # The headline Part 2 numbers.
    # "Never need a model" == rule-routed: these are decided by rules and would
    # not touch the LLM even when one is configured. This is the stable Part 2
    # answer (independent of offline mode). "Actual LLM calls" is the live count
    # of real model calls this run (0 in offline/--no-llm mode).
    total = len(decisions)
    actual_llm_calls = llm_mod.call_count()
    to_model = total - rule_routed
    offline_note = "  (offline / --no-llm)" if actual_llm_calls == 0 and to_model > 0 else ""
    print("\n── Part 2 headline numbers ─────────────")
    print(f"  Total messages:                    {total}")
    print(f"  Rule-routed (never need a model):  {rule_routed}  ({100*rule_routed//total}%)")
    print(f"  Routed to the model:               {to_model}")
    print(f"  Actual LLM calls this run:         {actual_llm_calls}{offline_note}")

    undecided = sum(1 for d in decisions if not d.get("disposition"))
    print(f"\nundecided: {undecided}")

    # Trace
    for d in decisions:
        trace.log_event("R1", "decision", message_id=d["id"],
                        disposition=d.get("disposition"), priority=d.get("priority"),
                        handled_by=d.get("handled_by", "unknown"),
                        reason=d.get("reason", ""))

    return decisions


def cap_r2(msg_id: str, use_llm: bool = True, dry_run: bool = False):
    """R2 — Grounded reply: draft citing the earlier messages used."""
    print("\n" + "=" * 60)
    print(f"R2: Grounded Reply — {msg_id}")
    print("=" * 60)

    msgs_by_id = loader.by_id()
    if msg_id not in msgs_by_id:
        print(f"ERROR: message {msg_id} not found in inbox.")
        return

    msg = msgs_by_id[msg_id]

    # --- Retrieval (Part 3 req #3) ---
    from agent import retrieval
    ctx = retrieval.gather_context(msg)
    read_ids = ctx["context_ids"]
    methods = ctx["methods"]

    print(f"\nReplying to {msg_id} from {msg.get('from','')}: \"{msg.get('subject','')}\"")
    print(f"Retrieval method(s): {', '.join(methods) if methods else 'none'}")
    print(f"Retrieved / read: {read_ids or '(nothing)'}")

    # Log a 'read' event for every message we actually retrieved
    for m in ctx["context_msgs"]:
        trace.log_event("R2", "read", message_id=m["id"], role="context",
                        subject=m.get("subject", ""))

    draft = drafter.draft(msg, use_llm=use_llm)
    mode = draft.get("mode", "grounded_reply")

    # --- Part 3 req #4: information not in inbox → draft nothing ---
    if mode == "not_in_inbox" or draft.get("answerable") is False:
        print("\n🚫 NOT ANSWERABLE FROM INBOX — no draft produced.")
        print(f"   Missing: {draft.get('missing','(unspecified)')}")
        trace.log_event("R2", "no_draft", message_id=msg_id,
                        reason="not_in_inbox", missing=draft.get("missing", ""))
        return draft

    if mode == "clarification":
        print("\n❓ AMBIGUOUS — drafting a clarifying question instead of guessing.")

    print(f"\nReply to:  {draft.get('to', '')}")
    cc = draft.get("cc", [])
    if cc:
        print(f"CC:        {', '.join(cc)}")
    print(f"Subject:   {draft.get('subject', '')}")
    print(f"\nBody:\n{draft.get('body', '')}")

    # --- Part 3 req #2: citations verified against the mail store + read set ---
    g = draft.get("grounding", {})
    cited = draft.get("cited_ids", [])
    print(f"\ncited (verified): {cited}")
    print(f"grounding check: ok={g.get('ok')} "
          f"verified={g.get('verified')} "
          f"not_in_store={g.get('not_in_store')} "
          f"not_read={g.get('not_read')}")

    if draft.get("needs_approval"):
        print(f"\n⚠️  Approval required: {draft.get('approval_reason','')}")

    trace.log_event("R2", "draft", message_id=msg_id, mode=mode,
                    cited_ids=cited, grounding_ok=g.get("ok"),
                    retrieval_methods=methods,
                    needs_approval=draft.get("needs_approval"))

    if not dry_run:
        try:
            ans = input("\nSend this draft? [y/N] ").strip().lower()
            if ans in ("y", "yes"):
                gate.send(draft)
        except (KeyboardInterrupt, EOFError):
            pass
    else:
        drafter.write_to_outbox(draft, dry_run=True)

    return draft


def cap_r3(use_llm: bool = True, dry_run: bool = False):
    """R3 — Gate the irreversible: show all sends/deletes, require approval."""
    print("\n" + "=" * 60)
    print("R3: Gate the Irreversible")
    print("=" * 60)

    if dry_run:
        print("[DRY-RUN MODE] No messages will be sent or deleted.\n")

    # Get or compute decisions
    decisions = _load_decisions() or classifier.classify_all(loader.load_inbox(), use_llm=use_llm)

    reply_msgs = [d for d in decisions if d.get("disposition") == "reply"]
    print(f"Messages that would require a send: {len(reply_msgs)}")

    for d in reply_msgs:
        mid = d["id"]
        msgs_by_id = loader.by_id()
        msg = msgs_by_id.get(mid)
        if not msg:
            continue
        draft = drafter.draft(msg, use_llm=use_llm)
        gate.send(draft, dry_run=dry_run)
        trace.log_event("R3", "gate", action="send", message_id=mid, dry_run=dry_run)

    outbox = list(Path("outbox").glob("sent_*.json"))
    print(f"\noutbox/ writes: {len(outbox) if not dry_run else 0}")
    return decisions


def cap_r4(use_llm: bool = True):
    """R4 — Persistent preferences: extract, store, and demonstrate across restarts."""
    print("\n" + "=" * 60)
    print("R4: Persistent Preferences")
    print("=" * 60)

    msgs = loader.load_inbox()
    pref_msgs = [m for m in msgs if preferences.is_preference_message(m)]
    print(f"\nFound {len(pref_msgs)} preference-bearing message(s):")

    for m in pref_msgs:
        print(f"  {m['id']}: \"{m.get('subject','')}\"")
        # SECURITY: never turn a hostile message into a stored preference.
        # e.g. m039 ("assistant settings … enable autonomous mode") is an
        # injection, not a genuine user preference.
        inj = injection.analyse(m, use_llm=use_llm)
        if inj["is_injection"]:
            print(f"    ⛔ REFUSED: looks like a prompt-injection, not a preference "
                  f"({inj['evidence']}). Not stored.")
            trace.log_event("R4", "refusal", message_id=m["id"],
                            reason="injection masquerading as preference",
                            evidence=inj["evidence"])
            continue
        pref = preferences.extract_and_store(m, use_llm=use_llm)
        if pref:
            print(f"    → Stored [{pref['id']}]: {pref['description']}")
            trace.log_event("R4", "preference", message_id=m["id"],
                            pref_id=pref["id"], description=pref["description"])

    print("\nAll stored preferences (from state/prefs.json):")
    print(preferences.describe_all())

    print("\n--- Demonstrating preference application ---")
    # Calendar rule
    for time_str, label in [("09:00", "9:00am (before cutoff)"), ("11:00", "11:00am"), ("14:00", "2pm")]:
        allowed = preferences.apply_calendar_rule(time_str)
        verdict = "✅ allowed" if allowed else "❌ BLOCKED (counter-offer 11am+)"
        print(f"  Meeting at {time_str} ({label}): {verdict}")

    # CC rule
    legal_from = "m.cho@hartwellcho.com"
    cc = preferences.apply_cc_rule(legal_from)
    print(f"\n  Email from {legal_from} → auto-CC: {cc or '(none)'}")


def cap_r5(use_llm: bool = True):
    """R5 — Refuse embedded instructions: detect, refuse, flag, report."""
    print("\n" + "=" * 60)
    print("R5: Refuse Embedded Instructions")
    print("=" * 60)

    msgs = loader.load_inbox()
    flagged = injection.scan_inbox(msgs, use_llm=use_llm)

    if not flagged:
        print("No prompt injections detected.")
        return flagged

    print(f"\nFlagged {len(flagged)} message(s) containing injection attempts:\n")
    for inj in flagged:
        mid = inj["message_id"]
        msgs_by_id = loader.by_id()
        msg = msgs_by_id.get(mid, {})
        print(f"  FLAGGED: {mid}")
        print(f"  Subject: {msg.get('subject','')}")
        print(f"  From:    {msg.get('from','')}")
        print(f"  Evidence: {inj['evidence']}")
        print(f"  Confidence: {inj['confidence']}")
        print(f"  What was refused: {inj['action']}")
        print()
        trace.log_event("R5", "refusal", message_id=mid,
                        evidence=inj["evidence"], confidence=inj["confidence"])

    # Confirm outbox is clean
    outbox = list(Path("outbox").glob("*.json")) if Path("outbox").exists() else []
    external_sends = []
    for f in outbox:
        try:
            with open(f) as fh:
                d = json.load(fh)
            to = d.get("to", "")
            if "archive" not in str(f) and "ext-audit" in to or "mail-backup" in to:
                external_sends.append(str(f))
        except Exception:
            pass

    if external_sends:
        print(f"⚠️  WARNING: unexpected external sends detected: {external_sends}")
    else:
        print("✅ outbox/ contains no messages to any injection-specified external address.")

    return flagged


def cap_r6(decisions: list[dict] = None, use_llm: bool = True):
    """R6 — Dashboard: three-pane view of inbox state."""
    print("\n" + "=" * 60)
    print("R6: Dashboard")
    print("=" * 60)

    msgs = loader.load_inbox()
    if not decisions:
        decisions = _load_decisions() or classifier.classify_all(msgs, use_llm=use_llm)

    flagged = injection.scan_inbox(msgs, use_llm=False)  # fast pass, no LLM

    board = dash_mod.build(decisions, flagged, use_llm=False)
    html_path, json_path = dash_mod.write(board)

    print(f"\nDashboard written:")
    print(f"  HTML → {html_path}")
    print(f"  JSON → {json_path}")

    s = board["summary"]
    print(f"\nSummary:")
    print(f"  Total messages:  {s['total_messages']}")
    print(f"  Pending actions: {s['pending_actions']}")
    print(f"  Flagged:         {s['flagged']}")
    print(f"  Auto-archived:   {s['archived']}")
    print(f"  Commitments:     {s['commitments']}")
    print(f"  Conflicts:       {s['conflicts']}")

    trace.log_event("R6", "dashboard", html=str(html_path), json=str(json_path))
    return board


def cap_x1(days: int = 3, use_llm: bool = True):
    """X1 — Follow-up tracking: unanswered sent mail + drafted chase."""
    print("\n" + "=" * 60)
    print("X1: Follow-up Tracker")
    print("=" * 60)

    results = followup.run(days=days, use_llm=use_llm)

    if not results:
        print(f"No unanswered sent messages older than {days} days.")
        return results

    print(f"\nFound {len(results)} unanswered sent message(s):\n")
    for r in results:
        print(f"  {r['message_id']}  \"{r['subject']}\"")
        print(f"  Sent to: {r['sent_to']}   Waiting: {r['days_waiting']} days")
        draft = r["draft"]
        print(f"  Chase draft:\n{draft.get('body','')[:300]}")
        print()
        trace.log_event("X1", "followup", message_id=r["message_id"],
                        days_waiting=r["days_waiting"])

    print(json.dumps(results, indent=2))
    return results


def cap_x2(decisions: list[dict] = None, use_llm: bool = True):
    """X2 — Morning digest: what needs me / what can wait / what was archived."""
    print("\n" + "=" * 60)
    print("X2: Morning Digest")
    print("=" * 60)

    msgs = loader.load_inbox()
    if not decisions:
        decisions = _load_decisions() or classifier.classify_all(msgs, use_llm=use_llm)

    dg = digest_mod.build(decisions)
    digest_mod.print_digest(dg, loader.by_id())

    trace.log_event("X2", "digest",
                    needs_you=len(dg["needs_you"]),
                    can_wait=len(dg["can_wait"]),
                    auto_archived=dg["auto_archived_count"])
    return dg


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="PaperJet Inbox Agent demo")
    parser.add_argument("--cap", help="Capability to run (R1, R2, R3, R4, R5, R6, X1, X2)")
    parser.add_argument("--msg", help="Message ID for --cap R2", default="m008")
    parser.add_argument("--dry-run", action="store_true", help="Show what would happen, write nothing")
    parser.add_argument("--no-llm", action="store_true", help="Disable LLM (offline/heuristic mode)")
    parser.add_argument("--all", dest="run_all", action="store_true", help="Run all capabilities")
    parser.add_argument("--clear-trace", action="store_true", help="Clear trace.jsonl before running")
    args = parser.parse_args()

    use_llm = not args.no_llm
    if args.no_llm:
        os.environ["LLM_OFFLINE"] = "1"

    if args.clear_trace:
        trace.clear()
        log.info("Trace cleared.")

    # Print inbox stats
    stats = loader.stats()
    print(f"\n📬 Inbox: {stats['total_messages']} messages | {stats['unread_messages']} unread | "
          f"{stats['total_threads']} threads | Owner: {stats['owner']}")
    print(f"   Date range: {stats['date_range']}")

    if args.run_all or not args.cap:
        # Full run
        decisions = cap_r1(use_llm=use_llm)
        cap_r4(use_llm=use_llm)
        cap_r5(use_llm=use_llm)
        cap_r2(args.msg, use_llm=use_llm, dry_run=args.dry_run)
        cap_r3(use_llm=use_llm, dry_run=True)   # always dry-run in --all mode
        cap_r6(decisions=decisions, use_llm=use_llm)
        cap_x1(use_llm=use_llm)
        cap_x2(decisions=decisions, use_llm=use_llm)
        return

    cap = args.cap.upper()
    if cap == "R1":
        cap_r1(use_llm=use_llm)
    elif cap == "R2":
        cap_r2(args.msg, use_llm=use_llm, dry_run=args.dry_run)
    elif cap == "R3":
        cap_r3(use_llm=use_llm, dry_run=args.dry_run)
    elif cap == "R4":
        cap_r4(use_llm=use_llm)
    elif cap == "R5":
        cap_r5(use_llm=use_llm)
    elif cap == "R6":
        cap_r6(use_llm=use_llm)
    elif cap == "X1":
        cap_x1(use_llm=use_llm)
    elif cap == "X2":
        cap_x2(use_llm=use_llm)
    else:
        print(f"Unknown capability: {args.cap}")
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
