"""Tests for sentry_envelope parser — pure unit tests, no DB needed."""

from __future__ import annotations

import json

import pytest

from aegis.server.lib.sentry_envelope import (
    SentryEnvelopeParseError,
    extract_exception,
    extract_top_frame,
    parse_envelope,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_ENVELOPE_HEADER = json.dumps({"sent_at": "2026-06-01T00:00:00Z"})
_ITEM_HEADER_EVENT = json.dumps({"type": "event", "content_type": "application/json"})
_ITEM_HEADER_TXN = json.dumps({"type": "transaction"})

_SIMPLE_EVENT = {
    "event_id": "abc123",
    "level": "error",
    "exception": {
        "values": [
            {
                "type": "TypeError",
                "value": "unsupported operand",
                "stacktrace": {
                    "frames": [
                        {"function": "outer", "filename": "/app/outer.py"},
                        {"function": "inner", "filename": "/app/inner.py"},
                    ]
                },
            }
        ]
    },
}


def _make_envelope(*items: tuple[str, str]) -> bytes:
    """Build envelope bytes from (item_header_json, item_payload_json) pairs."""
    parts = [_ENVELOPE_HEADER]
    for header, payload in items:
        parts += [header, payload]
    return ("\n".join(parts) + "\n").encode()


# ---------------------------------------------------------------------------
# parse_envelope
# ---------------------------------------------------------------------------


def test_parse_envelope_single_event() -> None:
    raw = _make_envelope((_ITEM_HEADER_EVENT, json.dumps(_SIMPLE_EVENT)))
    events = parse_envelope(raw)
    assert len(events) == 1
    assert events[0]["exception"]["values"][0]["type"] == "TypeError"


def test_parse_envelope_skips_transaction() -> None:
    txn_payload = json.dumps({"type": "transaction", "spans": []})
    raw = _make_envelope(
        (_ITEM_HEADER_TXN, txn_payload),
        (_ITEM_HEADER_EVENT, json.dumps(_SIMPLE_EVENT)),
    )
    events = parse_envelope(raw)
    assert len(events) == 1  # only the event item


def test_parse_envelope_multiple_events() -> None:
    raw = _make_envelope(
        (_ITEM_HEADER_EVENT, json.dumps(_SIMPLE_EVENT)),
        (_ITEM_HEADER_EVENT, json.dumps({**_SIMPLE_EVENT, "event_id": "def456"})),
    )
    events = parse_envelope(raw)
    assert len(events) == 2


def test_parse_envelope_invalid_utf8_raises() -> None:
    with pytest.raises(SentryEnvelopeParseError, match="invalid utf-8"):
        parse_envelope(b"\xff\xfe")


def test_parse_envelope_invalid_header_raises() -> None:
    with pytest.raises(SentryEnvelopeParseError, match="invalid envelope header"):
        parse_envelope(b"not-json\n")


def test_parse_envelope_malformed_item_skipped() -> None:
    # First item has malformed payload — should be skipped, second should be parsed
    raw = (
        _ENVELOPE_HEADER
        + "\n"
        + _ITEM_HEADER_EVENT
        + "\n"
        + "not-json\n"
        + _ITEM_HEADER_EVENT
        + "\n"
        + json.dumps(_SIMPLE_EVENT)
        + "\n"
    ).encode()
    events = parse_envelope(raw)
    assert len(events) == 1


# ---------------------------------------------------------------------------
# extract_top_frame
# ---------------------------------------------------------------------------


def test_extract_top_frame_present() -> None:
    func, fname = extract_top_frame(_SIMPLE_EVENT)
    # last frame in the list is innermost
    assert func == "inner"
    assert fname == "/app/inner.py"


def test_extract_top_frame_missing_no_exception() -> None:
    func, fname = extract_top_frame({"level": "error"})
    assert func is None
    assert fname is None


def test_extract_top_frame_missing_no_frames() -> None:
    payload = {"exception": {"values": [{"type": "OSError", "stacktrace": {"frames": []}}]}}
    func, fname = extract_top_frame(payload)
    assert func is None
    assert fname is None


# ---------------------------------------------------------------------------
# extract_exception
# ---------------------------------------------------------------------------


def test_extract_exception_standard() -> None:
    exc_type, exc_value = extract_exception(_SIMPLE_EVENT)
    assert exc_type == "TypeError"
    assert exc_value == "unsupported operand"


def test_extract_exception_message_fallback() -> None:
    payload = {"message": "something went wrong"}
    exc_type, exc_value = extract_exception(payload)
    assert exc_type == "Message"
    assert exc_value == "something went wrong"


def test_extract_exception_message_dict_fallback() -> None:
    payload = {"message": {"formatted": "dict message"}}
    exc_type, exc_value = extract_exception(payload)
    assert exc_type == "Message"
    assert exc_value == "dict message"


def test_extract_exception_no_exception_or_message_raises() -> None:
    with pytest.raises(SentryEnvelopeParseError):
        extract_exception({"level": "info"})
