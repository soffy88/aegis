"""Tests for the Prometheus text-format parser."""

from __future__ import annotations

import math

from aegis.server.services.prometheus_parse import parse_prometheus_text

_SAMPLE = """# HELP http_requests_total The total number of HTTP requests.
# TYPE http_requests_total counter
http_requests_total{method="post",code="200"} 1027 1395066363000
http_requests_total{method="post",code="400"}    3
msg{text="line1\\nline2",path="/a\\"b"} 1
node_cpu_seconds_total{cpu="0",mode="idle"} 12345.67
go_gc_duration_seconds{quantile="0.5"} 0.000123
go_gc_duration_seconds_sum 0.01
go_gc_duration_seconds_count 42
weird_nan NaN
weird_inf +Inf
no_labels_metric 99
"""


def test_parses_all_samples_skipping_comments() -> None:
    s = parse_prometheus_text(_SAMPLE)
    assert len(s) == 10  # 2 comment lines skipped


def test_labels_and_value() -> None:
    s = parse_prometheus_text(_SAMPLE)
    assert s[0].name == "http_requests_total"
    assert s[0].value == 1027.0
    assert s[0].labels == {"method": "post", "code": "200"}
    assert s[1].value == 3.0  # extra whitespace + no timestamp


def test_escaped_label_values() -> None:
    msg = next(x for x in parse_prometheus_text(_SAMPLE) if x.name == "msg")
    assert msg.labels["text"] == "line1\nline2"
    assert msg.labels["path"] == '/a"b'


def test_histogram_summary_lines_become_flat_samples() -> None:
    names = {x.name for x in parse_prometheus_text(_SAMPLE)}
    assert {"go_gc_duration_seconds", "go_gc_duration_seconds_sum", "go_gc_duration_seconds_count"} <= names


def test_special_floats() -> None:
    by = {x.name: x.value for x in parse_prometheus_text(_SAMPLE)}
    assert math.isnan(by["weird_nan"])
    assert math.isinf(by["weird_inf"])
    assert by["no_labels_metric"] == 99.0


def test_empty_and_blank_lines() -> None:
    assert parse_prometheus_text("\n\n   \n# only a comment\n") == []
