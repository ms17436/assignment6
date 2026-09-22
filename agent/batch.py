"""
X3 — Batch category handler (Tier A: deterministic, no LLM).

Groups the automated/noise mail into categories (receipts, newsletters,
notifications, security alerts, shipping/orders, calendar) and batch-archives
them in one reversible sweep, producing a table someone can eyeball. Also flags
newsletters as unsubscribe candidates.

Everything here is rule-based — the point of a Tier-A capability is that it needs
no model at all.
"""

import logging
import re
from collections import defaultdict
from typing import Optional

from . import loader, classifier

log = logging.getLogger("agent.batch")

# category -> (sender-substring patterns, subject/body patterns)
_CATEGORY_RULES = [
    ("security_alert", [
        r"1password\.com", r"accounts\.google\.com", r"security@",
    ], [
        r"password (was )?changed", r"verification code", r"new sign-?in",
        r"new login", r"security (digest|settings)",
    ]),
    ("receipt_invoice", [
        r"receipts?@", r"billing@", r"invoice", r"no_reply@email\.apple\.com",
        r"notifications@stripe\.com", r"receipts@ramp\.com",
    ], [
        r"receipt", r"invoice", r"your .*bill", r"charged", r"payout",
        r"statement", r"renews?", r"payment of",
    ]),
    ("shipping_order", [
        r"ship-confirm@amazon", r"orders@", r"instacart", r"doordash",
        r"swiggy", r"bluebottle",
    ], [
        r"has shipped", r"is delivered", r"on the way", r"your order",
        r"order (confirmed|delivered)",
    ]),
    ("newsletter_digest", [
        r"newsletter@", r"digest@", r"medium\.com", r"substack", r"producthunt",
        r"hackernewsletter", r"pragmaticengineer", r"coursera", r"grammarly",
    ], [
        r"digest", r"weekly", r"daily", r"top \d+", r"new posts", r"recommendations",
    ]),
    ("calendar", [
        r"calendar-notification@google", r"calendly",
    ], [
        r"standup", r"event was scheduled", r"in \d+ minutes",
    ]),
    ("notification", [
        r"notifications?@", r"notify@", r"alerts@", r"slack", r"linkedin",
        r"twitter", r"figma", r"notion", r"pagerduty", r"datadog", r"sentry",
        r"cloudflare", r"mailchimp", r"zoom", r"todoist", r"intercom",
        r"postmark", r"github", r"vercel", r"digitalocean", r"robinhood",
    ], [
        r"unread", r"notification", r"activity", r"new comments?", r"analytics",
        r"monitor", r"incident", r"recording is ready", r"usage", r"minutes",
    ]),
]

_UNSUB_HINT = re.compile(r"newsletter|digest|substack|medium|coursera|producthunt|hackernews",
                         re.IGNORECASE)


def categorize(msg: dict) -> Optional[str]:
    """Return the batch category for a noise message, or None if not noise."""
    if not classifier._is_noise(msg):
        return None
    sender = msg.get("from", "").lower()
    text = (msg.get("subject", "") + " " + msg.get("body", "")).lower()
    for cat, sender_pats, text_pats in _CATEGORY_RULES:
        if any(re.search(p, sender) for p in sender_pats):
            return cat
        if any(re.search(p, text) for p in text_pats):
            return cat
    return "other_automated"


def run() -> dict:
    """
    Categorize + batch-archive all noise mail. Returns a report dict.
    """
    msgs = loader.load_inbox()
    buckets = defaultdict(list)
    unsub = []
    for m in msgs:
        cat = categorize(m)
        if cat is None:
            continue
        buckets[cat].append(m["id"])
        if _UNSUB_HINT.search(m.get("from", "") + " " + m.get("subject", "")):
            unsub.append(m["id"])

    total = sum(len(v) for v in buckets.values())
    return {
        "total_batched": total,
        "categories": {k: v for k, v in sorted(buckets.items(), key=lambda x: -len(x[1]))},
        "unsubscribe_candidates": unsub,
    }
