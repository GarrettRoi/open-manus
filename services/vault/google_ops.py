"""Structured Google Workspace operations for the vault.

Each granted Google connection is exposed to agents as dedicated per-product
methods (Sheets, Tasks, Gmail, Docs, Forms, Slides, Chat, Drive, People,
Calendar) instead of only the generic HTTP proxy.  This module translates a
(product, operation, args) triple into a concrete HTTPS request spec that the
vault executes with the connection's OAuth token — agents never see the token.

Every product also supports a raw ``request`` operation, but it is pinned to
that product's Google API host so a Sheets tool can never be steered at, say,
the OAuth token endpoint.
"""

from __future__ import annotations

import base64
from email.message import EmailMessage
from typing import Any, Dict, List
import re as _re
from urllib.parse import quote, unquote


class GoogleOpsError(Exception):
    """Raised for invalid products, operations, or arguments."""


# Host + default path prefix per product. `request` paths must start with the
# prefix, keeping the raw escape hatch inside the product's own API surface.
PRODUCTS: Dict[str, Dict[str, str]] = {
    "gmail":    {"host": "gmail.googleapis.com",    "prefix": "/gmail/v1/"},
    "drive":    {"host": "www.googleapis.com",      "prefix": "/drive/v3/"},
    "sheets":   {"host": "sheets.googleapis.com",   "prefix": "/v4/spreadsheets"},
    "docs":     {"host": "docs.googleapis.com",     "prefix": "/v1/documents"},
    "slides":   {"host": "slides.googleapis.com",   "prefix": "/v1/presentations"},
    "forms":    {"host": "forms.googleapis.com",    "prefix": "/v1/forms"},
    "tasks":    {"host": "tasks.googleapis.com",    "prefix": "/tasks/v1/"},
    "chat":     {"host": "chat.googleapis.com",     "prefix": "/v1/spaces"},
    "people":   {"host": "people.googleapis.com",   "prefix": "/v1/people"},
    "calendar": {"host": "www.googleapis.com",      "prefix": "/calendar/v3/"},
}

_PERSON_FIELDS = "names,emailAddresses,phoneNumbers,organizations,addresses,birthdays"


def _need(args: Dict[str, Any], *keys: str) -> List[Any]:
    out = []
    for k in keys:
        v = args.get(k)
        if v is None or (isinstance(v, str) and not v.strip()):
            raise GoogleOpsError(f"'{k}' is required for this operation")
        out.append(v)
    return out


_SAFE_ID = _re.compile(r"^[A-Za-z0-9@_=-]+(\.[A-Za-z0-9@_=-]+)*$")


def _seg(value: Any, label: str) -> str:
    """URL-encode a single path segment; refuse separators/traversal."""
    s = str(value).strip()
    if not s:
        raise GoogleOpsError(f"'{label}' is required")
    if s.startswith(("spaces/", "people/")):
        prefix, _, rest = s.partition("/")
        if not _SAFE_ID.match(rest):
            raise GoogleOpsError(
                f"'{label}' must be exactly '{prefix}/<id>' with a plain id")
        return f"{prefix}/{quote(rest, safe='')}"
    if "/" in s or "%" in s or ".." in s:
        raise GoogleOpsError(f"'{label}' must be a plain identifier")
    return quote(s, safe="")


def _limit(args: Dict[str, Any], default: int = 25, cap: int = 100) -> int:
    try:
        return max(1, min(int(args.get("limit") or default), cap))
    except (TypeError, ValueError):
        raise GoogleOpsError("'limit' must be an integer")


def _spec(method: str, host: str, path: str, *, params: Dict[str, Any] | None = None,
          json_body: Any = None) -> Dict[str, Any]:
    return {
        "method": method,
        "url": f"https://{host}{path}",
        "params": {k: v for k, v in (params or {}).items() if v is not None},
        "json": json_body,
    }


