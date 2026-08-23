"""MegaPlan store: git-backed markdown plans with YAML frontmatter.

One plan = one file `<id>.md` under DATA_PATH (a git repo). Every mutation writes
the file and auto-commits. Tasks live as markdown checkboxes with a stable `<!-- tN -->`
id so tools can target them without line numbers. Plans are portable/hand-editable;
the store re-parses on read and tolerates hand edits.
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

def parse_tasks(body: str) -> list[dict]:
    tasks, n = [], 0
    for line in body.splitlines():
        m = _TASK_RE.match(line)
        if not m or not line.lstrip().startswith("- ["):
            continue
        n += 1
        text = m.group("text")
        est = spent = None
        blocked = "@blocked" in text
        mm = _META_RE.search(text)
        if mm:
            for part in mm.group(1).split(","):
                if ":" in part:
                    k, v = part.split(":", 1)
                    k = k.strip().lower()
                    if k == "est":
                        est = _parse_hours(v)
                    elif k == "spent":
                        spent = _parse_hours(v)
        label = _META_RE.sub("", text).replace("@blocked", "").replace("@wip", "").strip()
        tasks.append({
            "id": m.group("tid"),   # None if unlabeled; assign_ids() fills it
            "done": m.group("mark").lower() == "x",
            "text": label,
            "est_hours": est, "spent_hours": spent,
            "blocked": blocked, "depends": m.group("dep"),
            "_raw": line, "_labeled": bool(m.group("tid")),
        })
    return tasks


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


def progress(tasks: list[dict]) -> dict:
    total = len(tasks)
    done = sum(1 for t in tasks if t["done"])
    return {"done": done, "total": total, "pct": round(100 * done / total) if total else 0}


def _render_task(t: dict) -> str:
    mark = "x" if t["done"] else " "
    meta = []
    if t.get("est_hours") is not None:
        meta.append(f"est: {_fmt_hours(t['est_hours'])}")
    if t.get("spent_hours"):
        meta.append(f"spent: {_fmt_hours(t['spent_hours'])}")
    parts = [f"- [{mark}] {t['text']}"]
    if t.get("blocked"):
        parts.append("@blocked")
    if meta:                              # meta MUST stay trailing: _META_RE anchors to end-of-text
        parts.append(f"({', '.join(meta)})")
    comment = t["id"] + (f" depends: {t['depends']}" if t.get("depends") else "")
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
    tasks = assign_ids(parse_tasks(post.content))
    return {**post.metadata, "progress": progress(tasks), "tasks": tasks,
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


def add_task(pid, text, est_hours=None):
    with _lock:
        post = read_post(pid)
        tasks = assign_ids(parse_tasks(post.content))
        used = {int(t["id"][1:]) for t in tasks}
        nxt = 1
        while nxt in used:
            nxt += 1
        new = {"id": f"t{nxt}", "done": False, "text": text.strip(),
               "est_hours": est_hours, "spent_hours": None, "blocked": False, "depends": None}
        tasks.append(new)
        post.content = _rewrite_tasks(post.content, tasks)
        _dump(pid, post, f"task add {pid}#{new['id']}")
        return full(post)


def _find_task(tasks, task_id):
    for t in tasks:
        if t["id"] == task_id:
            return t
    raise KeyError(task_id)


def update_task(pid, task_id, text=None, done=None, blocked=None, est_hours=None):
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
            t["est_hours"] = est_hours
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
            t["spent_hours"] = (t["spent_hours"] or 0) + hours
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
