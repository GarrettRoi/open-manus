"""Proxy-only Apple iCloud operations.

The vault stores an Apple ID and an iCloud app-specific password encrypted in
Redis.  This module is deliberately an operation layer, not a generic HTTP
proxy: all URLs are discovered from Apple's CalDAV/CardDAV services and every
request is restricted to Apple's public domains.
"""

from __future__ import annotations

import base64
import datetime as dt
import html
import re
import uuid
from typing import Any, Dict, Iterable, List, Optional
from urllib.parse import urljoin, urlsplit
import xml.etree.ElementTree as ET

import httpx


DAV = "DAV:"
CALDAV = "urn:ietf:params:xml:ns:caldav"
CARDDAV = "urn:ietf:params:xml:ns:carddav"


class AppleOpsError(Exception):
    """A safe, user-facing Apple operation error."""


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _text(node: Optional[ET.Element]) -> str:
    return (node.text or "").strip() if node is not None else ""


def _href(node: Optional[ET.Element], base: str) -> str:
    value = _text(node)
    return urljoin(base, html.unescape(value)) if value else ""


def _xml(body: str) -> ET.Element:
    try:
        return ET.fromstring(body)
    except ET.ParseError as exc:
        raise AppleOpsError("Apple returned an invalid DAV response") from exc


