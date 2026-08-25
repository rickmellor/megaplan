"""Progress reports — compose a plan's schedule, effort and task state into one readable
markdown document and save it into the store, so every report has a stable URL.

Reports live in `<DATA>/reports/` and are committed like any other mutation: a progress report
is a point-in-time record of where something stood, which is exactly the kind of thing worth
having history for. `store.list_plans` globs `<DATA>/*.md` non-recursively, so the subdirectory
is invisible to the plan store itself.
"""

import glob
import os
import re
from datetime import datetime, timezone

import schedule
import store

REPORTS_DIRNAME = "reports"
# The service cannot know its own external address, so it is configured. Used only to build the
# `url` handed back to callers; the file itself is served from <PUBLIC_URL>/reports/<name>.
PUBLIC_URL = os.environ.get("MEGAPLAN_PUBLIC_URL", "http://localhost:8932").rstrip("/")
TASK_TEXT_MAX = 200      # a task line is a reminder, not the task; the plan itself has the full text


def reports_dir() -> str:
    d = os.path.join(store.DATA, REPORTS_DIRNAME)
    os.makedirs(d, exist_ok=True)
    return d


def _bar(pct: int, width: int = 28) -> str:
    filled = round((max(0, min(100, pct)) / 100) * width)
    return "█" * filled + "░" * (width - filled)


def _hours(h) -> str:
    if not h:
        return "—"
    return f"{h:g}h" if h < 8 else f"{h:g}h ({h / 8:.1f}d)"


def _intent(body: str, limit: int = 900) -> str:
    """The plan's own prose — everything before the first `##` section. A plan states its
    purpose at the top; a report should quote that rather than re-derive it from tasks."""
    text = re.split(r"^##\s", body or "", maxsplit=1, flags=re.M)[0]
    text = re.sub(r"^#\s+.*$", "", text, count=1, flags=re.M).strip()   # drop the H1 title
    if len(text) > limit:
        cut = text[:limit].rsplit("\n\n", 1)[0] or text[:limit]
        text = cut.rstrip() + " …"
    return text


def _warnings(ws: list) -> str:
    """Group by code. A 33-task plan with no durations emits 33 near-identical warnings; a
    report that lists them all buries everything else."""
    if not ws:
        return ""
    by = {}
    for w in ws:
        by.setdefault(w.get("code", "warning"), []).append(w)
    out = []
    for code, group in sorted(by.items(), key=lambda kv: -len(kv[1])):
        ids = [t for w in group for t in (w.get("tasks") or [])]
        if len(group) == 1:
            out.append(f"- **{code}** — {group[0].get('message', '')}")
        else:
            shown = ", ".join(ids[:6]) + (f" … +{len(ids) - 6} more" if len(ids) > 6 else "")
            out.append(f"- **{code}** × {len(group)} — {group[0].get('message', '')}"
                       f"\n  <br>affects: {shown}")
    return "\n".join(out)


def _task_line(t: dict) -> str:
    bits = []
    if t.get("critical") and not t.get("is_summary"):
        bits.append("critical")
    if t.get("who"):
        bits.append(str(t["who"]))
    if t.get("deadline"):
        bits.append(f"due {t['deadline']}"
                    + (f" — **{t['deadline_missed_days']:g}d late**" if t.get("deadline_missed_days") else ""))
    if t.get("est_hours"):
        bits.append(f"est {_hours(t['est_hours'])}")
    tail = f"  <sub>{' · '.join(bits)}</sub>" if bits else ""
    text = " ".join((t.get("text") or "").split())
    if len(text) > TASK_TEXT_MAX:      # plan tasks here run to essay length; a report is a summary
        text = text[:TASK_TEXT_MAX].rstrip(" ,;.—-") + "…"
    return f"- `{t['id']}` {text}{tail}"


