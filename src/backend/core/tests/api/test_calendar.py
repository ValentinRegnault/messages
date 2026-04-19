"""Tests for calendar API views using a real in-process Radicale CalDAV server."""
# pylint: disable=redefined-outer-name, unused-argument, protected-access, missing-function-docstring

import shutil
import tempfile
import threading
from datetime import datetime, timedelta, timezone
from unittest import mock
from wsgiref.simple_server import WSGIRequestHandler, make_server

import pytest
import radicale.app
import radicale.config
import requests
from icalendar import Calendar as ICalendar

from core import factories
from core.enums import MailboxRoleChoices
from core.services.calendar.service import CalDAVService


class _SilentHandler(WSGIRequestHandler):
    """Suppress Radicale request logs during tests."""

    def log_message(self, format, *args):  # pylint: disable=redefined-builtin
        pass


@pytest.fixture()
def radicale_server():
    """Start a real Radicale CalDAV server in a background thread."""
    tmpdir = tempfile.mkdtemp()
    configuration = radicale.config.load()
    configuration.update(
        {
            "storage": {
                "filesystem_folder": tmpdir,
                "type": "multifilesystem_nolock",
            },
            "auth": {"type": "none"},
        },
        "test",
    )

    app = radicale.app.Application(configuration)
    server = make_server("localhost", 0, app, handler_class=_SilentHandler)
    port = server.server_address[1]

    thread = threading.Thread(target=server.serve_forever)
    thread.daemon = True
    thread.start()

    yield f"http://localhost:{port}"

    server.shutdown()
    thread.join(timeout=5)
    shutil.rmtree(tmpdir, ignore_errors=True)


RADICALE_USER = "testuser"
RADICALE_PASSWORD = "testpass"


def _mkcalendar(url, display_name, auth):
    """Create a calendar on a CalDAV server via MKCALENDAR."""
    body = (
        '<?xml version="1.0"?>'
        '<c:mkcalendar xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">'
        "<d:set><d:prop>"
        f"<d:displayname>{display_name}</d:displayname>"
        "</d:prop></d:set>"
        "</c:mkcalendar>"
    )
    resp = requests.request(
        "MKCALENDAR",
        url,
        data=body.encode("utf-8"),
        auth=auth,
        headers={"Content-Type": "application/xml; charset=utf-8"},
        timeout=5,
    )
    resp.raise_for_status()


def _put_event(calendar_url, uid, ics, auth):
    resp = requests.put(
        calendar_url.rstrip("/") + f"/{uid}.ics",
        data=ics.encode("utf-8"),
        auth=auth,
        headers={"Content-Type": "text/calendar; charset=utf-8"},
        timeout=5,
    )
    resp.raise_for_status()


@pytest.fixture()
def radicale_with_calendar(radicale_server):
    """Create a default calendar on the Radicale server.

    Returns (calendar_url, put_event_callable).
    """
    calendar_url = f"{radicale_server}/{RADICALE_USER}/test-cal/"
    _mkcalendar(
        calendar_url,
        "Test Calendar",
        auth=(RADICALE_USER, RADICALE_PASSWORD),
    )

    def put_event(uid, ics):
        _put_event(calendar_url, uid, ics, auth=(RADICALE_USER, RADICALE_PASSWORD))

    return calendar_url, put_event


@pytest.fixture()
def caldav_channel(radicale_server, mailbox):
    """Create a Channel of type caldav pointing at the Radicale server."""
    return factories.ChannelFactory(
        mailbox=mailbox,
        type="caldav",
        settings={
            "url": f"{radicale_server}/{RADICALE_USER}/",
            "username": RADICALE_USER,
            "password": RADICALE_PASSWORD,
        },
    )


@pytest.fixture()
def user_with_mailbox(mailbox):
    """Create a user with access to the mailbox."""
    user = factories.UserFactory()
    factories.MailboxAccessFactory(
        mailbox=mailbox,
        user=user,
        role=MailboxRoleChoices.ADMIN,
    )
    return user


