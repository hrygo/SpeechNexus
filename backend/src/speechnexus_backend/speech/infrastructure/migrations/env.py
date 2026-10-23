"""Alembic entry point with a database-scoped, transaction-held migration mutex."""

from __future__ import annotations

import os

from alembic import context
from sqlalchemy import Connection, create_engine, text
from sqlalchemy.pool import NullPool

from speechnexus_backend.speech.infrastructure.job_schema import checked_sql_name


def _migrate(connection: Connection) -> None:
    schema = checked_sql_name(context.config.attributes.get("schema", "speech"))
    context.config.attributes["schema"] = schema
    if connection.dialect.name != "postgresql":
        raise ValueError("Product migrations require PostgreSQL")
    with connection.begin():
        connection.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
            {"key": "speechnexus:migrations:" + schema},
        )
        connection.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{schema}"'))
        connection.execute(
            text("SELECT set_config('search_path', :schema, true)"),
            {"schema": schema + ",pg_catalog"},
        )
        context.configure(connection=connection, version_table_schema=schema)
        with context.begin_transaction():
            context.run_migrations()


provided = context.config.attributes.get("connection")
if provided is not None:
    _migrate(provided)
else:
    # Separate DDL identity; do not fall back to the non-owner runtime DSN.
    migration_url = os.environ.get("SPEECHNEXUS_MIGRATION_DATABASE_URL")
    if not migration_url:
        raise ValueError("Set SPEECHNEXUS_MIGRATION_DATABASE_URL for explicit migration")
    migration_url = migration_url.replace("postgresql://", "postgresql+psycopg://", 1)
    engine = create_engine(migration_url, poolclass=NullPool, hide_parameters=True)
    try:
        with engine.connect() as migration_connection:
            _migrate(migration_connection)
    finally:
        engine.dispose()
