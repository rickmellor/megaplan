"""MegaPlan scheduling: a CPM pass over a plan's task graph.

Dates are DERIVED, never stored — the same precedent as `store.progress()`. Give tasks
durations, typed links and the odd constraint; this computes early/late dates, float and
the critical path, and the Gantt is then just a rendering of the result.

Time is elapsed days (no working calendar, by design): a float day-offset from the plan
anchor, converted to dates with `timedelta`. `end` is EXCLUSIVE (`start + dur`), which is
what Mermaid's `start, duration` form expects and sidesteps the off-by-one.

Link semantics, with `lag` in days (negative = lead):

    FS  succ.ES >= pred.EF + lag        SS  succ.ES >= pred.ES + lag
    FF  succ.EF >= pred.EF + lag        SF  succ.EF >= pred.ES + lag
"""

from __future__ import annotations

from datetime import date, timedelta

import store

EPS = 1e-9
DEFAULT_DUR_DAYS = 1.0          # MS Project's own default for a task with no duration


def _d(anchor: date, offset: float) -> str:
    return (anchor + timedelta(days=offset)).isoformat()


def _parse_date(s):
    try:
        return date.fromisoformat(str(s)[:10])
    except (TypeError, ValueError):
        return None


# ---- graph -------------------------------------------------------------------

def _topo(ids, edges, warnings):
    """Kahn's algorithm. Nodes left over are in a cycle — report them, never hang."""
    indeg = {i: 0 for i in ids}
    succ = {i: [] for i in ids}
    for pred, s in edges:
        succ[pred].append(s)
        indeg[s] += 1
    queue = sorted([i for i in ids if indeg[i] == 0])
    order = []
    while queue:
        n = queue.pop(0)
        order.append(n)
        for s in sorted(succ[n]):
            indeg[s] -= 1
            if indeg[s] == 0:
                queue.append(s)
    if len(order) < len(ids):
        stuck = sorted(set(ids) - set(order))
        warnings.append({"code": "cycle", "tasks": stuck,
                         "message": f"dependency cycle among {', '.join(stuck)} — links ignored"})
    return order, succ


def _duration(t, warnings):
    if t.get("is_summary"):
        return 0.0                       # rolled up from children after the passes
    d = t.get("dur_days")
    if d is None and t.get("pert"):
        o, m, p = t["pert"]["o"], t["pert"]["m"], t["pert"]["p"]
        d = (o + 4 * m + p) / 6.0
    if d is None:
        warnings.append({"code": "no_duration", "tasks": [t["id"]],
                         "message": f"{t['id']} has no `dur` — assumed {DEFAULT_DUR_DAYS:g}d"})
        d = DEFAULT_DUR_DAYS
    return max(0.0, float(d))


def _pert_sigma(t):
    if not t.get("pert"):
        return 0.0
    return (t["pert"]["p"] - t["pert"]["o"]) / 6.0


# ---- the pass ----------------------------------------------------------------

