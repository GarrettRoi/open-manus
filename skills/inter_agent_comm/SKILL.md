# Retired inter-agent inbox

This skill's Redis inbox/send_task path is retired. Do not send or check live
fleet work through it. Read `/app/skills/harmony_communication/SKILL.md` for
the current request board: on-demand discovery, explicit recipient, structured
ticket and structured outcome. `agent_dispatch` remains the compatibility
tool name. If the running tool lacks the new actions during rollout, follow
its exposed schema for existing chains and notify the owner about unavailable
new functionality. Do not fall back to this inbox.