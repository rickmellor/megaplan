# MegaPlan service

A persistent, git-backed **markdown plan store**, exposed **MCP-first** so any client
(Claude Code, `input`, anything speaking MCP) can create and manage plans that live
together and inform each other. Plans are **informed by** an [Astoria](https://github.com/rickmellor/astoria)
memory service (retrieval, soft dependency) but stored only here. FastMCP 2.x on FastAPI,
streamable HTTP, docker.

- **Endpoint:** `http://<nas>:8932/mcp/` (streamable HTTP MCP) + REST (`/plans`, `/context`, `/health`).
- **Store:** one markdown file per plan under `/data` (a git repo); every mutation auto-commits (versioned history).
- **Plan format:** YAML frontmatter (`id, title, status, priority, depends_on, tags, created, updated, time`, plus optional `schedule` / `baseline`) + markdown body with `- [ ]` task checklists (each carries a stable `<!-- tN -->` id). Hand-editable/portable.

## MCP surface — one tool

The model-facing MCP surface is a **single action-dispatch tool** so planning feels like one
thing, not 16 CRUD verbs (and only one schema loads per request):

```
megaplan(action, …)
  reads:  context · list · get · deps · graph · blocked_by · time_report · review
          schedule · gantt
  writes: save · update · add_task · update_task · complete · depend · log_time · archive
          baseline
```

The planning hot path is just two moves: `megaplan(action="context", goal=…)` to ground in
related prior work + recalled memory, then `megaplan(action="save", title=…, body=…)` with
the whole plan (incl. a `## Tasks` checklist) in the body. `save` upserts (create, or patch if
the `id` exists). Every op is still reachable programmatically over REST via
`POST /op {"action": …, …}` plus the convenience routes (`/plans`, `/plans/{id}`, `/context`).

**Interactive planning:** Claude Code gets a `/megaplan` command and `input` gets a `/plan`
mode — both run research → discuss → refine → persist-to-MegaPlan on top of this tool.

## Scheduling (Gantt-capable)

Tasks carry optional scheduling attributes in the trailing meta block. Dates are **derived,
never stored** — `schedule.py` runs a CPM forward/backward pass on read, the same way
`progress` is derived. Time is **elapsed days** by design: no working calendar, no holidays.

```markdown
## Tasks
- [ ] Design                                                          <!-- t1 -->
  - [x] Spike            (dur: 2d)                                    <!-- t2 -->
  - [ ] Schema           (dur: 3d, dep: t2, pct: 50, who: rick)       <!-- t3 -->
- [ ] Build              (est: 20h, dur: 5d, dep: t3FS+1d)            <!-- t4 -->
- [ ] Ship               (dur: 0d, dep: t4, deadline: 2026-09-30)     <!-- t5 -->
```

| Key | Meaning |
|---|---|
| `dur` | Elapsed duration — `3d` `2w` `12h` `1.5d` (`3ed` accepted). **`0d` = milestone.** Absent ⇒ 1d |
| `dep` | Predecessors in MS-Project column notation, **space-separated**: `t4` (= FS+0), `t4FS+2d`, `t7SS`, `t2FF-1d`, `t9SF`. Cross-plan: `<plan-id>#t14` |
| `pct` | Percent complete 0–100. The checkbox still wins: `[x]` ⇒ 100 |
| `who` | Swimlane / grouping label |
| `start` | Start-no-earlier-than constraint (`YYYY-MM-DD`) — pins a task, the rest flows around it |
| `deadline` | **Soft** — never moves dates; a miss is reported in `warnings` |
| `o`/`m`/`p` | PERT three-point; supplies `dur` when `dur` is absent (`(o + 4m + p) / 6`), σ rolls up along the critical path |
| `est`/`spent` | Unchanged — these stay **work** (effort), independent of **duration** |

**Indentation is significant**: an indented task is a child, and its parent becomes a
**summary task** whose bar spans its children and whose `pct` is duration-weighted. Link
leaf tasks, not summaries (a `dep` on a summary is ignored with a warning).

The project anchor is the plan's `created` date — deterministic, so a plan schedules
identically tomorrow — unless overridden with `update(schedule_start=…)`.

```bash
curl -s http://<nas>:8932/plans/<id>/schedule    # dates, ES/EF/LS/LF, float, critical path
curl -s http://<nas>:8932/plans/<id>/gantt       # Mermaid gantt source (group=outline|who)
curl -s -X POST http://<nas>:8932/op -d '{"action":"baseline","id":"<id>"}'
```

`baseline` snapshots the computed schedule into frontmatter; later `schedule` calls then
report `start_variance_days` / `finish_variance_days` per task (a Tracking Gantt).

Mermaid limits worth knowing: no baseline bars, no assignee swimlanes (sections are the only
grouping), and lag is not drawn — it is implicit in the computed bar positions. An MSPDI XML
export is the eventual answer for full fidelity.

## Progress reports

`render` composes a plan into one readable markdown document — intent (the plan's own prose, not
a re-derivation from its tasks), a progress bar, effort, a schedule summary, grouped scheduler
warnings, a Mermaid gantt, a baseline-tracking section when the plan has a baseline, and tasks
grouped by state.

```bash
curl -s -X POST http://<nas>:8932/op -d '{"action":"render","id":"<id>"}'
#  -> {"url": "http://<nas>:8932/reports/<id>-20260825-111806.md",
#      "latest_url": "http://<nas>:8932/reports/<id>-latest.md", ...}
curl -s http://<nas>:8932/reports                 # every saved report, newest first
```

Reports are written to `reports/` inside the store and committed like any other mutation — a
progress report is a point-in-time record, which is exactly the kind of thing worth having
history for. Each render writes a timestamped file plus a `-latest.md` that keeps a stable URL.
`save: false` returns the markdown inline without writing. `store.list_plans` globs `*.md`
non-recursively, so the subdirectory is invisible to the plan store itself.

Two honest limits worth knowing:

- **Mermaid cannot overlay baseline bars** (see the note under Scheduling). So the tracking view
  is a variance table for precision, plus a second, deliberately small gantt containing *only*
  the tasks that actually moved — the part anyone actually looks at.
- **An unscheduled plan still produces a schedule.** With no `dur`, every task defaults to a day,
  which makes the project look one day long and puts every task on the critical path. Rather than
  present that as fact, the report detects it and says so.

## Deploy (NAS: /volume1/docker/megaplan)

```bash
cd ~/repos/megaplan
git commit -am "..." && git push          # always commit + push BEFORE the tar
tar czf - Dockerfile docker-compose.yml app.py store.py memory.py schedule.py \
          requirements.txt README.md .env.example \
  | ssh root-dxp4800gt 'mkdir -p /volume1/docker/megaplan && tar xzf - -C /volume1/docker/megaplan'
# first time: make the bind-mounted store writable by the container's uid 1000
ssh root-dxp4800gt 'mkdir -p /volume1/docker/megaplan/data && chown -R 1000:1000 /volume1/docker/megaplan/data'
ssh root-dxp4800gt 'cd /volume1/docker/megaplan && docker compose up -d --build'
curl http://192.168.1.134:8932/health
```

## Tests

```bash
python -m venv .venv && .venv/bin/pip install -r requirements.txt pytest
.venv/bin/pytest tests/ -q      # 54 tests: parse/render round-trip + the CPM engine
```

The round-trip tests matter: `update_task` rewrites every checklist line, so a renderer bug
would corrupt whole plans. Before changing `parse_tasks`/`_render_task`, re-run the rewrite
over the live store and diff against the old output.

## Clients

- **Claude Code** — add to `~/.claude.json` `mcpServers`: `{"megaplan": {"type": "http", "url": "http://192.168.1.134:8932/mcp/"}}`.
- **input** (formerly qchat) — add to `~/.config/input/settings.json` `mcp_servers`: `[{"name": "megaplan", "url": "http://192.168.1.134:8932/mcp/"}]` (auto-registers **disabled**; enable in `/tools`), or `/mcp add megaplan http://192.168.1.134:8932/mcp/`.

See [`integration-kit/`](integration-kit/README.md) for ready-to-paste snippets **and the
`/megaplan` command file** — note the MCP server and the slash command install separately, so a
machine can have the tool without the command.

## Phase 2 (later)

Active LLM suggestions (server calls SAINT `saint-auto` to propose overlaps / improvements /
cross-links) and embedding-based related-plan similarity (reuse the NAS TEI/nomic container).
`plan_context`'s interface stays the same.
