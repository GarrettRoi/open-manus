#!/usr/bin/env python3
"""
Cal.com API Client — Scheduling and booking management for Open Manus agents.

Connects to the self-hosted Cal.com instance on Railway.
URL: https://calcom-web-app-production-5fdf.up.railway.app

Environment variables:
    CALCOM_API_URL    — Cal.com instance URL (set automatically)
    CALCOM_API_KEY    — Cal.com API key (generate in Cal.com settings)

Usage:
    python3 calcom_client.py list-event-types
    python3 calcom_client.py list-bookings --status upcoming
    python3 calcom_client.py get-booking --id 123
    python3 calcom_client.py create-booking --event-type-id 1 --name "John Doe" \
        --email "john@example.com" --start "2026-04-15T14:00:00Z"
    python3 calcom_client.py cancel-booking --id 123 --reason "Client request"
    python3 calcom_client.py get-availability --event-type-id 1 --date-from "2026-04-15" --date-to "2026-04-20"
"""
import argparse
import json
import os
import sys
import requests
from datetime import datetime, timezone
from typing import Dict, List, Optional

CALCOM_API_URL = os.getenv(
    "CALCOM_API_URL",
    "https://calcom-web-app-production-5fdf.up.railway.app/api/v2"
)
CALCOM_API_KEY = os.getenv("CALCOM_API_KEY", "")


def get_headers() -> Dict:
    """Return auth headers for Cal.com API."""
    if not CALCOM_API_KEY:
        raise ValueError("CALCOM_API_KEY environment variable is not set. "
                         "Generate one in Cal.com Settings > API Keys.")
    return {
        "Authorization": f"Bearer {CALCOM_API_KEY}",
        "Content-Type": "application/json",
        "cal-api-version": "2024-08-13",
    }


def api_get(path: str, params: Dict = None) -> Dict:
    """Make a GET request to Cal.com API."""
    url = f"{CALCOM_API_URL}{path}"
    resp = requests.get(url, headers=get_headers(), params=params, timeout=30)
    resp.raise_for_status()
    return resp.json()


def api_post(path: str, data: Dict) -> Dict:
    """Make a POST request to Cal.com API."""
    url = f"{CALCOM_API_URL}{path}"
    resp = requests.post(url, headers=get_headers(), json=data, timeout=30)
    resp.raise_for_status()
    return resp.json()


def api_patch(path: str, data: Dict) -> Dict:
    """Make a PATCH request to Cal.com API."""
    url = f"{CALCOM_API_URL}{path}"
    resp = requests.patch(url, headers=get_headers(), json=data, timeout=30)
    resp.raise_for_status()
    return resp.json()


def list_event_types() -> List[Dict]:
    """List all event types (booking links)."""
    result = api_get("/event-types")
    return result.get("data", [])


def list_bookings(status: str = "upcoming", limit: int = 20) -> List[Dict]:
    """List bookings. Status: upcoming, past, cancelled, all."""
    params = {"limit": limit}
    if status != "all":
        params["status"] = status
    result = api_get("/bookings", params=params)
    return result.get("data", {}).get("bookings", [])


def get_booking(booking_id: int) -> Dict:
    """Get a specific booking by ID."""
    result = api_get(f"/bookings/{booking_id}")
    return result.get("data", {})


def create_booking(
    event_type_id: int,
    name: str,
    email: str,
    start: str,
    notes: Optional[str] = None,
    timezone_str: str = "America/Chicago"
) -> Dict:
    """Create a new booking."""
    data = {
        "eventTypeId": event_type_id,
        "attendee": {
            "name": name,
            "email": email,
            "timeZone": timezone_str,
        },
        "start": start,
    }
    if notes:
        data["metadata"] = {"notes": notes}
    
    result = api_post("/bookings", data)
    return result.get("data", {})


def cancel_booking(booking_id: int, reason: str = "Cancelled by agent") -> Dict:
    """Cancel a booking."""
    result = api_patch(f"/bookings/{booking_id}/cancel", {"cancellationReason": reason})
    return result.get("data", {})


