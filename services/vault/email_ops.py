"""IMAP/SMTP operations for email-kind vault connections.

The vault performs classic email-client actions server-side using the
stored mailbox credentials — agents call POST /api/vault/email/{conn}
and never see the password. Everything here is synchronous stdlib
(imaplib/smtplib/email) and is run in a worker thread by the endpoint.

Actions:
    folders                      -> {"folders": [...]}
    list   (folder, limit,       -> {"messages": [{uid, from, to, subject,
            unseen_only)              date, seen}, ...]}
    read   (uid, folder,         -> {"message": {uid, from, to, cc, subject,
            mark_seen)                date, body, truncated}}
    send   (to, subject, body,   -> {"sent": true, "to": [...]}
            cc, bcc, reply_to)
"""

from __future__ import annotations

import email
import imaplib
import re
import smtplib
import socket
from email.header import decode_header, make_header
from email.message import EmailMessage
from email.utils import formataddr, parseaddr
from typing import Any, Dict, List

TIMEOUT = 25  # seconds per server operation
LIST_MAX = 50
BODY_MAX = 20_000  # chars returned for a read


class EmailOpError(Exception):
    """User-facing email operation failure (bad login, bad folder, ...)."""


def _check_host(host: str, label: str) -> None:
    """Connect-time SSRF re-check (mitigates DNS rebinding after save-time
    validation in the admin API): the hostname must not be an IP literal and
    must resolve to global addresses only."""
    import ipaddress
    try:
        ipaddress.ip_address(host)
        raise EmailOpError(f"{label} server must be a hostname, not an IP address")
    except ValueError:
        pass
    try:
        addrs = {ai[4][0] for ai in socket.getaddrinfo(host, None,
                                                       proto=socket.IPPROTO_TCP)}
    except socket.gaierror:
        raise EmailOpError(f"{label} server {host} does not resolve")
    for addr in addrs:
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            continue
        if not ip.is_global:
            raise EmailOpError(
                f"{label} server {host} resolves to a private/internal "
                "address — refusing to connect")


def _dec(value: Any) -> str:
    """Decode a possibly RFC2047-encoded header to a plain string."""
    if value is None:
        return ""
    try:
        return str(make_header(decode_header(str(value))))
    except Exception:
        return str(value)


def _imap_connect(secrets: Dict[str, Any]) -> imaplib.IMAP4_SSL:
    host = (secrets.get("imap_host") or "").strip()
    port = int(secrets.get("imap_port") or 993)
    if not host:
        raise EmailOpError("No IMAP server configured for this connection")
    _check_host(host, "IMAP")
    try:
        m = imaplib.IMAP4_SSL(host, port, timeout=TIMEOUT)
    except (OSError, socket.timeout) as e:
        raise EmailOpError(f"Cannot reach IMAP server {host}:{port} ({e})")
    try:
        m.login(secrets.get("username") or "", secrets.get("password") or "")
    except imaplib.IMAP4.error as e:
        try:
            m.logout()
        except Exception:
            pass
        raise EmailOpError(
            f"IMAP login failed: {e}. For Gmail/Yahoo/iCloud you usually need "
            "an app password, not the normal account password."
        )
    return m


def _select_folder(m: imaplib.IMAP4_SSL, folder: str) -> None:
    # Quote the mailbox name; reject anything that could break the quoting.
    name = (folder or "INBOX").strip() or "INBOX"
    if '"' in name or "\r" in name or "\n" in name:
        raise EmailOpError(f"Invalid folder name: {name!r}")
    typ, _ = m.select(f'"{name}"', readonly=True)
    if typ != "OK":
        raise EmailOpError(f"Folder not found: {name}")


_LIST_RE = re.compile(rb'\((?P<flags>[^)]*)\) "(?P<delim>[^"]*)" (?P<name>.+)')


