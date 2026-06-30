"""Minimal Prometheus text-exposition-format parser.

Parses the `/metrics` text format into flat samples. Histograms/summaries are
already flattened in the exposition format (``_bucket`` / ``_sum`` / ``_count`` /
quantile lines), so each line becomes one sample — no type-specific handling needed.

Spec: https://prometheus.io/docs/instrumenting/exposition_formats/
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field


@dataclass
class Sample:
    name: str
    value: float
    labels: dict[str, str] = field(default_factory=dict)


_FLOAT_SPECIAL = {"nan": math.nan, "+inf": math.inf, "-inf": -math.inf, "inf": math.inf}


def _parse_value(token: str) -> float | None:
    t = token.strip()
    low = t.lower()
    if low in _FLOAT_SPECIAL:
        return _FLOAT_SPECIAL[low]
    try:
        return float(t)
    except ValueError:
        return None


def _parse_labels(blob: str) -> dict[str, str]:
    """Parse the inside of ``{...}`` into a dict. Handles quoted values + escapes."""
    labels: dict[str, str] = {}
    i, n = 0, len(blob)
    while i < n:
        # skip separators / whitespace
        while i < n and blob[i] in ", \t":
            i += 1
        if i >= n:
            break
        # label name
        start = i
        while i < n and blob[i] not in "=":
            i += 1
        key = blob[start:i].strip()
        if i >= n or not key:
            break
        i += 1  # skip '='
        while i < n and blob[i] in " \t":
            i += 1
        if i >= n or blob[i] != '"':
            break
        i += 1  # opening quote
        chars: list[str] = []
        while i < n:
            c = blob[i]
            if c == "\\" and i + 1 < n:
                nxt = blob[i + 1]
                chars.append({"n": "\n", '"': '"', "\\": "\\"}.get(nxt, nxt))
                i += 2
                continue
            if c == '"':
                i += 1
                break
            chars.append(c)
            i += 1
        labels[key] = "".join(chars)
    return labels


def parse_prometheus_text(text: str) -> list[Sample]:
    """Parse exposition text into a list of Samples (comments/blank lines skipped)."""
    samples: list[Sample] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue

        if "{" in line:
            name = line[: line.index("{")].strip()
            close = line.rindex("}")
            labels = _parse_labels(line[line.index("{") + 1 : close])
            rest = line[close + 1 :].strip()
        else:
            parts = line.split(None, 1)
            name = parts[0]
            rest = parts[1] if len(parts) > 1 else ""
            labels = {}

        if not name or not rest:
            continue
        # rest = "<value> [timestamp]" — take the first token as the value.
        value = _parse_value(rest.split()[0])
        if value is None:
            continue
        samples.append(Sample(name=name, value=value, labels=labels))
    return samples