SAMPLE_ICS = """\
BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Test//Test//EN
METHOD:REQUEST
BEGIN:VEVENT
UID:test-event-001@example.com
DTSTART:{dtstart}
DTEND:{dtend}
SUMMARY:Team Meeting
ORGANIZER:mailto:organizer@example.com
ATTENDEE;PARTSTAT=NEEDS-ACTION;RSVP=TRUE:mailto:{attendee}
END:VEVENT
END:VCALENDAR"""


def _make_ics(mailbox, dtstart=None, dtend=None):
    now = datetime.now(tz=timezone.utc)
    dtstart = dtstart or now + timedelta(hours=1)
    dtend = dtend or dtstart + timedelta(hours=1)
    return SAMPLE_ICS.format(
        dtstart=dtstart.strftime("%Y%m%dT%H%M%SZ"),
        dtend=dtend.strftime("%Y%m%dT%H%M%SZ"),
        attendee=str(mailbox),
    )


# ---------------------------------------------------------------------------
# Permission tests
# ---------------------------------------------------------------------------


@pytest.mark.django_db()
class TestCalendarPermissions:
    """Verify that calendar endpoints enforce mailbox access."""

    def test_anonymous_cannot_access(self, api_client, mailbox, caldav_channel):
        """Anonymous users are rejected."""
        base = f"/api/v1.0/mailboxes/{mailbox.id}/calendar"
        assert api_client.get(f"{base}/calendars/").status_code == 401
        assert api_client.post(f"{base}/conflicts/", {}).status_code == 401
        assert api_client.post(f"{base}/rsvp/", {}).status_code == 401
        assert api_client.post(f"{base}/add/", {}).status_code == 401

    def test_user_without_access_is_forbidden(
        self, api_client, mailbox, caldav_channel, other_user
    ):
        """Authenticated user without MailboxAccess is rejected."""
        api_client.force_authenticate(user=other_user)
        base = f"/api/v1.0/mailboxes/{mailbox.id}/calendar"
        assert api_client.get(f"{base}/calendars/").status_code == 403
        assert api_client.post(f"{base}/conflicts/", {}).status_code == 403
        assert api_client.post(f"{base}/rsvp/", {}).status_code == 403
        assert api_client.post(f"{base}/add/", {}).status_code == 403

    def test_user_with_access_is_allowed(
        self,
        api_client,
        mailbox,
        caldav_channel,
        user_with_mailbox,
        radicale_with_calendar,
    ):
        """Authenticated user with MailboxAccess can reach the endpoints."""
        api_client.force_authenticate(user=user_with_mailbox)
        base = f"/api/v1.0/mailboxes/{mailbox.id}/calendar"

        # calendars list should succeed
        resp = api_client.get(f"{base}/calendars/")
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Calendar list
# ---------------------------------------------------------------------------


@pytest.mark.django_db()
class TestCalendarListView:
    """Tests for the calendar list endpoint."""

    def test_list_calendars(
        self,
        api_client,
        mailbox,
        caldav_channel,
        user_with_mailbox,
        radicale_with_calendar,
    ):
        api_client.force_authenticate(user=user_with_mailbox)
        resp = api_client.get(f"/api/v1.0/mailboxes/{mailbox.id}/calendar/calendars/")
        assert resp.status_code == 200
        calendars = resp.json()["calendars"]
        assert len(calendars) >= 1
        assert any(c["name"] == "Test Calendar" for c in calendars)

    def test_list_calendars_no_channel(self, api_client, mailbox, user_with_mailbox):
        """Without a caldav channel, returns an empty list."""
        api_client.force_authenticate(user=user_with_mailbox)
        resp = api_client.get(f"/api/v1.0/mailboxes/{mailbox.id}/calendar/calendars/")
        assert resp.status_code == 200
        assert resp.json()["calendars"] == []


# ---------------------------------------------------------------------------
# Conflicts
# ---------------------------------------------------------------------------


