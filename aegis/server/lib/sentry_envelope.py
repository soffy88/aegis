"""Sentry envelope parser — M1 subset (event items only).


Envelope format reference: https://develop.sentry.dev/sdk/envelopes/

  {envelope_header_json}\\n
  {item_header_json}\\n{item_payload_json}\\n
  ...

M1 supports type='event' items only. transaction / session / replay / minidump
items are silently skipped.
"""

from __future__ import annotations

import contextlib
import json
from typing import Any


class SentryEnvelopeParseError(ValueError):
    """Raised when the envelope cannot be parsed."""


def parse_envelope(envelope_bytes: bytes) -> list[dict[str, Any]]:
    """Parse Sentry envelope bytes and return a list of event payload dicts.

    Non-event items (transaction / session / replay / minidump) are skipped.
    A single malformed item does not abort parsing of the rest.

    Raises:
        SentryEnvelopeParseError: envelope header is missing or invalid UTF-8.
    """
    try:
        text = envelope_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SentryEnvelopeParseError(f"invalid utf-8: {exc}") from exc

    lines = text.split("\n")
    if not lines:
        raise SentryEnvelopeParseError("empty envelope")

    try:
        json.loads(lines[0])  # envelope header — parse to validate, value unused in M1
    except json.JSONDecodeError as exc:
        raise SentryEnvelopeParseError(f"invalid envelope header: {exc}") from exc

    events: list[dict[str, Any]] = []
    i = 1
    while i < len(lines) - 1:
        if not lines[i].strip():
            i += 1
            continue

        try:
            item_header = json.loads(lines[i])
        except json.JSONDecodeError:
            i += 1
            continue

        item_type = item_header.get("type")
        i += 1

        if i >= len(lines):
            break

        if item_type == "event":
            with contextlib.suppress(json.JSONDecodeError):
                events.append(json.loads(lines[i]))
        # else: skip transaction / session / replay / minidump (M1 not supported)

        i += 1

    return events


def extract_top_frame(
    event_payload: dict[str, Any],
) -> tuple[str | None, str | None]:
    """Return (function, filename) of the innermost stack frame.

    Sentry frames are ordered outermost-first; the last frame is the throw site.
    Returns (None, None) when no Python frames are present (e.g. C extensions).
    """
    values = event_payload.get("exception", {}).get("values", [])
    if not values:
        return (None, None)
    frames = values[0].get("stacktrace", {}).get("frames", [])
    if not frames:
        return (None, None)
    top = frames[-1]
    return (top.get("function"), top.get("filename"))


def extract_exception(
    event_payload: dict[str, Any],
) -> tuple[str, str | None]:
    """Return (exception_type, exception_value) from a Sentry event payload.

    Falls back to the message field for capture_message() events.

    Raises:
        SentryEnvelopeParseError: neither exception nor message found.
    """
    values = event_payload.get("exception", {}).get("values", [])
    if values:
        first = values[0]
        exc_type = first.get("type")
        if not exc_type:
            raise SentryEnvelopeParseError("exception missing type")
        return (exc_type, first.get("value"))

    # capture_message() fallback
    message = event_payload.get("message")
    if isinstance(message, dict):
        message = message.get("formatted") or message.get("message", "")
    if message:
        return ("Message", str(message)[:1000])

    raise SentryEnvelopeParseError("no exception or message in event payload")
