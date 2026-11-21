"""Least-privilege grants for the app's runtime database roles, applied by migrations.

Migrations run as the database owner (the lucromei-migrate job). A migration that adds a
table calls grant_runtime() so the runtime role can use it right away, with no manual step.
Roles that do not exist (local databases, CI) are skipped. terraform/db-grants-test.sql keeps
the full list for reference and for rebuilding a database by hand.
"""

import re

from alembic import op

RUNTIME_ROLES = ("lucromei_test_runtime", "lucromei_prod_runtime")
_IDENTIFIER = re.compile(r"^[a-z_][a-z0-9_]*$")
_PRIVILEGES = {"SELECT", "INSERT", "UPDATE", "DELETE"}


def grant_runtime(table: str, privileges: tuple[str, ...]) -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    if not _IDENTIFIER.fullmatch(table) or not set(privileges) <= _PRIVILEGES:
        raise ValueError("invalid grant")
    for role in RUNTIME_ROLES:
        op.execute(
            f"DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{role}') THEN "
            f"GRANT {', '.join(privileges)} ON {table} TO {role}; END IF; END $$"
        )
