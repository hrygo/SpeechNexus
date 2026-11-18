"""The migration head, derived once so a new revision cannot break unrelated tests."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
VERSIONS = ROOT / "backend/src/speechnexus_backend/speech/infrastructure/migrations/versions"

_REVISION = re.compile(r'^revision = "([^"]+)"', re.MULTILINE)
_DOWN = re.compile(r'^down_revision = "([^"]+)"', re.MULTILINE)


def head_revision() -> str:
    revisions: set[str] = set()
    parents: set[str] = set()
    for path in VERSIONS.glob("*.py"):
        source = path.read_text(encoding="utf-8")
        revision = _REVISION.search(source)
        if revision is None:
            continue
        revisions.add(revision.group(1))
        down = _DOWN.search(source)
        if down is not None:
            parents.add(down.group(1))
    heads = revisions - parents
    if len(heads) != 1:
        raise RuntimeError(f"Expected exactly one migration head, found {sorted(heads)}")
    return heads.pop()


HEAD = head_revision()


ASR_SETTLEMENT_REVISION = "0057_asr_settlement"
PRE_ASR_SETTLEMENT_REVISION = "0056_debt_probe_live_state_index"


def step_below_asr_settlement(connection: Any) -> None:
    """Start an older-guard downgrade from the pre-0057 head.

    0057 preserves ASR settlement history unconditionally, so a test that
    exercises an earlier revision's downgrade refusal would only ever see 0057's
    own refusal. Removing the table 0057 adds -- its only object -- and stamping
    the version back reconstructs the database a pre-0057 deployment has, without
    weakening 0057's refusal in production.
    """
    execute = connection.execute
    execute("DROP TABLE IF EXISTS speech_asr_settlement")
    execute(
        "UPDATE alembic_version SET version_num = %s",
        (PRE_ASR_SETTLEMENT_REVISION,),
    )
