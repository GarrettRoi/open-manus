---
name: calcom
description: Cal.com scheduling and booking management. Use to check Garrett's calendar, create bookings, view availability, and manage appointments for all three businesses (DJ, Real Estate, Cana).
version: 1.0.0
author: Samantha
category: scheduling
metadata:
  hermes:
    tags: [Calendar, Scheduling, Booking, Cal.com, Appointments]
    requires_config:
      - CALCOM_API_KEY
      - CALCOM_API_URL
---

# Cal.com Scheduling Skill

Manages Garrett's calendar and bookings via the self-hosted Cal.com instance.

**Instance URL:** https://calcom-web-app-production-5fdf.up.railway.app

## Setup Required
1. Log into Cal.com at the URL above
2. Go to Settings → API Keys → Create new key
3. Store the key in the Vault: `vault_client.py set CALCOM_API_KEY <key>`

## Quick Reference

### Check upcoming bookings
```bash
python3 /app/skills/calcom/calcom_client.py list-bookings --status upcoming
```

### Check available event types (booking links)
```bash
python3 /app/skills/calcom/calcom_client.py list-event-types
```

### Create a booking
```bash
python3 /app/skills/calcom/calcom_client.py create-booking \
    --event-type-id 1 \
    --name "John Smith" \
    --email "john@example.com" \
    --start "2026-04-20T14:00:00Z"
```

### Check availability for a date range
```bash
python3 /app/skills/calcom/calcom_client.py get-availability \
    --event-type-id 1 \
    --date-from "2026-04-15" \
    --date-to "2026-04-20"
```

### Cancel a booking
```bash
python3 /app/skills/calcom/calcom_client.py cancel-booking \
    --id 123 \
    --reason "Client request"
```

## Event Types to Create in Cal.com

After logging in, create these event types:

| Event Type | Duration | Business | Description |
|------------|----------|----------|-------------|
| DJ Consultation | 30 min | Vows & Vinyl | Initial DJ booking inquiry |
| Real Estate Consultation | 45 min | McGarry Homes | First-time buyer consultation |
| Homebuyer Webinar | 60 min | McGarry Homes | Group webinar for engaged couples |
| Cana Vendor Inquiry | 20 min | Cana Collective | Vendor onboarding call |
| Photo Booth Inquiry | 20 min | Vows & Vinyl | Photo booth add-on discussion |

## Who Uses This Skill

- **Samantha** — Primary scheduler, manages all bookings
- **Tatiana** — Real estate appointment scheduling
- **Jade** — DJ and photo booth booking coordination
- **Scarlett** — Client follow-up scheduling
