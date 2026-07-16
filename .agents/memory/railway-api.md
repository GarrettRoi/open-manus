---
name: Railway API access
description: How to talk to Railway for the Open Manus production project.
---
- Railway CLI auth fails even with a valid token. Use raw GraphQL: `curl https://backboard.railway.app/graphql/v2 -H "Authorization: Bearer $RAILWAY_TOKEN"` (python urllib gets 403).
- Project "Open Manus Agents": project id `ea6649cb-ac92-44fd-bea9-3fbf6ad5e473`, env `production` `e57f146e-e0b8-4d5c-a443-c30e0baf016f`, workspace `c782b6e2-7bfd-45cb-96e8-c4f7ec256c45`.
- Useful ops: `variables(projectId, environmentId, serviceId, unrendered:true)`, mutations `variableUpsert`, `serviceInstanceUpdate`, `serviceInstanceRedeploy`.
- **Start commands do not shell-expand `$VAR` and do not template `${{VAR}}`.** Wrap in `sh -c 'exec cmd "$VAR"'`.
- Agent volumes mount at `/root/.hermes/workspace` (containers run as root). Redis public endpoint for external access: `maglev.proxy.rlwy.net:12539` (`.internal` doesn't resolve from Replit).
- Never byte-inspect secrets; only length/boolean checks.
