from __future__ import annotations

import os
from uuid import uuid4
from typing import Any

import psycopg
from psycopg_pool import ConnectionPool
from psycopg.rows import dict_row

from .security import hash_password, verify_password


_pool: ConnectionPool | None = None


def _connection_pool() -> ConnectionPool:
    """Reuse connections across warm Vercel Python function invocations."""
    global _pool
    url = os.getenv("POSTGRES_URL", "")
    if not url:
        raise RuntimeError("POSTGRES_URL is required for user management")
    if _pool is None:
        # Keep SSL as configured in the Neon connection URL (normally sslmode=require).
        # A small pool prevents excess database connections across Lambda instances.
        _pool = ConnectionPool(
            conninfo=url,
            min_size=1,
            max_size=3,
            timeout=8,
            kwargs={"row_factory": dict_row, "connect_timeout": 5},
        )
    return _pool


def ensure_schema() -> None:
    with _connection_pool().connection() as connection, connection.cursor() as cursor:
        cursor.execute("CREATE TABLE IF NOT EXISTS app_users (email TEXT PRIMARY KEY, password_hash TEXT NOT NULL, role TEXT NOT NULL CHECK(role IN ('admin','client')), active BOOLEAN NOT NULL DEFAULT TRUE, created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())")
        # Online migration: existing installations previously used email as the key.
        cursor.execute("ALTER TABLE app_users ADD COLUMN IF NOT EXISTS id TEXT")
        cursor.execute("SELECT email FROM app_users WHERE id IS NULL")
        for row in cursor.fetchall():
            cursor.execute("UPDATE app_users SET id=%s WHERE email=%s", (str(uuid4()), row["email"]))
        cursor.execute("CREATE UNIQUE INDEX IF NOT EXISTS app_users_id_key ON app_users(id)")
        cursor.execute("SELECT COUNT(*) AS count FROM app_users")
        if cursor.fetchone()["count"] == 0:
            email, password = os.getenv("ADMIN_EMAIL", ""), os.getenv("ADMIN_INITIAL_PASSWORD", "")
            if not email or not password:
                raise RuntimeError("Set ADMIN_EMAIL and ADMIN_INITIAL_PASSWORD to bootstrap the first admin")
            cursor.execute("INSERT INTO app_users (id,email,password_hash,role) VALUES (%s,%s,%s,'admin')", (str(uuid4()), email.lower(), hash_password(password)))


def authenticate(email: str, password: str) -> dict[str, Any] | None:
    ensure_schema()
    with _connection_pool().connection() as connection, connection.cursor() as cursor:
        cursor.execute("SELECT id,email,password_hash,role,active FROM app_users WHERE email=%s", (email.lower(),))
        user = cursor.fetchone()
    return user if user and user["active"] and verify_password(password, user["password_hash"]) else None


def list_users() -> list[dict[str, Any]]:
    ensure_schema()
    with _connection_pool().connection() as connection, connection.cursor() as cursor:
        cursor.execute("SELECT id,email,role,active,created_at FROM app_users ORDER BY created_at")
        return list(cursor.fetchall())


def upsert_user(email: str, password: str | None, role: str, active: bool) -> dict[str, Any]:
    email = email.strip().lower()
    if not email or "@" not in email:
        raise ValueError("A valid email is required")
    if role not in {"admin", "client"}:
        raise ValueError("role must be admin or client")
    ensure_schema()
    with _connection_pool().connection() as connection, connection.cursor() as cursor:
        cursor.execute("SELECT id,password_hash FROM app_users WHERE email=%s", (email,))
        existing = cursor.fetchone()
        if not existing and not password:
            raise ValueError("password is required for a new user")
        password_hash = hash_password(password) if password else existing["password_hash"]
        user_id = existing["id"] if existing else str(uuid4())
        cursor.execute("INSERT INTO app_users(id,email,password_hash,role,active) VALUES(%s,%s,%s,%s,%s) ON CONFLICT(email) DO UPDATE SET password_hash=EXCLUDED.password_hash,role=EXCLUDED.role,active=EXCLUDED.active RETURNING id,email,role,active,created_at", (user_id, email, password_hash, role, active))
        return cursor.fetchone()


def delete_user(id_or_email: str) -> bool:
    """Delete a non-admin user by UUID or email address."""
    ensure_schema()
    value = id_or_email.strip()
    if not value:
        raise ValueError("id is required")
    column = "email" if "@" in value else "id"
    with _connection_pool().connection() as connection, connection.cursor() as cursor:
        cursor.execute(f"DELETE FROM app_users WHERE {column}=%s AND role <> 'admin'", (value.lower() if column == "email" else value,))
        return cursor.rowcount == 1
