"""
Trace log writer — every significant agent action is appended here.
Graders can replay what the agent did, in order, from trace.jsonl.

Call log_event() from anywhere in the pipeline.
"""

import json
import time
from pathlib import Path

TRACE_PATH = Path(__file__).parent.parent / "state" / "trace.jsonl"


def log_event(cap: str, event_type: str, **kwargs):
    """
    Append one structured event to the trace log.

    cap        — capability id (R1, R2, R3 …)
    event_type — 'decision', 'draft', 'read', 'refusal', 'gate', 'preference', etc.
    **kwargs   — additional fields
    """
    TRACE_PATH.parent.mkdir(parents=True, exist_ok=True)
    event = {
        "cap": cap,
        "type": event_type,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
        **kwargs,
    }
    with open(TRACE_PATH, "a") as fh:
        fh.write(json.dumps(event) + "\n")


def read_events(cap: str = None) -> list[dict]:
    """Read all trace events, optionally filtered by capability id."""
    if not TRACE_PATH.exists():
        return []
    events = []
    with open(TRACE_PATH) as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
                if cap is None or ev.get("cap") == cap:
                    events.append(ev)
            except json.JSONDecodeError:
                continue
    return events


def clear():
    """Truncate the trace log (used before a fresh demo run)."""
    if TRACE_PATH.exists():
        TRACE_PATH.write_text("")
