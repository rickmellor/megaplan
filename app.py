"""MegaPlan network service — a git-backed markdown plan store, MCP-first.

  MCP   /mcp   (streamable HTTP; ONE `megaplan(action, …)` tool over the whole store)
  REST  /plans, /plans/{id}, /context, /op, /health

The model-facing MCP surface is a single action-dispatch tool so planning feels like one
thing, not 16 CRUD verbs. Every op is still reachable programmatically via REST (`/op`
takes {action, …} and the convenience routes mirror the common reads/writes). Plans are
informed by a memory service (retrieval, soft dependency) but stored only here.
"""

import os

from fastapi import APIRouter, FastAPI, HTTPException
from fastapi.responses import FileResponse, PlainTextResponse
from pydantic import BaseModel

import memory
import report
import schedule
import store

store.ensure_repo()

VALID_ACTIONS = ("context", "list", "get", "deps", "graph", "blocked_by", "time_report",
                 "review", "schedule", "gantt", "report", "render", "portfolio", "reports",
                 "save", "update",
                 "add_task", "update_task", "complete", "depend", "log_time", "archive", "baseline")

# scheduling attributes accepted by save/add_task/update_task
SCHED_PARAMS = ("dur", "dep", "pct", "who", "start", "deadline", "o", "m", "p")


def _sched_kwargs(p):
    return {k: p[k] for k in SCHED_PARAMS if p.get(k) is not None}


def _safe(fn, *a, **k):
    """Run a store op; convert its exceptions into an {"error": ...} dict."""
    try:
        return fn(*a, **k)
    except KeyError as e:
        return {"error": f"not found: {e}"}
    except ValueError as e:
        return {"error": str(e)}


def _exists(pid):
    return bool(pid) and os.path.exists(store._path(pid))


# What each action cannot work without. Checked up front because the branches below index
# `p` directly, and a bare KeyError escapes _safe — over REST that surfaced as a 500.
REQUIRED = {"get": ("id",), "deps": ("id",), "blocked_by": ("id",), "review": ("id",),
            "schedule": ("id",), "gantt": ("id",), "report": ("id",), "render": ("id",),
            "update": ("id",),
            "add_task": ("id", "text"), "update_task": ("id", "task_id"),
            "complete": ("id", "task_id"), "depend": ("id", "depends_on_id"),
            "log_time": ("id", "hours"), "archive": ("id",), "baseline": ("id",),
            "save": ("title",)}


def _with_warnings(pid, result):
    """Attach the CPM pass's warnings to a write's result.

    A write that creates a broken graph — a dep on a task that does not exist, a dep on a
    summary task (silently ignored by the scheduler) — used to return a perfectly successful
    acknowledgement, and the damage was only visible if you separately called `schedule`. The
    write still succeeds; it just stops being silent."""
    if not isinstance(result, dict) or "error" in result:
        return result
    try:
        w = schedule.compute(pid).get("warnings") or []
    except Exception:
        return result                      # a report on the write must never fail the write
    if w:
        result["warnings"] = w
    return result


