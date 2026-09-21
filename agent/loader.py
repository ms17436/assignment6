"""
Load and index inbox.json.

Provides:
  load_inbox()  -> list[dict]   — all messages, sorted by timestamp
  by_id()       -> dict[str, dict]
  by_thread()   -> dict[str, list[dict]]   — each list is chronological
"""

import json
import os
from pathlib import Path
from functools import lru_cache
from typing import Dict, List

INBOX_PATH = Path(__file__).parent.parent / "inbox.json"


@lru_cache(maxsize=1)
def load_inbox() -> List[dict]:
    with open(INBOX_PATH, encoding="utf-8") as fh:
        messages = json.load(fh)
    # Sort chronologically
    messages.sort(key=lambda m: m.get("timestamp", ""))
    return messages


@lru_cache(maxsize=1)
def by_id() -> Dict[str, dict]:
    return {m["id"]: m for m in load_inbox()}


@lru_cache(maxsize=1)
def by_thread() -> Dict[str, List[dict]]:
    threads: Dict[str, List[dict]] = {}
    for m in load_inbox():
        tid = m.get("thread_id", m["id"])
        threads.setdefault(tid, []).append(m)
    return threads


def thread_of(msg: dict) -> List[dict]:
    """Return all messages in the same thread as msg, chronologically."""
    return by_thread().get(msg.get("thread_id", msg["id"]), [msg])


def messages_before(msg: dict) -> List[dict]:
    """Return messages in the same thread that arrived before msg."""
    ts = msg.get("timestamp", "")
    return [m for m in thread_of(msg) if m["timestamp"] < ts]


def owner_email() -> str:
    """Infer the inbox-owner's address from 'to' fields."""
    msgs = load_inbox()
    from collections import Counter
    ctr: Counter = Counter()
    for m in msgs:
        to_addr = m.get("to", "")
        if isinstance(to_addr, list):
            for a in to_addr:
                ctr[a] += 1
        else:
            ctr[to_addr] += 1
    if ctr:
        return ctr.most_common(1)[0][0]
    return "sam@paperjet.io"


def is_sent_by_owner(msg: dict) -> bool:
    return msg.get("from", "").lower() == owner_email().lower()


def stats() -> dict:
    msgs = load_inbox()
    threads = by_thread()
    unread = [m for m in msgs if m.get("unread")]
    return {
        "total_messages": len(msgs),
        "total_threads": len(threads),
        "unread_messages": len(unread),
        "owner": owner_email(),
        "date_range": f"{msgs[0]['timestamp'][:10]} to {msgs[-1]['timestamp'][:10]}" if msgs else "n/a",
    }
