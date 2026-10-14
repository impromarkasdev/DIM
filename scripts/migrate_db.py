"""Explicit, serialized PostgreSQL schema migration for the DIM service.

Run this command from a trusted deployment environment before deploying code
that depends on a new schema. Never invoke DDL from a request/queue handler.
"""

from __future__ import annotations

import os
from uuid import uuid4

from dotenv import load_dotenv

load_dotenv()

from app.security import hash_password
from app.users import _connection_pool

MIGRATION_LOCK_ID = 731_904_261_448_203


def migrate() -> None:
    pool = _connection_pool()
    try:
        with pool.connection() as connection, connection.cursor() as cursor:
            # Serialize explicit migration runs from separate deploy machines.
            cursor.execute("SELECT pg_advisory_xact_lock(%s)", (MIGRATION_LOCK_ID,))

            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS app_users (
                    email TEXT PRIMARY KEY,
                    password_hash TEXT NOT NULL,
                    role TEXT NOT NULL CHECK(role IN ('admin','client')),
                    active BOOLEAN NOT NULL DEFAULT TRUE,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )
            cursor.execute("ALTER TABLE app_users ADD COLUMN IF NOT EXISTS id TEXT")
            cursor.execute("SELECT email FROM app_users WHERE id IS NULL ORDER BY email")
            for row in cursor.fetchall():
                cursor.execute(
                    "UPDATE app_users SET id=%s WHERE email=%s AND id IS NULL",
                    (str(uuid4()), row["email"]),
                )
            cursor.execute("CREATE UNIQUE INDEX IF NOT EXISTS app_users_id_key ON app_users(id)")
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS auth_login_attempts (
                    email_hash TEXT PRIMARY KEY,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    window_started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    blocked_until TIMESTAMPTZ
                )
                """
            )
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS auth_login_attempts_window_idx "
                "ON auth_login_attempts(window_started_at)"
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS dim_jobs (
                    id UUID PRIMARY KEY,
                    owner_email TEXT NOT NULL,
                    idempotency_key TEXT,
                    job_type TEXT NOT NULL DEFAULT 'redact',
                    status TEXT NOT NULL DEFAULT 'queued',
                    total INTEGER NOT NULL,
                    completed INTEGER NOT NULL DEFAULT 0,
                    failed INTEGER NOT NULL DEFAULT 0,
                    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    last_error TEXT
                )
                """
            )
            cursor.execute(
                "ALTER TABLE dim_jobs ADD COLUMN IF NOT EXISTS job_type TEXT NOT NULL DEFAULT 'redact'"
            )
            cursor.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS dim_jobs_owner_idempotency_key "
                "ON dim_jobs(owner_email,idempotency_key) WHERE idempotency_key IS NOT NULL"
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS dim_job_items (
                    id UUID PRIMARY KEY,
                    job_id UUID NOT NULL REFERENCES dim_jobs(id) ON DELETE CASCADE,
                    item_index INTEGER NOT NULL,
                    reference TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'queued',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    queue_generation INTEGER NOT NULL DEFAULT 0,
                    queue_attempted_at TIMESTAMPTZ,
                    lease_until TIMESTAMPTZ,
                    result JSONB,
                    error TEXT,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    UNIQUE(job_id,item_index)
                )
                """
            )
            cursor.execute(
                "ALTER TABLE dim_job_items ADD COLUMN IF NOT EXISTS queue_attempted_at TIMESTAMPTZ"
            )
            cursor.execute(
                "ALTER TABLE dim_job_items ADD COLUMN IF NOT EXISTS queue_generation INTEGER NOT NULL DEFAULT 0"
            )
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS dim_job_items_job_status "
                "ON dim_job_items(job_id,status,item_index)"
            )

            cursor.execute("SELECT COUNT(*) AS count FROM app_users")
            if cursor.fetchone()["count"] == 0:
                email = os.getenv("ADMIN_EMAIL", "").strip().lower()
                password = os.getenv("ADMIN_INITIAL_PASSWORD", "")
                if not email or not password:
                    raise RuntimeError(
                        "Set ADMIN_EMAIL and ADMIN_INITIAL_PASSWORD to create the initial administrator"
                    )
                cursor.execute(
                    "INSERT INTO app_users(id,email,password_hash,role) "
                    "VALUES(%s,%s,%s,'admin') ON CONFLICT(email) DO NOTHING",
                    (str(uuid4()), email, hash_password(password)),
                )
        print("Database schema migration completed successfully.")
    finally:
        pool.close()


if __name__ == "__main__":
    migrate()
