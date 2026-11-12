import base64
import os
import sys
from email import message_from_bytes, policy
from email.message import EmailMessage
from unittest.mock import Mock

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from core.server import server
from core.tool_registry import get_tool_components
from core.utils import UserInputError
from gmail.gmail_helpers import (
    _decode_base64url,
    draft_is_reply,
    rewrite_draft_mime,
)
from gmail.gmail_tools import (
    delete_gmail_draft,
    list_gmail_drafts,
    manage_gmail_trash,
    send_gmail_draft,
    update_gmail_draft,
)

USER = "user@example.com"


def _unwrap(tool):
    """Unwrap FunctionTool + decorators to the original async function."""
    fn = tool.fn if hasattr(tool, "fn") else tool
    while hasattr(fn, "__wrapped__"):
        fn = fn.__wrapped__
    return fn


def _build(body="Original body", *, reply=False, attachment=False, html=False):
    msg = EmailMessage()
    msg["From"] = "Jonathan <me@example.com>"
    msg["To"] = "first@example.com"
    msg["Cc"] = "copy@example.com"
    msg["Subject"] = "Original subject"
    if reply:
        msg["In-Reply-To"] = "<parent@example.com>"
        msg["References"] = "<root@example.com> <parent@example.com>"
    msg.set_content(body)
    if html:
        msg.add_alternative(f"<p>{body}</p>", subtype="html")
    if attachment:
        msg.add_attachment(
            b"PDFDATA", maintype="application", subtype="pdf", filename="plan.pdf"
        )
    return msg.as_bytes()


