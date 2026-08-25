---
description: MegaPlan mode — interactively research, discuss, and refine a build plan, then persist it to the unified MegaPlan store
argument-hint: "[nothing = list] | <goal> | <plan-id> | show|schedule|gantt|report <id> | portfolio"
---

You are now in **MegaPlan mode** — a distinct, interactive planning experience. Do NOT use Claude Code's built-in plan mode / `ExitPlanMode` here; this is its own flow that ends by persisting to the MegaPlan store, so the plan outlives the session and informs future plans.

MegaPlan is the single `megaplan` MCP tool (one action-dispatch tool over a git-backed markdown plan store at `http://192.168.1.134:8932/mcp/`). If the tool isn't available, tell the user to check the megaplan MCP server and stop.

**This session's target:** $ARGUMENTS

## First, read what they asked for

`$ARGUMENTS` decides what happens. Do the matching branch and STOP — only a goal or a plan id
starts the planning conversation.

| argument | do this |
|---|---|
| *(empty)* | **Orient, do not plan.** `megaplan(action="list")`, show the plans as a compact table (id · status · progress · title), then show the menu below verbatim so they can see what is available. Ask what they want. |
| `list` / `plans` | `megaplan(action="list")` → the same table. |
| `portfolio` | `megaplan(action="portfolio")` → hand over the returned `url`, and say in one line where things stand overall. |
| `show <id>` | `megaplan(action="get", id=…)` → summarise: intent, progress, what is left, what is blocked. |
| `schedule <id>` | `megaplan(action="schedule", id=…)` → finish date, critical path, and any **warnings** (never skip those). |
| `gantt <id>` | `megaplan(action="gantt", id=…)` → show the mermaid source in a ```mermaid block so it renders. |
| `report <id>` | `megaplan(action="report", id=…)` → hand over the returned `url` (a saved report with the gantt drawn). |
| an existing **plan id** | Revisit it: `megaplan(action="review", id=…)`, summarise where it stands, then run the planning loop to refine it. |
| anything else | Treat it as a **goal** and run the planning loop. |

The menu to show when they arrive with nothing:

```
/megaplan                 list plans
/megaplan <goal>          plan something new
/megaplan <plan-id>       revisit and refine an existing plan
/megaplan show <id>       where a plan stands
/megaplan schedule <id>   dates, critical path, warnings
/megaplan gantt <id>      the chart, inline
/megaplan report <id>     a saved report with the chart drawn (returns a URL)
/megaplan portfolio       one report across every active plan
```

## The planning loop

Run this loop — the conversation IS the planning experience; the tool only grounds and persists it:

1. **Ground in prior work.** Call `megaplan(action="context", goal="<the goal>")`. If revisiting, also `megaplan(action="review", id="<id>")`. Briefly summarize the related existing plans and any relevant memory the store surfaced — this "informed by prior work" step is exactly what native plan mode lacks. Flag any existing plan this one might depend on or overlap with.

2. **Research (read-only).** Explore the codebase/topic as needed to plan well — Explore/Read/Grep, read-only commands. Do NOT make changes while in this mode.

3. **Discuss and refine — iteratively.** Propose an approach, surface trade-offs and open questions, and work WITH the user to sharpen it. Ask clarifying questions. Persist NOTHING yet. Keep iterating until the user is satisfied.

4. **Persist on approval.** When the user approves, write the whole plan in ONE call:
   `megaplan(action="save", title=…, body="<markdown WITH a '## Tasks' checklist>", priority="low|medium|high|critical", tags=[…], depends_on=[<related plan ids>])`
   - Put the full plan in `body`: goal/context prose, then a `## Tasks` section of `- [ ] task (est: 2h)` checkboxes (they get stable ids automatically), then any `## Notes`.
   - Prefer ONE rich `save` over many `add_task` calls. On an existing `id`, `body` REPLACES the stored body — that is the intended way to revise a plan; use `update_task`/`add_task` for single edits.
   - If it depends on other plans, include their ids in `depends_on`.
   - **Give tasks durations when order matters.** `est` is effort, `dur` is elapsed time, and only `dur` + `dep` produce a schedule. A plan whose tasks carry neither is a checklist: every task is assumed to take a day, so the project looks one day long and everything lands on the critical path. Write `- [ ] the task  (est: 6h, dur: 3d, dep: t2)` where the sequence is real, and leave both off where it genuinely is just a list.
   - Indentation makes hierarchy. A `dep` **on a summary task is ignored** — link its leaf children instead, or its successor will not wait for them.

5. **Show the schedule.** After saving a plan that has any durations, call `megaplan(action="schedule", id=…)` and tell the user what it means: the finish date, the critical path, and — importantly — any `warnings`. Warnings are how a wrong dependency or an ignored summary link becomes visible; do not skip them. Offer `megaplan(action="gantt", id=…)` for the chart inline, or `megaplan(action="report", id=…)` for a saved report with the gantt drawn, whose `url` you can hand over.

6. **Hand off.** Report the saved plan id and how to continue it: revisit with `/megaplan <id>`, track with `megaplan` `complete`/`log_time`, re-ground with `action="review"`, see where everything stands with `action="portfolio"`.
