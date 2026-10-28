"""Startup check for the optional durable abort capability."""

from speechnexus_gateway.infrastructure.postgres_pool import GatewayPool


def verify_abort_authority(pool: GatewayPool, schema: str) -> bool:
    """Fail closed for both login and effective principal, without changing grants."""
    try:
        with pool.connection(timeout=3) as connection:
            connection.execute("SET LOCAL statement_timeout='3s'")
            identities = connection.execute(
                "SELECT SESSION_USER AS login, CURRENT_USER AS effective"
            ).fetchone()
            if identities is None:
                return False
            for role in dict.fromkeys((identities["login"], identities["effective"])):
                row = connection.execute(
                    "SELECT r.rolsuper OR pg_catalog.has_schema_privilege(%s::name,%s,'CREATE') "
                    "OR pg_catalog.pg_has_role(%s::name,d.datdba,'MEMBER') "
                    "OR pg_catalog.pg_has_role(%s::name,c.relowner,'MEMBER') AS unsafe, "
                    "(pg_catalog.has_table_privilege(%s::name,c.oid,'SELECT') AND "
                    "pg_catalog.has_table_privilege(%s::name,c.oid,'INSERT')) AS writable, "
                    "pg_catalog.has_table_privilege(%s::name,c.oid,"
                    "'DELETE,TRUNCATE,REFERENCES,TRIGGER') AS destructive "
                    "FROM pg_catalog.pg_roles r CROSS JOIN pg_catalog.pg_database d "
                    "JOIN pg_catalog.pg_namespace n ON n.nspname=%s "
                    "JOIN pg_catalog.pg_class c ON c.relnamespace=n.oid "
                    "AND c.relname='gateway_abort_intents' "
                    "WHERE r.rolname=%s AND d.datname=CURRENT_DATABASE()",
                    (role, schema, role, role, role, role, role, schema, role),
                ).fetchone()
                if row is None or row["unsafe"] or not row["writable"] or row["destructive"]:
                    return False
                columns = connection.execute(
                    "SELECT a.attname, "
                    "pg_catalog.has_column_privilege(%s::name,c.oid,a.attnum,'UPDATE') "
                    "AS updatable "
                    "FROM pg_catalog.pg_class c "
                    "JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace "
                    "JOIN pg_catalog.pg_attribute a ON a.attrelid=c.oid "
                    "WHERE n.nspname=%s AND c.relname='gateway_abort_intents' "
                    "AND a.attnum>0 AND NOT a.attisdropped",
                    (role, schema),
                ).fetchall()
                if {row["attname"] for row in columns if row["updatable"]} != {"dispatched_at_us"}:
                    return False
            return True
    except Exception:
        return False