def _raw_request(product: str, args: Dict[str, Any]) -> Dict[str, Any]:
    info = PRODUCTS[product]
    method = str(args.get("method") or "GET").upper()
    if method not in {"GET", "POST", "PUT", "PATCH", "DELETE"}:
        raise GoogleOpsError(f"Unsupported method: {method}")
    (path,) = _need(args, "path")
    path = str(path)
    if not path.startswith("/"):
        path = "/" + path
    # Canonicalize before any prefix decision: repeatedly percent-decode so
    # encoded traversal (%2e%2e, double-encoding) cannot smuggle dot segments
    # past the product pinning, then reject dot segments and empty segments.
    decoded = path
    for _ in range(5):
        nxt = unquote(decoded)
        if nxt == decoded:
            break
        decoded = nxt
    segments = decoded.split("/")
    if any(seg in {".", ".."} for seg in segments) or "//" in decoded or "\\" in decoded:
        raise GoogleOpsError("Invalid path")
    if path != decoded:
        # No percent-encoding tricks in raw paths — require literal paths.
        raise GoogleOpsError("Percent-encoded characters are not allowed in 'path'")
    if not path.startswith(info["prefix"].rstrip("/")):
        raise GoogleOpsError(
            f"For the {product} tool, 'path' must start with {info['prefix']}")
    params = args.get("params") if isinstance(args.get("params"), dict) else None
    body = args.get("json")
    return _spec(method, info["host"], path, params=params, json_body=body)


# ---------------------------------------------------------------------------
# Per-product operation builders
# ---------------------------------------------------------------------------

def _gmail(op: str, a: Dict[str, Any]) -> Dict[str, Any]:
    h = PRODUCTS["gmail"]["host"]
    if op == "search":
        (q,) = _need(a, "q")
        return _spec("GET", h, "/gmail/v1/users/me/messages",
                     params={"q": q, "maxResults": _limit(a)})
    if op == "read":
        (mid,) = _need(a, "id")
        return _spec("GET", h, f"/gmail/v1/users/me/messages/{_seg(mid, 'id')}",
                     params={"format": a.get("format") or "full"})
    if op == "send":
        to, subject, body = _need(a, "to", "subject", "body")
        msg = EmailMessage()
        msg["To"] = str(to)
        msg["Subject"] = str(subject)
        if a.get("cc"):
            msg["Cc"] = str(a["cc"])
        if a.get("bcc"):
            msg["Bcc"] = str(a["bcc"])
        msg.set_content(str(body))
        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode().rstrip("=")
        return _spec("POST", h, "/gmail/v1/users/me/messages/send",
                     json_body={"raw": raw})
    if op == "modify":
        (mid,) = _need(a, "id")
        return _spec("POST", h, f"/gmail/v1/users/me/messages/{_seg(mid, 'id')}/modify",
                     json_body={"addLabelIds": a.get("add_labels") or [],
                                "removeLabelIds": a.get("remove_labels") or []})
    if op == "labels":
        return _spec("GET", h, "/gmail/v1/users/me/labels")
    raise GoogleOpsError(f"Unknown gmail operation: {op}")


def _drive(op: str, a: Dict[str, Any]) -> Dict[str, Any]:
    h = PRODUCTS["drive"]["host"]
    fields = "files(id,name,mimeType,modifiedTime,size,parents,webViewLink)"
    if op == "search":
        q = a.get("q")
        if not q and a.get("name_contains"):
            esc = str(a["name_contains"]).replace("\\", "\\\\").replace("'", "\\'")
            q = f"name contains '{esc}' and trashed = false"
        if not q:
            q = "trashed = false"
        return _spec("GET", h, "/drive/v3/files",
                     params={"q": q, "pageSize": _limit(a), "fields": fields,
                             "orderBy": "modifiedTime desc"})
    if op == "get":
        (fid,) = _need(a, "file_id")
        return _spec("GET", h, f"/drive/v3/files/{_seg(fid, 'file_id')}",
                     params={"fields": "*"})
    if op == "download":
        (fid,) = _need(a, "file_id")
        return _spec("GET", h, f"/drive/v3/files/{_seg(fid, 'file_id')}",
                     params={"alt": "media"})
    if op == "export":
        fid, mt = _need(a, "file_id", "mime_type")
        return _spec("GET", h, f"/drive/v3/files/{_seg(fid, 'file_id')}/export",
                     params={"mimeType": mt})
    if op == "create_folder":
        (name,) = _need(a, "name")
        body: Dict[str, Any] = {"name": name,
                                "mimeType": "application/vnd.google-apps.folder"}
        if a.get("parent_id"):
            body["parents"] = [str(a["parent_id"])]
        return _spec("POST", h, "/drive/v3/files", json_body=body)
    if op == "delete":
        (fid,) = _need(a, "file_id")
        return _spec("DELETE", h, f"/drive/v3/files/{_seg(fid, 'file_id')}")
    raise GoogleOpsError(f"Unknown drive operation: {op}")


