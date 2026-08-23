"""MegaPlan memory-informed retrieval (Phase 1: deterministic).

Two sources, combined by plan_context():
  - related plans   : deterministic tag/title/body-token overlap over the local store.
  - memory service  : POST /recall on the NAS Astoria service (soft dependency;
                      degrades gracefully if unreachable). MEGAPLAN_MEMORY_URL is
                      URL ending in /retrieve speaks the old MemoryOS shape.
Phase 2 (later) swaps the deterministic scorer for embedding similarity (reuse the
NAS TEI/nomic container) — plan_context()'s interface stays identical.
"""

from __future__ import annotations

import os
import re

import httpx

import store

MEMORY_URL = os.environ.get("MEGAPLAN_MEMORY_URL") or "http://192.168.1.134:8933/recall"
MEMORY_USER = os.environ.get("MEGAPLAN_MEMORY_USER", "rick")
MEMORY_CLIENT = os.environ.get("MEGAPLAN_MEMORY_CLIENT", "megaplan")
MEMORY_TOKEN = os.environ.get("MEGAPLAN_MEMORY_TOKEN", "")
MEMORY_LAYERS = ["semantic", "episodic", "profile", "procedural"]


def _is_legacy(url: str) -> bool:
    """True when the configured URL is the old MemoryOS /retrieve endpoint."""
    return url.rstrip("/").endswith("/retrieve")


def _headers() -> dict:
    h = {"X-Astoria-Client": MEMORY_CLIENT}
    if MEMORY_TOKEN:
        h["Authorization"] = f"Bearer {MEMORY_TOKEN}"
    return h

_STOP = {"the", "a", "an", "to", "of", "and", "or", "for", "in", "on", "with", "this",
         "that", "is", "it", "as", "at", "by", "be", "we", "i", "my", "our"}


def _tokens(s: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", (s or "").lower())
            if len(w) > 2 and w not in _STOP}


def related_plans(goal: str, tags=None, limit=5, exclude=None) -> list[dict]:
    gt = _tokens(goal) | {t.lower() for t in (tags or [])}
    rows = []
    for p in store.list_plans(include_archived=False):
        if exclude and p["id"] == exclude:
            continue
        ptags = {t.lower() for t in (p.get("tags") or [])}
        ttoks = _tokens(p.get("title", ""))
        try:
            btoks = _tokens(store.get_plan(p["id"]).get("body", ""))
        except Exception:
            btoks = set()
        tag_ov, title_ov, body_ov = len(gt & ptags), len(gt & ttoks), len(gt & btoks)
        score = 3 * tag_ov + 2 * title_ov + body_ov
        if score:
            why = []
            if tag_ov:
                why.append("tags: " + ", ".join(sorted(gt & ptags)))
            if title_ov:
                why.append("title: " + ", ".join(sorted(gt & ttoks)))
            rows.append({"id": p["id"], "title": p["title"], "status": p["status"],
                         "priority": p["priority"], "progress": p["progress"],
                         "score": score, "why": "; ".join(why) or "body overlap"})
    rows.sort(key=lambda r: -r["score"])
    return rows[:limit]


def _from_legacy(d: dict) -> dict:
    """MemoryOS /retrieve response → the plan_context memory dict."""
    prof = d.get("user_profile")
    return {
        "user_profile": prof if prof and prof != "None" else None,
        "user_knowledge": d.get("retrieved_user_knowledge", []),
        "assistant_knowledge": d.get("retrieved_assistant_knowledge", []),
        "pages": d.get("retrieved_pages", []),
    }


def _from_astoria(d: dict) -> dict:
    """Astoria /recall response → the same memory dict shape (facts → user_knowledge,
    episodes → pages as {"meta_info": text}); assistant_knowledge stays empty."""
    prof = d.get("profile") or {}
    narrative = (prof.get("narrative") or "").strip() if isinstance(prof, dict) else ""
    knowledge, pages = [], []
    for it in d.get("items") or []:
        if not isinstance(it, dict):
            continue
        text = (it.get("text") or "").strip()
        if not text:
            continue
        if it.get("kind") == "episode" or it.get("layer") == "episodic":
            pages.append({"meta_info": text, "id": it.get("id"),
                          "occurred_at": it.get("occurred_at")})
        else:
            knowledge.append({"knowledge": text, "confidence": it.get("confidence"),
                              "layer": it.get("layer"), "id": it.get("id")})
    return {"user_profile": narrative or None, "user_knowledge": knowledge,
            "assistant_knowledge": [], "pages": pages}


def memory_retrieve(query: str):
    """Return (memory_dict, degraded). Never raises."""
    try:
        if _is_legacy(MEMORY_URL):
            r = httpx.post(MEMORY_URL, json={"user_id": MEMORY_USER, "query": query},
                           timeout=httpx.Timeout(4.0, read=10.0))
            r.raise_for_status()
            return _from_legacy(r.json()), False
        r = httpx.post(MEMORY_URL, headers=_headers(),
                       json={"user_id": MEMORY_USER, "query": query, "layers": MEMORY_LAYERS,
                             "limit": 8, "max_tokens": 800, "include_profile": True},
                       timeout=httpx.Timeout(4.0, read=10.0))
        r.raise_for_status()
        return _from_astoria(r.json()), False
    except Exception:
        return None, True


def plan_context(goal: str, tags=None, limit=5, exclude=None) -> dict:
    mem, degraded = memory_retrieve(goal)
    return {"goal": goal, "related_plans": related_plans(goal, tags, limit, exclude),
            "memory": mem, "degraded": degraded}
