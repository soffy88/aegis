"""CLI: aegis <subcommand>"""
from __future__ import annotations

import argparse
import sys

from aegis.server.runtime.config import AegisSettings


def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn
    cfg = AegisSettings()
    uvicorn.run(
        "aegis.server.app:app",
        host=cfg.host, port=cfg.port,
        log_level=cfg.log_level.lower(),
        reload=args.reload,
    )
    return 0


def cmd_migrate(args: argparse.Namespace) -> int:
    import asyncio

    from aegis.server.persistence import apply_migrations, close_pool, get_pool, init_pool

    async def _run() -> int:
        cfg = AegisSettings()
        await init_pool(dsn=cfg.postgres_dsn)
        try:
            async with get_pool().acquire() as conn:
                n = await apply_migrations(conn)
                print(f"applied {n} migrations")
            return 0
        finally:
            await close_pool()

    return asyncio.run(_run())


def cli_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="aegis")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_serve = sub.add_parser("serve", help="Run the HTTP server")
    p_serve.add_argument("--reload", action="store_true")
    p_serve.set_defaults(func=cmd_serve)

    p_migrate = sub.add_parser("migrate", help="Apply pending DB migrations")
    p_migrate.set_defaults(func=cmd_migrate)

    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(cli_main())
