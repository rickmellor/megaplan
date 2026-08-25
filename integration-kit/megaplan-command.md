---
description: MegaPlan mode — interactively research, discuss, and refine a build plan, then persist it to the unified MegaPlan store
argument-hint: [goal, or an existing plan id to revisit]
---

You are now in **MegaPlan mode** — a distinct, interactive planning experience. Do NOT use Claude Code's built-in plan mode / `ExitPlanMode` here; this is its own flow that ends by persisting to the MegaPlan store, so the plan outlives the session and informs future plans.

MegaPlan is the single `megaplan` MCP tool (one action-dispatch tool over a git-backed markdown plan store at `http://192.168.1.134:8932/mcp/`). If the tool isn't available, tell the user to check the megaplan MCP server and stop.

**This session's target:** $ARGUMENTS

Run this loop — the conversation IS the planning experience; the tool only grounds and persists it:

1. **Ground in prior work.** Call `megaplan(action="context", goal="<the goal>")`. If revisiting, also `megaplan(action="review", id="<id>")`. Briefly summarize the related existing plans and any relevant memory the store surfaced — this "informed by prior work" step is exactly what native plan mode lacks. Flag any existing plan this one might depend on or overlap with.

2. **Research (read-only).** Explore the codebase/topic as needed to plan well — Explore/Read/Grep, read-only commands. Do NOT make changes while in this mode.

3. **Discuss and refine — iteratively.** Propose an approach, surface trade-offs and open questions, and work WITH the user to sharpen it. Ask clarifying questions. Persist NOTHING yet. Keep iterating until the user is satisfied.

4. **Persist on approval.** When the user approves, write the whole plan in ONE call:
   `megaplan(action="save", title=…, body="<markdown WITH a '## Tasks' checklist>", priority="low|medium|high|critical", tags=[…], depends_on=[<related plan ids>])`
   - Put the full plan in `body`: goal/context prose, then a `## Tasks` section of `- [ ] task (est: 2h)` checkboxes (they get stable ids automatically), then any `## Notes`.
   - Prefer ONE rich `save` over many `add_task` calls.
   - If revisiting, pass the existing `id` to update in place. If it depends on other plans, include their ids in `depends_on`.

5. **Hand off.** Report the saved plan id and how to continue it: revisit with `/megaplan <id>`, track with `megaplan` `complete`/`log_time`, re-ground with `action="review"`.