def _raw(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _parse(data: bytes):
    return message_from_bytes(data, policy=policy.default)


def test_draft_raw_decode_tolerates_missing_padding():
    data = b"Subject: x\r\n\r\nhello!"
    assert _decode_base64url(_raw(data)) == data


def test_rewrite_changes_only_the_requested_fields():
    new_bytes, changed = rewrite_draft_mime(_build(reply=True), subject="New subject")
    msg = _parse(new_bytes)
    assert changed == ["subject"]
    assert msg["Subject"] == "New subject"
    assert msg["To"] == "first@example.com"
    assert msg["Cc"] == "copy@example.com"
    assert msg["From"] == "Jonathan <me@example.com>"
    assert msg["In-Reply-To"] == "<parent@example.com>"
    assert "<root@example.com>" in msg["References"]
    assert msg.get_body(("plain",)).get_content().strip() == "Original body"


def test_rewrite_replaces_plain_body_and_drops_stale_html_alternative():
    new_bytes, changed = rewrite_draft_mime(_build(html=True), body="Fresh text")
    msg = _parse(new_bytes)
    assert changed == ["body"]
    assert msg.get_content_type() == "text/plain"
    assert msg.get_content().strip() == "Fresh text"
    assert "Original body" not in new_bytes.decode()


def test_rewrite_html_body_builds_plain_and_html_alternatives():
    new_bytes, _ = rewrite_draft_mime(
        _build(), body="<p>Hello <b>there</b></p>", body_format="html"
    )
    msg = _parse(new_bytes)
    assert msg.get_content_type() == "multipart/alternative"
    assert "Hello" in msg.get_body(("plain",)).get_content()
    assert "<b>there</b>" in msg.get_body(("html",)).get_content()


def test_rewrite_keeps_attachments_when_body_changes():
    new_bytes, _ = rewrite_draft_mime(_build(attachment=True), body="With file")
    msg = _parse(new_bytes)
    attachments = list(msg.iter_attachments())
    assert msg.get_content_type() == "multipart/mixed"
    assert [a.get_filename() for a in attachments] == ["plan.pdf"]
    assert attachments[0].get_content() == b"PDFDATA"
    assert msg.get_body(("plain",)).get_content().strip() == "With file"


def test_rewrite_empty_string_clears_recipient_header():
    new_bytes, changed = rewrite_draft_mime(_build(), cc="", to="a@x.com, b@x.com")
    msg = _parse(new_bytes)
    assert set(changed) == {"to", "cc"}
    assert msg["Cc"] is None
    assert msg["To"] == "a@x.com, b@x.com"


def test_rewrite_handles_non_ascii_and_long_lines():
    body = "Café – " + ("long " * 60)
    new_bytes, _ = rewrite_draft_mime(_build(), subject="Résumé", body=body)
    msg = _parse(new_bytes)
    assert msg["Subject"] == "Résumé"
    assert msg.get_content().strip() == body.strip()
    assert all(len(line) <= 998 for line in new_bytes.split(b"\r\n"))


@pytest.mark.parametrize("field", ["subject", "to", "cc", "bcc"])
def test_rewrite_rejects_header_injection(field):
    with pytest.raises(UserInputError):
        rewrite_draft_mime(_build(), **{field: "x\r\nBcc: evil@example.com"})


def test_rewrite_rejects_unknown_body_format():
    with pytest.raises(UserInputError):
        rewrite_draft_mime(_build(), body="x", body_format="markdown")


def testdraft_is_reply_detects_reply_headers():
    assert draft_is_reply(_build(reply=True)) is True
    assert draft_is_reply(_build()) is False


@pytest.mark.asyncio
async def test_update_gmail_draft_keeps_thread_for_reply_drafts():
    service = Mock()
    service.users().drafts().get().execute.return_value = {
        "id": "r-1",
        "message": {"id": "m-old", "threadId": "t-9", "raw": _raw(_build(reply=True))},
    }
    service.users().drafts().update().execute.return_value = {
        "id": "r-1",
        "message": {"id": "m-new", "threadId": "t-9"},
    }

    result = await _unwrap(update_gmail_draft)(
        service=service, user_google_email=USER, draft_id="r-1", body="Revised"
    )

    get_kwargs = service.users.return_value.drafts.return_value.get.call_args.kwargs
    assert get_kwargs == {"userId": "me", "id": "r-1", "format": "raw"}
    update_kwargs = (
        service.users.return_value.drafts.return_value.update.call_args.kwargs
    )
    assert update_kwargs["id"] == "r-1"
    assert update_kwargs["body"]["message"]["threadId"] == "t-9"
    sent = _parse(_decode_base64url(update_kwargs["body"]["message"]["raw"]))
    assert sent.get_content().strip() == "Revised"
    assert sent["Subject"] == "Original subject"
    assert "m-new" in result and "Changed: body" in result


@pytest.mark.asyncio
async def test_update_gmail_draft_omits_thread_for_standalone_drafts():
    service = Mock()
    service.users().drafts().get().execute.return_value = {
        "id": "r-2",
        "message": {"id": "m-old", "threadId": "m-old", "raw": _raw(_build())},
    }
    service.users().drafts().update().execute.return_value = {
        "id": "r-2",
        "message": {"id": "m-new"},
    }

    await _unwrap(update_gmail_draft)(
        service=service, user_google_email=USER, draft_id="r-2", subject="S"
    )

    update_kwargs = (
        service.users.return_value.drafts.return_value.update.call_args.kwargs
    )
    assert "threadId" not in update_kwargs["body"]["message"]


@pytest.mark.asyncio
async def test_update_gmail_draft_requires_a_change():
    service = Mock()
    with pytest.raises(UserInputError):
        await _unwrap(update_gmail_draft)(
            service=service, user_google_email=USER, draft_id="r-1"
        )
    service.users.return_value.drafts.return_value.update.assert_not_called()


@pytest.mark.asyncio
async def test_send_gmail_draft_sends_by_id_without_retries():
    service = Mock()
    service.users().drafts().send().execute.return_value = {
        "id": "m-sent",
        "threadId": "t-1",
    }

    result = await _unwrap(send_gmail_draft)(
        service=service, user_google_email=USER, draft_id="r-7"
    )

    send_kwargs = service.users.return_value.drafts.return_value.send.call_args.kwargs
    assert send_kwargs == {"userId": "me", "body": {"id": "r-7"}}
    execute = service.users.return_value.drafts.return_value.send.return_value.execute
    assert execute.call_args.kwargs == {"num_retries": 0}
    assert "m-sent" in result and "t-1" in result


@pytest.mark.asyncio
async def test_delete_gmail_draft_defaults_to_trash():
    service = Mock()
    service.users().drafts().get().execute.return_value = {
        "id": "r-3",
        "message": {"id": "m-3"},
    }

    result = await _unwrap(delete_gmail_draft)(
        service=service, user_google_email=USER, draft_id="r-3"
    )

    trash_kwargs = (
        service.users.return_value.messages.return_value.trash.call_args.kwargs
    )
    assert trash_kwargs == {"userId": "me", "id": "m-3"}
    service.users.return_value.drafts.return_value.delete.assert_not_called()
    assert "Trash" in result and "m-3" in result


@pytest.mark.asyncio
async def test_delete_gmail_draft_permanent_calls_drafts_delete():
    service = Mock()

    result = await _unwrap(delete_gmail_draft)(
        service=service, user_google_email=USER, draft_id="r-4", permanent=True
    )

    delete_kwargs = (
        service.users.return_value.drafts.return_value.delete.call_args.kwargs
    )
    assert delete_kwargs == {"userId": "me", "id": "r-4"}
    service.users.return_value.messages.return_value.trash.assert_not_called()
    assert "permanently" in result


@pytest.mark.asyncio
async def test_manage_gmail_trash_handles_messages_and_threads():
    service = Mock()

    result = await _unwrap(manage_gmail_trash)(
        service=service,
        user_google_email=USER,
        action="trash",
        message_ids=["m-1", "m-2"],
        thread_ids=["t-1"],
    )

    messages = service.users.return_value.messages.return_value
    threads = service.users.return_value.threads.return_value
    assert [c.kwargs["id"] for c in messages.trash.call_args_list] == ["m-1", "m-2"]
    assert [c.kwargs["id"] for c in threads.trash.call_args_list] == ["t-1"]
    messages.untrash.assert_not_called()
    assert "3 of 3" in result


@pytest.mark.asyncio
async def test_manage_gmail_trash_untrash_and_validation():
    service = Mock()

    result = await _unwrap(manage_gmail_trash)(
        service=service, user_google_email=USER, action="untrash", message_ids=["m-1"]
    )
    assert "Restored from Trash: 1 of 1" in result
    service.users.return_value.messages.return_value.untrash.assert_called_once()

    with pytest.raises(UserInputError):
        await _unwrap(manage_gmail_trash)(
            service=service, user_google_email=USER, action="trash"
        )
    with pytest.raises(UserInputError):
        await _unwrap(manage_gmail_trash)(
            service=service,
            user_google_email=USER,
            action="trash",
            message_ids=[str(i) for i in range(101)],
        )


@pytest.mark.asyncio
async def test_list_gmail_drafts_returns_draft_and_message_ids():
    service = Mock()
    service.users().drafts().list().execute.return_value = {
        "drafts": [{"id": "r-1", "message": {"id": "m-1", "threadId": "t-1"}}],
        "nextPageToken": "tok",
    }
    service.users().drafts().get().execute.return_value = {
        "id": "r-1",
        "message": {
            "id": "m-1",
            "threadId": "t-1",
            "snippet": "Hi Joe, quick note",
            "payload": {
                "headers": [
                    {"name": "To", "value": "joe@example.com"},
                    {"name": "Subject", "value": "Hello"},
                ]
            },
        },
    }

    result = await _unwrap(list_gmail_drafts)(
        service=service, user_google_email=USER, query="subject:Hello", page_size=500
    )

    list_kwargs = service.users.return_value.drafts.return_value.list.call_args.kwargs
    assert list_kwargs == {"userId": "me", "maxResults": 50, "q": "subject:Hello"}
    for expected in (
        "Draft ID: r-1",
        "Message ID: m-1",
        "joe@example.com",
        "Hello",
        "tok",
    ):
        assert expected in result


@pytest.mark.asyncio
async def test_list_gmail_drafts_empty():
    service = Mock()
    service.users().drafts().list().execute.return_value = {}
    result = await _unwrap(list_gmail_drafts)(service=service, user_google_email=USER)
    assert result == "No drafts found."


def test_new_tools_are_registered_with_expected_schema():
    components = get_tool_components(server)
    for name in (
        "list_gmail_drafts",
        "update_gmail_draft",
        "send_gmail_draft",
        "delete_gmail_draft",
        "manage_gmail_trash",
    ):
        assert name in components

    update_props = components["update_gmail_draft"].parameters
    assert "draft_id" in update_props["required"]
    assert "body" not in update_props["required"]
    assert "service" not in update_props["properties"]

    trash_props = components["manage_gmail_trash"].parameters["properties"]
    for field in ("message_ids", "thread_ids"):
        assert trash_props[field]["type"] == "array"
        assert trash_props[field]["items"] == {"type": "string"}
    assert (
        components["delete_gmail_draft"].parameters["properties"]["permanent"][
            "default"
        ]
        is False
    )
