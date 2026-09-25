from __future__ import annotations

import os
from typing import Any

import psycopg
from psycopg.rows import dict_row

from .security import hash_password, verify_password


def _connection() -> psycopg.Connection[Any]:
    url = os.getenv("POSTGRES_URL", "")
    if not url:
        raise RuntimeError("POSTGRES_URL is required for user management")
    return psycopg.connect(url, row_factory=dict_row)


def ensure_schema() -> None:
    with _connection() as connection, connection.cursor() as cursor:
        cursor.execute("CREATE TABLE IF NOT EXISTS app_users (email TEXT PRIMARY KEY, password_hash TEXT NOT NULL, role TEXT NOT NULL CHECK(role IN ('admin','client')), active BOOLEAN NOT NULL DEFAULT TRUE, created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())")
        cursor.execute("SELECT COUNT(*) AS count FROM app_users")
        if cursor.fetchone()["count"] == 0:
            email, password = os.getenv("ADMIN_EMAIL", ""), os.getenv("ADMIN_INITIAL_PASSWORD", "")
            if not email or not password:
                raise RuntimeError("Set ADMIN_EMAIL and ADMIN_INITIAL_PASSWORD to bootstrap the first admin")
            cursor.execute("INSERT INTO app_users (email,password_hash,role) VALUES (%s,%s,'admin')", (email.lower(), hash_password(password)))


def authenticate(email: str, password: str) -> dict[str, Any] | None:
    ensure_schema()
    with _connection() as connection, connection.cursor() as cursor:
        cursor.execute("SELECT email,password_hash,role,active FROM app_users WHERE email=%s", (email.lower(),))
        user = cursor.fetchone()
    return user if user and user["active"] and verify_password(password, user["password_hash"]) else None


def list_users() -> list[dict[str, Any]]:
    ensure_schema()
    with _connection() as connection, connection.cursor() as cursor:
        cursor.execute("SELECT email,role,active,created_at FROM app_users ORDER BY created_at")
        return list(cursor.fetchall())


def upsert_user(email: str, password: str | None, role: str, active: bool) -> None:
    if role not in {"admin", "client"}:
        raise ValueError("role must be admin or client")
    ensure_schema()
    with _connection() as connection, connection.cursor() as cursor:
        cursor.execute("SELECT password_hash FROM app_users WHERE email=%s", (email.lower(),))
        existing = cursor.fetchone()
        if not existing and not password:
            raise ValueError("password is required for a new user")
        password_hash = hash_password(password) if password else existing["password_hash"]
        cursor.execute("INSERT INTO app_users(email,password_hash,role,active) VALUES(%s,%s,%s,%s) ON CONFLICT(email) DO UPDATE SET password_hash=EXCLUDED.password_hash,role=EXCLUDED.role,active=EXCLUDED.active", (email.lower(), password_hash, role, active))


def delete_user(email: str) -> None:
    ensure_schema()
    with _connection() as connection, connection.cursor() as cursor:
        cursor.execute("DELETE FROM app_users WHERE email=%s AND role <> 'admin'", (email.lower(),))
