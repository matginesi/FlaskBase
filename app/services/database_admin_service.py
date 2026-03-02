from __future__ import annotations

import json
import re
from datetime import timedelta
from typing import Any

from sqlalchemy import inspect, text

from ..extensions import db
from ..models import AppSettings, LogEvent, User, now_utc


def _format_bytes(value: Any) -> str:
    try:
        size = int(value or 0)
    except Exception:
        return "N/A"
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    if size < 1024 * 1024 * 1024:
        return f"{size / (1024 * 1024):.2f} MB"
    return f"{size / (1024 * 1024 * 1024):.2f} GB"


def _masked_engine_url() -> str:
    url = str(db.engine.url)
    if "@" not in url:
        return url
    head, tail = url.split("@", 1)
    if ":" not in head:
        return url
    scheme, creds = head.split("://", 1)
    user = creds.split(":", 1)[0]
    return f"{scheme}://{user}:***@{tail}"


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value or 0)
    except Exception:
        return default


def get_database_overview() -> dict[str, Any]:
    engine = db.engine
    insp = inspect(engine)
    dialect = str(engine.dialect.name or "").lower()
    table_names = sorted(insp.get_table_names())

    db_size_bytes = None
    active_connections = None
    idle_connections = None
    total_connections = None
    engine_version = None
    stats_reset_at = None
    current_database = str(engine.url.database or "")
    current_schema = "public"
    total_table_bytes = 0
    total_index_bytes = 0
    total_dead_rows = 0

    if dialect == "postgresql":
        db_size_bytes = db.session.execute(text("SELECT pg_database_size(current_database())")).scalar()
        active_connections = db.session.execute(
            text("SELECT COUNT(*) FROM pg_stat_activity WHERE datname = current_database() AND state = 'active'")
        ).scalar()
        idle_connections = db.session.execute(
            text("SELECT COUNT(*) FROM pg_stat_activity WHERE datname = current_database() AND state <> 'active'")
        ).scalar()
        total_connections = db.session.execute(
            text("SELECT COUNT(*) FROM pg_stat_activity WHERE datname = current_database()")
        ).scalar()
        engine_version = db.session.execute(text("SHOW server_version")).scalar()
        current_database = db.session.execute(text("SELECT current_database()")).scalar() or current_database
        current_schema = db.session.execute(text("SELECT current_schema()")).scalar() or current_schema
        stats_reset_at = db.session.execute(text("SELECT stats_reset FROM pg_stat_database WHERE datname = current_database()")).scalar()

        table_stats_raw = db.session.execute(
            text(
                """
                SELECT
                  st.relname AS table_name,
                  COALESCE(st.n_live_tup, 0) AS live_rows_estimate,
                  COALESCE(st.n_dead_tup, 0) AS dead_rows_estimate,
                  COALESCE(pg_total_relation_size(cls.oid), 0) AS total_bytes,
                  COALESCE(pg_relation_size(cls.oid), 0) AS table_bytes,
                  COALESCE(pg_indexes_size(cls.oid), 0) AS index_bytes,
                  COALESCE(st.seq_scan, 0) AS seq_scan,
                  COALESCE(st.idx_scan, 0) AS idx_scan,
                  st.last_vacuum,
                  st.last_autovacuum,
                  st.last_analyze,
                  st.last_autoanalyze
                FROM pg_stat_user_tables st
                JOIN pg_class cls ON cls.relname = st.relname
                JOIN pg_namespace ns ON ns.oid = cls.relnamespace AND ns.nspname = 'public'
                ORDER BY pg_total_relation_size(cls.oid) DESC, st.relname ASC
                """
            )
        ).mappings().all()
        table_details = [
            {
                "name": row["table_name"],
                "rows": int(row["live_rows_estimate"] or 0),
                "dead_rows": int(row["dead_rows_estimate"] or 0),
                "size": _format_bytes(row["total_bytes"]),
                "table_size": _format_bytes(row["table_bytes"]),
                "index_size": _format_bytes(row["index_bytes"]),
                "seq_scan": int(row["seq_scan"] or 0),
                "idx_scan": int(row["idx_scan"] or 0),
                "last_vacuum": row["last_vacuum"].isoformat() if row["last_vacuum"] else None,
                "last_autovacuum": row["last_autovacuum"].isoformat() if row["last_autovacuum"] else None,
                "last_analyze": row["last_analyze"].isoformat() if row["last_analyze"] else None,
                "last_autoanalyze": row["last_autoanalyze"].isoformat() if row["last_autoanalyze"] else None,
                "columns": [c["name"] for c in insp.get_columns(row["table_name"])],
            }
            for row in table_stats_raw
        ]
        total_table_bytes = sum(_safe_int(row["table_bytes"]) for row in table_stats_raw)
        total_index_bytes = sum(_safe_int(row["index_bytes"]) for row in table_stats_raw)
        total_dead_rows = sum(_safe_int(row["dead_rows"]) for row in table_details)
    else:
        table_details = []
        for tname in table_names:
            cols = [c["name"] for c in insp.get_columns(tname)]
            try:
                qname = insp.dialect.identifier_preparer.quote(tname)
                count = db.session.execute(text(f"SELECT COUNT(*) FROM {qname}")).scalar()
            except Exception:
                count = "?"
            table_details.append(
                {
                    "name": tname,
                    "rows": count,
                    "dead_rows": None,
                    "size": "N/A",
                    "table_size": "N/A",
                    "index_size": "N/A",
                    "seq_scan": None,
                    "idx_scan": None,
                    "last_vacuum": None,
                    "last_autovacuum": None,
                    "last_analyze": None,
                    "last_autoanalyze": None,
                    "columns": cols,
                }
            )

    settings_row = db.session.get(AppSettings, 1)
    info = {
        "Engine": engine.dialect.name,
        "Database": current_database or "N/A",
        "Schema": current_schema,
        "URL": _masked_engine_url(),
        "SQLAlchemy": "2.x",
    }
    if engine_version:
        info["Version"] = str(engine_version)
    if total_connections is not None:
        info["Connections"] = int(total_connections or 0)
    if active_connections is not None:
        info["Active connections"] = int(active_connections or 0)
    if idle_connections is not None:
        info["Idle connections"] = int(idle_connections or 0)
    if stats_reset_at is not None:
        info["Stats reset"] = stats_reset_at.isoformat() if hasattr(stats_reset_at, "isoformat") else str(stats_reset_at)

    return {
        "engine": engine.dialect.name.upper(),
        "database_name": current_database or "N/A",
        "database_schema": current_schema or "public",
        "engine_version": str(engine_version or "N/A"),
        "connections": {
            "total": _safe_int(total_connections, 0),
            "active": _safe_int(active_connections, 0),
            "idle": _safe_int(idle_connections, 0),
        },
        "users": User.query.count(),
        "log_events": LogEvent.query.count(),
        "settings": settings_row is not None,
        "settings_updated_at": settings_row.updated_at.isoformat() if settings_row and settings_row.updated_at else None,
        "settings_revision": int(settings_row.revision or 1) if settings_row else None,
        "settings_seed_source": settings_row.seed_source if settings_row else None,
        "settings_last_imported_at": settings_row.last_imported_at.isoformat() if settings_row and settings_row.last_imported_at else None,
        "settings_last_exported_at": settings_row.last_exported_at.isoformat() if settings_row and settings_row.last_exported_at else None,
        "settings_app_name": settings_row.app_name if settings_row else None,
        "settings_app_version": settings_row.app_version if settings_row else None,
        "db_size": _format_bytes(db_size_bytes) if db_size_bytes is not None else "N/A",
        "table_storage": _format_bytes(total_table_bytes),
        "index_storage": _format_bytes(total_index_bytes),
        "dead_rows": total_dead_rows,
        "tables": len(table_names),
        "table_details": table_details,
        "info": info,
    }


