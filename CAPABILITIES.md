# CAPABILITIES.md

**Student:** [Your Name], [Your ID]
**Repository:** https://github.com/[your-username]/paperjet-inbox-agent

Run everything through one entry point:

```bash
pip install -r requirements.txt

# Individual capabilities
python demo.py --cap R1              # triage all 100 messages
python demo.py --cap R2 --msg m008  # grounded reply (needs thread context from m003)
python demo.py --cap R3 --dry-run   # show sends, write nothing
python demo.py --cap R4             # extract + persist preferences
python demo.py --cap R5             # detect all 4 injection attempts
python demo.py --cap R6             # generate dashboard.html + dashboard.json
python demo.py --cap X1             # follow-up tracker
python demo.py --cap X2             # morning digest

# Full run (R3 runs in dry-run automatically)
python demo.py --all

# Offline / no-API mode
python demo.py --all --no-llm
```

Set `GEMINI_API_KEY` for Gemini (recommended), or `OPENAI_API_KEY` + `OPENAI_BASE_URL`
for an OpenAI-compatible endpoint (including local Ollama). Set `LLM_OFFLINE=1` or
`--no-llm` to run with heuristics only.

---

## The system, in one paragraph

A single Python pipeline, no framework. Messages are loaded from `inbox.json`,
sorted chronologically, and processed in two passes: a cheap rule-based pass
(no LLM calls) handles noise, phishing, and obvious injections, then a model
pass (Gemini 1.5 Flash, with exponential back-off on HTTP 429) classifies the
remainder. Thread context is retrieved by walking `thread_id` — the inbox
already carries its own structure, so embeddings would be overhead. State that
must survive a process restart (preferences, decisions, the action log) is kept
in small JSON files under `state/`. Every irreversible effect (`send`, `delete`)
must pass through a single gate in `agent/gate.py`; nothing else in the code
can cause those effects, which is also the prompt-injection defence.

---

## Design choices

- **Framework: none.** The pipeline is linear with a single decision branch
  (rule path vs model path), so a crew or agent graph would add indirection
  without benefit.

- **Model: Gemini 1.5 Flash** for triage and drafting (cheap, fast, large
  context window suitable for threading). Falls back to heuristics when
  `LLM_OFFLINE=1`. Developed and tested offline first to stay within free-tier
  rate limits.

- **Rate-limit handling:** `agent/llm.py` sleeps `LLM_CALL_DELAY` seconds
  between every call (default 2 s), retries on HTTP 429 with exponential
  back-off (up to 5 attempts), and logs each wait. Batch: noise and obvious
  injections never hit the LLM at all.

- **Retrieval: thread-walk (primary) + cross-thread keyword search (fallback).**
  `retrieval.thread_walk()` walks `thread_id` for prior messages — cheaper and
  more precise than embeddings for a structured inbox. When the thread is thin or
  a message refers back to an earlier one ("previous email", "resend"),
  `retrieval.keyword_search()` does a conservative cross-thread lookup (content
  overlap, or same-correspondent + explicit back-reference). Hostile messages
  (injection/phishing) are filtered out of grounding context. Every citation is
  verified by `retrieval.verify_citations()` against both the mail store and the
  retrieved set; unverifiable citations are dropped, and if nothing grounds the
  answer the system drafts nothing.

- **Reversible vs irreversible.** `send` and `delete` are irreversible and
  always pass through `gate.require_approval()`. `draft`, `archive`, `defer`,
  `delegate` are reversible and run without a prompt. In `--dry-run` mode no
  outbox writes occur at all.

- **Where the gate sits.** Only `gate.send()` and `gate.delete()` can write to
  `outbox/` or modify stored state in an irreversible way. All other code —
  including the LLM prompt chain — can only produce a *draft* dict in memory.
  A hostile injected instruction can reach the draft stage but cannot cross the
  gate without a `y` from the user (or `AUTO_APPROVE=1` in tests).

- **Escalation line.** Messages to external recipients, anything touching money
  (`wire`, `payment`, `invoice`), legal documents, and all detected
  phishing/injection are `escalate` or `flag_injection`. Internal archives and
  defers are automatic. The trade-off: a wrongly-archived receipt is possible,
  in exchange for the user not being asked to approve every notification.

---

## Disposition vocabulary (Part 2)

Every message is assigned **exactly one** of these, with a one-line reason:

| disposition | meaning |
|---|---|
| `reply` | Agent should draft (and, after the gate, send) a response. |
| `archive` | No action needed — noise, receipts, FYIs, already-resolved threads. |
| `defer` | Legitimate but not urgent; surface later for the user. |
| `delegate` | Hand to someone other than the owner (`delegate_to` names them). |
| `escalate` | High-risk / high-value; needs the owner's immediate attention (incl. phishing). |
| `flag_injection` | Contains instructions aimed at the AI; refused, flagged, never acted on. |

