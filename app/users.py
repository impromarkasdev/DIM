from __future__ import annotations

import os
import logging
import hashlib
import time
from functools import wraps
from threading import Lock
from uuid import uuid4
from typing import Any, Callable, TypeVar

import psycopg
from psycopg_pool import ConnectionPool
from psycopg_pool import PoolTimeout
from psycopg.rows import dict_row

from .security import hash_password, verify_password


_pool: ConnectionPool | None = None
_pool_lock = Lock()
_schema_lock = Lock()
_jobs_schema_lock = Lock()
_schema_ready = False
_jobs_schema_ready = False
logger = logging.getLogger(__name__)
T = TypeVar("T")


def _retry_database_operation(function: Callable[..., T]) -> Callable[..., T]:
    """Retry idempotent DB operations after stale pooled-connection failures."""
    @wraps(function)
    def wrapped(*args: Any, **kwargs: Any) -> T:
        for attempt in range(3):
            try:
                return function(*args, **kwargs)
            except (psycopg.OperationalError, psycopg.InterfaceError, PoolTimeout) as exc:
                if attempt == 2:
                    logger.exception("Database unavailable during %s", function.__name__)
                    raise RuntimeError("Database temporarily unavailable; please retry") from exc
                delay = 0.2 * (2 ** attempt)
                logger.warning(
                    "Database connection failed during %s; retry %s/3 in %.1fs",
                    function.__name__, attempt + 2, delay,
                )
                time.sleep(delay)
        raise RuntimeError("Database temporarily unavailable; please retry")
    return wrapped


def _connection_pool() -> ConnectionPool:
    """Reuse connections across warm Vercel Python function invocations."""
    global _pool
    url = os.getenv("POSTGRES_URL", "")
    if not url:
        raise RuntimeError("POSTGRES_URL is required for user management")
    if _pool is None or _pool.closed:
        with _pool_lock:
            if _pool is None or _pool.closed:
                # Recycle idle TLS connections quickly and health-check each
                # checkout. A Vercel instance can remain warm after Neon or its
                # pooler has already closed an idle SSL socket.
                _pool = ConnectionPool(
                    conninfo=url,
                    min_size=0,
                    max_size=3,
                    timeout=12,
                    reconnect_timeout=5,
                    max_idle=20,
                    max_lifetime=180,
                    check=ConnectionPool.check_connection,
                    kwargs={"row_factory": dict_row, "connect_timeout": 5},
                )
    return _pool


def ensure_schema() -> None:
    global _schema_ready
    if _schema_ready:
        return
    with _schema_lock:
        if _schema_ready:
            return
        with _connection_pool().connection() as connection, connection.cursor() as cursor:
            cursor.execute("CREATE TABLE IF NOT EXISTS app_users (email TEXT PRIMARY KEY, password_hash TEXT NOT NULL, role TEXT NOT NULL CHECK(role IN ('admin','client')), active BOOLEAN NOT NULL DEFAULT TRUE, created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())")
            # Online migration: existing installations previously used email as the key.
            cursor.execute("ALTER TABLE app_users ADD COLUMN IF NOT EXISTS id TEXT")
            cursor.execute("SELECT email FROM app_users WHERE id IS NULL")
            for row in cursor.fetchall():
                cursor.execute("UPDATE app_users SET id=%s WHERE email=%s", (str(uuid4()), row["email"]))
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
                "CREATE INDEX IF NOT EXISTS auth_login_attempts_window_idx ON auth_login_attempts(window_started_at)"
            )
            cursor.execute("SELECT COUNT(*) AS count FROM app_users")
            if cursor.fetchone()["count"] == 0:
                email, password = os.getenv("ADMIN_EMAIL", ""), os.getenv("ADMIN_INITIAL_PASSWORD", "")
                if not email or not password:
                    raise RuntimeError("Set ADMIN_EMAIL and ADMIN_INITIAL_PASSWORD to bootstrap the first admin")
                cursor.execute(
                    "INSERT INTO app_users (id,email,password_hash,role) VALUES (%s,%s,%s,'admin') ON CONFLICT (email) DO NOTHING",
                    (str(uuid4()), email.lower(), hash_password(password)),
                )
        _schema_ready = True


def ensure_jobs_schema() -> None:
    """Initialize asynchronous-job storage independently from login tables."""
    global _jobs_schema_ready
    if _jobs_schema_ready:
        return
    with _jobs_schema_lock:
        if _jobs_schema_ready:
            return
        with _connection_pool().connection() as connection, connection.cursor() as cursor:
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
                "CREATE UNIQUE INDEX IF NOT EXISTS dim_jobs_owner_idempotency_key ON dim_jobs(owner_email,idempotency_key) WHERE idempotency_key IS NOT NULL"
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
                "CREATE INDEX IF NOT EXISTS dim_job_items_job_status ON dim_job_items(job_id,status,item_index)"
            )
        _jobs_schema_ready = True