@pytest.mark.django_db()
class TestCalendarConflictsView:
    """Tests for the calendar conflicts endpoint."""

    def test_check_conflicts_empty(
        self,
        api_client,
        mailbox,
        caldav_channel,
        user_with_mailbox,
        radicale_with_calendar,
    ):
        """No events => no conflicts."""
        api_client.force_authenticate(user=user_with_mailbox)
        now = datetime.now(tz=timezone.utc)
        resp = api_client.post(
            f"/api/v1.0/mailboxes/{mailbox.id}/calendar/conflicts/",
            {
                "start": (now + timedelta(hours=1)).isoformat(),
                "end": (now + timedelta(hours=2)).isoformat(),
            },
        )
        assert resp.status_code == 200
        assert resp.json()["conflicts"] == []

    def test_check_conflicts_with_event(
        self,
        api_client,
        mailbox,
        caldav_channel,
        user_with_mailbox,
        radicale_with_calendar,
    ):
        """An event in the time range is returned as a conflict."""
        _, put_event = radicale_with_calendar
        now = datetime.now(tz=timezone.utc)
        event_start = now + timedelta(hours=1)
        event_end = event_start + timedelta(hours=1)

        put_event(
            "conflict-test",
            f"""BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Test//Test//EN
BEGIN:VEVENT
UID:conflict-test@example.com
DTSTART:{event_start.strftime("%Y%m%dT%H%M%SZ")}
DTEND:{event_end.strftime("%Y%m%dT%H%M%SZ")}
SUMMARY:Existing Meeting
END:VEVENT
END:VCALENDAR""",
        )

        api_client.force_authenticate(user=user_with_mailbox)
        resp = api_client.post(
            f"/api/v1.0/mailboxes/{mailbox.id}/calendar/conflicts/",
            {
                "start": event_start.isoformat(),
                "end": event_end.isoformat(),
            },
        )
        assert resp.status_code == 200
        conflicts = resp.json()["conflicts"]
        assert len(conflicts) >= 1
        assert any("Existing Meeting" in c["summary"] for c in conflicts)

    def test_check_conflicts_missing_fields(
        self,
        api_client,
        mailbox,
        caldav_channel,
        user_with_mailbox,
    ):
        api_client.force_authenticate(user=user_with_mailbox)
        resp = api_client.post(
            f"/api/v1.0/mailboxes/{mailbox.id}/calendar/conflicts/",
            {},
        )
        assert resp.status_code == 400

    def test_check_conflicts_no_channel(self, api_client, mailbox, user_with_mailbox):
        """Without a caldav channel, returns 404."""
        api_client.force_authenticate(user=user_with_mailbox)
        now = datetime.now(tz=timezone.utc)
        resp = api_client.post(
            f"/api/v1.0/mailboxes/{mailbox.id}/calendar/conflicts/",
            {
                "start": now.isoformat(),
                "end": (now + timedelta(hours=1)).isoformat(),
            },
        )
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# RSVP (task-based – we call the task synchronously via .apply())
# ---------------------------------------------------------------------------


@pytest.mark.django_db()
class TestCalendarRsvpView:
    """Tests for the calendar RSVP endpoint."""

    def test_rsvp_accepted(
        self,
        api_client,
        mailbox,
        caldav_channel,
        user_with_mailbox,
        radicale_with_calendar,
    ):
        api_client.force_authenticate(user=user_with_mailbox)
        ics_data = _make_ics(mailbox)
        resp = api_client.post(
            f"/api/v1.0/mailboxes/{mailbox.id}/calendar/rsvp/",
            {
                "ics_data": ics_data,
                "response": "ACCEPTED",
            },
        )
        assert resp.status_code == 200
        assert "task_id" in resp.json()

    def test_rsvp_declined(
        self,
        api_client,
        mailbox,
        caldav_channel,
        user_with_mailbox,
        radicale_with_calendar,
    ):
        api_client.force_authenticate(user=user_with_mailbox)
        ics_data = _make_ics(mailbox)
        resp = api_client.post(
            f"/api/v1.0/mailboxes/{mailbox.id}/calendar/rsvp/",
            {
                "ics_data": ics_data,
                "response": "DECLINED",
            },
        )
        assert resp.status_code == 200

    def test_rsvp_invalid_response(
        self,
        api_client,
        mailbox,
        caldav_channel,
        user_with_mailbox,
    ):
        api_client.force_authenticate(user=user_with_mailbox)
        resp = api_client.post(
            f"/api/v1.0/mailboxes/{mailbox.id}/calendar/rsvp/",
            {
                "ics_data": "BEGIN:VCALENDAR\nEND:VCALENDAR",
                "response": "INVALID",
            },
        )
        assert resp.status_code == 400

    def test_rsvp_missing_fields(
        self,
        api_client,
        mailbox,
        caldav_channel,
        user_with_mailbox,
    ):
        api_client.force_authenticate(user=user_with_mailbox)
        resp = api_client.post(
            f"/api/v1.0/mailboxes/{mailbox.id}/calendar/rsvp/",
            {},
        )
        assert resp.status_code == 400

    def test_rsvp_no_channel(self, api_client, mailbox, user_with_mailbox):
        """Without a caldav channel, returns 404 and does NOT schedule a task."""
        api_client.force_authenticate(user=user_with_mailbox)
        with mock.patch("core.api.viewsets.calendar.calendar_rsvp_task.delay") as delay:
            resp = api_client.post(
                f"/api/v1.0/mailboxes/{mailbox.id}/calendar/rsvp/",
                {
                    "ics_data": _make_ics(mailbox),
                    "response": "ACCEPTED",
                },
            )
        assert resp.status_code == 404
        delay.assert_not_called()


