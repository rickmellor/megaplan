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


# --- HTML view --------------------------------------------------------------------------------
# The markdown IS the artifact — agents read it, git diffs it. But `render` hands back a URL, and
# a URL that shows raw source (mermaid fences included) is not a report. So every render also
# writes a self-contained HTML sibling: prose and tables converted server-side, gantt drawn by a
# mermaid bundled INTO the image, so a report renders with no internet and no CDN.
_MERMAID_SRC = "/static/mermaid.min.js"
_FENCE = re.compile(r"^```mermaid\n(.*?)^```", re.M | re.S)

_CSS = """
:root { --bg:#fff; --fg:#1a1a1a; --muted:#666; --rule:#e3e3e3; --code:#f6f6f6; --accent:#2a6; }
@media (prefers-color-scheme: dark) {
  :root { --bg:#15171a; --fg:#e6e6e6; --muted:#9aa0a6; --rule:#2c2f34; --code:#1d2024; --accent:#4c9; }
}
* { box-sizing: border-box; }
body { background:var(--bg); color:var(--fg); margin:0; padding:2.5rem 1.25rem 5rem;
       font:16px/1.65 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, sans-serif; }
main { max-width: 62rem; margin: 0 auto; }
h1 { font-size:1.9rem; line-height:1.25; margin:0 0 .4rem; }
h2 { font-size:1.3rem; margin:2.4rem 0 .8rem; padding-bottom:.3rem; border-bottom:1px solid var(--rule); }
h3 { font-size:1.05rem; margin:1.6rem 0 .5rem; color:var(--muted); text-transform:uppercase;
     letter-spacing:.05em; font-weight:600; }
p, li { margin:.5rem 0; }
em { color:var(--muted); font-style:normal; }
code { background:var(--code); padding:.12em .38em; border-radius:4px; font-size:.88em;
       font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }
pre code { background:none; padding:0; }
table { border-collapse:collapse; margin:1rem 0; width:100%; font-size:.94rem; }
th, td { text-align:left; padding:.45rem .7rem; border-bottom:1px solid var(--rule); vertical-align:top; }
th { color:var(--muted); font-weight:600; font-size:.82rem; text-transform:uppercase; letter-spacing:.04em; }
tr:last-child td { border-bottom:none; }
blockquote { margin:1.2rem 0; padding:.8rem 1rem; border-left:3px solid var(--accent);
             background:var(--code); border-radius:0 6px 6px 0; }
blockquote p { margin:0; }
sub { color:var(--muted); font-size:.8em; }
hr { border:none; border-top:1px solid var(--rule); margin:2.5rem 0 1rem; }
ul { padding-left:1.3rem; }
.mermaid { background:var(--code); border-radius:8px; padding:1rem; margin:1.2rem 0;
           overflow-x:auto; text-align:center; }
.progress { font-family: ui-monospace, monospace; letter-spacing:-1px; }
"""


def html(md: str, title: str) -> str:
    """Wrap a report's markdown in a self-contained page. Mermaid fences are pulled out before
    conversion (a markdown converter would escape them) and put back as .mermaid divs."""
    charts, holder = [], "\u0000CHART%d\u0000"

    def stash(m):
        charts.append(m.group(1))
        return holder % (len(charts) - 1)

    body = _FENCE.sub(stash, md)
    try:
        import markdown as _md
        body = _md.markdown(body, extensions=["tables", "fenced_code", "sane_lists"])
    except ImportError:                      # degrade to readable rather than fail
        body = "<pre>" + body.replace("&", "&amp;").replace("<", "&lt;") + "</pre>"
    for i, src in enumerate(charts):
        esc = src.replace("&", "&amp;").replace("<", "&lt;")
        body = body.replace(holder % i, f'<div class="mermaid">{esc}</div>')
    return (
        "<!doctype html>\n<html lang=\"en\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        f"<title>{title}</title><style>{_CSS}</style></head><body><main>\n{body}\n</main>"
        f'<script src="{_MERMAID_SRC}"></script>'
        "<script>mermaid.initialize({startOnLoad:true,securityLevel:'loose',"
        "theme:matchMedia('(prefers-color-scheme: dark)').matches?'dark':'default'});</script>"
        "</body></html>\n")