def op_folders(secrets: Dict[str, Any], _body: Dict[str, Any]) -> Dict[str, Any]:
    m = _imap_connect(secrets)
    try:
        typ, rows = m.list()
        if typ != "OK":
            raise EmailOpError("Could not list folders")
        folders: List[str] = []
        for row in rows or []:
            if not isinstance(row, bytes):
                continue
            match = _LIST_RE.match(row)
            if not match:
                continue
            name = match.group("name").decode(errors="replace").strip()
            if name.startswith('"') and name.endswith('"'):
                name = name[1:-1]
            folders.append(name)
        return {"folders": folders}
    finally:
        try:
            m.logout()
        except Exception:
            pass


def op_list(secrets: Dict[str, Any], body: Dict[str, Any]) -> Dict[str, Any]:
    limit = max(1, min(int(body.get("limit") or 10), LIST_MAX))
    unseen_only = bool(body.get("unseen_only"))
    m = _imap_connect(secrets)
    try:
        _select_folder(m, str(body.get("folder") or "INBOX"))
        typ, data = m.uid("search", None, "UNSEEN" if unseen_only else "ALL")
        if typ != "OK":
            raise EmailOpError("Search failed")
        uids = (data[0] or b"").split()
        uids = uids[-limit:][::-1]  # newest first
        messages = []
        for uid in uids:
            typ, msg_data = m.uid(
                "fetch", uid,
                "(FLAGS BODY.PEEK[HEADER.FIELDS (FROM TO SUBJECT DATE)])",
            )
            if typ != "OK" or not msg_data:
                continue
            flags = b" ".join(p for p in msg_data if isinstance(p, bytes))
            header_blob = b""
            for part in msg_data:
                if isinstance(part, tuple) and len(part) >= 2:
                    if isinstance(part[0], bytes):
                        flags += b" " + part[0]
                    header_blob = part[1]
                    break
            msg = email.message_from_bytes(header_blob or b"")
            messages.append({
                "uid": uid.decode(),
                "from": _dec(msg.get("From")),
                "to": _dec(msg.get("To")),
                "subject": _dec(msg.get("Subject")),
                "date": _dec(msg.get("Date")),
                "seen": b"\\Seen" in flags,
            })
        return {"folder": str(body.get("folder") or "INBOX"),
                "unseen_only": unseen_only, "count": len(messages),
                "messages": messages}
    finally:
        try:
            m.logout()
        except Exception:
            pass


def _best_body(msg: email.message.Message) -> str:
    """Prefer text/plain; fall back to crudely de-tagged text/html."""
    def _decode(part) -> str:
        payload = part.get_payload(decode=True) or b""
        charset = part.get_content_charset() or "utf-8"
        return payload.decode(charset, errors="replace")

    if msg.is_multipart():
        html = ""
        for part in msg.walk():
            ctype = part.get_content_type()
            if "attachment" in str(part.get("Content-Disposition") or ""):
                continue
            if ctype == "text/plain":
                return _decode(part)
            if ctype == "text/html" and not html:
                html = _decode(part)
        text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html,
                      flags=re.S | re.I)
        return re.sub(r"<[^>]+>", " ", text)
    if msg.get_content_type() == "text/html":
        text = _decode(msg)
        text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", text,
                      flags=re.S | re.I)
        return re.sub(r"<[^>]+>", " ", text)
    return _decode(msg)