def compute(pid: str, _seen=None) -> dict:
    """Schedule one plan. Cross-plan links (`otherplan#tN`) pull that plan in, memoized."""
    _seen = _seen if _seen is not None else {}
    if pid in _seen:
        return _seen[pid]
    _seen[pid] = {"id": pid, "tasks": {}, "warnings": []}      # cycle guard across plans

    post = store.read_post(pid)
    tasks = store.link_parents(store.assign_ids(store.parse_tasks(post.content)))
    anchor = _parse_date(store.plan_anchor(post)) or date.today()
    warnings: list[dict] = []
    by_id = {t["id"]: t for t in tasks}

    dur = {t["id"]: _duration(t, warnings) for t in tasks}

    # --- resolve links -------------------------------------------------------
    # `external` holds finish offsets (in THIS plan's anchor frame) for cross-plan preds.
    edges, links, external = [], {i: [] for i in by_id}, {}
    for t in tasks:
        for dep in t.get("deps") or []:
            if t.get("is_summary"):
                warnings.append({"code": "summary_dep", "tasks": [t["id"]],
                                 "message": f"{t['id']} is a summary task — its `dep` is ignored "
                                            "(link its children instead)"})
                continue
            if dep.get("plan"):
                ext = _external_offsets(dep, anchor, warnings, _seen)
                if ext is None:
                    continue
                external.setdefault(t["id"], []).append((dep, ext))
                continue
            if dep["task"] not in by_id:
                warnings.append({"code": "missing_dep", "tasks": [t["id"]],
                                 "message": f"{t['id']} depends on unknown task {dep['task']}"})
                continue
            edges.append((dep["task"], t["id"]))
            links[t["id"]].append(dep)

    order, succ = _topo(list(by_id), edges, warnings)
    if len(order) < len(by_id):                  # cycle: fall back to file order, links dropped
        order = [t["id"] for t in tasks]
        links = {i: [] for i in by_id}
        edges, succ = [], {i: [] for i in by_id}

    # --- forward pass --------------------------------------------------------
    es, ef = {}, {}
    for tid in order:
        t, d = by_id[tid], dur[tid]
        start = 0.0
        for dep in links[tid]:
            p = dep["task"]
            lag, ty = dep["lag_days"], dep["type"]
            if ty == "FS":
                start = max(start, ef[p] + lag)
            elif ty == "SS":
                start = max(start, es[p] + lag)
            elif ty == "FF":
                start = max(start, ef[p] + lag - d)
            elif ty == "SF":
                start = max(start, es[p] + lag - d)
        for dep, (p_es, p_ef) in external.get(tid, []):
            lag, ty = dep["lag_days"], dep["type"]
            base = {"FS": p_ef + lag, "SS": p_es + lag,
                    "FF": p_ef + lag - d, "SF": p_es + lag - d}[ty]
            start = max(start, base)
        snet = _parse_date(t.get("start"))       # start-no-earlier-than constraint
        if snet:
            start = max(start, (snet - anchor).days)
        es[tid], ef[tid] = start, start + d

    project_finish = max(ef.values()) if ef else 0.0

    # --- backward pass -------------------------------------------------------
    ls, lf = {}, {}
    for tid in reversed(order):
        d = dur[tid]
        finish = project_finish
        for s in succ[tid]:
            for dep in links[s]:
                if dep["task"] != tid:
                    continue
                lag, ty = dep["lag_days"], dep["type"]
                if ty == "FS":
                    finish = min(finish, ls[s] - lag)
                elif ty == "SS":
                    finish = min(finish, ls[s] - lag + d)
                elif ty == "FF":
                    finish = min(finish, lf[s] - lag)
                elif ty == "SF":
                    finish = min(finish, lf[s] - lag + d)
        lf[tid], ls[tid] = finish, finish - d

    # --- float ---------------------------------------------------------------
    rows = {}
    for tid in by_id:
        total = ls[tid] - es[tid]
        # free float = how far this task can slip before it delays ANY successor's early
        # start. Credit the link's lag, or an FS+2d link reads as 2d of float it hasn't got.
        slack = []
        for s in succ[tid]:
            for dep in links[s]:
                if dep["task"] != tid:
                    continue
                lag, ty = dep["lag_days"], dep["type"]
                if ty == "FS":
                    slack.append(es[s] - lag - ef[tid])
                elif ty == "SS":
                    slack.append(es[s] - lag - es[tid])
                elif ty == "FF":
                    slack.append(ef[s] - lag - ef[tid])
                elif ty == "SF":
                    slack.append(ef[s] - lag - es[tid])
        free = min(slack) if slack else (project_finish - ef[tid])
        rows[tid] = {"es": es[tid], "ef": ef[tid], "ls": ls[tid], "lf": lf[tid],
                     "total_float": round(total, 4),
                     "free_float": round(max(0.0, min(free, total)), 4),
                     "critical": total <= EPS}

    # --- summary roll-up (children now have dates) ---------------------------
    for t in sorted(tasks, key=lambda x: -x["level"]):
        if not t.get("is_summary"):
            continue
        kids = [rows[c] for c in t["children"] if c in rows]
        if not kids:
            continue
        r = rows[t["id"]]
        r["es"] = min(k["es"] for k in kids)
        r["ef"] = max(k["ef"] for k in kids)
        r["ls"], r["lf"] = r["es"], r["ef"]
        r["total_float"] = round(min(k["total_float"] for k in kids), 4)
        r["free_float"] = 0.0
        r["critical"] = any(k["critical"] for k in kids)
        dur[t["id"]] = r["ef"] - r["es"]

    # --- assemble ------------------------------------------------------------
    base = (post.get("baseline") or {}).get("tasks") or {}
    out_tasks = []
    for t in tasks:
        tid = t["id"]
        r = rows[tid]
        row = {
            "id": tid, "text": t["text"], "level": t["level"], "parent": t.get("parent"),
            "is_summary": t.get("is_summary", False),
            "is_milestone": t.get("is_milestone", False),
            "done": t["done"], "pct": _rollup_pct(t, by_id, dur),
            "who": t.get("who"), "blocked": t.get("blocked", False),
            "duration_days": round(dur[tid], 4),
            "start": _d(anchor, r["es"]), "end": _d(anchor, r["ef"]),
            "late_start": _d(anchor, r["ls"]), "late_end": _d(anchor, r["lf"]),
            "total_float_days": r["total_float"], "free_float_days": r["free_float"],
            "critical": r["critical"],
            "deps": t.get("deps") or [], "est_hours": t.get("est_hours"),
            "spent_hours": t.get("spent_hours"),
        }
        if t.get("deadline"):
            row["deadline"] = str(t["deadline"])[:10]
            dl = _parse_date(t["deadline"])
            if dl and r["ef"] > (dl - anchor).days + EPS:
                row["deadline_missed_days"] = round(r["ef"] - (dl - anchor).days, 2)
                warnings.append({"code": "deadline_missed", "tasks": [tid],
                                 "message": f"{tid} finishes {row['deadline_missed_days']:g}d "
                                            f"after its {row['deadline']} deadline"})
        b = base.get(tid)
        if b:
            row["baseline_start"], row["baseline_end"] = b.get("start"), b.get("end")
            bs, be = _parse_date(b.get("start")), _parse_date(b.get("end"))
            if bs and be:
                row["start_variance_days"] = (anchor + timedelta(days=r["es"]) - bs).days
                row["finish_variance_days"] = (anchor + timedelta(days=r["ef"]) - be).days
        out_tasks.append(row)

    crit = [t["id"] for t in out_tasks if t["critical"] and not t["is_summary"]]
    sigma = sum(_pert_sigma(by_id[c]) ** 2 for c in crit) ** 0.5

    result = {
        "id": pid, "title": post.get("title"), "anchor": anchor.isoformat(),
        "start": _d(anchor, min((r["es"] for r in rows.values()), default=0.0)),
        "finish": _d(anchor, project_finish),
        "duration_days": round(project_finish, 4),
        "critical_path": crit,
        "pert_sigma_days": round(sigma, 3) if sigma else None,
        "baseline": (post.get("baseline") or {}).get("captured"),
        "tasks": out_tasks,
        "warnings": warnings,
    }
    _seen[pid] = result
    return result


