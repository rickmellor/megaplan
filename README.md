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

Every render writes a **markdown** file and a rendered **HTML** sibling. The returned `url` is
the HTML one — a self-contained page with the gantt actually drawn, styled, light/dark aware.
Mermaid is vendored into the image and served from `/static/mermaid.min.js`, so a report renders
with no internet and no CDN. `markdown_url` is the source, which is what agents and git diffs
want.

```bash
curl -s -X POST http://<host>:8932/op -d '{"action":"portfolio"}'   # one report, ALL plans
```

`portfolio` covers every non-archived plan: aggregate progress, a table of plans with schedule
and blockers linking to each plan's own report, a timeline gantt, and what is blocked. Plans that
carry no task durations are left out of the timeline rather than drawn at a fake one day each.

Set `MEGAPLAN_AUTORENDER_H=6` and the service re-renders changed plans (and the portfolio) on
that cadence, so `…-latest.html` is current without anyone asking. Only plans whose file is newer
than their last report are re-rendered — an unchanged plan would just add git noise. This runs
server-side deliberately: the NAS is always on, so reports stay fresh even when the machines that
write plans are powered off.

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

## Setup — server

Needs Docker and a host that stays up. Everything lives in one directory; the plan store is a
bind-mounted git repo, so the data is plain files you can read without the service running.

**1. Put the code on the host.** There is no registry image — the host builds it. Copy the
files the image needs (`tar`-over-`ssh` here because some NAS boxes restrict rsync):

```bash
cd ~/repos/megaplan
git commit -am "..." && git push        # commit BEFORE shipping, so the host matches a known ref
tar czf - Dockerfile docker-compose.yml app.py store.py memory.py schedule.py report.py \
          requirements.txt README.md .env.example \
  | ssh <host> 'mkdir -p /volume1/docker/megaplan && tar xzf - -C /volume1/docker/megaplan'
```

> Keep that file list in sync with the Dockerfile's `COPY` line. A module added to one and not
> the other builds an image that dies on import — which is exactly how this service once went
> down mid-deploy.

**2. Configure.** `cp .env.example .env` on the host and set at least:

| variable | why it matters |
|---|---|
| `MEGAPLAN_PUBLIC_URL` | how clients reach this box. `render` builds report URLs from it — leave it `localhost` and every report link is wrong |
| `MEGAPLAN_DATA_PATH` | `/data` inside the container; leave it alone unless you change the mount |
| `MEGAPLAN_GIT_NAME` / `_EMAIL` | the identity on every auto-commit |
| `MEGAPLAN_MEMORY_URL` | optional — an Astoria `/recall` endpoint for related-plan context. Absent or unreachable just degrades `context`; nothing else notices |
| `MEGAPLAN_OFFSITE_DIR` | optional — a share your cloud-sync tool mirrors, for the backup sidecar |

**3. Ownership.** The container runs as uid 1000 and writes into the bind mount, so the store
must be owned by it or every write fails with a permission error that looks like a git problem:

```bash
ssh <host> 'mkdir -p /volume1/docker/megaplan/{data,backups} \
            && chown -R 1000:1000 /volume1/docker/megaplan/data'
```

**4. Start it.** `git init`, the git identity, and `safe.directory` are all handled on first boot
by `store.ensure_repo()` — an empty `data/` is the expected starting state.

```bash
ssh <host> 'cd /volume1/docker/megaplan && docker compose up -d --build'
curl -s http://<host>:8932/health        # {"status":"ok","plans_count":0,...}
```

Two containers come up: `megaplan` (the service) and `megaplan-backup`, a sidecar that writes a
`git bundle` of the store every few hours, keeps a rotation, and copies to `MEGAPLAN_OFFSITE_DIR`
if one is set. It only bundles when the store has actually changed.

**Redeploying** is steps 1 and 4 again. `data/` is never in the tar, so the plan store is
untouched by a rebuild.

**Verifying:**

```bash
curl -s http://<host>:8932/health                              # service + memory reachability
curl -s -X POST http://<host>:8932/op -d '{"action":"list"}'   # the plans
ssh <host> 'docker exec megaplan git -C /data log --oneline -3'  # commits are landing
ssh <host> 'docker logs megaplan-backup --tail 5'                # "backup ok" / "backup skip"
```

## Setup — Claude Code

Two independent pieces, and only the first travels between machines. The MCP server is a
network endpoint, so any machine that can reach the host gets the tool; the `/megaplan` command
is a local file that has to exist on each machine. A machine with the tool and no slash command
is a half-finished install, not a broken one.

**1. The MCP server** — merge into `~/.claude.json`:

```json
"mcpServers": {
  "megaplan": { "type": "http", "url": "http://<host>:8932/mcp/" }
}
```

This gives the model the single `megaplan` tool — every action above, including `render`.

**2. The `/megaplan` command** — the interactive planning mode (research → discuss → persist):

```bash
mkdir -p ~/.claude/commands
ln -sfn "$PWD/integration-kit/megaplan-command.md" ~/.claude/commands/megaplan.md
```

A symlink tracks the repo as it changes; `cp` instead if you want it pinned.

**3. Restart Claude Code.** Commands and MCP servers are both enumerated at session start, so
neither appears in a session that was already running.

**Verifying:** `/help` lists `/megaplan`, and asking *"what plans do I have?"* calls the tool.
If the tool answers but `/megaplan` is unknown, you did step 1 and not step 2.

## Setup — input

```
/mcp add megaplan http://<host>:8932/mcp/
```

Or merge `integration-kit/input-settings-snippet.json` into `~/.config/input/settings.json`.
MCP servers auto-register **disabled** — enable megaplan's tool in `/tools`.

There is no command file to install: `input`'s planning mode is `/plan` (alias `/megaplan`) and
ships with the client.

[`integration-kit/`](integration-kit/README.md) has ready-to-paste snippets for both clients.

## Tests

```bash
python -m venv .venv && .venv/bin/pip install -r requirements.txt pytest
.venv/bin/pytest tests/ -q      # 54 tests: parse/render round-trip + the CPM engine
```

The round-trip tests matter: `update_task` rewrites every checklist line, so a renderer bug
would corrupt whole plans. Before changing `parse_tasks`/`_render_task`, re-run the rewrite
over the live store and diff against the old output.

## Phase 2 (later)

Active LLM suggestions (server calls SAINT `saint-auto` to propose overlaps / improvements /
cross-links) and embedding-based related-plan similarity (reuse the NAS TEI/nomic container).
`plan_context`'s interface stays the same.
