#!/usr/bin/env python3
"""
Email Router — Unified email access for all Open Manus agents.

Routes email operations to the correct backend based on the account:
  - Gmail accounts (sanctusmm@gmail.com, garrett@canaok.com, garrett@vowsok.com)
    → Uses Google Service Account with domain-wide delegation
  - Hostinger accounts (garrett@mcgarryhomes.com)
    → Uses IMAP/SMTP via hostinger_client.py

Environment variables:
    GOOGLE_SERVICE_ACCOUNT_JSON  — Service account JSON for Gmail
    GMAIL_ACCOUNTS               — Comma-separated list of Gmail addresses to manage
    HOSTINGER_EMAIL              — Hostinger email address
    HOSTINGER_PASSWORD           — Hostinger email password

Usage:
    python3 email_router.py list --account garrett@vowsok.com --count 10
    python3 email_router.py read --account garrett@canaok.com --uid 123
    python3 email_router.py send --account sanctusmm@gmail.com --to "client@example.com" \
        --subject "Hello" --body "Message"
    python3 email_router.py list-all --count 5  # Check all inboxes
"""
import argparse
import json
import os
import sys
from typing import Dict, List, Optional

# Gmail accounts managed via Service Account
GMAIL_ACCOUNTS = [
    "sanctusmm@gmail.com",
    "garrett@canaok.com",
    "garrett@vowsok.com",
]

# Hostinger accounts managed via IMAP/SMTP
HOSTINGER_ACCOUNTS = [
    "garrett@mcgarryhomes.com",
]

ALL_ACCOUNTS = GMAIL_ACCOUNTS + HOSTINGER_ACCOUNTS


def is_gmail(account: str) -> bool:
    return account.lower() in [a.lower() for a in GMAIL_ACCOUNTS]


def is_hostinger(account: str) -> bool:
    return account.lower() in [a.lower() for a in HOSTINGER_ACCOUNTS]


def list_emails_gmail(account: str, count: int = 10, unread_only: bool = False) -> List[Dict]:
    """List emails from a Gmail account using the gmail_client."""
    try:
        from gmail_client import GmailClient
        client = GmailClient(delegated_user=account)
        return client.list_messages(max_results=count, unread_only=unread_only)
    except Exception as e:
        return [{"error": f"Gmail error for {account}: {e}"}]


def list_emails_hostinger(count: int = 10, unread_only: bool = False) -> List[Dict]:
    """List emails from Hostinger account."""
    try:
        from hostinger_client import list_emails
        return list_emails(count=count, unread_only=unread_only)
    except Exception as e:
        return [{"error": f"Hostinger error: {e}"}]


def read_email_gmail(account: str, uid: str) -> Dict:
    """Read a specific email from Gmail."""
    try:
        from gmail_client import GmailClient
        client = GmailClient(delegated_user=account)
        return client.get_message(uid)
    except Exception as e:
        return {"error": f"Gmail error for {account}: {e}"}


def read_email_hostinger(uid: str) -> Dict:
    """Read a specific email from Hostinger."""
    try:
        from hostinger_client import read_email
        return read_email(uid)
    except Exception as e:
        return {"error": f"Hostinger error: {e}"}


def send_email_gmail(account: str, to: str, subject: str, body: str, html: bool = False) -> bool:
    """Send an email from a Gmail account."""
    try:
        from gmail_client import GmailClient
        client = GmailClient(delegated_user=account)
        client.send_message(to=to, subject=subject, body=body, html=html)
        return True
    except Exception as e:
        print(f"Gmail send error for {account}: {e}")
        return False


def send_email_hostinger(to: str, subject: str, body: str, html: bool = False) -> bool:
    """Send an email from Hostinger."""
    try:
        from hostinger_client import send_email
        return send_email(to=to, subject=subject, body=body, html=html)
    except Exception as e:
        print(f"Hostinger send error: {e}")
        return False


def route_list(account: str, count: int, unread_only: bool) -> List[Dict]:
    if is_gmail(account):
        return list_emails_gmail(account, count, unread_only)
    elif is_hostinger(account):
        return list_emails_hostinger(count, unread_only)
    else:
        return [{"error": f"Unknown account: {account}"}]


def route_read(account: str, uid: str) -> Dict:
    if is_gmail(account):
        return read_email_gmail(account, uid)
    elif is_hostinger(account):
        return read_email_hostinger(uid)
    else:
        return {"error": f"Unknown account: {account}"}


def route_send(account: str, to: str, subject: str, body: str, html: bool = False) -> bool:
    if is_gmail(account):
        return send_email_gmail(account, to, subject, body, html)
    elif is_hostinger(account):
        return send_email_hostinger(to, subject, body, html)
    else:
        print(f"Unknown account: {account}")
        return False


def list_all_inboxes(count: int = 5) -> Dict[str, List[Dict]]:
    """Check all inboxes and return a summary."""
    results = {}
    for account in ALL_ACCOUNTS:
        print(f"Checking {account}...")
        emails = route_list(account, count, unread_only=True)
        results[account] = emails
    return results


def main():
    parser = argparse.ArgumentParser(description="Unified Email Router")
    subparsers = parser.add_subparsers(dest="command")

    # list
    p_list = subparsers.add_parser("list", help="List emails from an account")
    p_list.add_argument("--account", required=True, choices=ALL_ACCOUNTS)
    p_list.add_argument("--count", type=int, default=10)
    p_list.add_argument("--unread", action="store_true")

    # read
    p_read = subparsers.add_parser("read", help="Read a specific email")
    p_read.add_argument("--account", required=True, choices=ALL_ACCOUNTS)
    p_read.add_argument("--uid", required=True)

    # send
    p_send = subparsers.add_parser("send", help="Send an email")
    p_send.add_argument("--account", required=True, choices=ALL_ACCOUNTS)
    p_send.add_argument("--to", required=True)
    p_send.add_argument("--subject", required=True)
    p_send.add_argument("--body", required=True)
    p_send.add_argument("--html", action="store_true")

    # list-all
    p_all = subparsers.add_parser("list-all", help="Check all inboxes for unread messages")
    p_all.add_argument("--count", type=int, default=5)

    args = parser.parse_args()

    if args.command == "list":
        results = route_list(args.account, args.count, args.unread)
        print(json.dumps(results, indent=2))
    elif args.command == "read":
        result = route_read(args.account, args.uid)
        print(json.dumps(result, indent=2))
    elif args.command == "send":
        success = route_send(args.account, args.to, args.subject, args.body, args.html)
        print("Sent" if success else "Failed")
    elif args.command == "list-all":
        results = list_all_inboxes(args.count)
        print(json.dumps(results, indent=2))
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