def get_availability(event_type_id: int, date_from: str, date_to: str) -> Dict:
    """Get available time slots for an event type."""
    params = {
        "eventTypeId": event_type_id,
        "startTime": f"{date_from}T00:00:00Z",
        "endTime": f"{date_to}T23:59:59Z",
    }
    result = api_get("/slots/available", params=params)
    return result.get("data", {})


def get_schedule() -> Dict:
    """Get the user's availability schedule."""
    result = api_get("/schedules")
    return result.get("data", [])


def format_booking(booking: Dict) -> str:
    """Format a booking for display."""
    start = booking.get("startTime", "")
    end = booking.get("endTime", "")
    attendees = booking.get("attendees", [])
    attendee_names = ", ".join([a.get("name", "") for a in attendees])
    
    return (
        f"ID: {booking.get('id')}\n"
        f"Title: {booking.get('title', 'Untitled')}\n"
        f"Start: {start}\n"
        f"End: {end}\n"
        f"Attendees: {attendee_names}\n"
        f"Status: {booking.get('status', 'unknown')}\n"
        f"Notes: {booking.get('description', '')}"
    )


def main():
    parser = argparse.ArgumentParser(description="Cal.com API Client")
    subparsers = parser.add_subparsers(dest="command")

    subparsers.add_parser("list-event-types", help="List all event types")
    
    p_bookings = subparsers.add_parser("list-bookings", help="List bookings")
    p_bookings.add_argument("--status", default="upcoming", 
                            choices=["upcoming", "past", "cancelled", "all"])
    p_bookings.add_argument("--limit", type=int, default=20)
    
    p_get = subparsers.add_parser("get-booking", help="Get a specific booking")
    p_get.add_argument("--id", type=int, required=True)
    
    p_create = subparsers.add_parser("create-booking", help="Create a new booking")
    p_create.add_argument("--event-type-id", type=int, required=True)
    p_create.add_argument("--name", required=True)
    p_create.add_argument("--email", required=True)
    p_create.add_argument("--start", required=True, 
                          help="ISO 8601 datetime (e.g., 2026-04-15T14:00:00Z)")
    p_create.add_argument("--notes")
    p_create.add_argument("--timezone", default="America/Chicago")
    
    p_cancel = subparsers.add_parser("cancel-booking", help="Cancel a booking")
    p_cancel.add_argument("--id", type=int, required=True)
    p_cancel.add_argument("--reason", default="Cancelled by agent")
    
    p_avail = subparsers.add_parser("get-availability", help="Get available slots")
    p_avail.add_argument("--event-type-id", type=int, required=True)
    p_avail.add_argument("--date-from", required=True, help="YYYY-MM-DD")
    p_avail.add_argument("--date-to", required=True, help="YYYY-MM-DD")
    
    subparsers.add_parser("get-schedule", help="Get availability schedule")

    args = parser.parse_args()

    try:
        if args.command == "list-event-types":
            result = list_event_types()
            print(json.dumps(result, indent=2))
        elif args.command == "list-bookings":
            result = list_bookings(args.status, args.limit)
            for b in result:
                print(format_booking(b))
                print("---")
        elif args.command == "get-booking":
            result = get_booking(args.id)
            print(format_booking(result))
        elif args.command == "create-booking":
            result = create_booking(
                args.event_type_id, args.name, args.email, args.start,
                args.notes, args.timezone
            )
            print("Booking created:")
            print(format_booking(result))
        elif args.command == "cancel-booking":
            result = cancel_booking(args.id, args.reason)
            print(f"Booking {args.id} cancelled.")
        elif args.command == "get-availability":
            result = get_availability(args.event_type_id, args.date_from, args.date_to)
            print(json.dumps(result, indent=2))
        elif args.command == "get-schedule":
            result = get_schedule()
            print(json.dumps(result, indent=2))
        else:
            parser.print_help()
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