def purge_old_logs(days: int) -> dict[str, Any]:
    cutoff = now_utc() - timedelta(days=max(1, int(days or 90)))
    deleted = LogEvent.query.filter(LogEvent.ts < cutoff).delete(synchronize_session=False)
    db.session.commit()
    return {"days": max(1, int(days or 90)), "cutoff": cutoff.isoformat(), "deleted": int(deleted or 0)}


def clear_all_logs() -> dict[str, Any]:
    deleted = LogEvent.query.delete(synchronize_session=False)
    db.session.commit()
    return {"deleted": int(deleted or 0)}


def analyze_database() -> dict[str, Any]:
    with db.engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        conn.execute(text("ANALYZE"))
    return {"executed_at": now_utc().isoformat(), "action": "ANALYZE"}


def vacuum_analyze_database() -> dict[str, Any]:
    with db.engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        conn.execute(text("VACUUM (ANALYZE)"))
    return {"executed_at": now_utc().isoformat(), "action": "VACUUM (ANALYZE)"}


def export_database_snapshot() -> dict[str, Any]:
    settings_row = db.session.get(AppSettings, 1)
    users = User.query.order_by(User.id.asc()).all()
    overview = get_database_overview()
    return {
        "meta": {
            "exported_at_utc": now_utc().isoformat() + "Z",
            "engine": str(db.engine.dialect.name or ""),
            "database": str(db.engine.url.database or ""),
        },
        "overview": {
            "engine": overview.get("engine"),
            "db_size": overview.get("db_size"),
            "tables": overview.get("tables"),
            "table_storage": overview.get("table_storage"),
            "index_storage": overview.get("index_storage"),
            "connections": overview.get("connections"),
        },
        "runtime_settings": settings_row.to_dict() if settings_row else None,
        "counts": {
            "users": User.query.count(),
            "log_events": LogEvent.query.count(),
        },
        "tables": [
            {
                "name": table["name"],
                "rows": table["rows"],
                "size": table.get("size"),
                "columns": table["columns"],
            }
            for table in overview.get("table_details", [])
        ],
        "users": [
            {
                "id": user.id,
                "email": user.email,
                "name": user.name,
                "role": user.role,
                "is_active": bool(user.is_active),
                "account_status": user.account_status,
                "email_verified": bool(user.email_verified),
            }
            for user in users
        ],
    }


