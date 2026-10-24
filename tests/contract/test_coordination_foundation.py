"""Keep versioned storage/role ownership aligned with the three-team contract."""

import json
import re
from pathlib import Path

import pytest

from speechnexus_backend.speech.infrastructure.admission_command_schema import (
    ADMISSION_COMMAND_DDL,
)
from speechnexus_backend.speech.infrastructure.coordination_grants import (
    GATEWAY_UPDATES,
    SPEECH_UPDATES,
    coordination_runtime_grants,
)
from speechnexus_backend.speech.infrastructure.coordination_schema import COORDINATION_DDL
from speechnexus_backend.speech.infrastructure.job_schema import JOB_DDL


def test_production_storage_and_dml_owners_match_the_authoritative_matrix() -> None:
    root = Path(__file__).resolve().parents[2]
    owners = json.loads((root / "contracts/coordination-ownership.json").read_text())["tables"]
    tables = {
        match[1]
        for ddl in (*JOB_DDL, *COORDINATION_DDL, *ADMISSION_COMMAND_DDL)
        if (match := re.match(r"CREATE TABLE (\w+)", ddl))
    }
    assert tables == set(owners)
    assert set(SPEECH_UPDATES) == {table for table, owner in owners.items() if owner == "A"}
    assert set(GATEWAY_UPDATES) == {table for table, owner in owners.items() if owner == "B"}


def test_runtime_role_generation_rejects_merged_owners_and_untrusted_sql_names() -> None:
    with pytest.raises(ValueError, match="separate runtime roles"):
        coordination_runtime_grants(
            "speech", speech_role="runtime_owner", gateway_role="runtime_owner"
        )
    for schema, speech, gateway in (
        ('speech"; DROP SCHEMA speech; --', "speech_runtime", "gateway_runtime"),
        ("speech", 'untrusted"role', "gateway_runtime"),
        ("speech", "speech_runtime", "Gateway"),
    ):
        with pytest.raises(ValueError, match="Invalid database object name"):
            coordination_runtime_grants(schema, speech_role=speech, gateway_role=gateway)