def _sheets(op: str, a: Dict[str, Any]) -> Dict[str, Any]:
    h = PRODUCTS["sheets"]["host"]
    if op == "create":
        (title,) = _need(a, "title")
        return _spec("POST", h, "/v4/spreadsheets",
                     json_body={"properties": {"title": title}})
    if op == "meta":
        (sid,) = _need(a, "spreadsheet_id")
        return _spec("GET", h, f"/v4/spreadsheets/{_seg(sid, 'spreadsheet_id')}",
                     params={"fields": "spreadsheetId,properties.title,sheets.properties"})
    if op in {"get", "update", "append"}:
        sid, rng = _need(a, "spreadsheet_id", "range")
        base = f"/v4/spreadsheets/{_seg(sid, 'spreadsheet_id')}/values/{quote(str(rng), safe='')}"
        if op == "get":
            return _spec("GET", h, base)
        (values,) = _need(a, "values")
        if not isinstance(values, list):
            raise GoogleOpsError("'values' must be a list of rows (lists)")
        body = {"values": values}
        if op == "update":
            return _spec("PUT", h, base,
                         params={"valueInputOption": "USER_ENTERED"}, json_body=body)
        return _spec("POST", h, base + ":append",
                     params={"valueInputOption": "USER_ENTERED",
                             "insertDataOption": "INSERT_ROWS"}, json_body=body)
    if op == "batch_get":
        sid, ranges = _need(a, "spreadsheet_id", "ranges")
        if not isinstance(ranges, list):
            raise GoogleOpsError("'ranges' must be a list")
        return _spec("GET", h,
                     f"/v4/spreadsheets/{_seg(sid, 'spreadsheet_id')}/values:batchGet",
                     params={"ranges": ranges})
    raise GoogleOpsError(f"Unknown sheets operation: {op}")


def _docs(op: str, a: Dict[str, Any]) -> Dict[str, Any]:
    h = PRODUCTS["docs"]["host"]
    if op == "create":
        (title,) = _need(a, "title")
        return _spec("POST", h, "/v1/documents", json_body={"title": title})
    if op == "get":
        (did,) = _need(a, "document_id")
        return _spec("GET", h, f"/v1/documents/{_seg(did, 'document_id')}")
    if op == "insert_text":
        did, text = _need(a, "document_id", "text")
        try:
            index = int(a.get("index") or 1)
        except (TypeError, ValueError):
            raise GoogleOpsError("'index' must be an integer")
        return _spec("POST", h,
                     f"/v1/documents/{_seg(did, 'document_id')}:batchUpdate",
                     json_body={"requests": [{"insertText": {
                         "location": {"index": index}, "text": str(text)}}]})
    if op == "batch_update":
        did, reqs = _need(a, "document_id", "requests")
        if not isinstance(reqs, list):
            raise GoogleOpsError("'requests' must be a list")
        return _spec("POST", h,
                     f"/v1/documents/{_seg(did, 'document_id')}:batchUpdate",
                     json_body={"requests": reqs})
    raise GoogleOpsError(f"Unknown docs operation: {op}")


def _slides(op: str, a: Dict[str, Any]) -> Dict[str, Any]:
    h = PRODUCTS["slides"]["host"]
    if op == "create":
        (title,) = _need(a, "title")
        return _spec("POST", h, "/v1/presentations", json_body={"title": title})
    if op == "get":
        (pid,) = _need(a, "presentation_id")
        return _spec("GET", h, f"/v1/presentations/{_seg(pid, 'presentation_id')}")
    if op == "batch_update":
        pid, reqs = _need(a, "presentation_id", "requests")
        if not isinstance(reqs, list):
            raise GoogleOpsError("'requests' must be a list")
        return _spec("POST", h,
                     f"/v1/presentations/{_seg(pid, 'presentation_id')}:batchUpdate",
                     json_body={"requests": reqs})
    raise GoogleOpsError(f"Unknown slides operation: {op}")


