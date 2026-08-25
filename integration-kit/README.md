# Integration kit

Two separate things, and they install separately — which is the usual confusion:

| | what it gives you | scope |
|---|---|---|
| **MCP server** | the `megaplan` tool (list/get/save/schedule/gantt/render …) | a network endpoint — any machine on the LAN |
| **`/megaplan` command** | the interactive planning *mode* (research → discuss → persist) | a **local file** on each machine |

A machine can have the tool and no slash command. That is not a broken install: the
MCP is reachable over the network, the command is a file that has to exist locally.

## Claude Code

**1. The MCP server** — merge `claude-code.json` into `~/.claude.json`:

```json
"mcpServers": { "megaplan": { "type": "http", "url": "http://<nas>:8932/mcp/" } }
```

**2. The `/megaplan` command** — copy or symlink `megaplan-command.md` into your commands dir:

```bash
mkdir -p ~/.claude/commands
ln -sfn "$(pwd)/megaplan-command.md" ~/.claude/commands/megaplan.md   # symlink: tracks the repo
# or: cp megaplan-command.md ~/.claude/commands/megaplan.md           # copy: independent
```

Commands are enumerated at startup, so **restart Claude Code** before `/megaplan`
shows up in `/help`.

## input

Merge `input-settings-snippet.json` into `~/.config/input/settings.json` (MCP servers
auto-register **disabled**; enable megaplan's tools in `/tools`), or just:

```
/mcp add megaplan http://<nas>:8932/mcp/
```

`input`'s planning mode is `/plan` (alias of `/megaplan`) and ships with the client,
so there is no command file to install.

## Verifying

```bash
curl -s http://<nas>:8932/health          # {"status":"ok", ...}
```

In Claude Code: `/help` should list `/megaplan`, and asking "what plans do I have?"
should call the `megaplan` tool. If the tool works but the command is missing, you
installed step 1 and not step 2.
