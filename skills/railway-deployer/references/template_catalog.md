# Railway Template Catalog

This document lists all available pre-configured templates.

## Available Templates

### 1. FastAPI + PostgreSQL (`fastapi.json`)

**Description**: FastAPI backend with PostgreSQL database

**Services**:
- `api`: FastAPI application from railwayapp-templates/fastapi
- `postgres`: PostgreSQL 16 database

**Features**:
- Auto-generated SECRET_KEY
- DATABASE_URL automatically configured
- Health check endpoint at /health
- Public domain enabled
- Persistent PostgreSQL storage

**Use Cases**:
- REST API backends
- Python web services
- Full-stack applications

---

### 2. PostgreSQL Database (`postgres.json`)

**Description**: Standalone PostgreSQL database

**Services**:
- `postgres`: PostgreSQL 16 database

**Features**:
- Auto-generated password
- Persistent storage
- DATABASE_URL provided

**Use Cases**:
- Standalone database
- Adding database to existing project
- Development/testing databases

---

### 3. Redis Cache (`redis.json`)

**Description**: Redis cache with persistent storage

**Services**:
- `redis`: Redis 7 (Alpine)

**Features**:
- Auto-generated password
- Persistent storage
- DATABASE_URL (Redis connection string) provided

**Use Cases**:
- Caching layer
- Session storage
- Message queues
- Real-time applications

---

### 4. MongoDB Database (`mongodb.json`)

**Description**: MongoDB database with persistent storage

**Services**:
- `mongodb`: MongoDB 7

**Features**:
- Auto-generated credentials
- Persistent storage
- DATABASE_URL (MongoDB connection string) provided

**Use Cases**:
- NoSQL database
- Document storage
- Flexible schema applications

---

## Template Variables

All templates support Railway template variables:

### Secret Generation
- `${{secret()}}` - Generate 32-character random secret
- `${{secret(64)}}` - Generate 64-character random secret
- `${{secret(16, "0123456789abcdef")}}` - Generate 16-character hex secret

### Random Integers
- `${{randomInt()}}` - Generate random integer (0-100)
- `${{randomInt(1000, 9999)}}` - Generate random integer between 1000-9999

### Service References
- `${{SERVICE.postgres.DATABASE_URL}}` - Reference DATABASE_URL from postgres service
- `${{SERVICE.redis.REDIS_PASSWORD}}` - Reference REDIS_PASSWORD from redis service

## Auto-Generated Variables

Database services automatically generate these variables:

### PostgreSQL
- `POSTGRES_PASSWORD` - Auto-generated password
- `POSTGRES_USER` - Default: postgres
- `POSTGRES_DB` - Database name (service name)
- `DATABASE_URL` - Full connection string

### MySQL
- `MYSQL_ROOT_PASSWORD` - Auto-generated password
- `MYSQL_DATABASE` - Database name (service name)
- `DATABASE_URL` - Full connection string

### MongoDB
- `MONGO_INITDB_ROOT_USERNAME` - Default: admin
- `MONGO_INITDB_ROOT_PASSWORD` - Auto-generated password
- `MONGO_INITDB_DATABASE` - Database name (service name)
- `DATABASE_URL` - Full connection string

### Redis
- `REDIS_PASSWORD` - Auto-generated password
- `DATABASE_URL` - Redis connection string

## Custom Templates

You can create custom templates by following the template format:

```json
{
  "name": "Template Name",
  "description": "Template description",
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
        "SECRET": "${{secret(32)}}"
      },
      "settings": {
        "startCommand": "command",
        "healthcheckPath": "/health",
        "rootDirectory": "/"
      },
      "networking": {
        "public": true
      },
      "volumes": [
        {
          "mountPath": "/data"
        }
      ]
    }
  ]
}
```

Save custom templates to the `templates/` directory and deploy them using the template deployer script.