# ---------------------------------------------------------------------------
# Add event
# ---------------------------------------------------------------------------


@pytest.mark.django_db()
class TestCalendarAddEventView:
    """Tests for the calendar add-event endpoint."""

    def test_add_event(
        self,
        api_client,
        mailbox,
        caldav_channel,
        user_with_mailbox,
        radicale_with_calendar,
    ):
        api_client.force_authenticate(user=user_with_mailbox)
        ics_data = _make_ics(mailbox)
        resp = api_client.post(
            f"/api/v1.0/mailboxes/{mailbox.id}/calendar/add/",
            {"ics_data": ics_data},
        )
        assert resp.status_code == 200
        assert "task_id" in resp.json()

    def test_add_event_missing_ics(
        self,
        api_client,
        mailbox,
        caldav_channel,
        user_with_mailbox,
    ):
        api_client.force_authenticate(user=user_with_mailbox)
        resp = api_client.post(
            f"/api/v1.0/mailboxes/{mailbox.id}/calendar/add/",
            {},
        )
        assert resp.status_code == 400

    def test_add_event_no_channel(self, api_client, mailbox, user_with_mailbox):
        """Without a caldav channel, returns 404 and does NOT schedule a task."""
        api_client.force_authenticate(user=user_with_mailbox)
        with mock.patch(
            "core.api.viewsets.calendar.calendar_add_event_task.delay"
        ) as delay:
            resp = api_client.post(
                f"/api/v1.0/mailboxes/{mailbox.id}/calendar/add/",
                {"ics_data": _make_ics(mailbox)},
            )
        assert resp.status_code == 404
        delay.assert_not_called()


# ---------------------------------------------------------------------------
# Instance-level CalDAV config (no per-mailbox channel)
# ---------------------------------------------------------------------------


# Static shared secret sent as the Basic Auth password. Radicale with
# auth.type=none ignores the actual value; production servers verify it.
INSTANCE_CALDAV_PASSWORD = "stub-shared-secret"


@pytest.fixture()
def instance_caldav_config(radicale_server, settings):
    """Configure instance-level CalDAV settings pointing at Radicale.

    CALDAV_DEFAULT_URL is the CalDAV server root — the service resolves the
    per-user calendar-home-set via principal discovery, using the mailbox
    email as the Basic Auth username.
    """
    settings.CALDAV_DEFAULT_URL = f"{radicale_server}/"
    settings.CALDAV_DEFAULT_PASSWORD = INSTANCE_CALDAV_PASSWORD


@pytest.fixture()
def instance_calendar(radicale_server, mailbox):
    """Create a calendar under the mailbox's principal on Radicale.

    Radicale with ``auth.type=none`` treats the Basic Auth username as the
    principal name and serves calendars under ``/{username}/``. Since the
    service now uses the mailbox email as the Basic Auth username, the
    calendar must live under that path.
    """
    cal_url = f"{radicale_server}/{mailbox}/instance-cal/"
    _mkcalendar(cal_url, "Instance Calendar", auth=(str(mailbox), "ignored"))
    return radicale_server