def _tracking(sch: dict) -> str:
    """Baseline comparison. Mermaid cannot overlay baseline bars on a gantt (see schedule.mermaid),
    so the precise view is a variance table, and the visual is a SECOND gantt containing only the
    tasks that actually moved — bounded, and the only part anyone looks at."""
    moved = [t for t in sch["tasks"]
             if not t.get("is_summary")
             and (t.get("start_variance_days") or t.get("finish_variance_days"))]
    if not any(t.get("baseline_start") for t in sch["tasks"]):
        return ""
    head = "## Tracking — against baseline\n"
    if not moved:
        return head + "\nNothing has moved: every task still starts and finishes on its baseline dates.\n"

    rows = ["| task | baseline | now | slip |", "|---|---|---|---|"]
    for t in sorted(moved, key=lambda x: -(x.get("finish_variance_days") or 0))[:25]:
        fv = t.get("finish_variance_days") or 0
        slip = f"**+{fv:g}d**" if fv > 0 else (f"{fv:g}d" if fv < 0 else "—")
        rows.append(f"| `{t['id']}` {' '.join((t.get('text') or '').split())[:44]} "
                    f"| {t.get('baseline_start')} → {t.get('baseline_end')} "
                    f"| {t['start']} → {t['end']} | {slip} |")
    extra = f"\n*{len(moved) - 25} further changed tasks not shown.*\n" if len(moved) > 25 else ""

    g = ["```mermaid", "gantt", "    title Baseline vs current — tasks that moved",
         "    dateFormat YYYY-MM-DD", "    axisFormat %b %d", "    section Baseline"]
    for t in moved[:12]:
        g.append(f"    {schedule._label(t.get('text'), t['id'], 42)} :done, b{t['id']}, "
                 f"{t['baseline_start']}, {t['baseline_end']}")
    g.append("    section Current")
    for t in moved[:12]:
        g.append(f"    {schedule._label(t.get('text'), t['id'], 42)} :"
                 f"{'crit, ' if (t.get('finish_variance_days') or 0) > 0 else ''}c{t['id']}, "
                 f"{t['start']}, {t['end']}")
    g.append("```")
    chart = "\n".join(g) if moved else ""
    return head + "\n" + "\n".join(rows) + "\n" + extra + "\n" + chart + "\n"


def compose(pid: str) -> str:
    """The whole report as markdown. Raises KeyError/FileNotFoundError via store if pid is unknown."""
    plan = store.get_plan(pid)
    sch = schedule.compute(pid)
    deps = store.list_dependencies(pid)
    blocked_by = store.blocked_by(pid)
    prog = plan.get("progress") or {}
    tm = plan.get("time") or {}
    tasks = sch["tasks"]
    leaves = [t for t in tasks if not t.get("is_summary")]

    done = [t for t in leaves if t.get("done")]
    blocked = [t for t in leaves if t.get("blocked") and not t.get("done")]
    wip = [t for t in leaves if not t.get("done") and not t.get("blocked") and (t.get("pct") or 0) > 0]
    todo = [t for t in leaves if not t.get("done") and not t.get("blocked") and not (t.get("pct") or 0)]
    est = tm.get("est_hours") or sum(t.get("est_hours") or 0 for t in leaves)
    spent = tm.get("spent_hours") or sum(t.get("spent_hours") or 0 for t in leaves)
    crit = sch.get("critical_path") or []
    now = datetime.now(timezone.utc).astimezone()

    L = [f"# {plan.get('title') or pid} — progress report", ""]
    tags = " ".join(f"`{t}`" for t in (plan.get("tags") or []))
    badge = f"`{pid}` · **{plan.get('status', '?')}** · {plan.get('priority', '?')} priority"
    if tags:
        badge += f" · {tags}"
    L.append(badge)
    L.append(f"*generated {now:%Y-%m-%d %H:%M %Z} · "
             f"plan last updated {str(plan.get('updated', ''))[:10]}*")
    L.append("")
    L.append(f"`{_bar(prog.get('pct', 0))}`  **{prog.get('pct', 0)}%** — "
             f"{prog.get('done', 0)} of {prog.get('total', 0)} tasks complete")
    L.append("")

    intent = _intent(plan.get("body", ""))
    if intent:
        L += ["## Intent", "", intent, ""]

    # An unscheduled plan still produces a schedule — every task defaults to 1 day, so the
    # project looks 1 day long and EVERY task lands on the critical path. Presenting that as
    # fact would be misleading, so detect it and say so instead.
    undated = {t for w in (sch.get("warnings") or []) if w.get("code") == "no_duration"
               for t in (w.get("tasks") or [])}
    unscheduled = leaves and len(undated) >= max(1, int(0.8 * len(leaves)))

    # Built row by row rather than as one list literal: every element here is a table row, and
    # implicit string concatenation inside a collection turns a missing comma into a silent merge.
    rows = []
    rows.append(f"| Tasks | {prog.get('done', 0)} done · {len(wip)} in progress · "
                f"{len(blocked)} blocked · {len(todo)} not started |")
    effort = (f"{_hours(spent)} logged · {_hours(max(0, est - spent))} remaining"
              if spent else "nothing logged yet")
    rows.append(f"| Effort | {_hours(est)} estimated · {effort} |")
    rows.append(f"| Schedule | {sch['start']} → {sch['finish']} · {sch['duration_days']:g} days |")
    if unscheduled:
        rows.append("| Critical path | not meaningful yet — see below |")
    elif crit:
        chain = " → ".join(f"`{c}`" for c in crit[:6]) + (" → …" if len(crit) > 6 else "")
        rows.append(f"| Critical path | {len(crit)} tasks — {chain} |")
    else:
        rows.append("| Critical path | none |")
    if sch.get("pert_sigma_days"):
        rows.append(f"| Confidence | PERT σ ±{sch['pert_sigma_days']:g} days on the critical path |")
    if blocked_by:
        rows.append(f"| Blocked by | {', '.join(f'`{b}`' for b in blocked_by)} |")
    if (deps or {}).get("dependents"):
        rows.append(f"| Blocks | {', '.join(f'`{d}`' for d in deps['dependents'])} |")
    L += ["## Where it stands", "", "| | |", "|---|---|", *rows, ""]

    warn = _warnings(sch.get("warnings") or [])
    if warn:
        L += ["### Notes from the scheduler", "", warn, ""]

    L += ["## Schedule", ""]
    if unscheduled:
        L.append(f"> **This plan is not scheduled.** {len(undated)} of {len(leaves)} tasks carry "
                 "no `dur`, so each is assumed to take a day — which makes the project look one "
                 "day long and puts every task on the critical path. The chart below shows "
                 "structure and dependencies, not dates. Add durations "
                 "(`- [ ] the task (dur: 3d)`) for a real schedule.")
        L.append("")
    L += ["```mermaid", schedule.mermaid(pid, "outline", True, 52), "```", ""]

    track = _tracking(sch)
    if track:
        L += [track, ""]

    L += ["## Tasks", ""]
    for label, group in (("In progress", wip), ("Blocked", blocked),
                         ("Not started", todo), ("Complete", done)):
        if not group:
            continue
        L += [f"### {label} ({len(group)})", ""]
        L += [_task_line(t) for t in group]
        L.append("")

    L.append("---")
    L.append(f"*MegaPlan report for `{pid}` · regenerate with "
             f"`megaplan(action=\"render\", id=\"{pid}\")`*")
    return "\n".join(L) + "\n"


