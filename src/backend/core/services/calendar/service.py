"""Minimal CalDAV client for calendar invite management.

Implements only the CalDAV operations we need (list calendars, search
events, add event, RSVP) directly over HTTP using ``requests`` and
``icalendar`` for parsing, rather than pulling in the full ``caldav``
library's dependency surface.
"""

import logging
import uuid
from datetime import timezone
from urllib.parse import urljoin

from django.conf import settings as django_settings

import defusedxml.ElementTree as ET
import requests
from icalendar import Calendar as ICalendar

logger = logging.getLogger(__name__)

CALDAV_TIMEOUT = 20

DAV_NS = "DAV:"
CALDAV_NS = "urn:ietf:params:xml:ns:caldav"


def _q(ns, tag):
    return f"{{{ns}}}{tag}"


def _format_utc(dt):
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc)
    return dt.strftime("%Y%m%dT%H%M%SZ")


class CalDAVError(Exception):
    """CalDAV protocol or server error."""


class CalDAVService:
    """Minimal CalDAV client (HTTP + icalendar, no full caldav lib)."""

    def __init__(self, url, username="", password="", headers=None):
        self.url = url
        self.username = username
        self.password = password
        self.extra_headers = headers or {}
        self._session = None
        self._home_set = None

    @property
    def session(self):
        """Lazily-created ``requests.Session`` with auth and headers applied."""
        if self._session is None:
            s = requests.Session()
            if self.username or self.password:
                s.auth = (self.username, self.password)
            s.headers.update(self.extra_headers)
            self._session = s
        return self._session

    def _request(self, method, url, **kwargs):
        kwargs.setdefault("timeout", CALDAV_TIMEOUT)
        resp = self.session.request(method, url, **kwargs)
        if resp.status_code >= 400:
            raise CalDAVError(f"{method} {url} failed: HTTP {resp.status_code}")
        return resp

    def _propfind(self, url, body, depth="0"):
        return self._request(
            "PROPFIND",
            url,
            data=body.encode("utf-8"),
            headers={
                "Depth": str(depth),
                "Content-Type": "application/xml; charset=utf-8",
            },
        )

    @property
    def home_set(self):
        """Calendar-home-set URL, resolved lazily via principal discovery.

        Falls back to the configured URL if discovery fails (for servers or
        URLs that already point directly at the home set).
        """
        if self._home_set is not None:
            return self._home_set
        try:
            self._home_set = self._discover_home_set() or self.url
        except Exception:  # pylint: disable=broad-exception-caught
            logger.debug(
                "home-set discovery failed for %s, using URL directly",
                self.url,
                exc_info=True,
            )
            self._home_set = self.url
        return self._home_set

    def _discover_home_set(self):
        body = (
            '<?xml version="1.0"?>'
            '<d:propfind xmlns:d="DAV:">'
            "<d:prop><d:current-user-principal/></d:prop>"
            "</d:propfind>"
        )
        root = ET.fromstring(self._propfind(self.url, body, depth="0").text)
        principal_href = root.findtext(
            f".//{_q(DAV_NS, 'current-user-principal')}/{_q(DAV_NS, 'href')}"
        )
        principal_url = (
            urljoin(self.url, principal_href.strip()) if principal_href else self.url
        )

        body = (
            '<?xml version="1.0"?>'
            '<d:propfind xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">'
            "<d:prop><c:calendar-home-set/></d:prop>"
            "</d:propfind>"
        )
        root = ET.fromstring(self._propfind(principal_url, body, depth="0").text)
        home_href = root.findtext(
            f".//{_q(CALDAV_NS, 'calendar-home-set')}/{_q(DAV_NS, 'href')}"
        )
        if not home_href:
            return principal_url
        return urljoin(self.url, home_href.strip())

    def list_calendars(self):
        """List all calendars with a single PROPFIND depth=1 (no N+1)."""
        body = (
            '<?xml version="1.0"?>'
            '<d:propfind xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">'
            "<d:prop><d:displayname/><d:resourcetype/></d:prop>"
            "</d:propfind>"
        )
        root = ET.fromstring(self._propfind(self.home_set, body, depth="1").text)
        result = []
        for response in root.findall(_q(DAV_NS, "response")):
            href = response.findtext(_q(DAV_NS, "href"))
            if not href:
                continue
            href = href.strip()
            rtype = response.find(f".//{_q(DAV_NS, 'resourcetype')}")
            if rtype is None or rtype.find(_q(CALDAV_NS, "calendar")) is None:
                continue
            displayname = response.findtext(f".//{_q(DAV_NS, 'displayname')}") or href
            result.append({"id": urljoin(self.url, href), "name": displayname.strip()})
        return result

    def check_conflicts(self, start, end):
        """List events overlapping [start, end] across all calendars."""
        conflicts = []
        for cal in self.list_calendars():
            try:
                events = self._calendar_query(cal["id"], start, end)
            except CalDAVError:
                logger.exception(
                    "Error searching for conflicts on calendar %s", cal["name"]
                )
                continue
            for ics_text in events:
                summary = self._summarize_event(ics_text, cal["name"])
                if summary is not None:
                    conflicts.append(summary)
        return conflicts

    def _calendar_query(self, calendar_url, start, end):
        body = (
            '<?xml version="1.0"?>'
            '<c:calendar-query xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">'
            "<d:prop><c:calendar-data/></d:prop>"
            "<c:filter>"
            '<c:comp-filter name="VCALENDAR">'
            '<c:comp-filter name="VEVENT">'
            f'<c:time-range start="{_format_utc(start)}" end="{_format_utc(end)}"/>'
            "</c:comp-filter>"
            "</c:comp-filter>"
            "</c:filter>"
            "</c:calendar-query>"
        )
        resp = self._request(
            "REPORT",
            calendar_url,
            data=body.encode("utf-8"),
            headers={
                "Depth": "1",
                "Content-Type": "application/xml; charset=utf-8",
            },
        )
        root = ET.fromstring(resp.text)
        return [
            r.findtext(f".//{_q(CALDAV_NS, 'calendar-data')}") or ""
            for r in root.findall(_q(DAV_NS, "response"))
            if r.findtext(f".//{_q(CALDAV_NS, 'calendar-data')}")
        ]

    @staticmethod
    def _summarize_event(ics_text, calendar_name):
        try:
            cal = ICalendar.from_ical(ics_text)
        except Exception:  # pylint: disable=broad-exception-caught
            logger.warning("Could not parse conflicting event", exc_info=True)
            return None
        for comp in cal.walk("VEVENT"):
            dtstart = comp.get("DTSTART")
            dtend = comp.get("DTEND")
            return {
                "summary": str(comp.get("SUMMARY") or "Untitled event"),
                "start": dtstart.dt.isoformat() if dtstart else None,
                "end": dtend.dt.isoformat() if dtend else None,
                "calendar_name": calendar_name,
            }
        return None

    def add_event(self, ics_data, calendar_id=None):
        """Store an event on the selected calendar (or the default one)."""
        self._put_event(self._pick_calendar_url(calendar_id), ics_data)
        return True

    def respond_to_event(self, ics_data, response, attendee_email, calendar_id=None):
        """Store an RSVP'd copy of the event on the user's calendar.

        This does NOT send a REPLY back to the organizer — iTIP REPLY
        delivery is expected to be handled by the calendar backend's own
        scheduling inbox (via the CalDAV scheduling extensions).
        """
        cal = ICalendar.from_ical(ics_data)
        self._update_partstat(cal, attendee_email, response)

        if response == "DECLINED":
            return True

        # A stored calendar event must not carry a scheduling METHOD.
        if "METHOD" in cal:
            del cal["METHOD"]

        self._put_event(
            self._pick_calendar_url(calendar_id),
            cal.to_ical().decode("utf-8"),
        )
        return True

    @staticmethod
    def _update_partstat(cal, attendee_email, new_partstat):
        """Update PARTSTAT (and drop RSVP=TRUE) for the given attendee, in-place."""
        email_lower = attendee_email.lower()
        for comp in cal.walk("VEVENT"):
            attendees = comp.get("ATTENDEE")
            if attendees is None:
                continue
            if not isinstance(attendees, list):
                attendees = [attendees]
            for att in attendees:
                if email_lower not in str(att).lower():
                    continue
                att.params["PARTSTAT"] = new_partstat
                att.params.pop("RSVP", None)

    def _pick_calendar_url(self, calendar_id):
        if calendar_id:
            return calendar_id
        calendars = self.list_calendars()
        if not calendars:
            raise CalDAVError("No calendars available on this CalDAV server.")
        return calendars[0]["id"]

    def _put_event(self, calendar_url, ics_data):
        uid = ""
        try:
            cal = ICalendar.from_ical(ics_data)
            for comp in cal.walk("VEVENT"):
                uid = str(comp.get("UID") or "")
                break
        except Exception:  # pylint: disable=broad-exception-caught
            logger.debug("Could not extract UID from ICS, using random", exc_info=True)
        if not uid:
            uid = str(uuid.uuid4())

        event_url = calendar_url.rstrip("/") + f"/{uid}.ics"
        data = ics_data.encode("utf-8") if isinstance(ics_data, str) else ics_data
        self._request(
            "PUT",
            event_url,
            data=data,
            headers={"Content-Type": "text/calendar; charset=utf-8"},
        )

    @classmethod
    def from_channel(cls, channel):
        """Create a CalDAVService from a Channel model instance.

        The channel settings should contain:
        - url: CalDAV server URL
        - username: Authentication username
        - password: Authentication password
        """
        settings = channel.settings
        url = settings.get("url")
        if not url:
            raise ValueError("CalDAV channel is missing 'url' in settings.")
        return cls(
            url=url,
            username=settings.get("username") or "",
            password=settings.get("password") or "",
        )

    @classmethod
    def from_instance_config(cls, username):
        """Create a CalDAVService from instance-level Django settings.

        Authenticates with HTTP Basic Auth: ``username`` is the acting
        mailbox email (passed per-request), and the password is the
        static ``CALDAV_DEFAULT_PASSWORD`` shared secret. This lets the
        CalDAV server resolve the user's calendars via principal
        discovery.
        """
        url = django_settings.CALDAV_DEFAULT_URL
        password = django_settings.CALDAV_DEFAULT_PASSWORD
        if not url or not password:
            raise ValueError(
                "Instance-level CalDAV is not configured "
                "(CALDAV_DEFAULT_URL and CALDAV_DEFAULT_PASSWORD are required)."
            )
        return cls(url=url, username=username, password=password)

    @classmethod
    def from_channel_or_instance(cls, channel, username):
        """Prefer a per-mailbox channel, falling back to instance-level config.

        ``username`` is the acting mailbox email, only used by the
        instance-level path (per-channel credentials are self-contained).
        Returns None if neither is available.
        """
        if channel:
            return cls.from_channel(channel)

        if (
            django_settings.CALDAV_DEFAULT_URL
            and django_settings.CALDAV_DEFAULT_PASSWORD
        ):
            return cls.from_instance_config(username)

        return None
