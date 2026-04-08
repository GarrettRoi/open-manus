# Open Manus Infrastructure & Tooling Plan

## 1. Persistent Memory & Storage
**Issue:** Agents lose file changes and cron jobs on Railway redeploys.
**Solution:** 
- Mount Railway Volumes to `/root/.hermes/workspace` and `/root/.hermes/skills` to persist data.
- The `memory_tool.py` already writes to disk (`MEMORY.md` and `USER.md`). By mounting a volume to the workspace directory, memory will persist across redeploys.
- Redis is already used for task tracking and inter-agent communication, which is good for state, but files need volumes.

## 2. Inter-Agent Communication (Avoiding Death Spirals)
**Issue:** Agents constantly replying to each other in Discord, creating an infinite loop.
**Solution:**
- The `TASK_BOARD_AND_ROUTING_SUMMARY.md` shows a system was recently built using `[REQUEST]`, `[END]`, and `[NOTIFY]` tags to prevent loops.
- However, if loops are still happening, we need to enforce strict Webhook-only communication for status updates, or use the `inter_agent_comm` Redis queues strictly instead of Discord mentions.
- **Action:** Update system prompts to strictly forbid `@` mentions except for Harmony. Use `webhook_comm.py` or `send_task.py` (Redis) for all cross-agent chatter.

## 3. Email Access (Samantha, Jadel, Sasha, Scarlet)
**Issue:** Agents need access to Gmail and Hostinger emails.
**Solution:**
- **Gmail:** Use the existing `agent-email-system` skill which uses Google Service Accounts and Domain-Wide Delegation. We need to set up the GCP project, generate the JSON key, and provide it to the agents via the Vault.
- **Hostinger (IMAP/SMTP):** Use the `himalaya` CLI tool (already in `skills/email/himalaya`). We need to configure `config.toml` with the Hostinger IMAP/SMTP credentials for `garrett@mcgarryhomes.com`.

## 4. Cal.com Integration
**Issue:** Agents need to schedule meetings.
**Solution:**
- Cal.com is deployed on Railway (`f09eea1c-7db8-4519-9323-12930c2b4fcf`).
- We need to create an admin user, generate API keys, and configure the agents with the `NEXT_PUBLIC_API_V2_URL` (`https://calcom-web-app-production-5fdf.up.railway.app/api/v2`).
- Build a simple `calcom_tool.py` skill if one doesn't exist, or use n8n to handle scheduling workflows.

## 5. Google Drive Integration
**Issue:** Agents need to share files via vowsok.com Drive.
**Solution:**
- Use the `gws` CLI tool (already mentioned in skills).
- We need to authenticate `gws` with the vowsok.com Google account and share the credentials/tokens via the Vault or mounted volume.

## 6. Postiz (Social Media for Sabrina)
**Issue:** Postiz API is hard to find/use.
**Solution:**
- Postiz is deployed on Railway (`6b3867d5-8490-4c9a-a327-9156c20442f0`).
- URL: `https://postiz-production-14aa.up.railway.app`
- The API is enabled (`ENABLE_API=true`). We need to create a user, generate an API key or use JWT auth, and build a `postiz_client.py` skill for Sabrina to use.

## 7. Image Generation (Cora)
**Issue:** Cora needs to generate images.
**Solution:**
- Use the `image_generation_tool.py` which is already in the codebase (currently uses FAL.ai).
- If OpenRouter is preferred for image generation (e.g., using specific multimodal models), we need to update the tool to use OpenRouter's image API or ensure Cora's prompt routes image tasks to the right provider.
