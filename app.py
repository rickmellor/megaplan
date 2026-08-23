"""MegaPlan network service — a git-backed markdown plan store, MCP-first.

  MCP   /mcp   (streamable HTTP; ONE `megaplan(action, …)` tool over the whole store)
  REST  /plans, /plans/{id}, /context, /op, /health

The model-facing MCP surface is a single action-dispatch tool so planning feels like one
thing, not 16 CRUD verbs. Every op is still reachable programmatically via REST (`/op`
takes {action, …} and the convenience routes mirror the common reads/writes). Plans are
informed by MemoryOS (retrieval) but stored only here.
"""

import os

from fastapi import APIRouter, FastAPI, HTTPException
from pydantic import BaseModel

import memory
import store

store.ensure_repo()

VALID_ACTIONS = ("context", "list", "get", "deps", "graph", "blocked_by", "time_report",
                 "review", "save", "update", "add_task", "update_task", "complete",
                 "depend", "log_time", "archive")


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


def do_action(action, p):
    """The one dispatcher behind both the `megaplan` MCP tool and REST `/op`. `p` is a dict
    of params (only those relevant to `action` are read). Returns a dict or list."""
    a = (action or "").strip().lower()

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
    if a == "review":
        pl = _safe(store.get_plan, p["id"])
        if isinstance(pl, dict) and "error" in pl:
            return pl
        ctx = memory.plan_context(f"{pl.get('title', '')} {' '.join(pl.get('tags') or [])}",
                                  pl.get("tags"), exclude=p["id"])
        return {"plan": pl, "context": ctx, "blocked_by": store.blocked_by(p["id"])}

    # ---- writes (auto-commit) ----
    if a == "save":
        if _exists(p.get("id")):                     # upsert: existing id → patch
            return _safe(store.update_plan, p["id"], p.get("title"), p.get("status"),
                         p.get("priority"), p.get("tags"), p.get("est_hours"),
                         p.get("append_body") or p.get("body"))
        r = _safe(store.create_plan, p["title"], p.get("body", ""), p.get("status", "backlog"),
                  p.get("priority", "medium"), p.get("tags") or [],
                  p.get("depends_on") or [], p.get("est_hours"))
        if p.get("attach_context", True) and isinstance(r, dict) and "error" not in r:
            r["context"] = memory.plan_context(f"{p.get('title', '')} {p.get('body', '')}",
                                               p.get("tags") or [], exclude=r.get("id"))
        return r
    if a == "update":
        return _safe(store.update_plan, p["id"], p.get("title"), p.get("status"),
                     p.get("priority"), p.get("tags"), p.get("est_hours"), p.get("append_body"))
    if a == "add_task":
        return _safe(store.add_task, p["id"], p["text"], p.get("est_hours"))
    if a == "update_task":
        return _safe(store.update_task, p["id"], p["task_id"], p.get("text"),
                     p.get("done"), p.get("blocked"), p.get("est_hours"))
    if a == "complete":
        return _safe(store.complete_task, p["id"], p["task_id"])
    if a == "depend":
        return _safe(store.add_dependency, p["id"], p["depends_on_id"])
    if a == "log_time":
        return _safe(store.log_time, p["id"], p["hours"], p.get("task_id"), p.get("note"))
    if a == "archive":
        return _safe(store.archive_plan, p["id"])

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
                 attach_context: bool = True) -> dict:
        """The single MegaPlan tool — one entry for the whole persistent, git-backed,
        memory-informed plan store. Set `action` and pass only the params it needs.

        READ:
          context      goal[, tags, limit] — related existing plans + MemoryOS knowledge
                       for a goal. Use FIRST when planning, to leverage prior work.
          list         [status, priority, tag, include_archived, sort] — plan summaries.
          get          id — one plan: frontmatter + tasks (stable tN ids) + progress + body.
          deps         id — direct dependencies + dependents.
          graph        [root] — dependency-graph nodes/edges.
          blocked_by   id — this plan's not-yet-done deps (empty = ready to start).
          time_report  [id] — est/spent/remaining across all plans, or per-task for one.
          review       id — plan + related plans/memory + blockers, in one call.

        WRITE (auto-commit to git):
          save         title, body[, status, priority, tags, depends_on, est_hours] —
                       create a plan. Put the WHOLE plan in `body` as markdown including a
                       '## Tasks' checklist ('- [ ] do the thing (est: 2h)'); tasks get
                       stable ids automatically. Returns the plan + (unless
                       attach_context=false) related work under 'context'. If `id` is given
                       and already exists, patches it instead of creating.
          update       id[, title, status, priority, tags, est_hours, append_body].
          add_task     id, text[, est_hours].
          update_task  id, task_id[, text, done, blocked, est_hours].
          complete     id, task_id — mark done (auto-completes the plan when all done).
          depend       id, depends_on_id — cross-plan dependency (cycle-checked).
          log_time     id, hours[, task_id, note].
          archive      id — soft delete (file + git history kept).

        status: backlog|active|blocked|done|archived. priority: low|medium|high|critical.
        Prefer ONE `save` with the full plan in `body` over many add_task calls.
        """
        p = {k: v for k, v in locals().items()
             if k not in ("action",) and v not in ("", None)}
        r = do_action(action, p)
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
