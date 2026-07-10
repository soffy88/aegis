"""Prometheus scrape service — pulls `/metrics` from registered targets.

A target is any HTTP endpoint exposing the Prometheus text format (node_exporter,
cAdvisor, app /metrics, …). Scraped samples land in the same `agent_metrics` table
the agent writes to (hostname = target name), so the existing query/series/retention
API works over them unchanged.
"""

from __future__ import annotations

import json
import logging

import asyncpg
import httpx

from aegis.server.services.prometheus_parse import parse_prometheus_text

log = logging.getLogger(__name__)

# Safety cap: never store more than this many samples from a single scrape.
_MAX_SAMPLES_PER_SCRAPE = 5000


async def scrape_url(url: str, *, timeout: float = 10.0) -> list[tuple[str, float, dict]]:
    """Fetch + parse one target. Returns (metric_name, value, labels) tuples.

    Raises httpx errors / ValueError(non-200) — callers record them on the target.
    SSRF guard: private/loopback exporters are allowed (that's the whole point of
    scraping), but cloud-metadata / link-local / reserved targets are rejected so a
    scrape target can't be pointed at 169.254.169.254 to exfiltrate IAM creds.
    """
    from aegis.server.lib.ssrf import guard_scrape  # noqa: PLC0415

    guard_scrape(url)
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.get(url, headers={"Accept": "text/plain"})
    if resp.status_code != 200:
        raise ValueError(f"scrape returned HTTP {resp.status_code}")
    samples = parse_prometheus_text(resp.text)
    return [(s.name, s.value, s.labels) for s in samples[:_MAX_SAMPLES_PER_SCRAPE]]


async def _store(
    conn: asyncpg.Connection,
    *,
    hostname: str,
    samples: list[tuple[str, float, dict]],
    static_labels: dict,
) -> int:
    if not samples:
        return 0
    rows = [
        (hostname, name, value, "", json.dumps({**labels, **static_labels}))
        for name, value, labels in samples
    ]
    await conn.executemany(
        "INSERT INTO agent_metrics (hostname, metric_name, value, unit, tags)"
        " VALUES ($1, $2, $3, $4, $5::jsonb)",
        rows,
    )
    return len(rows)


async def scrape_due_targets(conn: asyncpg.Connection) -> dict[str, int]:
    """Scrape every enabled target whose interval has elapsed. Returns a summary.

    Per-target failures are recorded on the row (last_status/last_error) and never
    abort the batch.
    """
    targets = await conn.fetch(
        "SELECT id, name, url, interval_seconds, labels FROM scrape_targets"
        " WHERE enabled = TRUE"
        "   AND (last_scrape_at IS NULL"
        "        OR last_scrape_at < now() - (interval_seconds * interval '1 second'))"
    )
    scraped = 0
    failed = 0
    samples_total = 0
    for t in targets:
        static_labels = t["labels"] if isinstance(t["labels"], dict) else json.loads(t["labels"])
        try:
            samples = await scrape_url(t["url"])
            n = await _store(conn, hostname=t["name"], samples=samples, static_labels=static_labels)
            samples_total += n
            scraped += 1
            await conn.execute(
                "UPDATE scrape_targets SET last_scrape_at = now(), last_status = $2,"
                " last_error = NULL WHERE id = $1",
                t["id"],
                f"ok ({n} samples)",
            )
        except Exception as exc:  # noqa: BLE001 — record + continue
            failed += 1
            log.warning("scrape_failed target=%s url=%s err=%s", t["name"], t["url"], exc)
            await conn.execute(
                "UPDATE scrape_targets SET last_scrape_at = now(), last_status = 'error',"
                " last_error = $2 WHERE id = $1",
                t["id"],
                str(exc)[:500],
            )
    if scraped or failed:
        log.info("scrape_cycle scraped=%d failed=%d samples=%d", scraped, failed, samples_total)
    return {"scraped": scraped, "failed": failed, "samples": samples_total}
