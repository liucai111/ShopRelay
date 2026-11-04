from __future__ import annotations

import argparse

from app.m1.db import default_database_url, upgrade_database


def main() -> None:
    parser = argparse.ArgumentParser(description="Explicit ShopRelay M1 database migration")
    parser.add_argument("upgrade", choices=("upgrade",))
    parser.add_argument("--database-url", default=None)
    args = parser.parse_args()
    upgrade_database(args.database_url or default_database_url())
    print("M1 migration upgraded to head")


if __name__ == "__main__":
    main()
