"""Explicit owner-only migration command, separate from application startup."""

from __future__ import annotations

import argparse
from importlib.resources import files

from alembic import command
from alembic.config import Config

from speechnexus_backend.speech.infrastructure.job_schema import checked_sql_name


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--schema", default="speech")
    args = parser.parse_args()
    config = Config()
    config.set_main_option("script_location", str(files(__package__).joinpath("migrations")))
    config.attributes["schema"] = checked_sql_name(args.schema)
    command.upgrade(config, "head")


if __name__ == "__main__":
    main()
