"""
Dashboard generator (Capability R6).

Produces three panes:
  1. Pending Actions   — messages requiring Sam's attention, sorted by urgency
  2. Flagged           — injections, phishing, suspicious items
  3. Commitments       — deadlines and meetings, with conflicts highlighted

Output: dashboard.html + dashboard.json (both reproducible from a single run).
"""

import json
import logging
import time
from pathlib import Path
from typing import Optional

from . import commitments as cmts

log = logging.getLogger("agent.dashboard")

OUT_DIR = Path(__file__).parent.parent
HTML_PATH = OUT_DIR / "dashboard.html"
JSON_PATH = OUT_DIR / "dashboard.json"


# How to describe the proposed action + why it needs a human, per disposition.
def _proposed(d: dict) -> tuple[str, str]:
    disp = d.get("disposition")
    if disp == "reply":
        return ("Send drafted reply",
                "Outbound send is irreversible — blocked by the Part 4 gate until you approve.")
    if disp == "delegate":
        who = d.get("delegate_to") or "someone else"
        return (f"Delegate to {who}",
                "Reassigns ownership — needs your confirmation.")
    if disp == "escalate":
        return ("Escalate for your decision", d.get("reason", "High-risk; needs your attention."))
    return ("Review", d.get("reason", ""))


def build(decisions: list[dict], flagged_injections: list[dict],
          use_llm: bool = False) -> dict:
    """
    Build the three-pane dashboard (Part 7) from a completed run.

    Pane 1 — Pending actions: things the system wants to do but may not do alone
             under Part 4 (gated sends, escalations, delegations). Row = message,
             proposed action, why it needs a human.
    Pane 2 — Flagged: injections (Part 6), phishing, and ungroundable messages.
             Row = what was attempted, what the system did instead.
    Pane 3 — Commitments (calendar) with VERIFIED multi-source citations and
             surfaced conflicts.
    """
    from . import loader, retrieval

    PRIORITY_ORDER = {"urgent": 0, "high": 1, "normal": 2, "low": 3}

    # --- Pane 1: gated / human-needed actions ---
    pending = []
    for d in decisions:
        if d.get("disposition") in ("reply", "escalate", "delegate"):
            action, why = _proposed(d)
            pending.append({
                "message_id": d["id"],
                "priority": d.get("priority", "normal"),
                "proposed_action": action,
                "why_needs_human": why,
                "disposition": d.get("disposition"),
            })
    pending.sort(key=lambda r: PRIORITY_ORDER.get(r.get("priority", "low"), 3))

    # --- Pane 2: flagged / refused ---
    flagged_items = []
    for inj in flagged_injections:
        flagged_items.append({
            "category": "prompt_injection",
            "message_id": inj["message_id"],
            "attempted": inj.get("attempted_action", "unknown"),
            "did_instead": inj.get("action", "Refused; flagged; left in place."),
            "evidence": inj.get("evidence", ""),
        })
    for d in decisions:
        if "phishing_detail" in d:
            flagged_items.append({
                "category": "phishing",
                "message_id": d["id"],
                "attempted": d.get("phishing_detail", d.get("reason", "")),
                "did_instead": "Escalated to Sam; did not act, did not pay, did not click.",
                "evidence": d.get("reason", ""),
            })
    # Ungroundable: refers back to something not in the inbox (e.g. m012)
    msgs_by_id = loader.by_id()
    flagged_ids = {f["message_id"] for f in flagged_items}
    for d in decisions:
        mid = d["id"]
        if mid in flagged_ids:
            continue
        msg = msgs_by_id.get(mid, {})
        if retrieval.refers_to_earlier(msg):
            ctx = retrieval.gather_context(msg)
            if not ctx["context_ids"]:
                flagged_items.append({
                    "category": "ungroundable",
                    "message_id": mid,
                    "attempted": f"Answer '{msg.get('subject','')}' which refers to earlier context not in the inbox.",
                    "did_instead": "Drafted nothing; will ask the user to clarify (Part 3).",
                    "evidence": "No thread-walk or keyword-search match.",
                })

    # --- Pane 3: commitments (calendar) + conflicts ---
    all_commitments, conflicts = cmts.get_all(use_llm=use_llm)

    # Group into a calendar (by date, sorted)
    from collections import defaultdict
    calendar = defaultdict(list)
    for c in all_commitments:
        calendar[c.get("date") or "undated"].append(c)
    calendar_sorted = [{"date": d, "items": calendar[d]} for d in sorted(calendar.keys())]

    dashboard = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "pane1_pending_actions": pending,
        "pane2_flagged": flagged_items,
        "pane3_calendar": calendar_sorted,
        "pane3_commitments": all_commitments,
        "pane3_conflicts": conflicts,
        "summary": {
            "total_messages": len(decisions),
            "pending_actions": len(pending),
            "flagged": len(flagged_items),
            "commitments": len(all_commitments),
            "multi_source_commitments": sum(1 for c in all_commitments if c.get("multi_source")),
            "conflicts": len(conflicts),
            "archived": sum(1 for d in decisions if d.get("disposition") == "archive"),
        },
    }
    return dashboard


