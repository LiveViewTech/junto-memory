"""Database query tools - read-only access to external databases.

Supports mssql and mysql. To add a new database, set env vars:
    DB_<NAME>_TYPE=mssql|mysql
    DB_<NAME>_HOST=<host>
    DB_<NAME>_PORT=<port>   (default 1433 mssql, 3306 mysql)
    DB_<NAME>_NAME=<database>
    DB_<NAME>_USER=<user>
    DB_<NAME>_PASS=<password>
    DB_<NAME>_READONLY=true
"""

import json
import re
from datetime import datetime

from mcp.server.fastmcp import Context

from shared_memory.app import mcp
from shared_memory.config import (
    DB_REGISTRY,
    SQL_BLOCKED_KEYWORDS,
    SQL_BLOCKED_KEYWORDS_MSSQL,
    SQL_BLOCKED_KEYWORDS_MYSQL,
)
from shared_memory.helpers import require_session


def _get_db_connection(db_name: str):
    """Get a connection to a registered database. Returns (conn, error)."""
    if db_name not in DB_REGISTRY:
        return None, f"Database '{db_name}' not registered. Available: {list(DB_REGISTRY.keys())}"

    config = DB_REGISTRY[db_name]
    db_type = config["type"]

    if db_type == "mssql":
        try:
            import pymssql
            conn = pymssql.connect(
                server=config["host"],
                port=config["port"],
                user=config["user"],
                password=config["password"],
                database=config["database"],
                login_timeout=10,
                timeout=config.get("query_timeout", 30),
                as_dict=True,
            )
            return conn, None
        except ImportError:
            return None, "pymssql not installed — add to requirements.txt or install the [mssql] extra"
        except Exception as e:
            return None, f"MSSQL connection failed: {str(e)}"

    elif db_type == "mysql":
        try:
            import pymysql
            import pymysql.cursors
            conn = pymysql.connect(
                host=config["host"],
                port=config["port"],
                user=config["user"],
                password=config["password"],
                database=config["database"],
                connect_timeout=10,
                read_timeout=config.get("query_timeout", 30),
                cursorclass=pymysql.cursors.DictCursor,
            )
            return conn, None
        except ImportError:
            return None, "pymysql not installed — add pymysql to requirements.txt"
        except Exception as e:
            return None, f"MySQL connection failed: {str(e)}"

    else:
        return None, f"Unsupported database type: {db_type} (supported: mssql, mysql)"


def _validate_sql_readonly(sql: str, db_type: str = "mssql") -> str | None:
    """Validate SQL is read-only. Returns error message or None if safe.

    Uses dialect-specific additional blocklists on top of the universal one.
    """
    stripped = sql.strip().rstrip(";").strip()

    if not re.match(r"^(SELECT|WITH)\b", stripped, re.IGNORECASE):
        return "Only SELECT queries are allowed (query must start with SELECT or WITH)"

    match = SQL_BLOCKED_KEYWORDS.search(stripped)
    if match:
        return f"Blocked keyword detected: '{match.group()}'. Only read-only queries are allowed."

    if db_type == "mssql":
        match = SQL_BLOCKED_KEYWORDS_MSSQL.search(stripped)
        if match:
            return f"MSSQL-specific blocked keyword: '{match.group()}'."
    elif db_type == "mysql":
        match = SQL_BLOCKED_KEYWORDS_MYSQL.search(stripped)
        if match:
            return f"MySQL-specific blocked keyword: '{match.group()}'."

    if ";" in stripped:
        return "Multiple statements (semicolons) are not allowed"

    if "--" in stripped or "/*" in stripped:
        return "SQL comments are not allowed in queries"

    return None


# ── Dialect-specific schema queries ──