def _forms(op: str, a: Dict[str, Any]) -> Dict[str, Any]:
    h = PRODUCTS["forms"]["host"]
    if op == "create":
        (title,) = _need(a, "title")
        return _spec("POST", h, "/v1/forms",
                     json_body={"info": {"title": title}})
    if op == "get":
        (fid,) = _need(a, "form_id")
        return _spec("GET", h, f"/v1/forms/{_seg(fid, 'form_id')}")
    if op == "responses":
        (fid,) = _need(a, "form_id")
        return _spec("GET", h, f"/v1/forms/{_seg(fid, 'form_id')}/responses")
    if op == "batch_update":
        fid, reqs = _need(a, "form_id", "requests")
        if not isinstance(reqs, list):
            raise GoogleOpsError("'requests' must be a list")
        return _spec("POST", h, f"/v1/forms/{_seg(fid, 'form_id')}:batchUpdate",
                     json_body={"requests": reqs})
    raise GoogleOpsError(f"Unknown forms operation: {op}")


def _tasks(op: str, a: Dict[str, Any]) -> Dict[str, Any]:
    h = PRODUCTS["tasks"]["host"]
    tl = _seg(a.get("tasklist") or "@default", "tasklist")
    if op == "lists":
        return _spec("GET", h, "/tasks/v1/users/@me/lists")
    if op == "list":
        return _spec("GET", h, f"/tasks/v1/lists/{tl}/tasks",
                     params={"maxResults": _limit(a),
                             "showCompleted": bool(a.get("show_completed", False))})
    if op == "create":
        (title,) = _need(a, "title")
        body: Dict[str, Any] = {"title": title}
        if a.get("notes"):
            body["notes"] = str(a["notes"])
        if a.get("due"):
            body["due"] = str(a["due"])
        return _spec("POST", h, f"/tasks/v1/lists/{tl}/tasks", json_body=body)
    if op == "complete":
        (tid,) = _need(a, "task_id")
        return _spec("PATCH", h, f"/tasks/v1/lists/{tl}/tasks/{_seg(tid, 'task_id')}",
                     json_body={"status": "completed"})
    if op == "delete":
        (tid,) = _need(a, "task_id")
        return _spec("DELETE", h, f"/tasks/v1/lists/{tl}/tasks/{_seg(tid, 'task_id')}")
    raise GoogleOpsError(f"Unknown tasks operation: {op}")


def _chat(op: str, a: Dict[str, Any]) -> Dict[str, Any]:
    h = PRODUCTS["chat"]["host"]
    if op == "spaces":
        return _spec("GET", h, "/v1/spaces", params={"pageSize": _limit(a)})
    if op == "messages":
        (space,) = _need(a, "space")
        space = str(space)
        if not space.startswith("spaces/"):
            space = f"spaces/{space}"
        return _spec("GET", h, f"/v1/{_seg(space, 'space')}/messages",
                     params={"pageSize": _limit(a)})
    if op == "send":
        space, text = _need(a, "space", "text")
        space = str(space)
        if not space.startswith("spaces/"):
            space = f"spaces/{space}"
        return _spec("POST", h, f"/v1/{_seg(space, 'space')}/messages",
                     json_body={"text": str(text)})
    raise GoogleOpsError(f"Unknown chat operation: {op}")


def _people(op: str, a: Dict[str, Any]) -> Dict[str, Any]:
    h = PRODUCTS["people"]["host"]
    if op == "contacts":
        return _spec("GET", h, "/v1/people/me/connections",
                     params={"personFields": _PERSON_FIELDS,
                             "pageSize": _limit(a, default=50, cap=200)})
    if op == "search":
        (query,) = _need(a, "query")
        return _spec("GET", h, "/v1/people:searchContacts",
                     params={"query": query, "readMask": _PERSON_FIELDS,
                             "pageSize": _limit(a, default=10, cap=30)})
    if op == "get":
        (rn,) = _need(a, "resource_name")
        rn = str(rn)
        if not rn.startswith("people/"):
            rn = f"people/{rn}"
        return _spec("GET", h, f"/v1/{_seg(rn, 'resource_name')}",
                     params={"personFields": _PERSON_FIELDS})
    raise GoogleOpsError(f"Unknown people operation: {op}")


