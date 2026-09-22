"""
Retrieval + grounding verification (Part 3).

Two retrieval methods, both named in the manifest:
  1. thread-walk   — primary. Walk `thread_id` for earlier messages in the same
                     thread (loader.messages_before). Precise and free.
  2. keyword/sender search — fallback for CROSS-THREAD lookups (e.g. a message
                     that says "see the previous email" from the same sender in a
                     different thread). Simple TF-style overlap on subject+body,
                     plus same-sender matching.

Grounding verification (the heart of Part 3 req #2):
  verify_citations() checks every cited id against the mail store AND against the
  exact set of messages that were actually retrieved ("read"). A citation to a
  message that does not exist, or that was never read, is rejected. This is what
  prevents "citing a message it never read" and anchors drafts to real evidence.
"""

import logging
import re
from typing import Optional

from . import loader

log = logging.getLogger("agent.retrieval")

# Words too common to be useful for cross-thread matching. Includes temporal /
# filler connectors ("before", "after", "about", ...) that otherwise create
# spurious overlaps (e.g. m012 "that thing ... before the call" vs an unrelated
# message that merely also contains "before").
_STOP = {
    "the", "a", "an", "and", "or", "but", "to", "of", "in", "on", "for", "is",
    "are", "was", "were", "be", "been", "this", "that", "it", "you", "i", "we",
    "with", "as", "at", "by", "from", "re", "your", "our", "can", "could",
    "would", "please", "thanks", "hi", "hey", "sam", "just", "get", "got",
    "need", "want", "one", "will", "have", "has", "do", "did", "any", "if",
    "before", "after", "about", "when", "then", "there", "here", "out", "over",
    "into", "back", "some", "sort", "kind", "ever", "chance", "thing", "things",
    "talked", "talk", "done", "call", "sec", "yet", "still", "much", "more",
}

# Phrases signalling the answer lives in an earlier message elsewhere
_REFERS_BACK = [
    r"\bprevious email\b", r"\bearlier (email|message|thread)\b",
    r"\bthe (url|link|address|creds|credentials|number|code) (you|i) (gave|sent|shared)\b",
    r"\bresend\b", r"\bas (i|we) discussed\b", r"\bthe portal link\b",
    r"\bthat (thing|one|link|url)\b", r"\bthe attached\b", r"\blike (i|we) said\b",
]
_REFERS_RE = [re.compile(p, re.IGNORECASE) for p in _REFERS_BACK]


def _tokens(text: str) -> set:
    words = re.findall(r"[a-zA-Z0-9_.@-]{3,}", text.lower())
    return {w for w in words if w not in _STOP}


def thread_walk(msg: dict) -> list[dict]:
    """Primary retrieval: earlier messages in the same thread, chronological."""
    return loader.messages_before(msg)


def keyword_search(msg: dict, exclude_ids: set, top_k: int = 3) -> list[dict]:
    """
    Cross-thread fallback: find messages (in OTHER threads) that plausibly hold
    the answer. Returns up to top_k.

    Qualification (deliberately conservative to avoid false grounding):
      • strong content match: >= 3 overlapping content tokens, OR
      • same correspondent AND this message explicitly refers back to an earlier
        one ("previous email", "resend", ...) AND >= 1 content token overlaps.
    Same-sender ALONE never qualifies — that is what produced the spurious
    m012 -> m030/m040 match before.
    """
    store = loader.load_inbox()
    query_tokens = _tokens(msg.get("subject", "") + " " + msg.get("body", ""))
    counterpart = msg.get("from", "").lower()
    back_ref = refers_to_earlier(msg)

    scored = []
    for m in store:
        if m["id"] in exclude_ids or m["id"] == msg["id"]:
            continue
        # Only look backwards in time (you can't cite the future)
        if m.get("timestamp", "") >= msg.get("timestamp", ""):
            continue
        m_tokens = _tokens(m.get("subject", "") + " " + m.get("body", ""))
        overlap = len(query_tokens & m_tokens)
        same_person = m.get("from", "").lower() == counterpart

        strong = overlap >= 3
        back_ref_match = same_person and back_ref and overlap >= 1
        if strong or back_ref_match:
            score = overlap + (2 if same_person else 0)
            scored.append((score, m))

    scored.sort(key=lambda x: (-x[0], x[1].get("timestamp", "")))
    return [m for _, m in scored[:top_k]]


def refers_to_earlier(msg: dict) -> bool:
    """Heuristic: does this message point at an earlier message for its answer?"""
    body = msg.get("subject", "") + " " + msg.get("body", "")
    return any(p.search(body) for p in _REFERS_RE)


def gather_context(msg: dict) -> dict:
    """
    Assemble grounding context for `msg`.

    Returns:
      {
        "context_msgs": [ ...full message dicts... ],   # what was 'read'
        "context_ids":  [ ...ids... ],                   # the read set
        "methods":      [ "thread-walk", "keyword-search" ],  # which fired
      }
    """
    methods = []
    context = thread_walk(msg)
    if context:
        methods.append("thread-walk")

    # Cross-thread fallback: use it when the thread alone is thin OR the message
    # explicitly refers back to something elsewhere.
    if not context or refers_to_earlier(msg):
        exclude = {m["id"] for m in context} | {msg["id"]}
        cross = keyword_search(msg, exclude_ids=exclude)
        if cross:
            context = context + cross
            if "keyword-search" not in methods:
                methods.append("keyword-search")

    # SECURITY: never ground a reply on a hostile message. Drop any retrieved
    # message that is a prompt-injection or phishing attempt before it can
    # become "evidence". (Also part of the Part 6 defence.)
    context = [m for m in context if not _is_hostile(m)]

    context_ids = [m["id"] for m in context]
    if not context and "keyword-search" in methods:
        methods.remove("keyword-search")
    return {"context_msgs": context, "context_ids": context_ids, "methods": methods}


def _is_hostile(msg: dict) -> bool:
    """True if msg is a known prompt-injection or phishing attempt."""
    from . import injection, classifier
    if injection.analyse(msg, use_llm=False)["is_injection"]:
        return True
    is_phish, _ = classifier._is_phishing(msg)
    return is_phish


def verify_citations(cited_ids: list, read_ids: list) -> dict:
    """
    Grounding verification (Part 3 req #2).

    Every cited id must:
      (a) exist in the mail store, and
      (b) be within the set of messages actually retrieved / read.

    Returns:
      {
        "ok": bool,
        "verified": [ ...ids that pass both checks... ],
        "not_in_store": [ ...ids that don't exist... ],
        "not_read": [ ...ids cited but never retrieved... ],
      }
    """
    store_ids = set(loader.by_id().keys())
    read = set(read_ids)

    verified, not_in_store, not_read = [], [], []
    for cid in cited_ids:
        if cid not in store_ids:
            not_in_store.append(cid)
        elif cid not in read:
            not_read.append(cid)
        else:
            verified.append(cid)

    ok = not not_in_store and not not_read
    return {
        "ok": ok,
        "verified": verified,
        "not_in_store": not_in_store,
        "not_read": not_read,
    }


def fact_traceable(snippet: str, context_msgs: list) -> bool:
    """
    Optional literal-detail check: returns True if `snippet` (e.g. a URL or code
    a draft quotes verbatim) actually appears in one of the retrieved messages.
    Used to catch invented details.
    """
    if not snippet:
        return True
    hay = " ".join(m.get("body", "") for m in context_msgs)
    return snippet.strip() in hay