def authenticate(email: str, password: str) -> dict[str, Any] | None:
    for attempt in range(3):
        try:
            ensure_schema()
            with _connection_pool().connection() as connection, connection.cursor() as cursor:
                cursor.execute("SELECT id,email,password_hash,role,active FROM app_users WHERE email=%s", (email.lower(),))
                user = cursor.fetchone()
            return user if user and user["active"] and verify_password(password, user["password_hash"]) else None
        except (psycopg.OperationalError, psycopg.InterfaceError, PoolTimeout) as exc:
            if attempt < 2:
                delay = 0.2 * (2 ** attempt)
                logger.warning(
                    "Database connection failed during login; retry %s/3 in %.1fs",
                    attempt + 2, delay,
                )
                # The pool discards a connection marked BAD on context exit;
                # a short pause lets its worker replace it before the retry.
                time.sleep(delay)
                continue
            logger.exception("Database unavailable during login")
            raise RuntimeError("Database temporarily unavailable; please retry") from exc
    raise RuntimeError("Database temporarily unavailable; please retry")


@_retry_database_operation
def session_is_active(email: str, role: str) -> bool:
    """Check persisted role/active status so demotion or deactivation takes effect immediately."""
    ensure_schema()
    with _connection_pool().connection() as connection, connection.cursor() as cursor:
        cursor.execute(
            "SELECT 1 FROM app_users WHERE email=%s AND role=%s AND active=TRUE",
            (email.strip().lower(), role),
        )
        return cursor.fetchone() is not None


def _email_hash(email: str) -> str:
    return hashlib.sha256(email.strip().lower().encode("utf-8")).hexdigest()


@_retry_database_operation
def login_is_rate_limited(email: str) -> bool:
    """Check a durable per-account lockout without storing the clear email."""
    ensure_schema()
    with _connection_pool().connection() as connection, connection.cursor() as cursor:
        cursor.execute(
            "SELECT blocked_until > NOW() AS blocked FROM auth_login_attempts WHERE email_hash=%s",
            (_email_hash(email),),
        )
        row = cursor.fetchone()
    return bool(row and row["blocked"])


def record_login_failure(email: str) -> None:
    """Lock an account for 15 minutes after 10 failures in its rolling window."""
    ensure_schema()
    try:
        with _connection_pool().connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO auth_login_attempts(email_hash,attempts,window_started_at)
                VALUES(%s,1,NOW())
                ON CONFLICT(email_hash) DO UPDATE SET
                    attempts=CASE
                        WHEN auth_login_attempts.window_started_at < NOW()-INTERVAL '15 minutes' THEN 1
                        ELSE auth_login_attempts.attempts+1
                    END,
                    window_started_at=CASE
                        WHEN auth_login_attempts.window_started_at < NOW()-INTERVAL '15 minutes' THEN NOW()
                        ELSE auth_login_attempts.window_started_at
                    END,
                    blocked_until=CASE
                        WHEN auth_login_attempts.window_started_at < NOW()-INTERVAL '15 minutes' THEN NULL
                        WHEN auth_login_attempts.attempts+1 >= 10 THEN NOW()+INTERVAL '15 minutes'
                        ELSE auth_login_attempts.blocked_until
                    END
                """,
                (_email_hash(email),),
            )
            cursor.execute(
                "DELETE FROM auth_login_attempts WHERE window_started_at < NOW()-INTERVAL '30 days'"
            )
    except (psycopg.OperationalError, psycopg.InterfaceError, PoolTimeout) as exc:
        # This upsert increments a counter and must not be retried blindly after
        # an uncertain commit; translate the error for a clean HTTP 503.
        raise RuntimeError("Database temporarily unavailable; please retry") from exc


@_retry_database_operation
def clear_login_failures(email: str) -> None:
    ensure_schema()
    with _connection_pool().connection() as connection, connection.cursor() as cursor:
        cursor.execute("DELETE FROM auth_login_attempts WHERE email_hash=%s", (_email_hash(email),))


@_retry_database_operation
def list_users() -> list[dict[str, Any]]:
    ensure_schema()
    with _connection_pool().connection() as connection, connection.cursor() as cursor:
        cursor.execute("SELECT id,email,role,active,created_at FROM app_users ORDER BY created_at")
        return list(cursor.fetchall())


@_retry_database_operation
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


@_retry_database_operation
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