def render(pid: str, save: bool = True) -> dict:
    """Compose and (by default) save. Returns the report's location so a caller can link it."""
    md = compose(pid)
    if not save:
        return {"id": pid, "format": "markdown", "saved": False, "markdown": md}
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    title = f"{pid} — progress report"
    written = _write_pair(f"{pid}-{stamp}", md, title) + _write_pair(f"{pid}-latest", md, title)
    try:
        store._commit(written, f"report {pid}")
    except Exception:
        pass                                            # a report is worth having even uncommitted
    return {"id": pid, "format": "markdown", "saved": True,
            "file": f"{REPORTS_DIRNAME}/{pid}-{stamp}.md",
            "url": f"{PUBLIC_URL}/reports/{pid}-{stamp}.html",       # the readable one first
            "markdown_url": f"{PUBLIC_URL}/reports/{pid}-{stamp}.md",
            "latest_url": f"{PUBLIC_URL}/reports/{pid}-latest.html",
            "bytes": len(md.encode()), "generated": datetime.now(timezone.utc).isoformat()}


def _write_pair(stem: str, md: str, title: str) -> list:
    """Write <stem>.md and its rendered <stem>.html. Returns the paths written."""
    d, out = reports_dir(), []
    for name, text in ((f"{stem}.md", md), (f"{stem}.html", html(md, title))):
        with open(os.path.join(d, name), "w", encoding="utf-8") as f:
            f.write(text)
        out.append(os.path.join(d, name))
    return out


def list_reports(pid: str | None = None) -> list:
    out = []
    for p in sorted(glob.glob(os.path.join(reports_dir(), "*.md")), reverse=True):
        name = os.path.basename(p)
        if name.endswith("-latest.md"):
            continue
        if pid and not name.startswith(pid + "-"):
            continue
        out.append({"file": f"{REPORTS_DIRNAME}/{name}",
                    "url": f"{PUBLIC_URL}/reports/{name[:-3]}.html",
                    "markdown_url": f"{PUBLIC_URL}/reports/{name}",
                    "bytes": os.path.getsize(p),
                    "generated": datetime.fromtimestamp(os.path.getmtime(p)).isoformat(timespec="seconds")})
    return out


# --- portfolio ---------------------------------------------------------------------------------
def compose_portfolio(include_done: bool = False) -> str:
    """One document across every plan: where the whole body of work stands, rather than one plan.

    Per-plan scheduling is computed defensively — one unschedulable plan must not take the
    portfolio down with it."""
    rows, charted, now = [], [], datetime.now(timezone.utc).astimezone()
    done_t = total_t = est_h = spent_h = 0

    for p in store.list_plans(include_archived=False):
        pid, status = p["id"], p.get("status", "?")
        if status == "done" and not include_done:
            continue
        full = store.get_plan(pid)
        prog = full.get("progress") or {}
        tm = full.get("time") or {}
        done_t += prog.get("done", 0); total_t += prog.get("total", 0)
        est_h += tm.get("est_hours") or 0; spent_h += tm.get("spent_hours") or 0
        start = finish = None
        try:
            sch = schedule.compute(pid)
            start, finish = sch["start"], sch["finish"]
            # A plan with a couple of undated tasks still has a real shape; one that is ENTIRELY
            # undated does not (every task defaults to a day). Same 80% test the per-plan report
            # uses to decide whether its own schedule is meaningful.
            leaves = [t for t in sch["tasks"] if not t.get("is_summary")]
            undated = {t for w in (sch.get("warnings") or []) if w.get("code") == "no_duration"
                       for t in (w.get("tasks") or [])}
            if leaves and len(undated) < max(1, int(0.8 * len(leaves))):
                charted.append((pid, full.get("title") or pid, status, start, finish))
        except Exception:
            pass
        rows.append({"id": pid, "title": full.get("title") or pid, "status": status,
                     "priority": p.get("priority", ""), "prog": prog,
                     "blocked_by": store.blocked_by(pid), "start": start, "finish": finish,
                     "updated": str(full.get("updated", ""))[:10]})

    pct = round(100 * done_t / total_t) if total_t else 0
    L = ["# Portfolio — all active plans", ""]
    L.append(f"*generated {now:%Y-%m-%d %H:%M %Z} · {len(rows)} plans*")
    L.append("")
    L.append(f"`{_bar(pct)}`  **{pct}%** — {done_t} of {total_t} tasks complete across the portfolio")
    L.append("")
    if est_h:
        L.append(f"Effort: {_hours(est_h)} estimated · "
                 + (f"{_hours(spent_h)} logged · {_hours(max(0, est_h - spent_h))} remaining"
                    if spent_h else "nothing logged yet"))
        L.append("")

    L += ["## Plans", "", "| plan | status | progress | schedule | blocked by |", "|---|---|---|---|---|"]
    for r in sorted(rows, key=lambda x: (x["status"] != "active", -(x["prog"].get("pct") or 0))):
        pr = r["prog"]
        when = f"{r['start']} → {r['finish']}" if r["start"] else "—"
        blocked = ", ".join(f"`{b}`" for b in r["blocked_by"]) or "—"
        L.append(f"| [{r['title'][:52]}]({PUBLIC_URL}/reports/{r['id']}-latest.html) "
                 f"| {r['status']} | {pr.get('pct', 0)}% ({pr.get('done', 0)}/{pr.get('total', 0)}) "
                 f"| {when} | {blocked} |")
    L.append("")

    if charted:
        L += ["## Timeline", "",
              "*Only plans with task durations appear — an unscheduled plan has no real dates.*", "",
              "```mermaid", "gantt", "    title Portfolio", "    dateFormat YYYY-MM-DD",
              "    axisFormat %b %d", "    section Plans"]
        for pid, title, status, start, finish in charted:
            tag = "done, " if status == "done" else ("active, " if status == "active" else "")
            L.append(f"    {schedule._label(title, pid, 44)} :{tag}{pid.replace('-', '_')[:24]}, "
                     f"{start}, {finish}")
        L.append("```")
        L.append("")

    stuck = [r for r in rows if r["blocked_by"]]
    if stuck:
        L += ["## Blocked", ""]
        for r in stuck:
            L.append(f"- **{r['title'][:60]}** waits on "
                     + ", ".join(f"`{b}`" for b in r["blocked_by"]))
        L.append("")

    L.append("---")
    L.append('*MegaPlan portfolio · regenerate with `megaplan(action="portfolio")`*')
    return "\n".join(L) + "\n"