_TABLE_LIST_MSSQL = """
    SELECT
        t.TABLE_SCHEMA as [schema],
        t.TABLE_NAME as name,
        t.TABLE_SCHEMA + '.' + t.TABLE_NAME as qualified_name,
        p.rows as row_count
    FROM INFORMATION_SCHEMA.TABLES t
    LEFT JOIN sys.partitions p
        ON p.object_id = OBJECT_ID(t.TABLE_SCHEMA + '.' + t.TABLE_NAME)
        AND p.index_id IN (0, 1)
    WHERE t.TABLE_TYPE = 'BASE TABLE'
    ORDER BY t.TABLE_SCHEMA, t.TABLE_NAME
"""

_TABLE_LIST_MYSQL = """
    SELECT
        TABLE_SCHEMA AS `schema`,
        TABLE_NAME AS name,
        CONCAT(TABLE_SCHEMA, '.', TABLE_NAME) AS qualified_name,
        TABLE_ROWS AS row_count
    FROM INFORMATION_SCHEMA.TABLES
    WHERE TABLE_TYPE = 'BASE TABLE'
      AND TABLE_SCHEMA = DATABASE()
    ORDER BY TABLE_NAME
"""

_TABLE_COLUMNS_MSSQL = """
    SELECT
        c.COLUMN_NAME as name,
        c.DATA_TYPE as type,
        c.CHARACTER_MAXIMUM_LENGTH as max_length,
        c.IS_NULLABLE as nullable,
        c.COLUMN_DEFAULT as default_value,
        CASE WHEN pk.COLUMN_NAME IS NOT NULL THEN 'YES' ELSE 'NO' END as is_primary_key
    FROM INFORMATION_SCHEMA.COLUMNS c
    LEFT JOIN (
        SELECT ku.COLUMN_NAME, ku.TABLE_NAME
        FROM INFORMATION_SCHEMA.TABLE_CONSTRAINTS tc
        JOIN INFORMATION_SCHEMA.KEY_COLUMN_USAGE ku
            ON tc.CONSTRAINT_NAME = ku.CONSTRAINT_NAME
        WHERE tc.CONSTRAINT_TYPE = 'PRIMARY KEY'
    ) pk ON pk.TABLE_NAME = c.TABLE_NAME AND pk.COLUMN_NAME = c.COLUMN_NAME
    WHERE c.TABLE_NAME = %s
    ORDER BY c.ORDINAL_POSITION
"""

_TABLE_COLUMNS_MYSQL = """
    SELECT
        COLUMN_NAME AS name,
        DATA_TYPE AS type,
        CHARACTER_MAXIMUM_LENGTH AS max_length,
        IS_NULLABLE AS nullable,
        COLUMN_DEFAULT AS default_value,
        CASE WHEN COLUMN_KEY = 'PRI' THEN 'YES' ELSE 'NO' END AS is_primary_key
    FROM INFORMATION_SCHEMA.COLUMNS
    WHERE TABLE_NAME = %s
      AND TABLE_SCHEMA = DATABASE()
    ORDER BY ORDINAL_POSITION
"""


