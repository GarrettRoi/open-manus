# Webhook Communication: Cross-Channel Orchestration

The **Webhook Communication** tool allows agents to communicate across their private Discord channels without creating chat "doom loops." All communications are automatically logged to the `#harmony-communication` channel for a full team audit trail.

## Key Rules
- **Harmony -> Agent**: Harmony must always `@` the agent in the message.
- **Agent -> Harmony**: Agents can respond to Harmony's webhook *without* @mentioning her to maintain flow.
- **Finish Notification**: Only when a task is finished or a blocker is hit should an agent use the `webhook_send` tool to notify Harmony.
- **Task Board First**: Always update the **Task Board** *before* sending a completion webhook to Harmony.

## Usage Commands

### 1. Harmony Sending to an Agent
```bash
python3 /app/skills/hive_mind/webhook_comm.py --target "Raven" --message "Check the task board for a new research job. @Raven" --sender "Harmony"
```

### 2. Agent Notifying Harmony (Completion)
```bash
python3 /app/skills/hive_mind/webhook_comm.py --target "Harmony" --message "Task TASK-001 completed. Research results are in research.txt." --sender "Raven"
```

### 3. Agent Notifying Harmony (Blocker)
```bash
python3 /app/skills/hive_mind/webhook_comm.py --target "Harmony" --message "Blocked on TASK-002. Sabrina needs to clarify the sales copy first." --sender "Cora"
```

## Anti-Doom-Loop Protocol
- **Do Not @Mention Harmony** in your normal private channel work.
- **Only Webhook Harmony** when you are **Finished** or **Blocked**.
- This keeps Harmony's attention focused on orchestration rather than every individual step.
