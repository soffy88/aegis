"""HTTP uptime probing.

Probes each enabled `uptime_targets` row (respecting its interval) via
oprim.network_http_health and records two gauges into `agent_metrics` —
`probe_up` (1.0 up / 0.0 down) and `probe_latency_ms` — keyed by the target name,
so the existing per-series alert evaluation can alert on `probe_up < 1` (and feed
the autoheal signal). Also stores last status on the target row for the UI.
"""

from __future__ import annotations

import asyncio
import json
import logging
from urllib.parse import urlparse

import asyncpg
from oprim._network import network_http_health  # v3 not top-level

log = logging.getLogger(__name__)

_PROBE_TIMEOUT_SEC = 8
_TLS_WARN_DAYS = 14  # §3.2: 证书剩余 <= 此值即告警


def _tls_days_remaining(url: str) -> float | None:
    """HTTPS 目标顺带取 TLS 证书剩余天数(握手即得,§3.2)。非 https/握手失败 → None(best-effort,
    不影响 uptime 拨测本身)。同步(阻塞握手)→ 由调用方经 to_thread 调。"""
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname:
        return None
    from oprim import tls_cert_expiry_probe  # noqa: PLC0415

    try:
        info = tls_cert_expiry_probe(
            host=parsed.hostname,
            port=parsed.port or 443,
            warn_days=_TLS_WARN_DAYS,
            timeout_sec=float(_PROBE_TIMEOUT_SEC),
        )
        return float(info.days_remaining)
    except Exception as exc:  # noqa: BLE001
        log.debug("tls_probe_skip url=%s err=%s", url, exc)
        return None


async def probe_due_targets(conn: asyncpg.Connection) -> int:
    """Probe every uptime target whose interval has elapsed. Returns count probed."""
    rows = await conn.fetch(
        """
        SELECT id, org_id, name, url, expected_status
        FROM uptime_targets
        WHERE enabled = TRUE
          AND (last_checked_at IS NULL
               OR last_checked_at <= now() - (interval_seconds * interval '1 second'))
        """
    )
    from aegis.server.lib.ssrf import SSRFBlocked, guard_scrape  # noqa: PLC0415

    probed = 0
    for t in rows:
        up = False
        latency = 0
        err: str | None = None
        tls_days: float | None = None
        try:
            # SSRF: allow private/loopback (legit internal uptime targets) but reject
            # cloud-metadata / link-local / reserved before opening any socket.
            guard_scrape(t["url"])
        except SSRFBlocked as exc:
            err = str(exc)[:200]
        else:
            try:
                res = await asyncio.to_thread(
                    network_http_health,
                    url=t["url"],
                    timeout_sec=_PROBE_TIMEOUT_SEC,
                    expected_status=t["expected_status"],
                )
                up = bool(getattr(res, "healthy", False))
                latency = int(getattr(res, "elapsed_ms", 0) or 0)
                err = getattr(res, "error", None)
            except Exception as exc:  # noqa: BLE001 — a probe failure is a "down", not a crash
                err = str(exc)[:200]

            # §3.2 SHOULD: HTTPS 目标顺带做 TLS 证书到期检查(握手即得),记 tls_cert_days_remaining
            # gauge 供 per-series 规则告警(如 < 14 天)。best-effort,不影响 uptime 判定。
            tls_days = await asyncio.to_thread(_tls_days_remaining, t["url"])

        tags = json.dumps({"url": t["url"], "source": "uptime", "target": t["name"]})
        metrics = [
            (t["name"], "probe_up", 1.0 if up else 0.0, "", tags),
            (t["name"], "probe_latency_ms", float(latency), "ms", tags),
        ]
        if tls_days is not None:
            metrics.append((t["name"], "tls_cert_days_remaining", tls_days, "days", tags))
            if tls_days <= _TLS_WARN_DAYS:
                log.warning("tls_cert_expiring target=%s days_remaining=%.0f", t["name"], tls_days)
        await conn.executemany(
            "INSERT INTO agent_metrics (hostname, metric_name, value, unit, tags)"
            " VALUES ($1, $2, $3, $4, $5::jsonb)",
            metrics,
        )
        await conn.execute(
            "UPDATE uptime_targets SET last_up=$1, last_latency_ms=$2,"
            " last_checked_at=now(), last_error=$3, last_tls_days_remaining=$4 WHERE id=$5",
            up,
            latency,
            err,
            tls_days,
            t["id"],
        )
        probed += 1

    if probed:
        log.debug("uptime_probe_tick probed=%d", probed)
    return probed
