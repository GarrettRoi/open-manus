# Hive Mind — Shared Memory System

The Hive Mind is the team's shared knowledge base stored in Redis. All agents can read from it. All agents can submit lessons to it. Only the Lexi curates and distributes knowledge.

## How to Use

### Reading Knowledge (All Agents)

Search the hive mind for lessons relevant to your current task:

```bash
python3 /app/skills/hive_mind/hive_search.py --query "your search terms" --agent "your_name"
```

This searches across all categories using keyword matching and returns the most relevant lessons. Always do this before starting a new task (see Boot Instructions in your SOUL.md).

Check your personal knowledge feed for lessons the Lexi has routed to you:

```bash
python3 /app/skills/hive_mind/hive_feed.py --agent "your_name"
```

### Submitting Lessons (All Agents)

When you discover something useful during a task, submit it to the Lexi for review:

```bash
python3 /app/skills/hive_mind/hive_submit.py --agent "your_name" --title "Short descriptive title" --content "The full lesson content" --tags "tag1,tag2,tag3"
```

Tags should include relevant categories like: `sales`, `real_estate`, `dj`, `photo_booth`, `cana`, `marketing`, `social_media`, `ads`, `automation`, `finance`, `client_relations`, `content`, `research`.

### Managing Knowledge (Lexi Only)

Review the intake queue:

```bash
python3 /app/skills/hive_mind/hive_review.py --action list
```

Approve and distribute a lesson (after Garrett's approval in Discord):

```bash
python3 /app/skills/hive_mind/hive_review.py --action approve --lesson-id "LESSON_ID" --targets "agent1,agent2" --adapted-content "Optional reframed content for specific agents"
```

Update an existing lesson instead of creating a new one:

```bash
python3 /app/skills/hive_mind/hive_review.py --action update --lesson-id "EXISTING_LESSON_ID" --content "Updated content that merges old and new information"
```

Archive a lesson (remove from active distribution):

```bash
python3 /app/skills/hive_mind/hive_review.py --action archive --lesson-id "LESSON_ID"
```

Reject a submission with feedback:

```bash
python3 /app/skills/hive_mind/hive_review.py --action reject --lesson-id "LESSON_ID" --reason "Why it was rejected"
```

### Viewing the Skill Registry (All Agents)

See what skills are available to a specific agent:

```bash
python3 /app/skills/hive_mind/skill_registry.py --action list --agent "agent_name"
```

See all skills across all agents:

```bash
python3 /app/skills/hive_mind/skill_registry.py --action list-all
```

## Redis Key Reference

| Key Pattern | Purpose |
|---|---|
| `hive:inbox:librarian` | Lexi's intake queue (new submissions) |
| `hive:lessons:{lesson_id}` | Individual lesson entries |
| `hive:feed:{agent_name}` | Per-agent knowledge feed (approved lessons) |
| `hive:goals:{agent_name}` | Agent's organizational goals (for matching) |
| `hive:skills:{agent_name}` | Skills assigned to this agent |
| `hive:skills:registry` | Master skill registry |
| `hive:archive` | Archived/retired lessons |