def do_action(action, p):
    """The one dispatcher behind both the `megaplan` MCP tool and REST `/op`. `p` is a dict
    of params (only those relevant to `action` are read). Returns a dict or list."""
    a = (action or "").strip().lower()
    if a not in VALID_ACTIONS:
        return {"error": f"unknown action {a!r}. valid: {', '.join(VALID_ACTIONS)}"}
    missing = [k for k in REQUIRED.get(a, ()) if p.get(k) in (None, "")]
    if missing and not (a == "save" and p.get("id")):      # save-as-patch needs only an id
        return {"error": f"action {a!r} requires: {', '.join(missing)}"}

    # ---- reads ----
    if a == "context":
        return memory.plan_context(p.get("goal", ""), p.get("tags") or [], p.get("limit", 5))
    if a == "list":
        return store.list_plans(p.get("status"), p.get("priority"), p.get("tag"),
                                p.get("include_archived", False), p.get("sort", "priority"))
    if a == "get":
        return _safe(store.get_plan, p["id"])
    if a == "deps":
        return _safe(store.list_dependencies, p["id"])
    if a == "graph":
        return store.dependency_graph(p.get("root"))
    if a == "blocked_by":
        return store.blocked_by(p["id"])
    if a == "time_report":
        return store.time_report(p.get("id"))
    if a == "schedule":
        return _safe(schedule.compute, p["id"])
    if a == "gantt":
        return _safe(lambda: {"id": p["id"], "format": "mermaid",
                              "source": schedule.mermaid(p["id"], p.get("group", "outline"),
                                                         p.get("include_done", True),
                                                         p.get("label_max", 60))})
    if a in ("report", "render"):        # `render` kept as an alias: it shipped under that name
        return _safe(report.render, p["id"], p.get("save", True))
    if a == "portfolio":
        return report.render_portfolio(p.get("save", True), p.get("include_done", False))
    if a == "reports":
        return report.list_reports(p.get("id") or None)
    if a == "review":
        pl = _safe(store.get_plan, p["id"])
        if isinstance(pl, dict) and "error" in pl:
            return pl
        ctx = memory.plan_context(f"{pl.get('title', '')} {' '.join(pl.get('tags') or [])}",
                                  pl.get("tags"), exclude=p["id"])
        return {"plan": pl, "context": ctx, "blocked_by": store.blocked_by(p["id"])}

    # ---- writes (auto-commit) ----
    if a == "save":
        if _exists(p.get("id")):                     # upsert: existing id → patch in place
            # `body` REPLACES. It used to be forwarded as append_body, so re-saving a plan
            # appended a second copy of itself — duplicate task ids, unreachable tasks, and a
            # critical path computed over a doubled graph. `append_body` still appends.
            return _safe(store.update_plan, p["id"], p.get("title"), p.get("status"),
                         p.get("priority"), p.get("tags"), p.get("est_hours"),
                         p.get("append_body"), p.get("body") or None)
        r = _safe(store.create_plan, p["title"], p.get("body", ""), p.get("status", "backlog"),
                  p.get("priority", "medium"), p.get("tags") or [],
                  p.get("depends_on") or [], p.get("est_hours"))
        if p.get("attach_context", True) and isinstance(r, dict) and "error" not in r:
            r["context"] = memory.plan_context(f"{p.get('title', '')} {p.get('body', '')}",
                                               p.get("tags") or [], exclude=r.get("id"))
        return r
    if a == "update":
        if p.get("schedule_start") is not None:
            r = _safe(store.set_schedule_start, p["id"], p["schedule_start"])
            if isinstance(r, dict) and "error" in r:
                return r
        return _safe(store.update_plan, p["id"], p.get("title"), p.get("status"),
                     p.get("priority"), p.get("tags"), p.get("est_hours"), p.get("append_body"))
    if a == "add_task":
        return _with_warnings(p["id"], _safe(store.add_task, p["id"], p["text"],
                                             p.get("est_hours"), p.get("indent"),
                                             **_sched_kwargs(p)))
    if a == "update_task":
        return _with_warnings(p["id"], _safe(store.update_task, p["id"], p["task_id"],
                                             p.get("text"), p.get("done"), p.get("blocked"),
                                             p.get("est_hours"), **_sched_kwargs(p)))
    if a == "complete":
        return _with_warnings(p["id"], _safe(store.complete_task, p["id"], p["task_id"]))
    if a == "depend":
        return _with_warnings(p["id"], _safe(store.add_dependency, p["id"], p["depends_on_id"]))
    if a == "log_time":
        return _safe(store.log_time, p["id"], p["hours"], p.get("task_id"), p.get("note"))
    if a == "archive":
        return _safe(store.archive_plan, p["id"])
    if a == "baseline":
        return _safe(schedule.baseline, p["id"], p.get("op", "capture"))

    return {"error": f"unknown action: {action!r}. valid: {', '.join(VALID_ACTIONS)}"}


# ---- REST mirror -------------------------------------------------------------

router = APIRouter()