**Rule-vs-model routing (Part 2 #3).** Obvious messages are routed by rules with
**no model call at all**. `python demo.py --cap R1` measures this live and prints:

```
── Part 2 headline numbers ─────────────
  Total messages:                    100
  Rule-routed (never need a model):  67  (67%)
  Routed to the model:               33
  Actual LLM calls this run:         N
undecided: 0
```

Each decision carries a `handled_by` tag (`rule:noise`, `rule:injection`,
`rule:phishing`, `rule:preference`, `rule:sent`, or `llm`) which is written to
`state/trace.jsonl`. The 67 rule-routed messages never touch the model even when
an API key is configured; `Actual LLM calls` is a live counter (0 in `--no-llm`).

## Grounded answering (Part 3)

Three outcomes, all reproducible with `python demo.py --cap R2 --msg <id>`:

| message | retrieval | outcome |
|---|---|---|
| `m008` (Devika: "resend the staging URL") | thread-walk → m001, m003, m005 | grounded reply; `cited (verified)` includes **m003**, which really holds the AMQP URL |
| `m055` (lawyer: "portal link in the **previous email**") | keyword-search (cross-thread) → **m018** | grounded on the prior SAFE email in a *different* thread |
| `m012` (Priya: "that **thing** we talked about after the standup") | none (refers to a verbal chat) | **NOT ANSWERABLE FROM INBOX — no draft produced** |

**Verification (req #2).** `retrieval.verify_citations(cited, read)` requires every
cited id to (a) exist in the mail store and (b) be in the set actually retrieved.
Proven to reject bad cites:

```
verify_citations(['m003','m999','m010'], ['m001','m003','m005'])
→ ok=False, verified=['m003'], not_in_store=['m999'], not_read=['m010']
```

The drafter keeps only `verified` ids, so a hallucinated or unread citation can
never appear in a finished draft. Each draft also carries a `grounding` report and
a `retrieval.methods` list, both written to `state/trace.jsonl`.

## Inbox analysis (Part 1)

| Stat | Value |
|------|-------|
| Total messages processed | 100 |
| Rule-routed (never need a model) | 67 (noise 56, injection 4, phishing 3, preference 2, sent 2) |
| Routed to the LLM | 33 |
| Prompt injection attempts detected | 4 (m017, m024, m039, m047) |
| Phishing / social-engineering | 3 (m021, m023, m045) |
| Standing preferences extracted | 2 (m041, m015) |
| Scheduling conflicts | 2 (Sep 15 15:00: investor call vs dental; Wed 14:00: ACME demo vs Raghav 1:1) |
| Calendar rule violations | 1 (m043: 9am slot violates no-meetings-before-11am) |
| Long thread with buried request | t-launch (9 messages; request in m030) |
| Thread-context-dependent reply | m008 requires m003 for AMQP URL |
| Ambiguous message | m012 ("the thing") — ask for clarification |

**Assumptions about data format:**
- All messages share the keys `id`, `thread_id`, `from`, `to`, `subject`,
  `timestamp`, `body`, `unread`.
- `timestamp` is ISO 8601 local time (no timezone offset). Treated as UTC for
  conflict detection.
- `to` is always a single string in this inbox (not an array).
- The owner is the address that appears most often in `to`: `sam@paperjet.io`.

---

## Capabilities

| id | name | tier | one-line claim |
|----|------|------|----------------|
| R1 | Zero the inbox | B | every message gets exactly one disposition + reason; none left undecided |
| R2 | Grounded reply | B | reply to m008 cites m003's AMQP URL; cited_ids recorded |
| R3 | Gate the irreversible | C | no send/delete without approval or --dry-run; single choke-point in gate.py |
| R4 | Persistent preference | C | m041 calendar rule and m015 CC rule survive process restart |
| R5 | Refuse embedded instructions | C | all 4 injections detected, refused, flagged, reported; outbox clean |
| R6 | Dashboard | C | three panes, Sep 15 15:00 conflict surfaced, cited to source messages |
| X1 | Follow-up tracking | B | m044 (unanswered invoice request) found; chase draft produced |
| X2 | Morning digest | B | urgent items separated from noise; archived count shown |

Full command, observable outcome, and evidence in `capabilities.json`.

---

## Final Report

*(Answers to the four report questions go here.)*

**Q1 — What was the hardest design decision?**
Deciding where to draw the escalation line between automatic and gated actions.
Archiving noise automatically saves time but risks burying something real;
gating every archive would produce alert fatigue. The chosen line (only `send`
and `delete` are gated; everything else is reversible or low-stakes) reflects
that the cost of a wrong archive is low compared to the cost of a wrong send.

**Q2 — How does the system handle ambiguous messages?**
Ambiguous messages (e.g. m012 "the thing") are classified as `reply` with a
draft that asks for clarification rather than guessing. The LLM prompt instructs
the model explicitly: "if the request is ambiguous, ask a clarifying question."
The draft is shown to the user before being gated for send.

**Q3 — How does the system resist prompt injection?**
Two-layer defence: (1) a rule-based pattern library in `agent/injection.py`
catches all four known injection patterns without touching the LLM; (2) an LLM
confirmation pass runs on messages that trigger soft signals. Critically, the
injections are detected *before* the classifier produces a disposition, so they
never influence a draft. And even if they reached the draft stage, the gate
would still require a human `y` before any send.

**Q4 — What would you change with more time?**
Add embedding-based retrieval for cross-thread context (e.g. finding a prior
conversation with the same sender across different threads). Add a confidence
threshold so the system asks the user to review borderline classifications
rather than silently deferring. Build a proper audit trail UI on top of
`trace.jsonl`.