def _rollup_pct(t, by_id, dur):
    """Summary % complete is duration-weighted across its immediate children."""
    if not t.get("is_summary"):
        return store.effective_pct(t)
    num = den = 0.0
    for c in t["children"]:
        k = by_id.get(c)
        if not k:
            continue
        w = max(dur.get(c, 0.0), EPS)
        num += w * _rollup_pct(k, by_id, dur)
        den += w
    return round(num / den) if den else 0


def _external_offsets(dep, anchor, warnings, seen):
    """Schedule the referenced plan and translate its predecessor dates into this frame."""
    try:
        other = compute(dep["plan"], seen)
    except KeyError:
        warnings.append({"code": "missing_plan", "tasks": [],
                         "message": f"cross-plan dep references unknown plan {dep['plan']}"})
        return None
    row = next((r for r in other.get("tasks", []) if r["id"] == dep["task"]), None)
    if not row:
        warnings.append({"code": "missing_dep", "tasks": [],
                         "message": f"{dep['plan']}#{dep['task']} not found"})
        return None
    s, e = _parse_date(row["start"]), _parse_date(row["end"])
    return ((s - anchor).days, (e - anchor).days)


# ---- Mermaid emitter ---------------------------------------------------------

_MERMAID_BAD = str.maketrans({":": "∶", "#": "", ";": ","})


def mermaid(pid: str, group: str = "outline", include_done: bool = True) -> str:
    """Mermaid `gantt` source for a plan.

    Mermaid limits worth knowing: no baseline bars, no assignee swimlanes (sections are the
    only grouping), and lag is not drawn — it is implicit in the computed positions. Those
    are the reasons an MSPDI export is the eventual answer for real Gantt fidelity.
    """
    sch = compute(pid)
    lines = ["gantt", f"    title {sch['title'] or pid}", "    dateFormat YYYY-MM-DD",
             "    axisFormat %b %d"]
    rows = [t for t in sch["tasks"] if include_done or not t["done"]]

    if group == "who":
        buckets: dict[str, list] = {}
        for t in rows:
            buckets.setdefault(t.get("who") or "unassigned", []).append(t)
        groups = list(buckets.items())
    else:
        groups, cur = [], ("", [])
        for t in rows:
            if t["is_summary"]:
                cur = (t["text"], [])
                groups.append(cur)
            else:
                if not groups:
                    cur = ("Tasks", [])
                    groups.append(cur)
                cur[1].append(t)

    for name, items in groups:
        if not items:
            continue
        lines.append(f"    section {name.translate(_MERMAID_BAD)}")
        for t in items:
            tags = []
            if t["is_milestone"]:
                tags.append("milestone")
            if t["critical"] and not t["is_milestone"]:
                tags.append("crit")
            if t["done"]:
                tags.append("done")
            elif t["pct"]:
                tags.append("active")
            label = t["text"].translate(_MERMAID_BAD).strip() or t["id"]
            if t["pct"] and not t["done"]:
                label += f" ({t['pct']}%)"
            spec = ", ".join(tags + [t["id"], t["start"], f"{t['duration_days']:g}d"])
            lines.append(f"    {label} :{spec}")
    return "\n".join(lines) + "\n"


def baseline(pid: str, op: str = "capture") -> dict:
    if op == "clear":
        store.clear_baseline(pid)
        return {"id": pid, "baseline": None}
    sch = compute(pid)
    dates = {t["id"]: {"start": t["start"], "end": t["end"]} for t in sch["tasks"]}
    store.save_baseline(pid, sch["anchor"], dates)
    return {"id": pid, "baseline": {"captured": True, "project_start": sch["anchor"],
                                    "tasks": len(dates)}}