def _calendar(op: str, a: Dict[str, Any]) -> Dict[str, Any]:
    h = PRODUCTS["calendar"]["host"]
    cal = _seg(a.get("calendar_id") or "primary", "calendar_id")
    if op == "calendars":
        return _spec("GET", h, "/calendar/v3/users/me/calendarList")
    if op == "events":
        return _spec("GET", h, f"/calendar/v3/calendars/{cal}/events",
                     params={"maxResults": _limit(a),
                             "singleEvents": True, "orderBy": "startTime",
                             "timeMin": a.get("time_min"),
                             "timeMax": a.get("time_max"),
                             "q": a.get("q")})
    if op == "create_event":
        summary, start, end = _need(a, "summary", "start", "end")
        body: Dict[str, Any] = {
            "summary": summary,
            "start": start if isinstance(start, dict) else {"dateTime": str(start)},
            "end": end if isinstance(end, dict) else {"dateTime": str(end)},
        }
        for k_src, k_dst in (("description", "description"), ("location", "location")):
            if a.get(k_src):
                body[k_dst] = str(a[k_src])
        if a.get("attendees"):
            att = a["attendees"]
            if not isinstance(att, list):
                raise GoogleOpsError("'attendees' must be a list of emails")
            body["attendees"] = [{"email": str(e)} for e in att]
        return _spec("POST", h, f"/calendar/v3/calendars/{cal}/events", json_body=body)
    if op == "update_event":
        (eid,) = _need(a, "event_id")
        patch = a.get("patch")
        if not isinstance(patch, dict) or not patch:
            raise GoogleOpsError("'patch' must be a non-empty object of event fields")
        return _spec("PATCH", h,
                     f"/calendar/v3/calendars/{cal}/events/{_seg(eid, 'event_id')}",
                     json_body=patch)
    if op == "delete_event":
        (eid,) = _need(a, "event_id")
        return _spec("DELETE", h,
                     f"/calendar/v3/calendars/{cal}/events/{_seg(eid, 'event_id')}")
    raise GoogleOpsError(f"Unknown calendar operation: {op}")


_BUILDERS = {
    "gmail": _gmail, "drive": _drive, "sheets": _sheets, "docs": _docs,
    "slides": _slides, "forms": _forms, "tasks": _tasks, "chat": _chat,
    "people": _people, "calendar": _calendar,
}

# Operation names surfaced in tool schemas / docs.
OPERATIONS: Dict[str, List[str]] = {
    "gmail": ["search", "read", "send", "modify", "labels", "request"],
    "drive": ["search", "get", "download", "export", "create_folder", "delete", "request"],
    "sheets": ["create", "meta", "get", "update", "append", "batch_get", "request"],
    "docs": ["create", "get", "insert_text", "batch_update", "request"],
    "slides": ["create", "get", "batch_update", "request"],
    "forms": ["create", "get", "responses", "batch_update", "request"],
    "tasks": ["lists", "list", "create", "complete", "delete", "request"],
    "chat": ["spaces", "messages", "send", "request"],
    "people": ["contacts", "search", "get", "request"],
    "calendar": ["calendars", "events", "create_event", "update_event", "delete_event", "request"],
}


def build_request(product: str, operation: str, args: Dict[str, Any] | None) -> Dict[str, Any]:
    """Translate (product, operation, args) into an HTTPS request spec."""
    product = (product or "").strip().lower()
    operation = (operation or "").strip().lower()
    if product not in PRODUCTS:
        raise GoogleOpsError(
            f"Unknown Google product '{product}'. One of: {', '.join(sorted(PRODUCTS))}")
    if not operation:
        raise GoogleOpsError("operation is required")
    a = args if isinstance(args, dict) else {}
    if operation == "request":
        return _raw_request(product, a)
    return _BUILDERS[product](operation, a)
