"""Run the account-scoped cloud API with PostgreSQL migrations applied."""

from __future__ import annotations

import os

import uvicorn

from .api import CloudSettings, create_app
from .db import apply_migrations


def main() -> None:
    settings = CloudSettings.from_environment()
    apply_migrations(settings.database_url)
    uvicorn.run(
        create_app(settings),
        host=os.environ.get("GROWTH_API_HOST", "127.0.0.1"),
        port=int(os.environ.get("GROWTH_API_PORT", "8766")),
        access_log=False,
    )


if __name__ == "__main__":
    main()