@router.get("/health")
def health():
    try:
        n = store.count()
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"store unavailable: {e}")
    _, degraded = memory.memory_retrieve("health probe")
    mem = "degraded" if degraded else "reachable"
    return {"status": "ok", "plans_count": n, "data_path": store.DATA,
            "memory": mem, "memory_url": memory.MEMORY_URL}


class CreateReq(BaseModel):
    title: str
    body: str = ""
    status: str = "backlog"
    priority: str = "medium"
    tags: list[str] = []
    depends_on: list[str] = []
    est_hours: float | None = None


class ContextReq(BaseModel):
    goal: str
    tags: list[str] = []
    limit: int = 5


@router.get("/plans")
def rest_list(status: str | None = None, priority: str | None = None,
              tag: str | None = None, include_archived: bool = False):
    return store.list_plans(status, priority, tag, include_archived)


@router.post("/plans")
def rest_create(req: CreateReq):
    r = _safe(store.create_plan, req.title, req.body, req.status, req.priority,
              req.tags, req.depends_on, req.est_hours)
    if isinstance(r, dict) and "error" in r:
        raise HTTPException(status_code=400, detail=r["error"])
    return r


@router.get("/plans/{pid}")
def rest_get(pid: str):
    r = _safe(store.get_plan, pid)
    if isinstance(r, dict) and "error" in r:
        raise HTTPException(status_code=404, detail=r["error"])
    return r


@router.get("/plans/{pid}/schedule")
def rest_schedule(pid: str):
    r = _safe(schedule.compute, pid)
    if isinstance(r, dict) and "error" in r:
        raise HTTPException(status_code=404, detail=r["error"])
    return r


@router.get("/plans/{pid}/gantt")
def rest_gantt(pid: str, group: str = "outline", include_done: bool = True,
               label_max: int = 60):
    try:
        return {"id": pid, "format": "mermaid",
                "source": schedule.mermaid(pid, group, include_done, label_max)}
    except KeyError:
        raise HTTPException(status_code=404, detail=f"not found: {pid}")


@router.get("/reports")
def rest_reports(id: str | None = None):
    return report.list_reports(id)


@router.get("/static/mermaid.min.js")
def rest_mermaid():
    """The gantt renderer, served from this box so a report needs no internet."""
    p = "/app/static/mermaid.min.js"
    if not os.path.isfile(p):
        raise HTTPException(404, "mermaid bundle missing from the image")
    return FileResponse(p, media_type="application/javascript")


@router.get("/reports/{name}")
def rest_report(name: str):
    """Serve a saved report. `.html` is the readable view, `.md` the source. Name-only (no path
    separators) — the reports dir is not a file server."""
    if "/" in name or "\\" in name or not name.endswith((".md", ".html")):
        raise HTTPException(400, "reports are addressed by file name (.md or .html)")
    path = os.path.join(report.reports_dir(), name)
    if not os.path.isfile(path):
        raise HTTPException(404, f"no report {name}")
    kind = "text/html" if name.endswith(".html") else "text/markdown"
    return PlainTextResponse(open(path, encoding="utf-8").read(), media_type=kind)


@router.post("/context")
def rest_context(req: ContextReq):
    return memory.plan_context(req.goal, req.tags, req.limit)


@router.post("/op")
def rest_op(body: dict):
    """Full access to every op for scripts: POST {"action": "...", ...params}."""
    return do_action(body.get("action", ""), {k: v for k, v in body.items() if k != "action"})


# ---- MCP (streamable HTTP); REST survives even if MCP wiring fails ------------