@pytest.mark.django_db()
class TestCalendarInstanceConfig:
    """Tests using instance-level CalDAV settings instead of a per-mailbox Channel."""

    def test_list_calendars(
        self,
        api_client,
        mailbox,
        user_with_mailbox,
        instance_caldav_config,
        instance_calendar,
    ):
        api_client.force_authenticate(user=user_with_mailbox)
        resp = api_client.get(f"/api/v1.0/mailboxes/{mailbox.id}/calendar/calendars/")
        assert resp.status_code == 200
        assert len(resp.json()["calendars"]) >= 1

    def test_conflicts(
        self,
        api_client,
        mailbox,
        user_with_mailbox,
        instance_caldav_config,
        instance_calendar,
    ):
        api_client.force_authenticate(user=user_with_mailbox)
        now = datetime.now(tz=timezone.utc)
        resp = api_client.post(
            f"/api/v1.0/mailboxes/{mailbox.id}/calendar/conflicts/",
            {
                "start": (now + timedelta(hours=1)).isoformat(),
                "end": (now + timedelta(hours=2)).isoformat(),
            },
        )
        assert resp.status_code == 200

    def test_rsvp(
        self,
        api_client,
        mailbox,
        user_with_mailbox,
        instance_caldav_config,
        instance_calendar,
    ):
        api_client.force_authenticate(user=user_with_mailbox)
        resp = api_client.post(
            f"/api/v1.0/mailboxes/{mailbox.id}/calendar/rsvp/",
            {"ics_data": _make_ics(mailbox), "response": "ACCEPTED"},
        )
        assert resp.status_code == 200
        assert "task_id" in resp.json()

    def test_add_event(
        self,
        api_client,
        mailbox,
        user_with_mailbox,
        instance_caldav_config,
        instance_calendar,
    ):
        api_client.force_authenticate(user=user_with_mailbox)
        resp = api_client.post(
            f"/api/v1.0/mailboxes/{mailbox.id}/calendar/add/",
            {"ics_data": _make_ics(mailbox)},
        )
        assert resp.status_code == 200
        assert "task_id" in resp.json()

    def test_channel_overrides_instance_config(
        self,
        api_client,
        mailbox,
        user_with_mailbox,
        caldav_channel,
        instance_caldav_config,
        radicale_with_calendar,
    ):
        """Per-mailbox channel takes precedence over instance config."""
        api_client.force_authenticate(user=user_with_mailbox)
        resp = api_client.get(f"/api/v1.0/mailboxes/{mailbox.id}/calendar/calendars/")
        assert resp.status_code == 200
        assert len(resp.json()["calendars"]) >= 1

    def test_no_config_returns_empty_calendars(
        self, api_client, mailbox, user_with_mailbox
    ):
        """Without channel or instance config, calendar list returns []."""
        api_client.force_authenticate(user=user_with_mailbox)
        resp = api_client.get(f"/api/v1.0/mailboxes/{mailbox.id}/calendar/calendars/")
        assert resp.status_code == 200
        assert resp.json()["calendars"] == []

    def test_no_config_returns_404_for_actions(
        self, api_client, mailbox, user_with_mailbox
    ):
        """Without channel or instance config, action endpoints return 404."""
        api_client.force_authenticate(user=user_with_mailbox)
        base = f"/api/v1.0/mailboxes/{mailbox.id}/calendar"
        assert (
            api_client.post(
                f"{base}/rsvp/",
                {"ics_data": _make_ics(mailbox), "response": "ACCEPTED"},
            ).status_code
            == 404
        )
        assert (
            api_client.post(
                f"{base}/add/", {"ics_data": _make_ics(mailbox)}
            ).status_code
            == 404
        )
        now = datetime.now(tz=timezone.utc)
        assert (
            api_client.post(
                f"{base}/conflicts/",
                {
                    "start": now.isoformat(),
                    "end": (now + timedelta(hours=1)).isoformat(),
                },
            ).status_code
            == 404
        )