def render_portfolio(save: bool = True, include_done: bool = False) -> dict:
    md = compose_portfolio(include_done)
    if not save:
        return {"format": "markdown", "saved": False, "markdown": md}
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    written = (_write_pair(f"portfolio-{stamp}", md, "Portfolio")
               + _write_pair("portfolio-latest", md, "Portfolio"))
    try:
        store._commit(written, "report portfolio")
    except Exception:
        pass
    return {"format": "markdown", "saved": True, "file": f"{REPORTS_DIRNAME}/portfolio-{stamp}.md",
            "url": f"{PUBLIC_URL}/reports/portfolio-{stamp}.html",
            "latest_url": f"{PUBLIC_URL}/reports/portfolio-latest.html",
            "bytes": len(md.encode()), "generated": datetime.now(timezone.utc).isoformat()}


# --- keeping reports current -------------------------------------------------------------------
AUTORENDER_H = float(os.environ.get("MEGAPLAN_AUTORENDER_H", "0") or 0)


def _stale(pid: str) -> bool:
    """Has the plan changed since its last report? Reports are derived, so re-rendering an
    unchanged plan only adds git noise — the backup sidecar takes the same view of bundles."""
    latest = os.path.join(reports_dir(), f"{pid}-latest.md")
    if not os.path.exists(latest):
        return True
    try:
        return os.path.getmtime(store._path(pid)) > os.path.getmtime(latest)
    except OSError:
        return True


def refresh(force: bool = False) -> dict:
    """Re-render every non-archived plan whose report is out of date, plus the portfolio."""
    done, skipped, failed = [], [], []
    for p in store.list_plans(include_archived=False):
        pid = p["id"]
        if not (force or _stale(pid)):
            skipped.append(pid); continue
        try:
            render(pid); done.append(pid)
        except Exception as e:
            failed.append({"id": pid, "error": str(e)[:200]})
    try:
        render_portfolio()
    except Exception as e:
        failed.append({"id": "portfolio", "error": str(e)[:200]})
    return {"rendered": done, "unchanged": skipped, "failed": failed,
            "at": datetime.now(timezone.utc).isoformat()}