def op_read(secrets: Dict[str, Any], body: Dict[str, Any]) -> Dict[str, Any]:
    uid = str(body.get("uid") or "").strip()
    if not uid.isdigit():
        raise EmailOpError("'uid' (from the list action) is required to read a message")
    m = _imap_connect(secrets)
    try:
        _select_folder(m, str(body.get("folder") or "INBOX"))
        typ, msg_data = m.uid("fetch", uid.encode(), "(BODY.PEEK[])")
        if typ != "OK" or not msg_data or not any(isinstance(p, tuple) for p in msg_data):
            raise EmailOpError(f"Message uid {uid} not found in that folder")
        raw = b""
        for part in msg_data:
            if isinstance(part, tuple) and len(part) >= 2:
                raw = part[1]
                break
        msg = email.message_from_bytes(raw)
        text = _best_body(msg)
        truncated = len(text) > BODY_MAX
        attachments = [
            _dec(part.get_filename())
            for part in (msg.walk() if msg.is_multipart() else [])
            if part.get_filename()
        ]
        return {"message": {
            "uid": uid,
            "from": _dec(msg.get("From")),
            "to": _dec(msg.get("To")),
            "cc": _dec(msg.get("Cc")),
            "subject": _dec(msg.get("Subject")),
            "date": _dec(msg.get("Date")),
            "body": text[:BODY_MAX],
            "truncated": truncated,
            "attachments": attachments,
        }}
    finally:
        try:
            m.logout()
        except Exception:
            pass


def _addr_list(value: Any) -> List[str]:
    if isinstance(value, list):
        items = [str(v) for v in value]
    else:
        items = re.split(r"[,;]", str(value or ""))
    out = []
    for item in items:
        name, addr = parseaddr(item.strip())
        if addr and "@" in addr:
            out.append(formataddr((name, addr)) if name else addr)
    return out


def op_send(secrets: Dict[str, Any], body: Dict[str, Any]) -> Dict[str, Any]:
    to = _addr_list(body.get("to"))
    if not to:
        raise EmailOpError("'to' with at least one valid address is required")
    cc = _addr_list(body.get("cc"))
    bcc = _addr_list(body.get("bcc"))
    subject = str(body.get("subject") or "").strip()
    text = str(body.get("body") or "")
    if not subject and not text:
        raise EmailOpError("Provide a 'subject' and/or 'body'")

    username = (secrets.get("username") or "").strip()
    msg = EmailMessage()
    msg["From"] = username
    msg["To"] = ", ".join(to)
    if cc:
        msg["Cc"] = ", ".join(cc)
    if subject:
        msg["Subject"] = subject
    reply_to = str(body.get("reply_to") or "").strip()
    if reply_to:
        msg["Reply-To"] = reply_to
    in_reply_to = str(body.get("in_reply_to") or "").strip()
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = in_reply_to
    msg.set_content(text)

    host = (secrets.get("smtp_host") or "").strip()
    port = int(secrets.get("smtp_port") or 587)
    if not host:
        raise EmailOpError("No SMTP server configured for this connection")
    _check_host(host, "SMTP")
    try:
        if port == 465:
            server: smtplib.SMTP = smtplib.SMTP_SSL(host, port, timeout=TIMEOUT)
        else:
            server = smtplib.SMTP(host, port, timeout=TIMEOUT)
            server.ehlo()
            server.starttls()
            server.ehlo()
        try:
            server.login(username, secrets.get("password") or "")
            server.send_message(msg, to_addrs=to + cc + bcc)
        finally:
            try:
                server.quit()
            except Exception:
                pass
    except smtplib.SMTPAuthenticationError as e:
        raise EmailOpError(
            f"SMTP login failed: {e.smtp_error!r}. For Gmail/Yahoo/iCloud you "
            "usually need an app password."
        )
    except (smtplib.SMTPException, OSError, socket.timeout) as e:
        raise EmailOpError(f"Sending failed via {host}:{port}: {e}")
    return {"sent": True, "to": to, "cc": cc, "bcc_count": len(bcc),
            "subject": subject}


ACTIONS = {
    "folders": op_folders,
    "list": op_list,
    "read": op_read,
    "send": op_send,
}


def run_action(action: str, secrets: Dict[str, Any], body: Dict[str, Any]) -> Dict[str, Any]:
    fn = ACTIONS.get(action)
    if fn is None:
        raise EmailOpError(
            f"Unknown action '{action}'. Use one of: {', '.join(ACTIONS)}.")
    return fn(secrets, body)
