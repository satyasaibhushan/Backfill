import argparse
import asyncio
import json

import uvicorn

from backfill import __version__
from backfill.config import load_settings
from backfill.database import create_database_engine, initialize_database
from backfill.providers.service import ProviderService


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="backfill",
        description="Fit queued engineering work into available provider headroom.",
    )
    parser.add_argument("--version", action="version", version=__version__)
    subparsers = parser.add_subparsers(dest="command")
    subparsers.add_parser("serve", help="Run the dashboard and scheduler")
    subparsers.add_parser("probe", help="Print provider quota snapshots as JSON")
    args = parser.parse_args()
    settings = load_settings()

    if args.command == "probe":
        asyncio.run(_probe(settings))
        return
    uvicorn.run(
        "backfill.main:app",
        host=settings.host,
        port=settings.port,
        proxy_headers=True,
        forwarded_allow_ips="127.0.0.1",
    )


async def _probe(settings) -> None:
    engine = create_database_engine(settings)
    initialize_database(engine)
    service = ProviderService(settings, engine)
    snapshots = await service.refresh(force=True)
    print(json.dumps([snapshot.model_dump(mode="json") for snapshot in snapshots], indent=2))
    engine.dispose()


if __name__ == "__main__":
    main()
