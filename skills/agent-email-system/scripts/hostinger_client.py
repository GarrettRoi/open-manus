#!/usr/bin/env python3
"""
Hostinger Email Client — IMAP/SMTP access for Hostinger-hosted email accounts.

Supports: garrett@mcgarryhomes.com (and any other Hostinger-hosted addresses)
Uses standard IMAP/SMTP with app password authentication.

Environment variables required:
    HOSTINGER_EMAIL        — The email address (e.g., garrett@mcgarryhomes.com)
    HOSTINGER_PASSWORD     — The email password or app password
    HOSTINGER_IMAP_HOST    — IMAP server (default: imap.hostinger.com)
    HOSTINGER_SMTP_HOST    — SMTP server (default: smtp.hostinger.com)

Usage:
    python3 hostinger_client.py list --folder INBOX --count 10
    python3 hostinger_client.py read --uid 123
    python3 hostinger_client.py send --to "recipient@example.com" --subject "Hello" --body "Message body"
    python3 hostinger_client.py search --query "from:client@example.com"
"""
import argparse
import email
import imaplib
import json
import os
import smtplib
import sys
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.header import decode_header
from typing import List, Dict, Optional

# Configuration
IMAP_HOST = os.getenv("HOSTINGER_IMAP_HOST", "imap.hostinger.com")
IMAP_PORT = int(os.getenv("HOSTINGER_IMAP_PORT", "993"))
SMTP_HOST = os.getenv("HOSTINGER_SMTP_HOST", "smtp.hostinger.com")
SMTP_PORT = int(os.getenv("HOSTINGER_SMTP_PORT", "465"))
EMAIL_ADDR = os.getenv("HOSTINGER_EMAIL", "")
EMAIL_PASS = os.getenv("HOSTINGER_PASSWORD", "")


def decode_str(s):
    """Decode email header string."""
    if s is None:
        return ""
    decoded_parts = decode_header(s)
    result = []
    for part, charset in decoded_parts:
        if isinstance(part, bytes):
            result.append(part.decode(charset or "utf-8", errors="replace"))
        else:
            result.append(str(part))
    return " ".join(result)


def get_imap():
    """Connect and authenticate to IMAP server."""
    if not EMAIL_ADDR or not EMAIL_PASS:
        raise ValueError("HOSTINGER_EMAIL and HOSTINGER_PASSWORD must be set")
    imap = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
    imap.login(EMAIL_ADDR, EMAIL_PASS)
    return imap


def get_smtp():
    """Connect and authenticate to SMTP server."""
    if not EMAIL_ADDR or not EMAIL_PASS:
        raise ValueError("HOSTINGER_EMAIL and HOSTINGER_PASSWORD must be set")
    smtp = smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT)
    smtp.login(EMAIL_ADDR, EMAIL_PASS)
    return smtp


def list_emails(folder: str = "INBOX", count: int = 10, unread_only: bool = False) -> List[Dict]:
    """List recent emails from a folder."""
    imap = get_imap()
    imap.select(folder)
    
    search_criteria = "UNSEEN" if unread_only else "ALL"
    _, message_ids = imap.search(None, search_criteria)
    
    ids = message_ids[0].split()
    recent_ids = ids[-count:] if len(ids) > count else ids
    recent_ids = list(reversed(recent_ids))  # Most recent first
    
    emails = []
    for uid in recent_ids:
        _, msg_data = imap.fetch(uid, "(RFC822.SIZE BODY[HEADER.FIELDS (FROM TO SUBJECT DATE)])")
        if msg_data and msg_data[0]:
            raw_headers = msg_data[0][1]
            msg = email.message_from_bytes(raw_headers)
            emails.append({
                "uid": uid.decode(),
                "from": decode_str(msg.get("From", "")),
                "to": decode_str(msg.get("To", "")),
                "subject": decode_str(msg.get("Subject", "(no subject)")),
                "date": msg.get("Date", ""),
            })
    
    imap.logout()
    return emails


