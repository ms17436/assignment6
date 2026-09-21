# CAPABILITIES.md — SAMPLE

> **This is an illustrative sample, not an answer key.** It describes a small
> imaginary triage system built against a made-up 24-message inbox, so the
> message ids below (`m014`, `m022`, `m031`, ...) are **not** the ids in the
> `inbox.json` you were given. Copy the *structure* of this file and of
> `capabilities.sample.json`; replace every word of the content with your own.
> Delete this note in your submission.

**Student:** Ada Example, 2026XXXXXX
**Repository:** https://github.com/ada-example/inboxhero

Run everything through one entry point:

```
python demo.py --cap R1        # one capability
python demo.py --all           # all of them, in the order below
```

---

## The system, in one paragraph

A single Python pipeline, no framework. Messages are loaded, cheap ones (receipts,
newsletters, calendar notifications) are dispatched by rule before any model is
touched, and the rest go through a classify → retrieve → draft → gate sequence. A
final pass builds the dashboard. State that must outlive a run (preferences, the
action log) is kept in small JSON files on disk.

## Design choices you were asked to state

- **Framework: none.** The work is a linear pipeline with one branch (rule-path vs
  model-path), so a crew or graph would have been overhead. See Final Report Q4.
- **Retrieval: thread-walk.** An inbox already carries its own structure in
  `thread_id`, so walking the thread is both cheaper and more precise than
  embeddings for this task. Keyword search is the fallback for cross-thread lookups.
- **Reversible vs irreversible.** `send` and `delete` are irreversible and gated.
  `draft`, `label`, `archive` and `defer` are reversible and run without a prompt.
  Deleting is treated as irreversible because the mock store has no trash.
- **Where the gate sits.** Only two functions can cause an irreversible effect, and
  both call `require_approval()` first. Nothing else in the system can reach them,
  which is also the Part 6 defence: a hostile message can influence a *draft* but
  cannot reach a send without passing the gate.
- **Escalation line.** The system asks for approval only on sends to external
  recipients and on anything touching money or legal. Internal archives and defers
  are automatic. The trade-off: a wrongly-archived internal note is possible, in
  exchange for the user not being asked to approve forty things.

## Capabilities

| id | name | tier | one-line claim |
|----|------|------|----------------|
| R1 | Zero the inbox | B | every message gets one disposition + reason, none left |
| R2 | Grounded reply | B | drafts cite the earlier message they used |
| R3 | Gate the irreversible | C | no send/delete without approval or --dry-run |
| R4 | Persistent preference | C | a stated preference survives a restart |
| R5 | Refuse embedded instructions | C | detects, refuses, flags, reports injections |
| R6 | Dashboard | C | three panes, commitments cited, conflicts surfaced |
| X1 | Follow-up tracking | B | unanswered sent mail, with a drafted chase |
| X2 | Morning digest | B | what needs me / what can wait / what was archived |

The exact command, observable outcome and evidence for each is in
`capabilities.sample.json`. That file is the machine-readable version and is what a
marking script reads; this file is for a human. Keep the two in step.

## Final Report

*(Your four answers go here. Omitted from the sample.)*