def write(dashboard: dict):
    """Write dashboard.json and dashboard.html."""
    # JSON
    with open(JSON_PATH, "w") as fh:
        json.dump(dashboard, fh, indent=2)
    log.info("Dashboard JSON written to %s", JSON_PATH)

    # HTML
    html = _render_html(dashboard)
    with open(HTML_PATH, "w") as fh:
        fh.write(html)
    log.info("Dashboard HTML written to %s", HTML_PATH)

    return HTML_PATH, JSON_PATH


def _render_html(d: dict) -> str:
    PRIORITY_COLOURS = {
        "urgent": "#c0392b",
        "high": "#e67e22",
        "normal": "#2980b9",
        "low": "#7f8c8d",
    }

    def badge(priority: str) -> str:
        colour = PRIORITY_COLOURS.get(priority, "#7f8c8d")
        return f'<span style="background:{colour};color:#fff;padding:2px 7px;border-radius:3px;font-size:0.8em">{priority.upper()}</span>'

    def action_row(item: dict) -> str:
        prio = item.get("priority", "normal")
        mid = item.get("message_id", "")
        return (
            f"<tr><td>{mid}</td><td>{badge(prio)}</td>"
            f"<td>{item.get('proposed_action','')}</td>"
            f"<td>{item.get('why_needs_human','')}</td></tr>"
        )

    CAT_STYLE = {
        "prompt_injection": ("🚨 INJECTION", "#c0392b"),
        "phishing": ("⚠️ PHISHING", "#e67e22"),
        "ungroundable": ("❔ UNGROUNDABLE", "#8e44ad"),
    }

    def flagged_row(item: dict) -> str:
        label, colour = CAT_STYLE.get(item.get("category", ""), ("FLAGGED", "#e67e22"))
        return (
            f'<tr style="background:#fff5f5">'
            f'<td style="color:{colour};font-weight:bold">{label}</td>'
            f'<td>{item.get("message_id","")}</td>'
            f'<td>{item.get("attempted","")}</td>'
            f'<td>{item.get("did_instead","")}</td></tr>'
        )

    def cite_badge(c: dict) -> str:
        g = c.get("grounding", {})
        ids = c.get("source_ids", [])
        ok = g.get("ok", True)
        colour = "#27ae60" if ok else "#c0392b"
        tick = "✓" if ok else "✗"
        multi = ' <span style="background:#8e44ad;color:#fff;padding:1px 5px;border-radius:3px;font-size:.75em">MULTI-SOURCE</span>' if c.get("multi_source") else ""
        return (f'<span style="color:{colour}">[{tick} cites: {", ".join(ids)}]</span>{multi}')

    # Calendar grouped by date
    def calendar_day(day: dict) -> str:
        rows = ""
        for c in day["items"]:
            viol = ' <span style="color:#c0392b">⚠️ CALENDAR RULE VIOLATED</span>' if c.get("calendar_rule_violated") else ""
            t = c.get("time") or "—"
            rows += (
                f"<tr><td style='white-space:nowrap'>{t}</td>"
                f"<td><code>{c.get('type','')}</code></td>"
                f"<td>{c.get('description','')}{viol}<br>"
                f"<small>{cite_badge(c)} · {c.get('derivation','')}</small></td></tr>"
            )
        return (f'<div class="day"><h4>{day["date"]}</h4>'
                f'<table>{rows}</table></div>')

    def conflict_box(conflict: dict) -> str:
        items_html = "<br>".join(
            f"• [{', '.join(i.get('source_ids', []))}] {i.get('description','')[:80]}"
            for i in conflict.get("items", [])
        )
        return (
            f'<div style="background:#fff3cd;border-left:4px solid #c0392b;padding:8px;margin:6px 0">'
            f'<strong>⚡ CONFLICT at {conflict["slot"]}</strong> '
            f'<small>(sources: {", ".join(conflict.get("source_ids", []))})</small>'
            f'<br>{items_html}</div>'
        )

    s = d.get("summary", {})
    gen = d.get("generated_at", "")

    action_rows = "".join(action_row(i) for i in d.get("pane1_pending_actions", []))
    flagged_rows = "".join(flagged_row(i) for i in d.get("pane2_flagged", []))
    calendar_html = "".join(calendar_day(day) for day in d.get("pane3_calendar", []))
    conflict_divs = "".join(conflict_box(cf) for cf in d.get("pane3_conflicts", []))

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>inboxHero Dashboard</title>
<style>
  body {{ font-family: system-ui, sans-serif; max-width: 1200px; margin: 2em auto; padding: 0 1em; color: #222; }}
  h1 {{ border-bottom: 3px solid #2c3e50; padding-bottom: .4em; }}
  h2 {{ background: #2c3e50; color: #fff; padding: .4em .8em; border-radius: 4px; }}
  table {{ width: 100%; border-collapse: collapse; margin-bottom: 1.5em; }}
  th {{ background: #ecf0f1; text-align: left; padding: .4em .6em; border-bottom: 2px solid #bdc3c7; }}
  td {{ padding: .35em .6em; border-bottom: 1px solid #ecf0f1; vertical-align: top; }}
  tr:hover {{ background: #f9f9f9; }}
  .summary-grid {{ display: grid; grid-template-columns: repeat(6, 1fr); gap: .5em; margin-bottom: 1.5em; }}
  .stat {{ background: #f0f4f8; border-radius: 6px; padding: .7em; text-align: center; }}
  .stat .num {{ font-size: 2em; font-weight: bold; color: #2c3e50; }}
  .stat .lbl {{ font-size: .75em; color: #666; }}
  code {{ background: #f0f0f0; padding: 1px 4px; border-radius: 3px; }}
  small {{ color: #666; }}
  .day {{ border:1px solid #e1e6ea; border-radius:6px; padding:.4em .8em; margin-bottom:.8em; }}
  .day h4 {{ margin:.3em 0; color:#2c3e50; border-bottom:1px solid #ecf0f1; }}
  .day table td {{ border:none; }}
</style>
</head>
<body>
<h1>📬 inboxHero Dashboard</h1>
<p>Generated: {gen} | <a href="dashboard.json">JSON version</a> | reproducible from <code>python demo.py --cap R6</code></p>

<div class="summary-grid">
  <div class="stat"><div class="num">{s.get("total_messages", 0)}</div><div class="lbl">Total messages</div></div>
  <div class="stat"><div class="num" style="color:#e67e22">{s.get("pending_actions", 0)}</div><div class="lbl">Pending actions</div></div>
  <div class="stat"><div class="num" style="color:#c0392b">{s.get("flagged", 0)}</div><div class="lbl">Flagged</div></div>
  <div class="stat"><div class="num">{s.get("commitments", 0)}</div><div class="lbl">Commitments</div></div>
  <div class="stat"><div class="num" style="color:#8e44ad">{s.get("multi_source_commitments", 0)}</div><div class="lbl">Multi-source</div></div>
  <div class="stat"><div class="num" style="color:{"#c0392b" if s.get("conflicts",0) > 0 else "#27ae60"}">{s.get("conflicts", 0)}</div><div class="lbl">Conflicts</div></div>
</div>

<h2>📋 Pane 1 — Pending Actions <small style="color:#ddd">(wants to do, may not do alone — Part 4)</small></h2>
<table>
  <tr><th>Message</th><th>Priority</th><th>Proposed action</th><th>Why it needs a human</th></tr>
  {action_rows if action_rows else "<tr><td colspan='4'>No pending actions in this run.</td></tr>"}
</table>

<h2>🚨 Pane 2 — Flagged <small style="color:#ddd">(refused: injections, phishing, ungroundable)</small></h2>
<table>
  <tr><th>Category</th><th>Message</th><th>What was attempted</th><th>What the system did instead</th></tr>
  {flagged_rows if flagged_rows else "<tr><td colspan='4'>No flagged messages.</td></tr>"}
</table>

<h2>📅 Pane 3 — Commitments (calendar)</h2>
{"<h3 style='color:#c0392b'>⚡ Conflicts surfaced</h3>" + conflict_divs if conflict_divs else "<p>No conflicts.</p>"}
<h3>Calendar</h3>
{calendar_html if calendar_html else "<p>No commitments found.</p>"}

</body>
</html>
"""
