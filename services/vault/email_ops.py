"""IMAP/SMTP operations for email-kind vault connections.

The vault performs classic email-client actions server-side using the
stored mailbox credentials — agents call POST /api/vault/email/{conn}
and never see the password. Everything here is synchronous stdlib
(imaplib/smtplib/email) and is run in a worker thread by the endpoint.

Actions:
    folders                      -> {"folders": [...]}
    list   (folder, limit,       -> {"messages": [{uid, from, to, subject,
            unseen_only)              date, seen}, ...]}
    search (from, to, cc,        -> {"total_matches", "more", "messages":
            subject, text,            [{..., attachments: [names]}]}
            since, before, last_days, unseen, flagged, min/max_size_kb,
            has_attachment, attachment_name, folder, limit)
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
import ssl
from email.header import decode_header, make_header
from email.message import EmailMessage
from email.utils import formataddr, parseaddr
from typing import Any, Dict, List

TIMEOUT = 25  # seconds per server operation
LIST_MAX = 50
BODY_MAX = 20_000  # chars returned for a read


class EmailOpError(Exception):
    """User-facing email operation failure (bad login, bad folder, ...)."""


def _resolve_pinned_ip(host: str, port: int, label: str) -> str:
    """Connect-time SSRF guard: resolve *once*, validate every address, and
    return one validated global IP. Callers MUST connect to the returned IP
    (not the hostname) so the address that was validated is the address used
    — closing the DNS-rebinding time-of-check/time-of-use gap. The original
    hostname is kept only for TLS SNI/certificate verification."""
    import ipaddress
    try:
        ipaddress.ip_address(host)
        raise EmailOpError(f"{label} server must be a hostname, not an IP address")
    except ValueError:
        pass
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        raise EmailOpError(f"{label} server {host} does not resolve")
    pinned = ""
    for info in infos:
        addr = info[4][0]
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            continue
        if not ip.is_global:
            raise EmailOpError(
                f"{label} server {host} resolves to a private/internal "
                "address — refusing to connect")
        if not pinned:
            pinned = addr
    if not pinned:
        raise EmailOpError(f"{label} server {host} has no usable address")
    return pinned


class _PinnedIMAP4_SSL(imaplib.IMAP4_SSL):
    """IMAP4_SSL that connects to a pre-validated IP while doing TLS
    SNI/hostname verification against the real hostname."""

    def __init__(self, host: str, port: int, pinned_ip: str, timeout: float):
        self._pinned_ip = pinned_ip
        self._tls_hostname = host
        ctx = ssl.create_default_context()
        super().__init__(host, port, ssl_context=ctx, timeout=timeout)

    def _create_socket(self, timeout):
        sock = socket.create_connection(
            (self._pinned_ip, self.port),
            timeout if timeout is not None else None)
        return self.ssl_context.wrap_socket(
            sock, server_hostname=self._tls_hostname)


class _PinnedSMTP(smtplib.SMTP):
    """SMTP that connects to a pre-validated IP; ``self._host`` keeps the real
    hostname so STARTTLS certificate verification still checks it."""

    def __init__(self, host: str, port: int, pinned_ip: str, timeout: float):
        self._pinned_ip = pinned_ip
        super().__init__(host, port, timeout=timeout)

    def _get_socket(self, host, port, timeout):
        return socket.create_connection((self._pinned_ip, port), timeout)


class _PinnedSMTP_SSL(smtplib.SMTP_SSL):
    """SMTP_SSL (implicit TLS, port 465) pinned to a pre-validated IP with
    SNI/certificate verification against the real hostname."""

    def __init__(self, host: str, port: int, pinned_ip: str, timeout: float):
        self._pinned_ip = pinned_ip
        super().__init__(host, port, timeout=timeout,
                         context=ssl.create_default_context())

    def _get_socket(self, host, port, timeout):
        sock = socket.create_connection((self._pinned_ip, port), timeout)
        return self.context.wrap_socket(sock, server_hostname=self._host)


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
    pinned_ip = _resolve_pinned_ip(host, port, "IMAP")
    try:
        m = _PinnedIMAP4_SSL(host, port, pinned_ip, timeout=TIMEOUT)
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


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------
SEARCH_SCAN_MAX = 150  # most candidates we'll inspect for attachment filters

_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
           "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def _imap_date(value: str, label: str):
    """Parse YYYY-MM-DD (or DD-Mon-YYYY) into an IMAP date string."""
    from datetime import datetime as _dt
    s = str(value).strip()
    for fmt in ("%Y-%m-%d", "%d-%b-%Y", "%Y/%m/%d", "%m/%d/%Y"):
        try:
            d = _dt.strptime(s, fmt)
            return f"{d.day:02d}-{_MONTHS[d.month - 1]}-{d.year}"
        except ValueError:
            continue
    raise EmailOpError(f"Invalid {label} date {s!r} — use YYYY-MM-DD")


def _quote_atom(value: str) -> str:
    """Quote a search string for IMAP; reject CR/LF injection."""
    s = str(value)
    if "\r" in s or "\n" in s:
        raise EmailOpError("Search text cannot contain line breaks")
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _build_imap_criteria(f: Dict[str, Any]) -> str:
    """Translate structured filters into standard IMAP SEARCH criteria (AND)."""
    crit: List[str] = []
    for key, kw in (("from", "FROM"), ("to", "TO"), ("cc", "CC"),
                    ("subject", "SUBJECT"), ("text", "TEXT")):
        if f.get(key):
            crit.append(f"{kw} {_quote_atom(f[key])}")
    if f.get("since"):
        crit.append(f"SINCE {_imap_date(f['since'], 'since')}")
    if f.get("before"):
        crit.append(f"BEFORE {_imap_date(f['before'], 'before')}")
    if f.get("unseen") is True:
        crit.append("UNSEEN")
    elif f.get("unseen") is False:
        crit.append("SEEN")
    if f.get("flagged"):
        crit.append("FLAGGED")
    if f.get("min_size_kb"):
        crit.append(f"LARGER {int(float(f['min_size_kb']) * 1024)}")
    if f.get("max_size_kb"):
        crit.append(f"SMALLER {int(float(f['max_size_kb']) * 1024)}")
    return f"({' '.join(crit)})" if crit else "ALL"


def _build_gmail_query(f: Dict[str, Any]) -> str:
    """Translate the same filters into Gmail X-GM-RAW search syntax."""
    def q(v: str) -> str:
        v = str(v).strip()
        return f'"{v}"' if (" " in v and '"' not in v) else v
    parts: List[str] = []
    if f.get("from"):
        parts.append(f"from:{q(f['from'])}")
    if f.get("to"):
        parts.append(f"to:{q(f['to'])}")
    if f.get("cc"):
        parts.append(f"cc:{q(f['cc'])}")
    if f.get("subject"):
        parts.append(f"subject:{q(f['subject'])}")
    if f.get("text"):
        parts.append(q(f["text"]))
    if f.get("since"):
        parts.append("after:" + str(f["since"]).replace("-", "/"))
    if f.get("before"):
        parts.append("before:" + str(f["before"]).replace("-", "/"))
    if f.get("unseen") is True:
        parts.append("is:unread")
    elif f.get("unseen") is False:
        parts.append("is:read")
    if f.get("flagged"):
        parts.append("is:starred")
    if f.get("min_size_kb"):
        parts.append(f"larger:{int(float(f['min_size_kb']))}k")
    if f.get("max_size_kb"):
        parts.append(f"smaller:{int(float(f['max_size_kb']))}k")
    if f.get("has_attachment"):
        parts.append("has:attachment")
    if f.get("attachment_name"):
        parts.append(f"filename:{q(f['attachment_name'])}")
    return " ".join(parts) or "in:anywhere"


# BODYSTRUCTURE attachment filenames: ("attachment" ("filename" "x")) or
# ("name" "x") parameters on parts.
_BS_NAME_RE = re.compile(
    rb'"(?:file)?name"\s+"((?:[^"\\]|\\.)*)"', re.I)


def _bodystructure_attachments(bs_blob: bytes) -> List[str]:
    names = []
    for m in _BS_NAME_RE.finditer(bs_blob or b""):
        raw = m.group(1).decode(errors="replace").replace('\\"', '"')
        name = _dec(raw)
        if name and name not in names:
            names.append(name)
    return names


def _attachment_name_matches(names: List[str], pattern: str) -> bool:
    """Case-insensitive substring or * wildcard match on attachment names."""
    import fnmatch
    pat = pattern.lower()
    for n in names:
        nl = n.lower()
        if "*" in pat or "?" in pat:
            if fnmatch.fnmatch(nl, pat):
                return True
        elif pat in nl:
            return True
    return False


def op_search(secrets: Dict[str, Any], body: Dict[str, Any]) -> Dict[str, Any]:
    """Structured mailbox search.

    Filters (all optional, combined with AND): from, to, cc, subject,
    text (keywords in headers+body), since / before (YYYY-MM-DD),
    last_days (relative shortcut), unseen (true=unread only, false=read
    only), flagged, min_size_kb / max_size_kb, has_attachment,
    attachment_name (substring or * wildcard), folder, limit.
    """
    from datetime import datetime as _dt, timedelta

    limit = max(1, min(int(body.get("limit") or 10), LIST_MAX))
    f: Dict[str, Any] = {}
    for key in ("from", "to", "cc", "subject", "text", "since", "before",
                "attachment_name"):
        val = body.get(key)
        if val not in (None, ""):
            f[key] = str(val).strip()
    if body.get("query") and not f.get("text"):
        f["text"] = str(body["query"]).strip()
    if body.get("last_days"):
        try:
            days = max(1, int(body["last_days"]))
        except (TypeError, ValueError):
            raise EmailOpError("'last_days' must be a whole number of days")
        f["since"] = (_dt.utcnow() - timedelta(days=days)).strftime("%Y-%m-%d")
    if "unseen" in body and body.get("unseen") is not None:
        f["unseen"] = bool(body["unseen"])
    elif body.get("unseen_only"):
        f["unseen"] = True
    if body.get("flagged"):
        f["flagged"] = True
    for key in ("min_size_kb", "max_size_kb"):
        if body.get(key) not in (None, ""):
            try:
                f[key] = float(body[key])
            except (TypeError, ValueError):
                raise EmailOpError(f"'{key}' must be a number (kilobytes)")
    if body.get("has_attachment") is not None and "has_attachment" in body:
        f["has_attachment"] = bool(body["has_attachment"])
    if f.get("attachment_name"):
        f["has_attachment"] = True

    m = _imap_connect(secrets)
    try:
        _select_folder(m, str(body.get("folder") or "INBOX"))
        gmail = "X-GM-EXT-1" in (m.capabilities or ())
        used_gmail = False
        if gmail:
            try:
                typ, data = m.uid("search", "X-GM-RAW",
                                  _quote_atom(_build_gmail_query(f)))
                if typ == "OK":
                    used_gmail = True
                else:
                    raise imaplib.IMAP4.error("X-GM-RAW rejected")
            except imaplib.IMAP4.error:
                typ, data = m.uid("search", None, _build_imap_criteria(f))
        else:
            typ, data = m.uid("search", None, _build_imap_criteria(f))
        if typ != "OK":
            raise EmailOpError("Search failed on the mail server")

        uids = (data[0] or b"").split()
        total_matches = len(uids)
        uids = uids[::-1]  # newest first

        # Gmail already applied attachment filters natively.
        need_att_filter = (not used_gmail) and (
            f.get("has_attachment") is not None or f.get("attachment_name"))
        want_has = f.get("has_attachment")

        messages = []
        scanned = 0
        exhausted_scan = False
        for uid in uids:
            if len(messages) >= limit:
                break
            if need_att_filter and scanned >= SEARCH_SCAN_MAX:
                exhausted_scan = True
                break
            scanned += 1
            typ, msg_data = m.uid(
                "fetch", uid,
                "(FLAGS BODYSTRUCTURE BODY.PEEK[HEADER.FIELDS (FROM TO SUBJECT DATE)])",
            )
            if typ != "OK" or not msg_data:
                continue
            flags_and_bs = b" ".join(p for p in msg_data if isinstance(p, bytes))
            header_blob = b""
            for part in msg_data:
                if isinstance(part, tuple) and len(part) >= 2:
                    if isinstance(part[0], bytes):
                        flags_and_bs += b" " + part[0]
                    header_blob = part[1]
                    break
            att_names = _bodystructure_attachments(flags_and_bs)
            if need_att_filter:
                if want_has is True and not att_names:
                    continue
                if want_has is False and att_names:
                    continue
                if f.get("attachment_name") and not _attachment_name_matches(
                        att_names, f["attachment_name"]):
                    continue
            msg = email.message_from_bytes(header_blob or b"")
            messages.append({
                "uid": uid.decode(),
                "from": _dec(msg.get("From")),
                "to": _dec(msg.get("To")),
                "subject": _dec(msg.get("Subject")),
                "date": _dec(msg.get("Date")),
                "seen": b"\\Seen" in flags_and_bs,
                "attachments": att_names,
            })

        more = (total_matches > scanned if need_att_filter
                else total_matches > len(messages))
        result = {
            "folder": str(body.get("folder") or "INBOX"),
            "total_matches": total_matches,
            "count": len(messages),
            "more": bool(more),
            "messages": messages,
            "search_engine": "gmail" if used_gmail else "imap",
        }
        if exhausted_scan:
            result["note"] = (
                f"Attachment filtering inspected the newest {SEARCH_SCAN_MAX} "
                "matches only — narrow the search (dates, sender) to see older mail.")
        return result
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


ATTACHMENT_MAX = 15 * 1024 * 1024  # 15 MB raw — beyond this, refuse
MESSAGE_MAX = 25 * 1024 * 1024     # refuse to even fetch messages above this


def op_attachment(secrets: Dict[str, Any], body: Dict[str, Any]) -> Dict[str, Any]:
    """Download one attachment from a message and return it base64-encoded.

    Body: {"action": "attachment", "uid": "...", "filename": "report.pdf"}
    or    {"action": "attachment", "uid": "...", "index": 0}
    The agent-side tool decodes and saves the file locally.
    """
    import base64

    uid = str(body.get("uid") or "").strip()
    if not uid.isdigit():
        raise EmailOpError("'uid' (from the list action) is required")
    want_name = str(body.get("filename") or "").strip()
    want_index = body.get("index")
    if not want_name and want_index is None:
        raise EmailOpError("Provide 'filename' (from read) or 'index' (0-based)")

    m = _imap_connect(secrets)
    try:
        _select_folder(m, str(body.get("folder") or "INBOX"))
        # Bound memory BEFORE downloading: check the full message size first.
        typ, size_data = m.uid("fetch", uid.encode(), "(RFC822.SIZE)")
        if typ == "OK" and size_data and size_data[0]:
            msize = re.search(rb"RFC822\.SIZE\s+(\d+)", size_data[0]
                              if isinstance(size_data[0], bytes)
                              else str(size_data[0]).encode())
            if msize and int(msize.group(1)) > MESSAGE_MAX:
                raise EmailOpError(
                    f"Message uid {uid} is {int(msize.group(1)) // (1024 * 1024)} MB — "
                    f"too large to fetch (limit {MESSAGE_MAX // (1024 * 1024)} MB)")
        typ, msg_data = m.uid("fetch", uid.encode(), "(BODY.PEEK[])")
        if typ != "OK" or not msg_data or not any(isinstance(p, tuple) for p in msg_data):
            raise EmailOpError(f"Message uid {uid} not found in that folder")
        raw = b""
        for part in msg_data:
            if isinstance(part, tuple) and len(part) >= 2:
                raw = part[1]
                break
        if len(raw) > MESSAGE_MAX:
            raise EmailOpError(
                f"Message uid {uid} is larger than the "
                f"{MESSAGE_MAX // (1024 * 1024)} MB fetch limit")
        msg = email.message_from_bytes(raw)

        atts = []
        for part in (msg.walk() if msg.is_multipart() else []):
            fname = part.get_filename()
            if fname:
                atts.append((_dec(fname), part))
        if not atts:
            raise EmailOpError(f"Message uid {uid} has no attachments")

        chosen = None
        if want_name:
            for fname, part in atts:
                if fname == want_name:
                    chosen = (fname, part)
                    break
            if chosen is None:  # forgiving case-insensitive fallback
                for fname, part in atts:
                    if fname.lower() == want_name.lower():
                        chosen = (fname, part)
                        break
            if chosen is None:
                raise EmailOpError(
                    f"No attachment named {want_name!r}. Available: "
                    + ", ".join(f for f, _ in atts))
        else:
            try:
                idx = int(want_index)
            except (TypeError, ValueError):
                raise EmailOpError("'index' must be a number (0-based)")
            if idx < 0 or idx >= len(atts):
                raise EmailOpError(
                    f"index {idx} out of range — message has {len(atts)} attachment(s)")
            chosen = atts[idx]

        fname, part = chosen
        payload = part.get_payload(decode=True)
        if payload is None:
            raise EmailOpError(f"Could not decode attachment {fname!r}")
        if len(payload) > ATTACHMENT_MAX:
            raise EmailOpError(
                f"Attachment {fname!r} is {len(payload) // (1024 * 1024)} MB — "
                f"larger than the {ATTACHMENT_MAX // (1024 * 1024)} MB limit")
        return {"attachment": {
            "uid": uid,
            "filename": fname,
            "content_type": part.get_content_type(),
            "size": len(payload),
            "content_b64": base64.b64encode(payload).decode("ascii"),
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
    pinned_ip = _resolve_pinned_ip(host, port, "SMTP")
    try:
        if port == 465:
            server: smtplib.SMTP = _PinnedSMTP_SSL(host, port, pinned_ip,
                                                   timeout=TIMEOUT)
        else:
            server = _PinnedSMTP(host, port, pinned_ip, timeout=TIMEOUT)
            server.ehlo()
            server.starttls(context=ssl.create_default_context())
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
    "search": op_search,
    "read": op_read,
    "send": op_send,
    "attachment": op_attachment,
}


def run_action(action: str, secrets: Dict[str, Any], body: Dict[str, Any]) -> Dict[str, Any]:
    fn = ACTIONS.get(action)
    if fn is None:
        raise EmailOpError(
            f"Unknown action '{action}'. Use one of: {', '.join(ACTIONS)}.")
    return fn(secrets, body)
