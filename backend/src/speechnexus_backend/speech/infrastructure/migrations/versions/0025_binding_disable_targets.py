"""Keep fixed binding-disable execution identities for recovery."""

from alembic import op
from sqlalchemy import text

from speechnexus_backend.speech.infrastructure.binding_disable_targets_schema import (
    ABORT_INTENT_DDL,
    TARGET_DDL,
)

revision = "0025_binding_disable_targets"
down_revision = "0024_binding_publish_intents"
branch_labels = None
depends_on = None


def upgrade() -> None:
    connection = op.get_bind()
    if connection.execute(text("SHOW transaction_isolation")).scalar_one() != "read committed":
        raise RuntimeError("Binding disable upgrade requires READ COMMITTED isolation")
    # Old 0023 installations do not have the root table. Prevent a concurrent
    # lifecycle writer from adding a disable between the backfill and commit.
    # Lifecycle writes the retirement before its audit event; keep that lock order.
    op.execute("LOCK TABLE speech_publication_retirements IN SHARE ROW EXCLUSIVE MODE")
    op.execute("LOCK TABLE speech_publication_lifecycle_events IN SHARE ROW EXCLUSIVE MODE")
    op.execute(
        "CREATE TABLE IF NOT EXISTS speech_binding_disable_intents ("
        'event_id VARCHAR(128) COLLATE "C" PRIMARY KEY '
        "REFERENCES speech_publication_lifecycle_events (event_id))"
    )
    op.execute("LOCK TABLE speech_binding_disable_intents IN SHARE ROW EXCLUSIVE MODE")
    connection = op.get_bind()
    columns = connection.execute(
        text(
            "SELECT a.attname, format_type(a.atttypid,a.atttypmod), a.attnotnull, c.collname "
            "FROM pg_catalog.pg_attribute a "
            "JOIN pg_catalog.pg_class t ON t.oid=a.attrelid AND t.relkind='r' "
            "LEFT JOIN pg_catalog.pg_collation c ON c.oid=a.attcollation "
            "WHERE a.attrelid=to_regclass('speech_binding_disable_intents') "
            "AND a.attnum>0 AND NOT a.attisdropped ORDER BY a.attnum"
        )
    ).fetchall()
    if [tuple(row) for row in columns] != [("event_id", "character varying(128)", True, "C")]:
        raise RuntimeError("Existing binding disable root has incompatible columns")
    constraints_ok = connection.execute(
        text(
            "SELECT count(*)=2 AND "
            "count(*) FILTER (WHERE con.contype='p' AND con.conkey=ARRAY[a.attnum] "
            "AND con.convalidated AND NOT con.condeferrable)=1 AND "
            "count(*) FILTER (WHERE con.contype='f' AND con.conkey=ARRAY[a.attnum] "
            "AND con.confrelid=to_regclass('speech_publication_lifecycle_events') "
            "AND con.confkey=ARRAY[e.attnum] AND con.convalidated AND NOT con.condeferrable "
            "AND con.confupdtype='a' AND con.confdeltype='a' AND con.confmatchtype='s')=1 "
            "FROM pg_catalog.pg_constraint con "
            "JOIN pg_catalog.pg_attribute a ON a.attrelid=con.conrelid AND a.attname='event_id' "
            "JOIN pg_catalog.pg_attribute e "
            "ON e.attrelid=to_regclass('speech_publication_lifecycle_events') "
            "AND e.attname='event_id' "
            "WHERE con.conrelid=to_regclass('speech_binding_disable_intents')"
        )
    ).scalar_one()
    if not constraints_ok:
        raise RuntimeError("Existing binding disable root has incompatible constraints")
    inconsistent = connection.execute(
        text(
            "SELECT EXISTS (SELECT 1 FROM speech_publication_lifecycle_events e "
            "LEFT JOIN speech_publication_retirements r ON r.kind=e.kind "
            "AND r.object_id=e.binding_id AND r.revision=e.revision "
            "AND r.revision_id=e.revision_id "
            "WHERE e.action='model_binding.disable' AND (r.revision_id IS NULL "
            "OR r.mode<>'disable' OR e.result_status<>'disabled' "
            "OR r.reason IS DISTINCT FROM e.disabled_reason "
            "OR r.retired_at_us IS DISTINCT FROM e.retired_at_us)) "
            "OR EXISTS (SELECT 1 FROM speech_publication_retirements r "
            "WHERE r.kind='binding' AND r.mode='disable' AND NOT EXISTS ("
            "SELECT 1 FROM speech_publication_lifecycle_events e "
            "WHERE e.kind=r.kind AND e.binding_id=r.object_id AND e.revision=r.revision "
            "AND e.revision_id=r.revision_id AND e.action='model_binding.disable')) "
            "OR EXISTS (SELECT 1 FROM speech_binding_disable_intents d "
            "JOIN speech_publication_lifecycle_events e ON e.event_id=d.event_id "
            "WHERE e.action<>'model_binding.disable')"
        )
    ).scalar_one()
    if inconsistent:
        raise RuntimeError("Historical binding disable facts require reconciliation")
    op.execute(
        "INSERT INTO speech_binding_disable_intents (event_id) "
        "SELECT event_id FROM speech_publication_lifecycle_events "
        "WHERE action='model_binding.disable' ON CONFLICT (event_id) DO NOTHING"
    )
    op.execute(TARGET_DDL)
    op.execute(ABORT_INTENT_DDL)


def downgrade() -> None:
    connection = op.get_bind()
    if connection.execute(text("SHOW transaction_isolation")).scalar_one() != "read committed":
        raise RuntimeError("Binding disable downgrade requires READ COMMITTED isolation")
    for table in (
        "speech_publication_retirements",
        "speech_publication_lifecycle_events",
        "speech_binding_disable_intents",
        "speech_binding_disable_targets",
        "speech_binding_disable_abort_intents",
    ):
        op.execute(f"LOCK TABLE {table} IN ACCESS EXCLUSIVE MODE")
    pending = connection.execute(
        text(
            "SELECT EXISTS (SELECT 1 FROM speech_binding_disable_intents) "
            "OR EXISTS (SELECT 1 FROM speech_binding_disable_targets) "
            "OR EXISTS (SELECT 1 FROM speech_binding_disable_abort_intents) "
            "OR EXISTS (SELECT 1 FROM speech_publication_lifecycle_events "
            "WHERE action='model_binding.disable') "
            "OR EXISTS (SELECT 1 FROM speech_publication_retirements "
            "WHERE kind='binding' AND mode='disable')"
        )
    ).scalar_one()
    if pending:
        raise RuntimeError("Binding disable cancellation targets require preservation")
    op.execute("DROP TABLE speech_binding_disable_abort_intents")
    op.execute("DROP TABLE speech_binding_disable_targets")
