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


def build(decisions: list[dict], flagged_injections: list[dict],
          use_llm: bool = False) -> dict:
    """
    Build the dashboard data structure from a list of classified decisions.

    decisions          — output of classifier.classify_all()
    flagged_injections — output of injection.scan_inbox()
    """
    # --- Pane 1: Pending actions ---
    PRIORITY_ORDER = {"urgent": 0, "high": 1, "normal": 2, "low": 3}
    action_items = [
        d for d in decisions
        if d.get("action_needed") or d.get("disposition") in ("reply", "escalate", "delegate")
    ]
    action_items.sort(key=lambda d: PRIORITY_ORDER.get(d.get("priority", "low"), 3))

    # --- Pane 2: Flagged ---
    flagged_items = []
    # Injections
    for inj in flagged_injections:
        flagged_items.append({
            "category": "prompt_injection",
            "message_id": inj["message_id"],
            "description": inj["evidence"],
            "action": inj["action"],
        })
    # Phishing from decisions
    for d in decisions:
        if "phishing_detail" in d:
            flagged_items.append({
                "category": "phishing",
                "message_id": d["id"],
                "description": d["reason"],
                "action": "Escalated to Sam. Do NOT act on this message.",
            })

    # --- Pane 3: Commitments + conflicts ---
    all_commitments, conflicts = cmts.get_all(use_llm=False)

    dashboard = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "pane1_pending_actions": action_items,
        "pane2_flagged": flagged_items,
        "pane3_commitments": all_commitments,
        "pane3_conflicts": conflicts,
        "summary": {
            "total_messages": len(decisions),
            "pending_actions": len(action_items),
            "flagged": len(flagged_items),
            "commitments": len(all_commitments),
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
        disp = item.get("disposition", "")
        prio = item.get("priority", "normal")
        reason = item.get("reason", "")
        mid = item.get("id", "")
        delegate = f" → {item['delegate_to']}" if item.get("delegate_to") else ""
        deadline = f" <em>by {item['deadline']}</em>" if item.get("deadline") else ""
        cited = ""
        if item.get("cited_ids"):
            cited = f' <small>[cites: {", ".join(item["cited_ids"])}]</small>'
        return (
            f"<tr><td>{mid}</td><td>{badge(prio)}</td>"
            f"<td><code>{disp}</code>{delegate}</td>"
            f"<td>{reason}{deadline}{cited}</td></tr>"
        )

    def flagged_row(item: dict) -> str:
        cat = item.get("category", "")
        colour = "#c0392b" if cat == "prompt_injection" else "#e67e22"
        label = "🚨 INJECTION" if cat == "prompt_injection" else "⚠️ PHISHING"
        return (
            f'<tr style="background:#fff5f5">'
            f'<td style="color:{colour};font-weight:bold">{label}</td>'
            f'<td>{item.get("message_id","")}</td>'
            f'<td>{item.get("description","")}</td>'
            f'<td>{item.get("action","")}</td></tr>'
        )

    def commitment_row(c: dict) -> str:
        viol = ""
        if c.get("calendar_rule_violated"):
            viol = ' <span style="color:#c0392b">⚠️ CALENDAR RULE VIOLATED</span>'
        date_str = c.get("date", "") or ""
        time_str = c.get("time", "") or ""
        slot = f"{date_str} {time_str}".strip()
        ctype = c.get("type", "")
        return (
            f"<tr><td><code>{ctype}</code></td><td>{slot}</td>"
            f"<td>{c.get('description','')}{viol}</td>"
            f"<td>{c.get('source_message_id','')}</td></tr>"
        )

    def conflict_box(conflict: dict) -> str:
        items_html = "<br>".join(
            f"• [{i.get('source_message_id','')}] {i.get('description','')[:80]}"
            for i in conflict.get("items", [])
        )
        return (
            f'<div style="background:#fff3cd;border-left:4px solid #e67e22;padding:8px;margin:4px 0">'
            f'<strong>⚡ CONFLICT at {conflict["slot"]}</strong><br>{items_html}</div>'
        )

    s = d.get("summary", {})
    gen = d.get("generated_at", "")

    # Build section HTML
    action_rows = "".join(action_row(i) for i in d.get("pane1_pending_actions", []))
    flagged_rows = "".join(flagged_row(i) for i in d.get("pane2_flagged", []))
    commitment_rows = "".join(commitment_row(c) for c in d.get("pane3_commitments", []))
    conflict_divs = "".join(conflict_box(cf) for cf in d.get("pane3_conflicts", []))

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>PaperJet Inbox Dashboard</title>
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
  small {{ color: #888; }}
</style>
</head>
<body>
<h1>📬 PaperJet Inbox Dashboard</h1>
<p>Generated: {gen} | <a href="dashboard.json">JSON version</a></p>

<div class="summary-grid">
  <div class="stat"><div class="num">{s.get("total_messages", 0)}</div><div class="lbl">Total messages</div></div>
  <div class="stat"><div class="num" style="color:#e67e22">{s.get("pending_actions", 0)}</div><div class="lbl">Pending actions</div></div>
  <div class="stat"><div class="num" style="color:#c0392b">{s.get("flagged", 0)}</div><div class="lbl">Flagged</div></div>
  <div class="stat"><div class="num" style="color:#27ae60">{s.get("archived", 0)}</div><div class="lbl">Archived</div></div>
  <div class="stat"><div class="num">{s.get("commitments", 0)}</div><div class="lbl">Commitments</div></div>
  <div class="stat"><div class="num" style="color:{"#c0392b" if s.get("conflicts",0) > 0 else "#27ae60"}">{s.get("conflicts", 0)}</div><div class="lbl">Conflicts</div></div>
</div>

<h2>📋 Pane 1 — Pending Actions</h2>
<table>
  <tr><th>ID</th><th>Priority</th><th>Disposition</th><th>Reason / Action</th></tr>
  {action_rows if action_rows else "<tr><td colspan='4'>No pending actions.</td></tr>"}
</table>

<h2>🚨 Pane 2 — Flagged Messages</h2>
<table>
  <tr><th>Category</th><th>Message ID</th><th>Evidence</th><th>Action Taken</th></tr>
  {flagged_rows if flagged_rows else "<tr><td colspan='4'>No flagged messages.</td></tr>"}
</table>

<h2>📅 Pane 3 — Commitments & Conflicts</h2>

{"<h3>⚡ Conflicts</h3>" + conflict_divs if conflict_divs else ""}

<table>
  <tr><th>Type</th><th>Date / Time</th><th>Description</th><th>Source</th></tr>
  {commitment_rows if commitment_rows else "<tr><td colspan='4'>No commitments found.</td></tr>"}
</table>

</body>
</html>
"""
