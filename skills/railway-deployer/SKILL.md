---
name: railway-deployer
description: Deploy and manage Railway services using templates via the Railway API. Use for deploying Railway templates (FastAPI, Next.js, databases), creating multi-service projects, managing Railway deployments, setting up infrastructure from pre-configured templates, or deploying custom template definitions.
---

# Railway Deployer

Deploy and manage Railway services using pre-configured templates via the Railway GraphQL API.

## Overview

This skill enables programmatic deployment of Railway templates, replicating the "Deploy to Railway" experience through API calls. Deploy popular stacks (FastAPI + PostgreSQL, databases, etc.) or custom template definitions with automatic configuration of services, variables, networking, and volumes.

## Prerequisites

Set Railway API token as environment variable:
```bash
export RAILWAY_TOKEN="your-token"
# or
export RailwayAPI="your-token"
```

Get token from: https://railway.com/account/tokens

## Quick Start

### Deploy a Pre-configured Template

```python
from scripts.template_deployer import deploy_template

# Deploy FastAPI + PostgreSQL
result = deploy_template('/home/ubuntu/skills/railway-deployer/templates/fastapi.json')

# Result includes project_id, service IDs, URLs, and configuration
print(result)
```

### Deploy Custom Template

Create a template JSON file following the template format (see Template Catalog), then deploy:

```python
result = deploy_template('/path/to/custom_template.json')
```

## Available Templates

Pre-configured templates in `templates/` directory:

1. **fastapi.json** - FastAPI + PostgreSQL backend
2. **postgres.json** - PostgreSQL database
3. **redis.json** - Redis cache
4. **mongodb.json** - MongoDB database

For detailed template information, read `/home/ubuntu/skills/railway-deployer/references/template_catalog.md`

## Template Structure

Templates define services, variables, settings, networking, and volumes:

```json
{
  "name": "Template Name",
  "description": "Description",
  "services": [
    {
      "name": "service-name",
      "source": {
        "type": "github|docker",
        "repo": "user/repo",
        "branch": "main",
        "image": "image:tag"
      },
      "variables": {
        "KEY": "value",
        "SECRET": "${{secret(32)}}",
        "DB_URL": "${{SERVICE.postgres.DATABASE_URL}}"
      },
      "settings": {
        "startCommand": "command",
        "healthcheckPath": "/path",
        "rootDirectory": "/"
      },
      "networking": {
        "public": true
      },
      "volumes": [
        {"mountPath": "/data"}
      ]
    }
  ]
}
```

## Template Functions

### Secret Generation
- `${{secret()}}` - 32-char random secret
- `${{secret(64)}}` - 64-char random secret
- `${{secret(16, "0123456789abcdef")}}` - 16-char hex secret

### Random Integers
- `${{randomInt()}}` - Random int (0-100)
- `${{randomInt(1000, 9999)}}` - Random int between 1000-9999

### Service References
- `${{SERVICE.postgres.DATABASE_URL}}` - Reference variable from another service

## Auto-Generated Database Variables

Database services (PostgreSQL, MySQL, MongoDB, Redis) automatically generate:
- Passwords/credentials
- DATABASE_URL connection strings
- Database names
- Usernames

Example for PostgreSQL:
- `POSTGRES_PASSWORD` - Auto-generated
- `POSTGRES_USER` - "postgres"
- `POSTGRES_DB` - Service name
- `DATABASE_URL` - Full connection string

## Deployment Workflow

1. **Create Project** - New Railway project created
2. **Deploy Services** - Services created from GitHub repos or Docker images
3. **Generate Secrets** - Template functions executed (passwords, keys)
4. **Set Variables** - Environment variables configured with resolved values
5. **Configure Settings** - Start commands, healthchecks, root directories set
6. **Setup Networking** - Public domains added for services
7. **Create Volumes** - Persistent storage volumes created
8. **Return Info** - Deployment details returned (IDs, URLs, configuration)

## Using the Scripts

### Template Deployer

Main deployment script:

```python
from scripts.template_deployer import TemplateDeployer, load_template
from scripts.railway_client import RailwayClient

# Load template
template = load_template('templates/fastapi.json')

# Create client
client = RailwayClient()

# Deploy
deployer = TemplateDeployer(client, template)
result = deployer.deploy()
```

### Railway Client

Direct API operations:

```python
from scripts.railway_client import RailwayClient

client = RailwayClient()

# Create project
project = client.create_project("My Project", "Description")

# Create service from GitHub
service = client.create_service_from_github(
    project_id=project['id'],
    name="api",
    repo="user/repo",
    branch="main"
)

# Set variables
client.set_variables(
    project_id=project['id'],
    environment_id=env_id,
    service_id=service['id'],
    variables={"KEY": "value"}
)

# Add domain
domain = client.add_railway_domain(
    service_id=service['id'],
    environment_id=env_id
)
```

### Template Functions

Generate secrets and resolve variables:

```python
from scripts.template_functions import (
    generate_secret,
    generate_random_int,
    parse_template_variable,
    resolve_all_variables
)

# Generate secret
secret = generate_secret(32)

# Generate random int
port = generate_random_int(3000, 9000)

# Parse template variable
value = parse_template_variable(
    "${{secret(32)}}",
    context={'services': {}}
)

# Resolve all variables in dict
resolved = resolve_all_variables(
    {"SECRET": "${{secret()}}", "PORT": "${{randomInt(3000, 9000)}}"},
    context={'services': {}}
)
```

## Common Operations

### Deploy Database

```python
# PostgreSQL
deploy_template('templates/postgres.json')

# Redis
deploy_template('templates/redis.json')

# MongoDB
deploy_template('templates/mongodb.json')
```

### Deploy Full Stack

```python
# FastAPI + PostgreSQL
deploy_template('templates/fastapi.json')
```

### Create Custom Multi-Service Template

```json
{
  "name": "Custom Stack",
  "description": "Frontend + Backend + Database",
  "services": [
    {
      "name": "frontend",
      "source": {"type": "github", "repo": "user/frontend"},
      "variables": {"API_URL": "${{SERVICE.backend.url}}"},
      "networking": {"public": true}
    },
    {
      "name": "backend",
      "source": {"type": "github", "repo": "user/backend"},
      "variables": {
        "DATABASE_URL": "${{SERVICE.postgres.DATABASE_URL}}",
        "SECRET_KEY": "${{secret(32)}}"
      },
      "networking": {"public": true}
    },
    {
      "name": "postgres",
      "source": {"type": "docker", "image": "postgres:16"},
      "volumes": [{"mountPath": "/var/lib/postgresql/data"}]
    }
  ]
}
```

## Troubleshooting

### Token Not Found
Ensure `RAILWAY_TOKEN` or `RailwayAPI` environment variable is set:
```bash
export RAILWAY_TOKEN="your-token"
```

### Service Creation Fails
- Verify GitHub repo is public or Railway has access
- Check Docker image exists and is accessible
- Ensure repo/image URLs are correct

### Variable Resolution Fails
- Check service references use correct service names
- Ensure referenced services are deployed before services that reference them
- Verify template function syntax: `${{function()}}`

### Deployment Takes Long
- Railway deploys services asynchronously
- Initial deployments may take 2-5 minutes
- Check deployment status in Railway dashboard

### Rate Limits
- Railway API has rate limits (see Railway API documentation)
- Add delays between operations if hitting limits
- Consider upgrading Railway plan for higher limits

## Deployment Info Structure

Successful deployment returns:

```json
{
  "project_id": "project-id",
  "environment_id": "environment-id",
  "template_name": "Template Name",
  "services": {
    "service-name": {
      "id": "service-id",
      "name": "service-name",
      "url": "https://service.railway.app",
      "has_database_url": true
    }
  }
}
```

## Best Practices

1. **Use Template Functions** - Generate secrets dynamically instead of hardcoding
2. **Service References** - Use `${{SERVICE.name.VAR}}` for service-to-service connections
3. **Database Volumes** - Always add volumes for databases to persist data
4. **Health Checks** - Configure healthcheck paths for web services
5. **Public Networking** - Only enable for services that need external access
6. **Custom Templates** - Save reusable templates for your common stacks

## Limitations

- No direct Railway template marketplace API (this skill replicates template functionality)
- Template deployment is sequential (services deployed one at a time)
- Service references only work for services deployed earlier in the list
- Private GitHub repos require Railway GitHub integration
- Private Docker images require registry credentials

## Next Steps

After deployment:
1. Check Railway dashboard for deployment status
2. View service logs for any errors
3. Access services via provided URLs
4. Configure custom domains if needed
5. Set up monitoring and alerts
6. Review and adjust resource limits
