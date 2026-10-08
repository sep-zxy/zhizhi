"""PostgreSQL migrations and account-scoped transactional helpers."""

from __future__ import annotations

import hashlib
import json
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from datetime import datetime

import psycopg
from psycopg import sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

MIGRATIONS = Path(__file__).resolve().parent / "migrations"


def connect(dsn: str) -> psycopg.Connection[dict[str, Any]]:
    return psycopg.connect(dsn, row_factory=dict_row, connect_timeout=5)  # pyright: ignore[reportReturnType, reportArgumentType]


def apply_migrations(dsn: str) -> list[str]:
    """Apply each numbered migration exactly once and verify its file hash."""
    applied_now: list[str] = []
    with connect(dsn) as conn:
        conn.execute("SELECT pg_advisory_xact_lock(782136410197)")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS growth_schema_migrations ("
            "version TEXT PRIMARY KEY, sha256 CHAR(64) NOT NULL, "
            "applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"
        )
        for file in sorted(MIGRATIONS.glob("[0-9][0-9][0-9]_*.sql")):
            digest = hashlib.sha256(file.read_bytes()).hexdigest()
            existing = conn.execute(
                "SELECT sha256 FROM growth_schema_migrations WHERE version=%s", (file.stem,)
            ).fetchone()
            if existing:
                if existing["sha256"].strip() != digest:
                    raise RuntimeError(f"已应用迁移内容发生变化：{file.name}")
                continue
            # Versioned migration files are trusted repository code, not user SQL.
            conn.execute(sql.SQL(file.read_text(encoding="utf-8")))  # pyright: ignore[reportArgumentType]
            conn.execute(
                "INSERT INTO growth_schema_migrations(version, sha256) VALUES (%s, %s)",
                (file.stem, digest),
            )
            applied_now.append(file.stem)
    return applied_now


def payload_hash(endpoint: str, payload: dict[str, Any]) -> str:
    canonical = json.dumps(
        {"endpoint": endpoint, "payload": payload},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def enqueue_projection(
    conn: psycopg.Connection[dict[str, Any]], *, account_id: uuid.UUID,
    source_type: str, source_id: uuid.UUID, source_version: int, action: str,
) -> str:
    """Record one deterministic projection in the caller's business transaction."""
    key = hashlib.sha256(
        f"v1:{account_id}:{source_type}:{source_id}:{source_version}:{action}".encode()
    ).hexdigest()
    conn.execute(
        "INSERT INTO growth_memory_jobs "
        "(account_id, source_type, source_id, source_version, projection_key, action) "
        "VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING",
        (account_id, source_type, source_id, source_version, key, action),
    )
    return key


def account_lock(conn: psycopg.Connection[dict[str, Any]], account_id: uuid.UUID) -> int:
    """Allocate a seq while holding the account row lock until commit."""
    conn.execute(
        "INSERT INTO growth_sync_heads(account_id) VALUES (%s) ON CONFLICT DO NOTHING",
        (account_id,),
    )
    row = conn.execute(
        "SELECT last_seq FROM growth_sync_heads WHERE account_id=%s FOR UPDATE",
        (account_id,),
    ).fetchone()
    assert row is not None
    seq = int(row["last_seq"]) + 1
    conn.execute(
        "UPDATE growth_sync_heads SET last_seq=%s WHERE account_id=%s",
        (seq, account_id),
    )
    return seq


def record_change(
    conn: psycopg.Connection[dict[str, Any]],
    *,
    account_id: uuid.UUID,
    trace_id: uuid.UUID,
    entity_type: str,
    entity_id: uuid.UUID,
    event_type: str,
    revision: int,
    payload: dict[str, Any],
    deleted_at: datetime | None = None,
) -> int:
    seq = account_lock(conn, account_id)
    conn.execute(
        "INSERT INTO growth_domain_events "
        "(account_id, event_id, trace_id, entity_type, entity_id, event_type, payload) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s)",
        (account_id, uuid.uuid4(), trace_id, entity_type, entity_id, event_type, Jsonb(payload)),
    )
    conn.execute(
        "INSERT INTO growth_change_feed "
        "(account_id, change_seq, entity_type, entity_id, event_type, "
        "revision, payload, deleted_at) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
        (account_id, seq, entity_type, entity_id, event_type, revision,
         Jsonb(payload), deleted_at),
    )
    return seq
