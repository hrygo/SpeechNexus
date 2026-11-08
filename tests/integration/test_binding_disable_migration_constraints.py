"""New NOT NULL catalogue rows must not hide incompatible disable roots."""

from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.pool import NullPool

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("extra_unique", [False, True])
def test_disable_root_accepts_not_null_but_rejects_extra_unique(extra_unique: bool) -> None:
    url = os.environ.get("SPEECHNEXUS_TEST_DATABASE_URL")
    if not url:
        pytest.fail(
            "Configure an isolated PostgreSQL DSN in SPEECHNEXUS_TEST_DATABASE_URL; "
            "skip is not evidence"
        )
    schema = "sn_disable_constraints_" + uuid.uuid4().hex[:10]
    root = Path(__file__).resolve().parents[2]
    engine = create_engine(
        url.replace("postgresql://", "postgresql+psycopg://", 1), poolclass=NullPool
    )
    config = Config(str(root / "alembic.ini"))
    config.attributes["schema"] = schema
    try:
        with engine.connect() as connection:
            config.attributes["connection"] = connection
            command.upgrade(config, "0024_binding_publish_intents")
        if extra_unique:
            with engine.begin() as connection:
                connection.execute(
                    text(
                        f'ALTER TABLE "{schema}".speech_binding_disable_intents '
                        "ADD CONSTRAINT unexpected_unique UNIQUE (event_id)"
                    )
                )
        with engine.connect() as connection:
            config.attributes["connection"] = connection
            if extra_unique:
                with pytest.raises(RuntimeError, match="incompatible constraints"):
                    command.upgrade(config, "0025_binding_disable_targets")
            else:
                command.upgrade(config, "0025_binding_disable_targets")
        with engine.connect() as connection:
            version = connection.execute(
                text(f'SELECT version_num FROM "{schema}".alembic_version')
            ).scalar_one()
            assert version == (
                "0024_binding_publish_intents" if extra_unique else "0025_binding_disable_targets"
            )
    finally:
        with engine.begin() as connection:
            connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        engine.dispose()