def export_database_snapshot_json() -> str:
    return json.dumps(export_database_snapshot(), indent=2, ensure_ascii=False)


def execute_readonly_query(sql: str, *, max_length: int = 2000, row_limit: int = 200) -> dict[str, Any]:
    raw_sql = str(sql or "").strip()
    if not raw_sql:
        raise ValueError("Query is empty.")
    if len(raw_sql) > max_length:
        raise ValueError(f"Query too long (max {max_length} chars).")

    normalized = raw_sql.upper().lstrip()
    if not normalized.startswith("SELECT"):
        raise ValueError("Only SELECT queries are allowed.")
    if ";" in raw_sql[:-1]:
        raise ValueError("Multiple queries are not allowed.")
    if "--" in raw_sql or "/*" in raw_sql or "*/" in raw_sql:
        raise ValueError("SQL comments are not allowed.")

    blocked = ("PRAGMA", "ATTACH", "DETACH", "DROP", "ALTER", "INSERT", "UPDATE", "DELETE", "REINDEX", "VACUUM")
    if any(word in normalized for word in blocked):
        raise ValueError("This query contains blocked SQL keywords.")
    if re.search(r"\bUNION\b", normalized):
        raise ValueError("UNION is not allowed in this console.")

    result = db.session.execute(text(raw_sql))
    columns = list(result.keys())
    rows = [list(row) for row in result.fetchmany(max(1, min(int(row_limit or 200), 500)))]
    return {"columns": columns, "rows": rows}
