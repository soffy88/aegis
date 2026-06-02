"""C3-7 e2e 验证脚本.

Usage: python -m scripts.c3_e2e_trigger

Prerequisites:
- Aegis local server running (AEGIS_SENTRY_ENABLED=true uvicorn ... --port 8000)
- AEGIS_SENTRY_DSN set (points to Aegis self-monitoring project)
- DATABASE_URL set (or AEGIS_POSTGRES_DSN)

Flow:
1. Verify env vars present
2. POST /api/test/error/ → sentry-sdk captures exception → sends envelope
3. Sleep 3s for SDK flush
4. Query DB: error_events / error_issues / webhook_delivery_queue
5. Report pass/fail
"""

from __future__ import annotations

import asyncio
import os
import time

import asyncpg
import httpx


async def run() -> int:
    sentry_dsn = os.environ.get("AEGIS_SENTRY_DSN")
    if not sentry_dsn:
        print("ERROR: AEGIS_SENTRY_DSN not set")
        return 1

    base_url = os.environ.get("AEGIS_BASE_URL", "http://localhost:8000")
    db_dsn = os.environ.get("DATABASE_URL") or os.environ.get(
        "AEGIS_POSTGRES_DSN", "postgresql://aegis:aegis@localhost:5434/aegis"
    )

    # 1. Trigger test error (sentry-sdk captures it automatically via FastAPI integration)
    print(f"Triggering test error at {base_url}/api/test/error/ ...")
    async with httpx.AsyncClient() as client:
        try:
            resp = await client.post(f"{base_url}/api/test/error/?error_type=ValueError")
            print(f"Response: HTTP {resp.status_code}")
        except Exception as exc:
            print(f"HTTP error (server raised exception, expected): {exc}")

    # 2. Wait for SDK to flush envelope
    print("Waiting 3s for SDK to flush ...")
    time.sleep(3)

    # 3. Check DB
    conn = await asyncpg.connect(db_dsn)
    try:
        row = await conn.fetchrow(
            "SELECT count(*) AS n FROM error_events"
            " WHERE exception_type = 'ValueError' AND ts > NOW() - INTERVAL '1 minute'"
        )
        events_count = row["n"]
        print(f"error_events (last 1min, ValueError): {events_count}")

        row = await conn.fetchrow(
            "SELECT count(*) AS n FROM error_issues WHERE exception_type = 'ValueError'"
        )
        issues_count = row["n"]
        print(f"error_issues (ValueError, all time): {issues_count}")

        row = await conn.fetchrow(
            "SELECT count(*) AS n FROM webhook_delivery_queue"
            " WHERE event_type = 'error.new_issue' AND created_at > NOW() - INTERVAL '1 minute'"
        )
        webhook_count = row["n"]
        print(f"webhook_delivery_queue (error.new_issue, last 1min): {webhook_count}")

    finally:
        await conn.close()

    # 4. Report
    if events_count >= 1:
        print("\n✅ C3-7 e2e PASS: SDK → envelope endpoint → DB pipeline verified.")
        return 0

    print("\n❌ C3-7 e2e FAIL: error_events empty — check AEGIS_SENTRY_ENABLED + DSN + Aegis logs.")
    return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run()))