# ---------------------------------------------------------------------------
# Unit tests for CalDAVService helpers (no network)
# ---------------------------------------------------------------------------


class TestUpdatePartstat:
    """Direct tests for PARTSTAT rewriting, independent of any CalDAV server."""

    def _build(
        self,
        attendee_line="ATTENDEE;PARTSTAT=NEEDS-ACTION;RSVP=TRUE:mailto:me@example.com",
    ):
        ics = (
            "BEGIN:VCALENDAR\r\n"
            "VERSION:2.0\r\n"
            "PRODID:-//Test//Test//EN\r\n"
            "BEGIN:VEVENT\r\n"
            "UID:x@example.com\r\n"
            "DTSTART:20260101T120000Z\r\n"
            "DTEND:20260101T130000Z\r\n"
            "SUMMARY:X\r\n"
            f"{attendee_line}\r\n"
            "END:VEVENT\r\n"
            "END:VCALENDAR\r\n"
        )
        return ICalendar.from_ical(ics)

    @staticmethod
    def _attendee(cal):
        vevent = cal.walk("VEVENT")[0]
        att = vevent.get("ATTENDEE")
        if isinstance(att, list):
            att = att[0]
        return att

    def test_updates_existing_partstat_and_drops_rsvp(self):
        cal = self._build()
        CalDAVService._update_partstat(cal, "me@example.com", "ACCEPTED")
        att = self._attendee(cal)
        assert att.params["PARTSTAT"] == "ACCEPTED"
        assert "RSVP" not in att.params

    def test_adds_partstat_when_missing(self):
        cal = self._build("ATTENDEE:mailto:me@example.com")
        CalDAVService._update_partstat(cal, "me@example.com", "DECLINED")
        att = self._attendee(cal)
        assert att.params["PARTSTAT"] == "DECLINED"

    def test_case_insensitive_email_match(self):
        cal = self._build("ATTENDEE:mailto:ME@Example.COM")
        CalDAVService._update_partstat(cal, "me@example.com", "TENTATIVE")
        att = self._attendee(cal)
        assert att.params["PARTSTAT"] == "TENTATIVE"

    def test_leaves_other_attendees_untouched(self):
        ics = (
            "BEGIN:VCALENDAR\r\n"
            "VERSION:2.0\r\n"
            "PRODID:-//Test//Test//EN\r\n"
            "BEGIN:VEVENT\r\n"
            "UID:x@example.com\r\n"
            "DTSTART:20260101T120000Z\r\n"
            "DTEND:20260101T130000Z\r\n"
            "SUMMARY:X\r\n"
            "ATTENDEE;PARTSTAT=NEEDS-ACTION:mailto:other@example.com\r\n"
            "ATTENDEE;PARTSTAT=NEEDS-ACTION;RSVP=TRUE:mailto:me@example.com\r\n"
            "END:VEVENT\r\n"
            "END:VCALENDAR\r\n"
        )
        cal = ICalendar.from_ical(ics)
        CalDAVService._update_partstat(cal, "me@example.com", "ACCEPTED")

        attendees = cal.walk("VEVENT")[0].get("ATTENDEE")
        by_email = {str(a).lower(): a for a in attendees}
        assert by_email["mailto:me@example.com"].params["PARTSTAT"] == "ACCEPTED"
        assert by_email["mailto:other@example.com"].params["PARTSTAT"] == "NEEDS-ACTION"


# ---------------------------------------------------------------------------
# Credential contract: Basic Auth user = mailbox email, password = setting
# ---------------------------------------------------------------------------


@pytest.mark.django_db()
def test_instance_config_sends_mailbox_email_as_basic_auth_user(settings, mailbox):
    """Instance-level auth: Basic Auth user must be the mailbox email and
    the password must be CALDAV_DEFAULT_PASSWORD verbatim."""
    settings.CALDAV_DEFAULT_URL = "https://caldav.example.com/"
    settings.CALDAV_DEFAULT_PASSWORD = "shared-secret-xyz"

    service = CalDAVService.from_instance_config(str(mailbox))

    assert service.username == str(mailbox)
    assert service.password == "shared-secret-xyz"
    assert service.session.auth == (str(mailbox), "shared-secret-xyz")
