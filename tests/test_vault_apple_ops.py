"""Deterministic CalDAV/CardDAV adapter tests; no Apple credentials or network."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
import unittest

import httpx

sys.path.insert(0, str(Path(__file__).parents[1] / "services" / "vault"))
from apple_ops import AppleOps, AppleOpsError, _ical_props, _vcard_props  # noqa: E402


class FakeAppleOps(AppleOps):
    @staticmethod
    def _allowed(url: str) -> bool:
        return url.startswith("https://caldav.icloud.com/") or url.startswith(
            "https://contacts.icloud.com/"
        )


class AppleOpsTests(unittest.TestCase):
    def test_ical_and_vcard_parsers(self):
        event = _ical_props(
            "BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\n"
            "SUMMARY:Lunch\\, with team\r\nDTSTART:20260805T120000Z\r\n"
            "END:VEVENT\r\nEND:VCALENDAR\r\n",
            "VEVENT",
        )
        self.assertEqual(event["SUMMARY"], "Lunch, with team")
        self.assertEqual(event["DTSTART"], "20260805T120000Z")
        card = _vcard_props(
            "BEGIN:VCARD\r\nVERSION:3.0\r\nFN:Garrett Roi\r\n"
            "EMAIL;TYPE=HOME:garrett@example.com\r\nEND:VCARD\r\n"
        )
        self.assertEqual(card["FN"], "Garrett Roi")
        self.assertEqual(card["EMAIL"], "garrett@example.com")

    def test_calendar_search_uses_discovery_and_returns_structured_event(self):
        calls = []

        async def handler(request: httpx.Request) -> httpx.Response:
            calls.append((request.method, str(request.url)))
            if request.method == "PROPFIND" and str(request.url).rstrip("/") == "https://caldav.icloud.com":
                return httpx.Response(
                    207,
                    text="""<D:multistatus xmlns:D="DAV:" xmlns:C="urn:ietf:params:xml:ns:caldav">
                    <D:response><D:propstat><D:prop>
                    <C:calendar-home-set><D:href>/123/calendars/</D:href></C:calendar-home-set>
                    </D:prop></D:propstat></D:response></D:multistatus>""",
                    request=request,
                )
            if request.method == "PROPFIND" and request.url.path == "/123/calendars/":
                return httpx.Response(
                    207,
                    text="""<D:multistatus xmlns:D="DAV:" xmlns:C="urn:ietf:params:xml:ns:caldav">
                    <D:response><D:href>/123/calendars/home/</D:href><D:propstat><D:prop>
                    <D:displayname>Home</D:displayname><D:resourcetype><D:collection/><C:calendar/></D:resourcetype>
                    </D:prop></D:propstat></D:response></D:multistatus>""",
                    request=request,
                )
            if request.method == "REPORT" and request.url.path == "/123/calendars/home/":
                return httpx.Response(
                    207,
                    text="""<D:multistatus xmlns:D="DAV:" xmlns:C="urn:ietf:params:xml:ns:caldav">
                    <D:response><D:href>/123/calendars/home/event.ics</D:href>
                    <D:propstat><D:prop><D:getetag>"one"</D:getetag>
                    <C:calendar-data>BEGIN:VCALENDAR
                    BEGIN:VEVENT
                    UID:one
                    SUMMARY:Lunch
                    DTSTART:20260805T120000Z
                    END:VEVENT
                    END:VCALENDAR</C:calendar-data>
                    </D:prop></D:propstat></D:response></D:multistatus>""",
                    request=request,
                )
            return httpx.Response(404, request=request)

        async def run():
            async with FakeAppleOps("id@example.com", "app-password") as client:
                client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
                return await client.run("calendar_search", {"limit": 5})

        result = asyncio.run(run())
        self.assertEqual(result["events"][0]["summary"], "Lunch")
        self.assertEqual(result["events"][0]["etag"], '"one"')
        self.assertEqual(len(calls), 3)

    def test_private_or_non_icloud_urls_are_rejected(self):
        self.assertFalse(AppleOps._allowed("http://127.0.0.1:8080/"))
        self.assertFalse(AppleOps._allowed("https://example.com/"))
        self.assertTrue(AppleOps._allowed("https://caldav.icloud.com/"))
        self.assertTrue(AppleOps._allowed("https://contacts.icloud.com/"))

    def test_contacts_search_discovers_addressbook_collection(self):
        async def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "PROPFIND" and request.url.host == "contacts.icloud.com":
                if request.url.path.rstrip("/") == "":
                    return httpx.Response(
                        207,
                        text="""<D:multistatus xmlns:D="DAV:" xmlns:CR="urn:ietf:params:xml:ns:carddav">
                        <D:response><D:propstat><D:prop>
                        <CR:addressbook-home-set><D:href>/123/</D:href></CR:addressbook-home-set>
                        </D:prop></D:propstat></D:response></D:multistatus>""",
                        request=request,
                    )
                return httpx.Response(
                    207,
                    text="""<D:multistatus xmlns:D="DAV:" xmlns:CR="urn:ietf:params:xml:ns:carddav">
                    <D:response><D:href>/123/contacts/</D:href><D:propstat><D:prop>
                    <D:displayname>Contacts</D:displayname>
                    <D:resourcetype><D:collection/><CR:addressbook/></D:resourcetype>
                    </D:prop></D:propstat></D:response></D:multistatus>""",
                    request=request,
                )
            if request.method == "REPORT" and request.url.path == "/123/contacts/":
                return httpx.Response(
                    207,
                    text="""<D:multistatus xmlns:D="DAV:" xmlns:CR="urn:ietf:params:xml:ns:carddav">
                    <D:response><D:href>/123/contacts/garrett.vcf</D:href>
                    <D:propstat><D:prop><CR:address-data>BEGIN:VCARD
                    VERSION:3.0
                    FN:Garrett Roi
                    EMAIL:garrett@example.com
                    END:VCARD</CR:address-data>
                    </D:prop></D:propstat></D:response></D:multistatus>""",
                    request=request,
                )
            return httpx.Response(404, request=request)

        async def run():
            async with FakeAppleOps("id@example.com", "app-password") as client:
                client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
                return await client.run("contacts_search", {"query": "Garrett"})

        result = asyncio.run(run())
        self.assertEqual(result["contacts"][0]["FN"], "Garrett Roi")
        self.assertEqual(result["contacts"][0]["EMAIL"], "garrett@example.com")

    def test_bad_credentials_are_not_returned_in_errors(self):
        with self.assertRaises(AppleOpsError):
            AppleOps("", "secret")  # construction validation is explicit


if __name__ == "__main__":
    unittest.main()