"""MegaPlan store: git-backed markdown plans with YAML frontmatter.

One plan = one file `<id>.md` under DATA_PATH (a git repo). Every mutation writes
the file and auto-commits. Tasks live as markdown checkboxes with a stable `<!-- tN -->`
id so tools can target them without line numbers. Plans are portable/hand-editable;
the store re-parses on read and tolerates hand edits.

Tasks carry optional scheduling attributes in the trailing meta block —
`dur` (elapsed duration), `dep` (typed predecessors with lag, MS-Project column
notation), `pct`, `who`, `start` (SNET constraint), `deadline`, and PERT `o/m/p`.
Dates are never stored: `schedule.py` derives them with a CPM pass, the same way
`progress()` is derived here. Leading indentation is significant — it is preserved
on rewrite and defines the task hierarchy (summary tasks).
"""

from __future__ import annotations

import glob
import os
import re
import subprocess
import threading
from datetime import datetime, timezone

import frontmatter

DATA = os.environ.get("MEGAPLAN_DATA_PATH", "/data")
GIT_NAME = os.environ.get("MEGAPLAN_GIT_NAME", "megaplan")
GIT_EMAIL = os.environ.get("MEGAPLAN_GIT_EMAIL", "megaplan@nas")
AUTO_DONE = os.environ.get("MEGAPLAN_AUTO_DONE", "1") == "1"

STATUSES = ("backlog", "active", "blocked", "done", "archived")
PRIORITIES = ("low", "medium", "high", "critical")
_PRIO_RANK = {p: i for i, p in enumerate(PRIORITIES)}

LINK_TYPES = ("FS", "SS", "FF", "SF")
INDENT_WIDTH = 2                      # spaces per outline level
# scheduling keys rendered in this order so files do not churn in git
_META_ORDER = ("est", "spent", "dur", "o", "m", "p", "dep", "pct", "who", "start", "deadline")

_lock = threading.Lock()

