# Postiz Social Media Skill

## Overview
Postiz is the self-hosted social media scheduling platform for Garrett's brands. Sabrina uses this to schedule and publish posts across all connected social media accounts.

## Instance
- **URL**: `https://postiz-production-14aa.up.railway.app`
- **API Key**: Set via `POSTIZ_API_KEY` environment variable
- **Auth Method**: Cookie-based JWT (login once, reuse token) OR API key for supported endpoints

## Authentication
Postiz uses a cookie-based JWT. The agent should:
1. Login via `POST /api/auth/login` with `{"email": "...", "password": "...", "provider": "LOCAL"}`
2. Store the `auth` cookie from the response
3. Use `Cookie: auth=<token>` on all subsequent requests

The `publicApi` key from `GET /api/user/self` can be used for some endpoints.

## Key Endpoints

### Get Connected Social Accounts
```
GET /api/integrations
Cookie: auth=<token>
```

### Schedule a Post
```
POST /api/posts
Cookie: auth=<token>
Content-Type: application/json

{
  "content": "Post text here",
  "date": "2026-04-10T14:00:00.000Z",
  "integrations": [{"id": "<integration_id>"}],
  "image": []  // optional: array of media IDs
}
```

### Get Scheduled Posts
```
GET /api/posts?startDate=2026-04-01T00:00:00Z&endDate=2026-04-30T23:59:59Z
Cookie: auth=<token>
```

### Upload Media
```
POST /api/media
Cookie: auth=<token>
Content-Type: multipart/form-data
file: <image_file>
```

## Usage by Sabrina
- Use `postiz_client.py` for all Postiz operations
- Always check connected integrations before scheduling
- Schedule posts at optimal times (see brand guidelines in USER.md)
- Vows & Vinyl: Instagram, Facebook — wedding content, DJ tips, behind-the-scenes
- Cana Collective: Instagram, Facebook — vendor spotlights, wedding inspiration
- McGarry Homes: Facebook — real estate tips, first-time buyer content

## Environment Variables Required
```
POSTIZ_URL=https://postiz-production-14aa.up.railway.app
POSTIZ_EMAIL=sanctusmm@gmail.com
POSTIZ_PASSWORD=<set in vault>
POSTIZ_API_KEY=f7f1a1569d2e7714afb7c1c9694b8ed7342eb76ff26cc3095f7c723844168ea3
```
