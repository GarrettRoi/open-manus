# Retired webhook communication

The webhook handoff and completion notification path is retired. Never call
`webhook_comm.py` or send Discord mentions to hand work to another agent.
Use the native `agent_dispatch` compatibility tool and read
`/app/skills/harmony_communication/SKILL.md` for the request-board contract.
Legacy in-flight dispatch chains remain on their original tool protocol;
do not duplicate them as new tickets or use this webhook as a fallback.
Hive Mind carries reusable knowledge, not live tickets.