def _esc_ical(value: Any) -> str:
    return str(value or "").replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def _ical_time(value: Any) -> str:
    """Accept iCalendar basic values or common ISO-8601 timestamps."""
    text = str(value or "").strip()
    if not text:
        return ""
    if re.fullmatch(r"\d{8}T\d{6}Z?", text):
        return text
    try:
        parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.timezone.utc)
        return parsed.astimezone(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    except ValueError:
        return text


def _unesc_ical(value: str) -> str:
    return value.replace("\\n", "\n").replace("\\N", "\n").replace("\\,", ",").replace("\\;", ";").replace("\\\\", "\\")


def _unfold_ical(data: str) -> str:
    """Unfold RFC 5545 line-folded iCalendar data.

    Lines longer than 75 octets are wrapped with CRLF (or LF) followed by a
    single SP or HT.  Rejoin them before parsing so no property is truncated.
    """
    return re.sub(r"\r?\n[ \t]", "", data)


def _ical_props(data: str, component: str) -> Dict[str, str]:
    out: Dict[str, str] = {}
    inside = False
    unfolded = _unfold_ical(data.replace("\r\n", "\n").replace("\r", "\n"))
    for raw in unfolded.split("\n"):
        line = raw.strip()
        if line == f"BEGIN:{component}":
            inside = True
            continue
        if line == f"END:{component}":
            break
        if inside and ":" in line:
            key, value = line.split(":", 1)
            out[key.split(";", 1)[0].upper()] = _unesc_ical(value)
    return out


def _vcard_props(data: str) -> Dict[str, Any]:
    props: Dict[str, Any] = {}
    for raw in data.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        raw = raw.strip()
        if ":" not in raw or raw.startswith(("BEGIN:", "END:")):
            continue
        key, value = raw.split(":", 1)
        name = key.split(";", 1)[0].upper()
        value = value.strip()
        if name in props:
            if not isinstance(props[name], list):
                props[name] = [props[name]]
            props[name].append(value)
        else:
            props[name] = value
    return props


class AppleOps:
    def __init__(self, apple_id: str, app_password: str):
        self.apple_id = apple_id.strip()
        self.app_password = app_password.strip()
        if not self.apple_id or not self.app_password:
            raise AppleOpsError("Apple ID and app-specific password are required")
        self._client: Optional[httpx.AsyncClient] = None

    async def __aenter__(self) -> "AppleOps":
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(30.0, read=60.0), follow_redirects=False)
        return self

    async def __aexit__(self, *_exc: Any) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None

    @staticmethod
    def _allowed(url: str) -> bool:
        p = urlsplit(url)
        host = (p.hostname or "").lower().rstrip(".")
        return p.scheme == "https" and (
            host == "icloud.com" or host.endswith(".icloud.com")
        )

    async def _request(self, method: str, url: str, *, headers: Optional[Dict[str, str]] = None,
                       content: str = "", expected: Iterable[int] = (200, 207, 201, 204, 206)) -> httpx.Response:
        if not self._client or not self._allowed(url):
            raise AppleOpsError("Apple URL was outside the allowed iCloud domains")
        auth = base64.b64encode(f"{self.apple_id}:{self.app_password}".encode()).decode()
        request_headers = {"Authorization": f"Basic {auth}", "User-Agent": "OpenManus-Vault/1.0"}
        request_headers.update(headers or {})
        current = url
        response: Optional[httpx.Response] = None
        for _ in range(5):
            if not self._allowed(current):
                raise AppleOpsError("Apple redirect left the allowed iCloud domains")
            try:
                response = await self._client.request(method, current, headers=request_headers, content=content)
            except httpx.HTTPError as exc:
                raise AppleOpsError(f"Apple service is unreachable: {exc}") from exc
            if response.status_code not in (301, 302, 303, 307, 308):
                break
            location = response.headers.get("location")
            if not location:
                break
            current = urljoin(current, location)
        if response is None:
            raise AppleOpsError("Apple service returned no response")
        if response.status_code not in set(expected):
            if response.status_code in (401, 403):
                raise AppleOpsError("Apple rejected the credentials; use an iCloud app-specific password")
            raise AppleOpsError(f"Apple service returned HTTP {response.status_code}")
        return response

    async def _propfind(self, url: str, body: str, depth: str = "0") -> ET.Element:
        response = await self._request(
            "PROPFIND", url,
            headers={"Depth": depth, "Content-Type": "application/xml; charset=utf-8"},
            content=body, expected=(200, 207),
        )
        return _xml(response.text)

    async def _discover_caldav(self) -> str:
        body = f"""<?xml version="1.0"?>
<d:propfind xmlns:d="{DAV}" xmlns:c="{CALDAV}"><d:prop>
<d:current-user-principal/><c:calendar-home-set/>
</d:prop></d:propfind>"""
        root = await self._propfind("https://caldav.icloud.com/", body)
        principal = next((n for n in root.iter() if _local(n.tag) == "current-user-principal"), None)
        direct_home = next((n for n in root.iter() if _local(n.tag) == "calendar-home-set"), None)
        home = next((n for n in (direct_home.iter() if direct_home is not None else []) if _local(n.tag) == "href"), None)
        if home is not None:
            return _href(home, "https://caldav.icloud.com/")
        href = next((n for n in (principal.iter() if principal is not None else []) if _local(n.tag) == "href"), None)
        principal_url = _href(href, "https://caldav.icloud.com/")
        if not principal_url:
            raise AppleOpsError("Apple did not provide a CalDAV principal")
        root = await self._propfind(principal_url, body)
        home_node = next((n for n in root.iter() if _local(n.tag) == "calendar-home-set"), None)
        href = next((n for n in (home_node.iter() if home_node is not None else []) if _local(n.tag) == "href"), None)
        home_url = _href(href, principal_url)
        if not home_url:
            raise AppleOpsError("Apple did not provide a calendar home")
        return home_url

    async def _calendars(self) -> List[Dict[str, str]]:
        home = await self._discover_caldav()
        body = f"""<?xml version="1.0"?>
<d:propfind xmlns:d="{DAV}" xmlns:c="{CALDAV}"><d:prop>
<d:displayname/><d:resourcetype/><c:supported-calendar-component-set/>
</d:prop></d:propfind>"""
        root = await self._propfind(home, body, "1")
        result = []
        for response in root.iter():
            if _local(response.tag) != "response":
                continue
            href_node = next((n for n in response if _local(n.tag) == "href"), None)
            props = {_local(n.tag): n for n in response.iter()}
            href = _href(href_node, home)
            resource_type = props.get("resourcetype")
            resource_names = {
                _local(n.tag) for n in resource_type.iter()
            } if resource_type is not None else set()
            if not href or "calendar" not in resource_names:
                continue
            result.append({"url": href, "name": _text(props.get("displayname")) or href.rstrip("/").rsplit("/", 1)[-1]})
        return result

    async def _calendar_url(self, requested: str = "") -> str:
        if requested:
            if not self._allowed(requested):
                raise AppleOpsError("calendar_url must be an iCloud URL")
            return requested
        calendars = await self._calendars()
        if not calendars:
            raise AppleOpsError("No iCloud calendars were found")
        return calendars[0]["url"]

    async def _calendar_report(self, calendar_url: str, component: str, start: str = "", end: str = "") -> List[Dict[str, Any]]:
        # Normalise start/end to iCalendar UTC format expected by Apple's server.
        utc_start = _ical_time(start) if start else ""
        utc_end = _ical_time(end) if end else ""

        time_filter = ""
        if utc_start or utc_end:
            attrs = (f' start="{utc_start}"' if utc_start else "") + (f' end="{utc_end}"' if utc_end else "")
            time_filter = f'<c:time-range{attrs}/>'

        query_body = f"""<?xml version="1.0"?>
<c:calendar-query xmlns:d="{DAV}" xmlns:c="{CALDAV}"><d:prop>
<d:getetag/><c:calendar-data/></d:prop><c:filter><c:comp-filter name="VCALENDAR">
<c:comp-filter name="{component}">{time_filter}</c:comp-filter>
</c:comp-filter></c:filter></c:calendar-query>"""

        query_resp = await self._request(
            "REPORT", calendar_url,
            headers={"Depth": "1", "Content-Type": "application/xml; charset=utf-8"},
            content=query_body, expected=(207,),
        )
        parsed = _xml(query_resp.text)

        # Phase 1 — collect hrefs + any inline calendar-data from the query response.
        # Apple's iCloud sometimes returns HREFs without inline data; those hrefs
        # are collected for a follow-up multiget in phase 2.
        phase1: Dict[str, dict] = {}   # href -> {"etag": ..., "ical": ...}
        missing_hrefs: List[str] = []  # hrefs that need a multiget fetch

        for response in parsed.iter():
            if _local(response.tag) != "response":
                continue
            href_node = next((n for n in response if _local(n.tag) == "href"), None)
            href = _href(href_node, calendar_url)
            if not href or not href.lower().endswith(".ics"):
                continue
            etag = next((_text(n) for n in response.iter() if _local(n.tag) == "getetag"), "")
            data_node = next((n for n in response.iter() if _local(n.tag) == "calendar-data"), None)
            ical = _text(data_node) if data_node is not None else ""
            phase1[href] = {"etag": etag, "ical": ical}
            if not ical:
                missing_hrefs.append(href)

        # Phase 2 — multiget for any hrefs that came back without calendar-data.
        if missing_hrefs:
            href_elems = "".join(f"<d:href>{html.escape(h)}</d:href>" for h in missing_hrefs)
            multiget_body = f"""<?xml version="1.0"?>
<c:calendar-multiget xmlns:d="{DAV}" xmlns:c="{CALDAV}"><d:prop>
<d:getetag/><c:calendar-data/></d:prop>{href_elems}</c:calendar-multiget>"""
            try:
                mg_resp = await self._request(
                    "REPORT", calendar_url,
                    headers={"Depth": "1", "Content-Type": "application/xml; charset=utf-8"},
                    content=multiget_body, expected=(207,),
                )
                mg_parsed = _xml(mg_resp.text)
                for response in mg_parsed.iter():
                    if _local(response.tag) != "response":
                        continue
                    href_node = next((n for n in response if _local(n.tag) == "href"), None)
                    href = _href(href_node, calendar_url)
                    if not href or href not in phase1:
                        continue
                    data_node = next((n for n in response.iter() if _local(n.tag) == "calendar-data"), None)
                    ical = _text(data_node) if data_node is not None else ""
                    etag = next((_text(n) for n in response.iter() if _local(n.tag) == "getetag"), phase1[href]["etag"])
                    if ical:
                        phase1[href] = {"etag": etag, "ical": ical}
            except AppleOpsError:
                pass  # multiget failed — proceed with whatever phase 1 gave us

        # Phase 3 — parse iCal data and build result rows.
        _WANTED = {"UID", "SUMMARY", "DESCRIPTION", "DTSTART", "DTEND", "DUE", "STATUS", "LOCATION"}
        result = []
        for href, info in phase1.items():
            ical = info["ical"]
            if not ical:
                continue
            props = _ical_props(ical, component)
            if not props:
                # The .ics data exists but we couldn't parse any VEVENT/VTODO —
                # include the stub so the caller knows the href exists.
                result.append({"href": href, "etag": info["etag"]})
                continue
            result.append({
                "href": href,
                "etag": info["etag"],
                **{k.lower(): v for k, v in props.items() if k in _WANTED},
            })
        return result

    async def run(self, operation: str, args: Dict[str, Any]) -> Dict[str, Any]:
        op = (operation or "").strip().lower()
        limit = max(1, min(int(args.get("limit") or 50), 200))
        if op == "calendar_list":
            return {"calendars": (await self._calendars())[:limit]}
        if op in {"calendar_search", "reminders_list"}:
            component = "VTODO" if op == "reminders_list" else "VEVENT"
            rows = await self._calendar_report(
                await self._calendar_url(str(args.get("calendar_url") or "")), component,
                str(args.get("start") or ""), str(args.get("end") or ""),
            )
            # Upcoming events and reminders are most useful first.
            rows.sort(key=lambda x: x.get("dtstart") or x.get("due") or "")
            return {"events" if component == "VEVENT" else "reminders": rows[:limit]}
        if op in {"calendar_create", "reminders_create"}:
            component = "VTODO" if op == "reminders_create" else "VEVENT"
            uid = str(args.get("uid") or uuid.uuid4())
            now = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//OpenManus//Vault//EN", f"BEGIN:{component}", f"UID:{_esc_ical(uid)}", f"DTSTAMP:{now}"]
            for key in (("summary", "SUMMARY"), ("description", "DESCRIPTION"), ("location", "LOCATION")):
                if args.get(key[0]):
                    lines.append(f"{key[1]}:{_esc_ical(args[key[0]])}")
            for key in (("start", "DTSTART"), ("end", "DTEND"), ("due", "DUE")):
                if args.get(key[0]):
                    lines.append(f"{key[1]}:{_ical_time(args[key[0]])}")
            if component == "VTODO" and args.get("status"):
                lines.append(f"STATUS:{_esc_ical(args['status'])}")
            lines += [f"END:{component}", "END:VCALENDAR", ""]
            calendar = await self._calendar_url(str(args.get("calendar_url") or ""))
            url = urljoin(calendar.rstrip("/") + "/", re.sub(r"[^A-Za-z0-9_.-]", "", uid) + ".ics")
            response = await self._request("PUT", url, headers={"Content-Type": "text/calendar; charset=utf-8", "If-None-Match": "*"}, content="\r\n".join(lines), expected=(201, 204))
            return {"created": True, "uid": uid, "href": url, "status": response.status_code}
        if op in {"calendar_update", "reminders_complete"}:
            href = str(args.get("href") or "")
            if not href or not self._allowed(href):
                raise AppleOpsError("A valid iCloud event href is required")
            response = await self._request("GET", href, expected=(200,))
            component = "VTODO" if op == "reminders_complete" else "VEVENT"
            data = response.text
            if op == "reminders_complete":
                data = re.sub(r"(?m)^STATUS:[^\r\n]*", "STATUS:COMPLETED", data)
                if "STATUS:" not in data:
                    data = data.replace(f"BEGIN:{component}", f"BEGIN:{component}\r\nSTATUS:COMPLETED", 1)
            for key, label in (
                ("summary", "SUMMARY"), ("description", "DESCRIPTION"),
                ("location", "LOCATION"), ("start", "DTSTART"),
                ("end", "DTEND"), ("due", "DUE"),
            ):
                if key in args:
                    value = _ical_time(args[key]) if label in {"DTSTART", "DTEND", "DUE"} else _esc_ical(args[key])
                    data = re.sub(rf"(?m)^{label}(?:;[^\r\n:]*)?:[^\r\n]*", f"{label}:{value}", data)
                    if f"{label}:" not in data:
                        data = data.replace(f"BEGIN:{component}", f"BEGIN:{component}\r\n{label}:{value}", 1)
            headers = {"Content-Type": "text/calendar; charset=utf-8"}
            if args.get("etag"):
                headers["If-Match"] = str(args["etag"])
            await self._request("PUT", href, headers=headers, content=data, expected=(201, 204))
            return {"updated": True, "href": href}
        if op == "calendar_delete":
            href = str(args.get("href") or "")
            if not href or not self._allowed(href):
                raise AppleOpsError("A valid iCloud event href is required")
            await self._request("DELETE", href, headers={"If-Match": str(args["etag"])} if args.get("etag") else None, expected=(204,))
            return {"deleted": True, "href": href}
        if op in {"contacts_search", "contacts_read"}:
            return await self._contacts(op, args, limit)
        raise AppleOpsError("Unknown Apple operation")

    async def _contacts(self, op: str, args: Dict[str, Any], limit: int) -> Dict[str, Any]:
        body = f"""<?xml version="1.0"?><d:propfind xmlns:d="{DAV}" xmlns:card="{CARDDAV}"><d:prop><d:current-user-principal/><card:addressbook-home-set/></d:prop></d:propfind>"""
        root = await self._propfind("https://contacts.icloud.com/", body)
        home_node = next((n for n in root.iter() if _local(n.tag) == "addressbook-home-set"), None)
        href_node = next((n for n in (home_node.iter() if home_node is not None else []) if _local(n.tag) == "href"), None)
        home = _href(href_node, "https://contacts.icloud.com/")
        if not home:
            principal = next((n for n in root.iter() if _local(n.tag) == "current-user-principal"), None)
            principal_href = next((n for n in (principal.iter() if principal is not None else []) if _local(n.tag) == "href"), None)
            principal_url = _href(principal_href, "https://contacts.icloud.com/")
            if not principal_url:
                raise AppleOpsError("Apple did not provide a CardDAV address-book home")
            root = await self._propfind(principal_url, body)
            node = next((n for n in root.iter() if _local(n.tag) == "addressbook-home-set"), None)
            href_node = next((n for n in (node.iter() if node is not None else []) if _local(n.tag) == "href"), None)
            home = _href(href_node, principal_url)
        if not home:
            raise AppleOpsError("Apple did not provide a CardDAV address-book home")
        # The home is a collection of address books; REPORT must target an
        # address-book collection, not the home itself.
        books_body = f"""<?xml version="1.0"?><d:propfind xmlns:d="{DAV}" xmlns:card="{CARDDAV}"><d:prop><d:displayname/><d:resourcetype/></d:prop></d:propfind>"""
        books_root = await self._propfind(home, books_body, "1")
        addressbooks = []
        for response_node in books_root.iter():
            if _local(response_node.tag) != "response":
                continue
            response_href = next((n for n in response_node if _local(n.tag) == "href"), None)
            resource_type = next((n for n in response_node.iter() if _local(n.tag) == "resourcetype"), None)
            resource_names = {
                _local(n.tag) for n in resource_type.iter()
            } if resource_type is not None else set()
            if "addressbook" in resource_names:
                candidate = _href(response_href, home)
                if candidate:
                    addressbooks.append(candidate)
        if not addressbooks:
            raise AppleOpsError("No iCloud address books were found")
        addressbook = addressbooks[0]
        if op == "contacts_read":
            href = str(args.get("href") or "")
            if not href or not self._allowed(href):
                raise AppleOpsError("A valid iCloud contact href is required")
            response = await self._request("GET", href, expected=(200,))
            return {"contact": {"href": href, **_vcard_props(response.text)}}
        query = str(args.get("query") or "").lower()
        report = f"""<?xml version="1.0"?><card:addressbook-query xmlns:d="{DAV}" xmlns:card="{CARDDAV}"><d:prop><d:getetag/><card:address-data/></d:prop><card:filter><card:prop-filter name="FN"><card:text-match collation="i;unicode-casemap" match-type="contains">{html.escape(query)}</card:text-match></card:prop-filter></card:filter></card:addressbook-query>"""
        response = await self._request("REPORT", addressbook, headers={"Depth": "1", "Content-Type": "application/xml; charset=utf-8"}, content=report, expected=(207,))
        contacts = []
        parsed = _xml(response.text)
        for item in parsed.iter():
            if _local(item.tag) != "response":
                continue
            href_node = next((n for n in item if _local(n.tag) == "href"), None)
            data_node = next((n for n in item.iter() if _local(n.tag) == "address-data"), None)
            if data_node is not None:
                contacts.append({"href": _href(href_node, addressbook), **_vcard_props(_text(data_node))})
        contacts.sort(key=lambda x: str(x.get("FN", "")).lower())
        return {"contacts": contacts[:limit]}


async def run_apple_operation(apple_id: str, app_password: str, operation: str, args: Dict[str, Any]) -> Dict[str, Any]:
    async with AppleOps(apple_id, app_password) as client:
        return await client.run(operation, args)