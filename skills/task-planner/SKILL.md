# Task Planner

This skill replicates the core functionality of the Manus.im `plan` tool. It allows you to create, manage, and display a structured, multi-phase task plan to guide your execution.

## When to Use

Use this skill at the beginning of every complex, multi-step task. Update the plan whenever the user changes requirements or you discover significant new information. Advance the plan when you complete a phase.

## Commands

### Create or Update a Plan

```
/task-planner update --goal "Your overall goal" --phases '[{"id":1,"title":"Phase 1"},{"id":2,"title":"Phase 2"}]' --current 1
```

This creates or overwrites the current plan. The `--phases` argument takes a JSON array of phase objects, each with an `id` (integer) and a `title` (string). The `--current` argument sets the active phase ID.

### Advance to the Next Phase

```
/task-planner advance --next 2
```

This marks the current phase as complete and sets the active phase to the specified ID.

### Show the Current Plan

```
/task-planner show
```

This displays the current plan, highlighting the active phase.

## Implementation

This skill is implemented by the `task_planner.py` script in this directory. It stores the plan state in a `plan.json` file in the agent's workspace directory (`~/.hermes/workspace/plan.json`).

The script is invoked by the agent using the `terminal` tool:

```bash
python ~/.hermes/skills/task-planner/task_planner.py update --goal "..." --phases "..." --current 1
```
