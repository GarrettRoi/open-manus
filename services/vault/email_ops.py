"""Small, blocking-in-a-thread IMAP/SMTP adapter used by the vault.

Apple Notes are exposed only as a read-only IMAP folder when the provider
offers one. Modern iCloud Notes are not guaranteed to appear in IMAP.
"""

from __future__ import annotations

import asyncio
import email
import imaplib
import smtplib
from email.header import decode_header
from email.message import EmailMessage
from email.parser import BytesParser
from email.policy import default
from typing import Any, Dict


class EmailOpsError(Exception):
    pass


def _decode(value: str) -> str:
    out = []
    for part, charset in decode_header(value or ""):
        if isinstance(part, bytes):
            out.append(part.decode(charset or "utf-8", "replace"))
        else:
            out.append(part)
    return "".join(out)


def _run_sync(user: str, password: str, operation: str, args: Dict[str, Any]) -> Dict[str, Any]:
    if not user or not password:
        raise EmailOpsError("Email address and app-specific password are required")
    op = operation.lower()
    try:
        if op == "send":
            message = EmailMessage()
            message["From"] = user
            message["To"] = ", ".join(args.get("to") if isinstance(args.get("to"), list) else [str(args.get("to") or "")])
            message["Subject"] = str(args.get("subject") or "")
            message.set_content(str(args.get("body") or ""))
            with smtplib.SMTP_SSL("smtp.mail.me.com", 465, timeout=30) as smtp:
                smtp.login(user, password)
                smtp.send_message(message)
            return {"sent": True, "to": message["To"], "subject": message["Subject"]}
        with imaplib.IMAP4_SSL("imap.mail.me.com", 993) as mail:
            mail.login(user, password)
            if op == "folders":
                status, rows = mail.list()
                return {"folders": [r.decode("utf-8", "replace") for r in (rows or []) if status == "OK"]}
            folder = str(args.get("folder") or "INBOX")
            if any(char in folder for char in '\r\n"') or len(folder) > 200:
                raise EmailOpsError("Invalid mailbox folder name")
            status, _ = mail.select(f'"{folder}"', readonly=True)
            if status != "OK":
                raise EmailOpsError(f"Cannot open mailbox folder {folder}")
            criterion = "ALL"
            query = str(args.get("query") or "").strip()
            if query:
                criterion = f'TEXT "{query.replace(chr(34), "")[:200]}"'
            status, data = mail.uid("SEARCH", None, criterion)
            uids = (data[0] or b"").split() if status == "OK" else []
            limit = max(1, min(int(args.get("limit") or 20), 100))
            if op == "read":
                requested = str(args.get("uid") or "")
                uids = [requested.encode("ascii")] if requested else []
            else:
                uids = uids[-limit:]
            messages = []
            for uid in reversed(uids):
                status, fetched = mail.uid("FETCH", uid, "(RFC822)")
                raw = next((part[1] for part in (fetched or []) if isinstance(part, tuple)), b"")
                msg = BytesParser(policy=default).parsebytes(raw)
                messages.append({
                    "uid": uid.decode("ascii", "replace"),
                    "subject": _decode(msg.get("Subject", "")),
                    "from": _decode(msg.get("From", "")),
                    "to": _decode(msg.get("To", "")),
                    "date": msg.get("Date", ""),
                    "body": msg.get_body(preferencelist=("plain", "html")).get_content()[:20000] if msg.get_body() else "",
                })
            return {"messages": messages, "notes_partial": folder.lower() == "notes"}
            try:
                mail.logout()
            except Exception:
                pass
    except EmailOpsError:
        raise
    except Exception as exc:
        # Do not expose IMAP/SMTP library internals or anything that could
        # include credential material in a dashboard/API error.
        raise EmailOpsError("Email provider rejected the credentials or is unavailable") from exc


async def run_email_operation(user: str, password: str, operation: str, args: Dict[str, Any]) -> Dict[str, Any]:
    return await asyncio.to_thread(_run_sync, user.strip(), password.strip(), operation, args)