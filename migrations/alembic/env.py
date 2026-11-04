from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

config = context.config
if config.config_file_name:
    fileConfig(config.config_file_name)


def run_migrations_offline() -> None:
    context.configure(url=config.get_main_option("sqlalchemy.url"), literal_binds=True,
                      dialect_opts={"paramstyle": "named"}, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(config.get_section(config.config_ini_section) or {},
                                     prefix="sqlalchemy.", poolclass=pool.NullPool)
    try:
        with connectable.connect() as connection:
            sqlite = connection.dialect.name == "sqlite"
            if sqlite:
                connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
                connection.commit()
                # pysqlite otherwise leaves DDL outside a transaction; force atomic migration DDL.
                connection.exec_driver_sql("BEGIN IMMEDIATE")
            try:
                context.configure(connection=connection, compare_type=True, render_as_batch=sqlite)
                with context.begin_transaction():
                    context.run_migrations()
                if connection.in_transaction():
                    connection.commit()
            except Exception:
                if connection.in_transaction():
                    connection.rollback()
                raise
            finally:
                if sqlite:
                    if connection.in_transaction():
                        connection.rollback()
                    connection.exec_driver_sql("PRAGMA foreign_keys=ON")
                    connection.commit()
    finally:
        connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