@mcp.tool()
async def memory_db(
    session_id: str,
    action: str,
    database: str = None,
    query: str = None,
    table: str = None,
    limit: int = 100,
    ctx: Context = None,
) -> str:
    """
    Query external databases (read-only). Supports mssql and mysql.

    Actions:

    action="list" - List available databases with their types
        No params required. Returns name, type (mssql/mysql), host, etc.

    action="schema" - Explore database schema
        Required: database
        Optional: table (show columns for specific table)
        Without table: lists all tables. With table: shows columns.

    action="query" - Run a SELECT query
        Required: database, query
        Optional: limit (default 100, max configured per-database)
        Only SELECT/WITH statements allowed. Dialect-aware keyword blocking.

    Args:
        session_id: Your session ID
        action: One of: list, schema, query
        database: Database name from DB_REGISTRY (see action="list")
        query: SQL SELECT query — mind dialect differences:
               MSSQL uses TOP/brackets, MySQL uses LIMIT/backticks
        table: Table name for schema exploration
        limit: Max rows to return (default 100)
    """
    error = require_session(session_id)
    if error:
        return error

    # Identity for the audit trail below. Read once here rather than at each
    # call site so a missing session can never silently degrade the actor to
    # "unknown" on a write.
    from shared_memory.state import active_sessions
    session_info = active_sessions.get(session_id, {})

    # -- LIST --
    if action == "list":
        databases = []
        for name, config in DB_REGISTRY.items():
            databases.append({
                "name": name,
                "type": config["type"],
                "host": config["host"],
                "database": config["database"],
                "read_only": config.get("read_only", True),
                "max_rows": config.get("max_rows", 500),
            })
        return json.dumps({"count": len(databases), "databases": databases}, indent=2)

    if not database:
        return json.dumps({"error": "database parameter required"})

    if database not in DB_REGISTRY:
        return json.dumps({
            "error": f"Database '{database}' not registered",
            "available": list(DB_REGISTRY.keys()),
        })

    config = DB_REGISTRY[database]
    db_type = config["type"]

    # -- SCHEMA --
    if action == "schema":
        conn, err = _get_db_connection(database)
        if err:
            return json.dumps({"error": err})

        try:
            cursor = conn.cursor()
            if table:
                columns_sql = (
                    _TABLE_COLUMNS_MSSQL if db_type == "mssql" else _TABLE_COLUMNS_MYSQL
                )
                cursor.execute(columns_sql, (table,))
                columns = cursor.fetchall()
                if not columns:
                    return json.dumps({"error": f"Table '{table}' not found"})
                return json.dumps({
                    "database": database,
                    "type": db_type,
                    "table": table,
                    "column_count": len(columns),
                    "columns": columns,
                }, indent=2)
            else:
                tables_sql = (
                    _TABLE_LIST_MSSQL if db_type == "mssql" else _TABLE_LIST_MYSQL
                )
                cursor.execute(tables_sql)
                tables = cursor.fetchall()
                qualified_example = (
                    "qualified_name (e.g., picFrame.Frames)"
                    if db_type == "mssql"
                    else "qualified_name (e.g., nimbus.frames) — MySQL uses backticks for identifiers"
                )
                return json.dumps({
                    "database": database,
                    "type": db_type,
                    "table_count": len(tables),
                    "note": f"Use {qualified_example} in queries",
                    "tables": tables,
                }, indent=2)
        except Exception as e:
            return json.dumps({"error": f"Schema query failed: {str(e)}"})
        finally:
            conn.close()

    # -- QUERY --
    elif action == "query":
        if not query:
            return json.dumps({"error": "query parameter required"})

        # AUDIT (Tom-directed 2026-08-09). Every external-DB query is logged —
        # verbatim SQL, actor, database, and whether the handle permits writes.
        # RATIONALE, worth keeping because it is not the obvious one: the
        # read-only flag and the SQL guard do NOT stop a determined agent —
        # both are one env var / one direct mysql client away, and an agent
        # that has convinced itself an action is necessary will find the path.
        # What CANNOT be reasoned around is a record made after the fact.
        # Detection is the control that survives motivated reasoning; the
        # guards above only make the unsafe path require a decision.
        # Writes are stamped WARNING severity so a write against a
        # write-capable handle is greppable in one query.
        _is_write_handle = not config.get("read_only", True)
        _sql_head = (query or "").strip()[:2000]
        _verb = _sql_head.split()[0].upper() if _sql_head.split() else "?"
        _mutating = _verb not in ("SELECT", "WITH")
        try:
            from shared_memory.audit import log_audit
            log_audit(
                "db.query" if not (_is_write_handle and _mutating) else "db.write",
                session_info.get("claude_instance", "unknown"),
                session_info.get("project", ""),
                {
                    "database": database,
                    "host": config.get("host", ""),
                    "db_user": config.get("user", ""),
                    "read_only_handle": not _is_write_handle,
                    "verb": _verb,
                    "mutating": _mutating,
                    "sql": _sql_head,
                    "truncated_sql": len(query or "") > 2000,
                    "severity": "WARNING" if (_is_write_handle and _mutating) else "info",
                },
                session_id,
            )
        except Exception:
            pass  # auditing must never block a query

        if config.get("read_only", True):
            validation_error = _validate_sql_readonly(query, db_type)
            if validation_error:
                try:
                    from shared_memory.audit import log_audit
                    log_audit(
                        "db.query_rejected",
                        session_info.get("claude_instance", "unknown"),
                        session_info.get("project", ""),
                        {"database": database, "verb": _verb,
                         "reason": validation_error, "sql": _sql_head},
                        session_id,
                    )
                except Exception:
                    pass
                return json.dumps({"error": validation_error})

        max_rows = config.get("max_rows", 500)
        if limit > max_rows:
            limit = max_rows

        conn, err = _get_db_connection(database)
        if err:
            return json.dumps({"error": err})

        try:
            cursor = conn.cursor()
            cursor.execute(query)

            # MUTATING STATEMENTS MUST BE COMMITTED (billing-team 2026-08-09).
            # Both drivers default to autocommit=False and memory_db opens a
            # NEW CONNECTION PER CALL, so an INSERT/UPDATE/DELETE executed
            # fine, returned no error, and was rolled back when the connection
            # closed. No call sequence could fix it client-side: semicolons are
            # blocked so `INSERT; COMMIT` is unreachable, and a later standalone
            # COMMIT lands on a different connection.
            # ⚠️ The audit made this WORSE, not neutral: a db.write row was
            # recorded for a write that never landed, so the trail corroborated
            # the wrong conclusion. Hence the separate committed-effect event
            # below — attempted and landed are now different records.
            if _mutating:
                affected = cursor.rowcount
                conn.commit()
                try:
                    from shared_memory.audit import log_audit
                    log_audit(
                        "db.write_committed",
                        session_info.get("claude_instance", "unknown"),
                        session_info.get("project", ""),
                        {
                            "database": database, "verb": _verb,
                            "affected_rows": affected, "sql": _sql_head,
                            "severity": "WARNING",
                        },
                        session_id,
                    )
                except Exception:
                    pass
                return json.dumps({
                    "database": database,
                    "type": db_type,
                    "affected_rows": affected,
                    "committed": True,
                    "note": (
                        "Mutating statement COMMITTED. `affected_rows` is the "
                        "real effect — 0 means the statement matched nothing, "
                        "not that it failed. Read the data back on a separate "
                        "call if the effect matters."
                    ),
                }, indent=2)

            rows = cursor.fetchmany(limit + 1)
            truncated = len(rows) > limit
            if truncated:
                rows = rows[:limit]

            clean_rows = []
            for row in rows:
                clean = {}
                for k, v in row.items():
                    if isinstance(v, datetime):
                        clean[k] = v.isoformat()
                    elif isinstance(v, bytes):
                        clean[k] = v.hex()
                    elif v is None:
                        clean[k] = None
                    else:
                        clean[k] = str(v) if not isinstance(v, (int, float, bool, str)) else v
                clean_rows.append(clean)

            result = {
                "database": database,
                "type": db_type,
                "row_count": len(clean_rows),
                "truncated": truncated,
                "rows": clean_rows,
            }
            if truncated:
                hint = "LIMIT" if db_type == "mysql" else "TOP or LIMIT"
                result["note"] = f"Results truncated to {limit} rows. Use {hint} or narrow your WHERE clause."

            return json.dumps(result, indent=2)
        except Exception as e:
            # Roll back explicitly so a partially-applied multi-row DML cannot
            # be left dangling on a connection that is about to close.
            if _mutating:
                try:
                    conn.rollback()
                except Exception:
                    pass
                try:
                    from shared_memory.audit import log_audit
                    log_audit(
                        "db.write_failed",
                        session_info.get("claude_instance", "unknown"),
                        session_info.get("project", ""),
                        {"database": database, "verb": _verb,
                         "error": str(e)[:500], "sql": _sql_head,
                         "severity": "WARNING"},
                        session_id,
                    )
                except Exception:
                    pass
            return json.dumps({"error": f"Query failed: {str(e)}"})
        finally:
            conn.close()

    else:
        return json.dumps({"error": f"Unknown action '{action}'. Must be one of: list, schema, query"})