# `- [x] text (est: 2h, spent: 1h) @blocked  <!-- t3 depends: t1 -->`
_TASK_RE = re.compile(
    r"^(?P<indent>\s*)-\s\[(?P<mark>[ xX])\]\s+(?P<text>.*?)"
    r"(?:\s*<!--\s*(?P<tid>t\d+)(?:\s+depends:\s*(?P<dep>t\d+))?\s*-->)?\s*$"
)
_META_RE = re.compile(r"\(([^)]*)\)\s*$")   # trailing (key: val, ...) block on the visible text


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _slug(title: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return s or "plan"


def _path(pid: str) -> str:
    return os.path.join(DATA, f"{pid}.md")


def _parse_hours(s: str) -> float | None:
    s = s.strip().lower()
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*(h|m)?", s)
    if not m:
        return None
    v = float(m.group(1))
    return v / 60.0 if m.group(2) == "m" else v


def _fmt_hours(h: float) -> str:
    return f"{h:g}h"


# ---- scheduling value parsers -------------------------------------------------

_DUR_UNITS = {"w": 7.0, "d": 1.0, "ed": 1.0, "h": 1.0 / 24.0}   # elapsed days; `ed` = MSP alias


def _parse_dur(s: str) -> float | None:
    """Elapsed duration -> days. `3d` `2w` `12h` `1.5d` `3ed`; bare number = days. 0 = milestone."""
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*(ed|[wdh])?", (s or "").strip().lower())
    if not m:
        return None
    return float(m.group(1)) * _DUR_UNITS.get(m.group(2) or "d", 1.0)


def _fmt_dur(days: float) -> str:
    return f"{days:g}d"


# `t4`, `t4FS+2d`, `t7SS`, `t2FF-1d`, `20260822-nova-stack#t14`
_DEP_RE = re.compile(
    r"^(?:(?P<plan>[A-Za-z0-9][A-Za-z0-9._-]*)#)?(?P<task>t\d+)"
    r"(?P<type>FS|SS|FF|SF)?(?P<lag>[+-]\d+(?:\.\d+)?\s*(?:ed|[wdh])?)?$",
    re.I)


def _parse_deps(s: str) -> list[dict]:
    """MS-Project predecessor notation, space-separated (the meta block splits on commas)."""
    out = []
    for tok in (s or "").split():
        m = _DEP_RE.match(tok.strip())
        if not m:
            continue
        lag = 0.0
        if m.group("lag"):
            sign = -1.0 if m.group("lag")[0] == "-" else 1.0
            lag = sign * (_parse_dur(m.group("lag")[1:]) or 0.0)
        out.append({"plan": m.group("plan"), "task": m.group("task").lower(),
                    "type": (m.group("type") or "FS").upper(), "lag_days": lag})
    return out


def _fmt_deps(deps: list[dict]) -> str:
    parts = []
    for d in deps:
        t = f"{d['plan']}#{d['task']}" if d.get("plan") else d["task"]
        if (d.get("type") or "FS") != "FS":
            t += d["type"]
        lag = d.get("lag_days") or 0.0
        if lag:
            t += ("+" if lag > 0 else "-") + _fmt_dur(abs(lag))
            if (d.get("type") or "FS") == "FS" and "FS" not in t:
                t = t.replace(d["task"], d["task"] + "FS", 1)   # lag needs an explicit type
        parts.append(t)
    return " ".join(parts)


# ---- git ---------------------------------------------------------------------

def _git(*args, check=True):
    return subprocess.run(["git", "-C", DATA, *args], check=check,
                          capture_output=True, text=True)


def ensure_repo() -> None:
    """Idempotent: make DATA a git repo with an identity; tolerate the non-root bind mount."""
    os.makedirs(DATA, exist_ok=True)
    subprocess.run(["git", "config", "--global", "--add", "safe.directory", DATA],
                   check=False, capture_output=True)
    if not os.path.isdir(os.path.join(DATA, ".git")):
        _git("init", "-q")
    _git("config", "user.name", GIT_NAME)
    _git("config", "user.email", GIT_EMAIL)


def _commit(paths: list[str], message: str) -> None:
    _git("add", *paths, check=False)
    r = _git("commit", "-q", "-m", message, check=False)
    if r.returncode != 0 and "nothing to commit" not in (r.stdout + r.stderr):
        # keep the file even if commit fails; surface nothing fatal to the caller
        pass


# ---- task parsing ------------------------------------------------------------

_META_KEYS = set(_META_ORDER) | {"deps"}
_FLAGS = ("@blocked", "@wip")


def _split_meta(text: str) -> tuple[str, dict]:
    """Split a trailing `(key: val, ...)` block off the visible text.

    Only treated as meta when every part is `key: value` AND at least one key is known —
    otherwise it is prose (`Fix the thing (again)`) and must survive the round trip.
    """
    mm = _META_RE.search(text)
    if not mm:
        return text, {}
    meta = {}
    for part in mm.group(1).split(","):
        if ":" not in part:
            return text, {}
        k, v = part.split(":", 1)
        meta[k.strip().lower()] = v.strip()
    if not (set(meta) & _META_KEYS):
        return text, {}
    return _META_RE.sub("", text), meta


def parse_tasks(body: str) -> list[dict]:
    tasks = []
    for line in body.splitlines():
        m = _TASK_RE.match(line)
        if not m or not line.lstrip().startswith("- ["):
            continue
        label, meta = _split_meta(m.group("text"))
        flags = [f for f in _FLAGS if f in label]
        for f in flags:
            label = label.replace(f, "")
        label = re.sub(r"\s{2,}", " ", label).strip()

        deps = _parse_deps(meta.get("dep") or meta.get("deps") or "")
        legacy = not deps and bool(m.group("dep"))
        if legacy:                     # `<!-- t3 depends: t1 -->` from before typed links
            deps = [{"plan": None, "task": m.group("dep"), "type": "FS", "lag_days": 0.0}]

        indent = m.group("indent") or ""
        pert = {k: _parse_dur(meta[k]) for k in ("o", "m", "p") if k in meta}
        tasks.append({
            "id": m.group("tid"),      # None if unlabeled; assign_ids() fills it
            "done": m.group("mark").lower() == "x",
            "text": label,
            "est_hours": _parse_hours(meta["est"]) if "est" in meta else None,
            "spent_hours": _parse_hours(meta["spent"]) if "spent" in meta else None,
            "blocked": "@blocked" in flags, "wip": "@wip" in flags,
            "depends": deps[0]["task"] if deps and not deps[0]["plan"] else None,   # legacy view
            "deps": deps,
            "dur_days": _parse_dur(meta["dur"]) if "dur" in meta else None,
            "pct": int(float(meta["pct"])) if meta.get("pct", "").strip() else None,
            "who": meta.get("who"),
            "start": meta.get("start"), "deadline": meta.get("deadline"),
            "pert": pert if len(pert) == 3 and None not in pert.values() else None,
            "indent": indent,
            "level": len(indent.expandtabs(INDENT_WIDTH)) // INDENT_WIDTH,
            "_raw": line, "_labeled": bool(m.group("tid")),
            "_meta": meta, "_legacy_dep": legacy,
        })
    return tasks


def task_set(t: dict, key: str, value) -> None:
    """Set one scheduling attribute, keeping the structured field and the raw meta in sync.

    `_render_task` renders from `_meta`, so every mutation must go through here.
    """
    meta = t.setdefault("_meta", {})
    if value is None or value == "":
        meta.pop(key, None)
    # the API hands these over as strings ("3d", "2h", "40"); accept both forms
    if isinstance(value, str) and value:
        if key in ("dur", "o", "m", "p"):
            value = _parse_dur(value)
        elif key in ("est", "spent"):
            value = _parse_hours(value)
        elif key == "pct":
            value = int(float(value))
        if value is None:
            raise ValueError(f"cannot parse {key!r} value")
    if key in ("est", "spent"):
        t[f"{key}_hours"] = value
        if value is not None:
            meta[key] = _fmt_hours(value)
    elif key == "dur":
        t["dur_days"] = value
        if value is not None:
            meta[key] = _fmt_dur(value)
    elif key == "dep":
        deps = _parse_deps(value) if isinstance(value, str) else (value or [])
        t["deps"] = deps
        t["depends"] = deps[0]["task"] if deps and not deps[0]["plan"] else None
        t["_legacy_dep"] = False
        meta.pop("deps", None)
        if deps:
            meta["dep"] = _fmt_deps(deps)
        else:
            meta.pop("dep", None)
    elif key == "pct":
        t["pct"] = None if value is None else int(value)
        if value is not None:
            meta[key] = str(int(value))
    elif key in ("who", "start", "deadline"):
        t[key] = value
        if value:
            meta[key] = str(value)
    elif key in ("o", "m", "p"):
        pert = dict(t.get("pert") or {})
        if value is None:
            pert.pop(key, None)
        else:
            pert[key] = value
            meta[key] = _fmt_dur(value)
        t["pert"] = pert if len(pert) == 3 else None


def assign_ids(tasks: list[dict]) -> list[dict]:
    """Fill in ids for unlabeled tasks with the smallest unused tN (deterministic)."""
    used = {int(t["id"][1:]) for t in tasks if t["id"]}
    nxt = 1
    for t in tasks:
        if not t["id"]:
            while nxt in used:
                nxt += 1
            t["id"] = f"t{nxt}"; used.add(nxt); nxt += 1
    return tasks


def link_parents(tasks: list[dict]) -> list[dict]:
    """Attach parent/children from indentation. A task with children is a summary task."""
    stack = []                                  # (level, task)
    for t in tasks:
        t["children"] = []
        while stack and stack[-1][0] >= t["level"]:
            stack.pop()
        t["parent"] = stack[-1][1]["id"] if stack else None
        if stack:
            stack[-1][1]["children"].append(t["id"])
        stack.append((t["level"], t))
    for t in tasks:
        t["is_summary"] = bool(t["children"])
        t["is_milestone"] = (not t["is_summary"]) and t.get("dur_days") == 0
    return tasks


def effective_pct(t: dict) -> int:
    """Checkbox stays authoritative; `pct` only fills in the in-between."""
    if t.get("done"):
        return 100
    return int(t.get("pct") or 0)


def progress(tasks: list[dict]) -> dict:
    total = len(tasks)
    done = sum(1 for t in tasks if t["done"])
    return {"done": done, "total": total, "pct": round(100 * done / total) if total else 0}


def _render_task(t: dict) -> str:
    mark = "x" if t["done"] else " "
    meta = dict(t.get("_meta") or {})
    ordered = [k for k in _META_ORDER if k in meta] + \
              [k for k in meta if k not in _META_ORDER]
    parts = [f"{t.get('indent', '')}- [{mark}] {t['text']}"]
    if t.get("blocked"):
        parts.append("@blocked")
    if t.get("wip"):
        parts.append("@wip")
    if ordered:                           # meta MUST stay trailing: _META_RE anchors to end-of-text
        parts.append("(" + ", ".join(f"{k}: {meta[k]}" for k in ordered) + ")")
    comment = t["id"]
    if t.get("_legacy_dep") and t.get("deps"):      # keep pre-typed-link files byte-stable
        comment += f" depends: {t['deps'][0]['task']}"
    parts.append(f"<!-- {comment} -->")
    return "  ".join(parts)


def _rewrite_tasks(body: str, tasks: list[dict]) -> str:
    """Replace every checkbox line (in file order) with the rendered `tasks`; append any
    extras (new adds) under `## Tasks`, creating the section if absent."""
    out, idx = [], 0
    for line in body.splitlines():
        if _TASK_RE.match(line) and line.lstrip().startswith("- ["):
            if idx < len(tasks):
                out.append(_render_task(tasks[idx])); idx += 1
            continue
        out.append(line)
    extra = tasks[idx:]
    if extra:
        if not any(re.match(r"^##\s+Tasks", l, re.I) for l in out):
            if out and out[-1].strip():
                out.append("")
            out.append("## Tasks")
        out += [_render_task(t) for t in extra]
    return "\n".join(out).rstrip() + "\n"


# ---- plan read ---------------------------------------------------------------

def read_post(pid: str) -> frontmatter.Post:
    p = _path(pid)
    if not os.path.exists(p):
        raise KeyError(pid)
    return frontmatter.load(p)


def _dump(pid: str, post: frontmatter.Post, message: str) -> None:
    post["updated"] = _now()
    with open(_path(pid), "w") as f:
        f.write(frontmatter.dumps(post, sort_keys=False, allow_unicode=True))
    _commit([f"{pid}.md"], message)


def summary(post: frontmatter.Post) -> dict:
    tasks = parse_tasks(post.content)
    m = post.metadata
    return {"id": m.get("id"), "title": m.get("title"), "status": m.get("status"),
            "priority": m.get("priority"), "tags": m.get("tags", []),
            "depends_on": m.get("depends_on", []), "progress": progress(tasks),
            "updated": m.get("updated")}


def full(post: frontmatter.Post) -> dict:
    tasks = link_parents(assign_ids(parse_tasks(post.content)))
    out = []
    for t in tasks:
        d = {k: v for k, v in t.items() if k not in ("_meta", "_legacy_dep")}
        d["pct"] = effective_pct(t)
        out.append(d)
    return {**post.metadata, "progress": progress(tasks), "tasks": out,
            "body": post.content}


# ---- public API (called by app.do_*) -----------------------------------------

def list_plans(status=None, priority=None, tag=None, include_archived=False, sort="priority"):
    with _lock:
        posts = [frontmatter.load(p) for p in glob.glob(os.path.join(DATA, "*.md"))]
    rows = []
    for post in posts:
        m = post.metadata
        if not include_archived and m.get("status") == "archived":
            continue
        if status and m.get("status") != status:
            continue
        if priority and m.get("priority") != priority:
            continue
        if tag and tag not in (m.get("tags") or []):
            continue
        rows.append(summary(post))
    if sort == "priority":
        rows.sort(key=lambda r: (-_PRIO_RANK.get(r["priority"], 0), r["id"] or ""))
    elif sort in ("updated", "created"):
        rows.sort(key=lambda r: r.get("updated") or "", reverse=True)
    return rows


def get_plan(pid):
    with _lock:
        return full(read_post(pid))


def create_plan(title, body="", status="backlog", priority="medium", tags=None,
                depends_on=None, est_hours=None):
    if status not in STATUSES or priority not in PRIORITIES:
        raise ValueError("invalid status/priority")
    with _lock:
        base = _slug(title)
        pid = f"{datetime.now(timezone.utc):%Y%m%d}-{base}"
        n = 2
        while os.path.exists(_path(pid)):
            pid = f"{datetime.now(timezone.utc):%Y%m%d}-{base}-{n}"; n += 1
        now = _now()
        post = frontmatter.Post(
            (body.strip() + "\n") if body.strip() else "",
            id=pid, title=title, status=status, priority=priority,
            tags=list(tags or []), depends_on=list(depends_on or []),
            created=now, updated=now,
            time={"est_hours": est_hours or 0, "spent_hours": 0})
        if "## Tasks" not in post.content:
            post.content = (post.content + "\n## Tasks\n").lstrip("\n")
        _dump(pid, post, f"create {pid}")
        return full(post)


def update_plan(pid, title=None, status=None, priority=None, tags=None,
                est_hours=None, append_body=None):
    with _lock:
        post = read_post(pid)
        if title is not None:
            post["title"] = title
        if status is not None:
            if status not in STATUSES:
                raise ValueError("invalid status")
            post["status"] = status
        if priority is not None:
            if priority not in PRIORITIES:
                raise ValueError("invalid priority")
            post["priority"] = priority
        if tags is not None:
            post["tags"] = list(tags)
        if est_hours is not None:
            t = dict(post.get("time") or {}); t["est_hours"] = est_hours; post["time"] = t
        if append_body:
            post.content = post.content.rstrip() + "\n\n" + append_body.strip() + "\n"
        _dump(pid, post, f"update {pid}")
        return full(post)


def add_task(pid, text, est_hours=None, indent=None, **sched):
    with _lock:
        post = read_post(pid)
        tasks = assign_ids(parse_tasks(post.content))
        used = {int(t["id"][1:]) for t in tasks}
        nxt = 1
        while nxt in used:
            nxt += 1
        new = {"id": f"t{nxt}", "done": False, "text": text.strip(),
               "est_hours": None, "spent_hours": None, "blocked": False, "wip": False,
               "depends": None, "deps": [], "indent": indent or "",
               "level": len(indent or "") // INDENT_WIDTH, "_meta": {}}
        if est_hours is not None:
            task_set(new, "est", est_hours)
        for k in ("dur", "dep", "pct", "who", "start", "deadline", "o", "m", "p"):
            if sched.get(k) is not None:
                task_set(new, k, sched[k])
        tasks.append(new)
        post.content = _rewrite_tasks(post.content, tasks)
        _dump(pid, post, f"task add {pid}#{new['id']}")
        return full(post)


def _find_task(tasks, task_id):
    for t in tasks:
        if t["id"] == task_id:
            return t
    raise KeyError(task_id)


def update_task(pid, task_id, text=None, done=None, blocked=None, est_hours=None, **sched):
    with _lock:
        post = read_post(pid)
        tasks = assign_ids(parse_tasks(post.content))
        t = _find_task(tasks, task_id)
        if text is not None:
            t["text"] = text
        if done is not None:
            t["done"] = bool(done)
        if blocked is not None:
            t["blocked"] = bool(blocked)
        if est_hours is not None:
            task_set(t, "est", est_hours)
        for k in ("dur", "dep", "pct", "who", "start", "deadline", "o", "m", "p"):
            if k in sched and sched[k] is not None:
                task_set(t, k, sched[k])
        post.content = _rewrite_tasks(post.content, tasks)
        if AUTO_DONE and tasks and all(x["done"] for x in tasks):
            post["status"] = "done"
        _dump(pid, post, f"task update {pid}#{task_id}")
        return full(post)


def complete_task(pid, task_id):
    return update_task(pid, task_id, done=True)


def log_time(pid, hours, task_id=None, note=None):
    with _lock:
        post = read_post(pid)
        tasks = assign_ids(parse_tasks(post.content))
        if task_id:
            t = _find_task(tasks, task_id)
            task_set(t, "spent", (t["spent_hours"] or 0) + hours)
            post.content = _rewrite_tasks(post.content, tasks)
        # roll up plan spent = sum of task spent, else bump plan-level
        rolled = sum(t["spent_hours"] or 0 for t in tasks)
        tm = dict(post.get("time") or {})
        tm["spent_hours"] = rolled if rolled else (tm.get("spent_hours", 0) + hours)
        post["time"] = tm
        if note:
            post.content = post.content.rstrip() + f"\n\n_time: +{_fmt_hours(hours)} — {note}_\n"
        _dump(pid, post, f"log {_fmt_hours(hours)} {pid}" + (f"#{task_id}" if task_id else ""))
        return full(post)


def add_dependency(pid, dep_id):
    with _lock:
        post = read_post(pid)
        if not os.path.exists(_path(dep_id)):
            raise KeyError(dep_id)
        if _has_path(dep_id, pid):        # dep_id already (transitively) depends on pid → cycle
            raise ValueError(f"cycle: {dep_id} already depends on {pid}")
        deps = list(post.get("depends_on") or [])
        if dep_id not in deps:
            deps.append(dep_id)
        post["depends_on"] = deps
        _dump(pid, post, f"dep {pid} -> {dep_id}")
        return full(post)


def _deps_of(pid):
    try:
        return list(read_post(pid).get("depends_on") or [])
    except KeyError:
        return []


def _has_path(src, dst, seen=None):
    """True if `dst` is reachable from `src` via depends_on edges."""
    seen = seen or set()
    if src in seen:
        return False
    seen.add(src)
    for d in _deps_of(src):
        if d == dst or _has_path(d, dst, seen):
            return True
    return False


def blocked_by(pid):
    with _lock:
        out = []
        for d in _deps_of(pid):
            try:
                dp = read_post(d)
                if dp.get("status") != "done":
                    out.append(summary(dp))
            except KeyError:
                out.append({"id": d, "title": "(missing)", "status": "missing"})
        return out


def list_dependencies(pid):
    with _lock:
        deps = _deps_of(pid)
        reverse = []
        for p in glob.glob(os.path.join(DATA, "*.md")):
            post = frontmatter.load(p)
            if pid in (post.get("depends_on") or []):
                reverse.append(post["id"])
        return {"id": pid, "depends_on": deps, "depended_on_by": reverse}


def dependency_graph(root=None):
    with _lock:
        nodes = {}
        for p in glob.glob(os.path.join(DATA, "*.md")):
            post = frontmatter.load(p)
            s = summary(post)
            nodes[s["id"]] = {"id": s["id"], "title": s["title"], "status": s["status"],
                              "progress": s["progress"], "depends_on": s["depends_on"]}
        edges = [{"from": nid, "to": d} for nid, n in nodes.items() for d in n["depends_on"]]
        return {"nodes": list(nodes.values()), "edges": edges}


def archive_plan(pid):
    return update_plan(pid, status="archived")


# ---- schedule anchor + baseline (frontmatter; dates themselves are never stored) ------

def set_schedule_start(pid, date_str):
    """Pin the project anchor. Default when unset is the plan's `created` date (deterministic)."""
    with _lock:
        post = read_post(pid)
        sc = dict(post.get("schedule") or {})
        if date_str:
            sc["start"] = date_str
        else:
            sc.pop("start", None)
        if sc:
            post["schedule"] = sc
        else:
            post.metadata.pop("schedule", None)
        _dump(pid, post, f"schedule start {pid}")
        return full(post)


def save_baseline(pid, project_start, task_dates):
    """Snapshot a computed schedule so later runs can report variance (Tracking Gantt)."""
    with _lock:
        post = read_post(pid)
        post["baseline"] = {"captured": _now(), "project_start": project_start,
                            "tasks": task_dates}
        _dump(pid, post, f"baseline capture {pid}")
        return full(post)


def clear_baseline(pid):
    with _lock:
        post = read_post(pid)
        post.metadata.pop("baseline", None)
        _dump(pid, post, f"baseline clear {pid}")
        return full(post)


def plan_anchor(post) -> str:
    """Project start: explicit `schedule.start`, else the plan's `created` date."""
    sc = post.get("schedule") or {}
    if sc.get("start"):
        return str(sc["start"])[:10]
    return str(post.get("created") or _now())[:10]


def time_report(pid=None):
    with _lock:
        if pid:
            post = read_post(pid)
            tasks = parse_tasks(post.content)
            tm = post.get("time") or {}
            return {"id": pid, "est_hours": tm.get("est_hours", 0),
                    "spent_hours": tm.get("spent_hours", 0),
                    "tasks": [{"id": t["id"], "text": t["text"],
                               "est_hours": t["est_hours"], "spent_hours": t["spent_hours"]}
                              for t in tasks]}
        rows = []
        for p in glob.glob(os.path.join(DATA, "*.md")):
            post = frontmatter.load(p)
            if post.get("status") == "archived":
                continue
            tm = post.get("time") or {}
            est, spent = tm.get("est_hours", 0), tm.get("spent_hours", 0)
            rows.append({"id": post["id"], "est_hours": est, "spent_hours": spent,
                         "remaining_hours": max(0, est - spent)})
        return {"plans": rows,
                "total": {"est_hours": sum(r["est_hours"] for r in rows),
                          "spent_hours": sum(r["spent_hours"] for r in rows)}}


def count():
    return len(glob.glob(os.path.join(DATA, "*.md")))
