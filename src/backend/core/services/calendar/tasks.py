"""Celery tasks for CalDAV calendar operations."""

from typing import Any, Dict

from celery.utils.log import get_task_logger
from sentry_sdk import capture_exception

from core.models import Channel
from core.services.calendar.service import CalDAVService

from messages.celery_app import app as celery_app

logger = get_task_logger(__name__)


def _get_caldav_service(channel_id: str | None, mailbox_email: str):
    """Build a CalDAVService from a channel ID or instance-level config.

    ``mailbox_email`` is the acting mailbox's email. It is the Basic Auth
    username for the instance-level path; per-channel auth is
    self-contained so the email is ignored there.
    """
    if channel_id:
        channel = Channel.objects.get(id=channel_id, type="caldav")
        return CalDAVService.from_channel(channel)

    return CalDAVService.from_instance_config(mailbox_email)


@celery_app.task(bind=True)
def calendar_rsvp_task(
    self,  # pylint: disable=unused-argument
    channel_id: str | None,
    mailbox_email: str,
    ics_data: str,
    response: str,
    attendee_email: str,
    calendar_id: str | None = None,
) -> Dict[str, Any]:
    """
    Respond to a calendar event via CalDAV (RSVP).

    Args:
        channel_id: UUID of the CalDAV channel, or None for instance config
        mailbox_email: Acting mailbox email (Basic Auth user for instance config)
        ics_data: Raw ICS content
        response: ACCEPTED, DECLINED, or TENTATIVE
        attendee_email: Email of the responding attendee
        calendar_id: Optional specific calendar URL to use
    """
    try:
        service = _get_caldav_service(channel_id, mailbox_email)
    except Channel.DoesNotExist:
        return {
            "status": "FAILURE",
            "result": None,
            "error": "CalDAV channel not found.",
        }
    except ValueError as e:
        return {
            "status": "FAILURE",
            "result": None,
            "error": str(e),
        }

    try:
        service.respond_to_event(
            ics_data=ics_data,
            response=response,
            attendee_email=attendee_email,
            calendar_id=calendar_id,
        )

        return {
            "status": "SUCCESS",
            "result": {"response": response},
            "error": None,
        }
    except Exception as e:  # pylint: disable=broad-exception-caught
        capture_exception(e)
        logger.exception("Error responding to calendar event: %s", e)
        return {
            "status": "FAILURE",
            "result": None,
            "error": f"Failed to send RSVP: {e}",
        }


@celery_app.task(bind=True)
def calendar_add_event_task(
    self,  # pylint: disable=unused-argument
    channel_id: str | None,
    mailbox_email: str,
    ics_data: str,
    calendar_id: str | None = None,
) -> Dict[str, Any]:
    """
    Add a calendar event to a CalDAV calendar.

    Args:
        channel_id: UUID of the CalDAV channel, or None for instance config
        mailbox_email: Acting mailbox email (Basic Auth user for instance config)
        ics_data: Raw ICS content
        calendar_id: Optional specific calendar URL to use
    """
    try:
        service = _get_caldav_service(channel_id, mailbox_email)
    except Channel.DoesNotExist:
        return {
            "status": "FAILURE",
            "result": None,
            "error": "CalDAV channel not found.",
        }
    except ValueError as e:
        return {
            "status": "FAILURE",
            "result": None,
            "error": str(e),
        }

    try:
        service.add_event(ics_data=ics_data, calendar_id=calendar_id)

        return {
            "status": "SUCCESS",
            "result": {"added": True},
            "error": None,
        }
    except Exception as e:  # pylint: disable=broad-exception-caught
        capture_exception(e)
        logger.exception("Error adding calendar event: %s", e)
        return {
            "status": "FAILURE",
            "result": None,
            "error": f"Failed to add event: {e}",
        }