def read_email(uid: str, folder: str = "INBOX") -> Dict:
    """Read a specific email by UID."""
    imap = get_imap()
    imap.select(folder)
    
    _, msg_data = imap.fetch(uid.encode(), "(RFC822)")
    if not msg_data or not msg_data[0]:
        imap.logout()
        return {"error": f"Email {uid} not found"}
    
    raw = msg_data[0][1]
    msg = email.message_from_bytes(raw)
    
    # Extract body
    body = ""
    if msg.is_multipart():
        for part in msg.walk():
            ct = part.get_content_type()
            if ct == "text/plain":
                body = part.get_payload(decode=True).decode("utf-8", errors="replace")
                break
            elif ct == "text/html" and not body:
                body = part.get_payload(decode=True).decode("utf-8", errors="replace")
    else:
        body = msg.get_payload(decode=True).decode("utf-8", errors="replace")
    
    # Mark as read
    imap.store(uid.encode(), "+FLAGS", "\\Seen")
    imap.logout()
    
    return {
        "uid": uid,
        "from": decode_str(msg.get("From", "")),
        "to": decode_str(msg.get("To", "")),
        "subject": decode_str(msg.get("Subject", "(no subject)")),
        "date": msg.get("Date", ""),
        "body": body[:5000],  # Truncate very long emails
    }


def send_email(to: str, subject: str, body: str, reply_to: Optional[str] = None, html: bool = False) -> bool:
    """Send an email."""
    msg = MIMEMultipart("alternative")
    msg["From"] = EMAIL_ADDR
    msg["To"] = to
    msg["Subject"] = subject
    if reply_to:
        msg["Reply-To"] = reply_to
    
    content_type = "html" if html else "plain"
    msg.attach(MIMEText(body, content_type))
    
    smtp = get_smtp()
    smtp.sendmail(EMAIL_ADDR, to, msg.as_string())
    smtp.quit()
    
    print(f"Email sent to {to}: {subject}")
    return True


def search_emails(query: str, folder: str = "INBOX", count: int = 20) -> List[Dict]:
    """Search emails using IMAP search criteria."""
    imap = get_imap()
    imap.select(folder)
    
    # Convert simple query to IMAP search
    if query.startswith("from:"):
        criteria = f'FROM "{query[5:]}"'
    elif query.startswith("subject:"):
        criteria = f'SUBJECT "{query[8:]}"'
    elif query.startswith("to:"):
        criteria = f'TO "{query[3:]}"'
    else:
        criteria = f'TEXT "{query}"'
    
    _, message_ids = imap.search(None, criteria)
    ids = message_ids[0].split()
    recent_ids = list(reversed(ids[-count:] if len(ids) > count else ids))
    
    emails = []
    for uid in recent_ids:
        _, msg_data = imap.fetch(uid, "(BODY[HEADER.FIELDS (FROM TO SUBJECT DATE)])")
        if msg_data and msg_data[0]:
            msg = email.message_from_bytes(msg_data[0][1])
            emails.append({
                "uid": uid.decode(),
                "from": decode_str(msg.get("From", "")),
                "subject": decode_str(msg.get("Subject", "(no subject)")),
                "date": msg.get("Date", ""),
            })
    
    imap.logout()
    return emails


def main():
    parser = argparse.ArgumentParser(description="Hostinger Email Client")
    subparsers = parser.add_subparsers(dest="command")

    # list
    p_list = subparsers.add_parser("list", help="List recent emails")
    p_list.add_argument("--folder", default="INBOX")
    p_list.add_argument("--count", type=int, default=10)
    p_list.add_argument("--unread", action="store_true")

    # read
    p_read = subparsers.add_parser("read", help="Read a specific email")
    p_read.add_argument("--uid", required=True)
    p_read.add_argument("--folder", default="INBOX")

    # send
    p_send = subparsers.add_parser("send", help="Send an email")
    p_send.add_argument("--to", required=True)
    p_send.add_argument("--subject", required=True)
    p_send.add_argument("--body", required=True)
    p_send.add_argument("--html", action="store_true")

    # search
    p_search = subparsers.add_parser("search", help="Search emails")
    p_search.add_argument("--query", required=True)
    p_search.add_argument("--folder", default="INBOX")
    p_search.add_argument("--count", type=int, default=20)

    args = parser.parse_args()

    if args.command == "list":
        emails = list_emails(args.folder, args.count, args.unread)
        print(json.dumps(emails, indent=2))
    elif args.command == "read":
        result = read_email(args.uid, args.folder)
        print(json.dumps(result, indent=2))
    elif args.command == "send":
        send_email(args.to, args.subject, args.body, html=args.html)
    elif args.command == "search":
        results = search_emails(args.query, args.folder, args.count)
        print(json.dumps(results, indent=2))
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
