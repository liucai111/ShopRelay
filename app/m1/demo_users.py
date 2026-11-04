from __future__ import annotations

import argparse
from getpass import getpass

from app.m1.db import default_database_url, make_engine, make_session_factory
from app.m1.services import list_demo_users, set_demo_password


def main() -> None:
    parser = argparse.ArgumentParser(description="Set local demo credentials for existing seeded users")
    parser.add_argument("--database-url", default=None)
    args = parser.parse_args()
    engine = make_engine(args.database_url or default_database_url())
    factory = make_session_factory(engine)
    try:
        with factory() as db:
            rows = list_demo_users(db)
        if len(rows) < 2:
            raise SystemExit("Expected the two explicitly seeded M0 demo users; run `python -m app.m0.cli seed` first.")
        for user in rows:
            print(f"Set local password for {user.user_id} ({user.display_name})")
            first = getpass("Password (minimum 12 characters): ")
            second = getpass("Repeat password: ")
            if first != second:
                raise SystemExit("Passwords did not match; no password was changed for this user.")
            with factory() as db:
                set_demo_password(db, user.user_id, first)
        print("Demo credentials initialized. Passwords were stored as scrypt hashes.")
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