app: FastAPI
try:
    from fastmcp import FastMCP

    mcp = FastMCP("megaplan")

    @mcp.tool
    def megaplan(action: str, id: str = "", title: str = "", body: str = "",
                 status: str = "", priority: str = "", tags: list[str] | None = None,
                 depends_on: list[str] | None = None, depends_on_id: str = "",
                 task_id: str = "", text: str = "", done: bool | None = None,
                 blocked: bool | None = None, est_hours: float | None = None,
                 hours: float | None = None, note: str = "", append_body: str = "",
                 goal: str = "", tag: str = "", root: str = "", sort: str = "priority",
                 limit: int = 5, include_archived: bool = False,
                 attach_context: bool = True,
                 dur: str = "", dep: str = "", pct: int | None = None, who: str = "",
                 start: str = "", deadline: str = "", o: str = "", m: str = "", p: str = "",
                 indent: str = "", schedule_start: str = "", group: str = "outline",
                 include_done: bool = True, label_max: int = 60,
                 op: str = "capture") -> dict:
        """The single MegaPlan tool — one entry for the whole persistent, git-backed,
        memory-informed plan store. Set `action` and pass only the params it needs.

        READ:
          context      goal[, tags, limit] — related existing plans + recalled memory
                       for a goal. Use FIRST when planning, to leverage prior work.
          list         [status, priority, tag, include_archived, sort] — plan summaries.
          get          id — one plan: frontmatter + tasks (stable tN ids) + progress + body.
          deps         id — direct dependencies + dependents.
          graph        [root] — dependency-graph nodes/edges.
          blocked_by   id — this plan's not-yet-done deps (empty = ready to start).
          time_report  [id] — est/spent/remaining across all plans, or per-task for one.
          review       id — plan + related plans/memory + blockers, in one call.
          schedule     id — CPM pass: computed start/end, early/late dates, total & free
                       float, critical path, and warnings. Dates are DERIVED, never stored.
          gantt        id[, group=outline|who, include_done, label_max] — Mermaid `gantt`
                       source (bar labels truncated to label_max chars, 0 = no limit).
          portfolio    [include_done] — ONE report across every active plan: aggregate
                       progress, a table of plans with schedule and blockers, a timeline
                       gantt, and what is blocked. Use for "where does everything stand".
          reports      [id] — saved reports, newest first, with their URLs.

        REPORTS (writes a file, auto-commits):
          report       id[, save=true] — a full progress report for a plan as ONE markdown
                       document: intent, progress bar, effort, schedule summary, scheduler
                       warnings, a Mermaid gantt, a baseline-tracking section when the plan
                       has a baseline, and tasks grouped by state. Saved under reports/ in
                       the store and returned with a `url` — hand the user that URL rather
                       than pasting the whole report. `save=false` returns the markdown
                       inline without writing anything. Use this when someone asks how a
                       plan/project is going, for a status write-up, or for charts.
                       (`render` is accepted as an alias. NOTE the plural `reports` LISTS
                       saved reports — it does not make one.)

        WRITE (auto-commit to git):
          save         title, body[, status, priority, tags, depends_on, est_hours] —
                       create a plan. Put the WHOLE plan in `body` as markdown including a
                       '## Tasks' checklist ('- [ ] do the thing (est: 2h)'); tasks get
                       stable ids automatically. Returns the plan + (unless
                       attach_context=false) related work under 'context'. If `id` is given
                       and already exists this PATCHES it, and `body` REPLACES the stored
                       body (use `append_body` to add to it instead). Note save is the
                       create/replace path — for ordinary edits use update_task/add_task,
                       which rewrite single lines in place and cannot disturb the rest.
          update       id[, title, status, priority, tags, est_hours, append_body].
          add_task     id, text[, est_hours].
          update_task  id, task_id[, text, done, blocked, est_hours]. Pass "-" for any
                       scheduling attribute (dep/pct/who/start/deadline/dur/o/m/p) to CLEAR
                       it — e.g. update_task(id, "t3", dep="-") removes a wrong dependency.
          complete     id, task_id — mark done (auto-completes the plan when all done).
          depend       id, depends_on_id — cross-plan dependency (cycle-checked).
          log_time     id, hours[, task_id, note].
          archive      id — soft delete (file + git history kept).
          baseline     id[, op=capture|clear] — snapshot the computed schedule so later
                       runs report start/finish variance (a Tracking Gantt).

        status: backlog|active|blocked|done|archived. priority: low|medium|high|critical.
        Prefer ONE `save` with the full plan in `body` over many add_task calls.

        SCHEDULING — put these in the task line's trailing meta block when writing `body`,
        or pass them to add_task/update_task:
          dur       elapsed duration: `3d` `2w` `12h` `1.5d`; `0d` = milestone. Default 1d.
          dep       predecessors, MS-Project notation, SPACE-separated: `t4` (=FS+0),
                    `t4FS+2d`, `t7SS`, `t2FF-1d`. Cross-plan: `<plan-id>#t14`.
          pct       0-100 percent complete (the checkbox still wins: [x] = 100).
          who       swimlane label (rick / claude / johnny-fleet / waiting-external).
          start     start-no-earlier-than constraint (YYYY-MM-DD) — pins a task.
          deadline  soft (YYYY-MM-DD): never moves dates, reported when missed.
          o/m/p     PERT three-point; used for `dur` when `dur` is absent.
        Indentation makes hierarchy: an indented task is a child, its parent is a summary
        whose bar spans its children. A `dep` ON a summary task is IGNORED (the scheduler
        warns) — link its leaf children instead, or the successor will not wait for them.
        `est`/`spent` stay WORK (effort), not duration.
        The project anchor is the plan's `created` date unless `update` sets schedule_start.

          - [ ] Wire the data plane  (est: 6h, dur: 3d, dep: t4FS+2d, pct: 40, who: rick)
            - [ ] Contract v1  (dur: 1d)
          - [ ] Ship  (dur: 0d, dep: t9)
        """
        # Empty values are dropped because the signature's ""/None defaults make "absent" and
        # "empty" indistinguishable — which also meant a scheduling attribute could never be
        # REMOVED once set (a mistyped `dep: t99` was uneditable except on the server). "-"
        # is the escape hatch: it reaches the store as "" and deletes the key.
        params = {k: ("" if (k in SCHED_PARAMS and v in ("-", "none", "null")) else v)
                  for k, v in locals().items()
                  if k not in ("action",) and v not in ("", None)}
        r = do_action(action, params)
        # Write actions return a COMPACT acknowledgement (no body, no raw task lines) so a
        # tool loop doing many updates doesn't re-read the whole plan each time; reads
        # (get/review/...) keep the full payload.
        if (action or "").lower() in ("save", "update", "add_task", "update_task", "complete",
                                      "depend", "log_time", "archive") and isinstance(r, dict) \
                and "error" not in r:
            slim = {k: v for k, v in r.items() if k not in ("body", "context")}
            if isinstance(slim.get("tasks"), list):
                slim["tasks"] = [{k: v for k, v in t.items() if not k.startswith("_")}
                                 for t in slim["tasks"]]
            if "context" in r:                         # save: keep related-plan ids only
                slim["related_plans"] = [x.get("id") for x in (r["context"] or {}).get("related_plans", [])]
            return slim
        return r if isinstance(r, dict) else {"result": r}

    mcp_app = mcp.http_app(path="/")
    app = FastAPI(title="MegaPlan service", lifespan=mcp_app.lifespan)
    app.mount("/mcp", mcp_app)
except Exception as e:  # MCP is best-effort; REST must survive
    print(f"WARNING: MCP endpoint disabled: {e}")
    app = FastAPI(title="MegaPlan service")

app.include_router(router)


@app.on_event("startup")
async def _autorender():
    """Keep reports current without anyone asking. Off unless MEGAPLAN_AUTORENDER_H is set.

    Server-side on purpose: the NAS is always on, so reports stay fresh even when the machines
    that create plans are powered off. Only plans that actually changed are re-rendered."""
    if report.AUTORENDER_H <= 0:
        return
    import asyncio

    async def loop():
        await asyncio.sleep(90)                       # let the service settle before the first pass
        while True:
            try:
                r = await asyncio.to_thread(report.refresh)
                if r["rendered"] or r["failed"]:
                    print(f"autorender: {len(r['rendered'])} rendered, "
                          f"{len(r['unchanged'])} unchanged, {len(r['failed'])} failed", flush=True)
            except Exception as e:                    # a refresh must never take the service down
                print(f"autorender FAILED: {e}", flush=True)
            await asyncio.sleep(report.AUTORENDER_H * 3600)

    asyncio.create_task(loop())