def render(pid: str, save: bool = True) -> dict:
    """Compose and (by default) save. Returns the report's location so a caller can link it."""
    md = compose(pid)
    if not save:
        return {"id": pid, "format": "markdown", "saved": False, "markdown": md}
    name = f"{pid}-{datetime.now().strftime('%Y%m%d-%H%M%S')}.md"
    path = os.path.join(reports_dir(), name)
    with open(path, "w", encoding="utf-8") as f:
        f.write(md)
    latest = os.path.join(reports_dir(), f"{pid}-latest.md")
    with open(latest, "w", encoding="utf-8") as f:      # stable URL for "the current report"
        f.write(md)
    try:
        store._commit([path, latest], f"report {pid}")
    except Exception:
        pass                                            # a report is worth having even uncommitted
    return {"id": pid, "format": "markdown", "saved": True, "file": f"{REPORTS_DIRNAME}/{name}",
            "url": f"{PUBLIC_URL}/reports/{name}",
            "latest_url": f"{PUBLIC_URL}/reports/{pid}-latest.md",
            "bytes": len(md.encode()), "generated": datetime.now(timezone.utc).isoformat()}


def list_reports(pid: str | None = None) -> list:
    out = []
    for p in sorted(glob.glob(os.path.join(reports_dir(), "*.md")), reverse=True):
        name = os.path.basename(p)
        if name.endswith("-latest.md"):
            continue
        if pid and not name.startswith(pid + "-"):
            continue
        out.append({"file": f"{REPORTS_DIRNAME}/{name}", "url": f"{PUBLIC_URL}/reports/{name}",
                    "bytes": os.path.getsize(p),
                    "generated": datetime.fromtimestamp(os.path.getmtime(p)).isoformat(timespec="seconds")})
    return out
