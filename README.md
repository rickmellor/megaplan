# MegaPlan service

A persistent, git-backed **markdown plan store**, exposed **MCP-first** so any client
(Claude Code, `input`/qchat, anything speaking MCP) can create and manage plans that
live together and inform each other. Plans are **informed by** MemoryOS (retrieval) but
stored only here. Modeled on `../memoryos-service` (FastMCP 2.x on FastAPI, streamable
HTTP, docker on the NAS).

- **Endpoint:** `http://<nas>:8932/mcp/` (streamable HTTP MCP) + REST (`/plans`, `/context`, `/health`).
- **Store:** one markdown file per plan under `/data` (a git repo); every mutation auto-commits (versioned history).
- **Plan format:** YAML frontmatter (`id, title, status, priority, depends_on, tags, created, updated, time`) + markdown body with `- [ ]` task checklists (each carries a stable `<!-- tN -->` id). Hand-editable/portable.

## MCP surface — one tool

The model-facing MCP surface is a **single action-dispatch tool** so planning feels like one
thing, not 16 CRUD verbs (and only one schema loads per request):

```
megaplan(action, …)
  reads:  context · list · get · deps · graph · blocked_by · time_report · review
  writes: save · update · add_task · update_task · complete · depend · log_time · archive
```

The planning hot path is just two moves: `megaplan(action="context", goal=…)` to ground in
related prior work + MemoryOS knowledge, then `megaplan(action="save", title=…, body=…)` with
the whole plan (incl. a `## Tasks` checklist) in the body. `save` upserts (create, or patch if
the `id` exists). Every op is still reachable programmatically over REST via
`POST /op {"action": …, …}` plus the convenience routes (`/plans`, `/plans/{id}`, `/context`).

**Interactive planning:** Claude Code gets a `/megaplan` command and `input` gets a `/plan`
mode — both run research → discuss → refine → persist-to-MegaPlan on top of this tool.

## Deploy (NAS: /volume1/docker/megaplan)

```bash
cd ~/repos/nas-ai-accelerator/megaplan-service
tar czf - . | ssh root-dxp4800gt 'mkdir -p /volume1/docker/megaplan && tar xzf - -C /volume1/docker/megaplan'
# first time: make the bind-mounted store writable by the container's uid 1000
ssh root-dxp4800gt 'mkdir -p /volume1/docker/megaplan/data && chown -R 1000:1000 /volume1/docker/megaplan/data'
ssh root-dxp4800gt 'cd /volume1/docker/megaplan && docker compose up -d --build'
curl http://192.168.1.134:8932/health
```

## Clients

- **Claude Code** — add to `~/.claude.json` `mcpServers`: `{"megaplan": {"type": "http", "url": "http://192.168.1.134:8932/mcp/"}}`.
- **input** (formerly qchat) — add to `~/.config/input/settings.json` `mcp_servers`: `[{"name": "megaplan", "url": "http://192.168.1.134:8932/mcp/"}]` (auto-registers **disabled**; enable in `/tools`), or `/mcp add megaplan http://192.168.1.134:8932/mcp/`.

See `integration-kit/` for ready-to-paste snippets.

## Phase 2 (later)

Active LLM suggestions (server calls SAINT `saint-auto` to propose overlaps / improvements /
cross-links) and embedding-based related-plan similarity (reuse the NAS TEI/nomic container).
`plan_context`'s interface stays the same.
