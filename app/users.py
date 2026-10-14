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
    """Retry idempotent operations after transient DB and transaction failures."""
    @wraps(function)
    def wrapped(*args: Any, **kwargs: Any) -> T:
        for attempt in range(3):
            try:
                return function(*args, **kwargs)
            except (
                psycopg.OperationalError,
                psycopg.InterfaceError,
                psycopg.errors.DeadlockDetected,
                psycopg.errors.SerializationFailure,
                PoolTimeout,
            ) as exc:
                if attempt == 2:
                    logger.exception("Database unavailable during %s", function.__name__)
                    raise RuntimeError("Database temporarily unavailable; please retry") from exc
                delay = 0.2 * (2 ** attempt)
                logger.warning(
                    "Transient database failure during %s (%s); retry %s/3 in %.1fs",
                    function.__name__, type(exc).__name__, attempt + 2, delay,
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
    """Read-only production preflight; schema DDL belongs in scripts/migrate_db.py."""
    global _schema_ready
    if _schema_ready:
        return
    with _schema_lock:
        if _schema_ready:
            return
        with _connection_pool().connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT to_regclass('public.app_users') AS users, "
                "to_regclass('public.auth_login_attempts') AS login_attempts"
            )
            tables = cursor.fetchone()
            if not tables or not tables["users"] or not tables["login_attempts"]:
                raise RuntimeError(
                    "Database schema is not initialized; run scripts/migrate_db.py before serving traffic"
                )
        _schema_ready = True


def ensure_jobs_schema() -> None:
    """Read-only preflight for durable jobs; never run DDL inside a request."""
    global _jobs_schema_ready
    if _jobs_schema_ready:
        return
    with _jobs_schema_lock:
        if _jobs_schema_ready:
            return
        with _connection_pool().connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT to_regclass('public.dim_jobs') AS jobs, "
                "to_regclass('public.dim_job_items') AS items"
            )
            tables = cursor.fetchone()
            if not tables or not tables["jobs"] or not tables["items"]:
                raise RuntimeError(
                    "Document-job schema is not initialized; run scripts/migrate_db.py before serving traffic"
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
            "SELECT 1 FROM app_users WHERE email=%s AND LOWER(BTRIM(role))=LOWER(BTRIM(%s)) AND active=TRUE",
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
def change_client_password(id_or_email: str, password: str) -> bool:
    """Set a non-admin user's password by UUID or email; never returns password data."""
    value = str(id_or_email or "").strip()
    if not value:
        raise ValueError("User ID or email is required")
    if not isinstance(password, str) or not 12 <= len(password) <= 1024:
        raise ValueError("Password must contain between 12 and 1024 characters")
    column = "email" if "@" in value else "id"
    lookup_value = value.lower() if column == "email" else value
    ensure_schema()
    with _connection_pool().connection() as connection, connection.cursor() as cursor:
        cursor.execute(f"SELECT id,role FROM app_users WHERE {column}=%s", (lookup_value,))
        user = cursor.fetchone()
        if not user:
            return False
        if str(user["role"]).strip().casefold() != "client":
            raise PermissionError("Only client account passwords can be changed through this endpoint")
        password_hash = hash_password(password)
        cursor.execute(
            "UPDATE app_users SET password_hash=%s WHERE id=%s AND LOWER(BTRIM(role)) = 'client'",
            (password_hash, user["id"]),
        )
        return cursor.rowcount == 1


@_retry_database_operation
def delete_user(id_or_email: str) -> bool:
    """Delete a non-admin user by UUID or email address."""
    ensure_schema()
    value = id_or_email.strip()
    if not value:
        raise ValueError("id is required")
    column = "email" if "@" in value else "id"
    with _connection_pool().connection() as connection, connection.cursor() as cursor:
        lookup_value = value.lower() if column == "email" else value
        cursor.execute(f"SELECT id,role FROM app_users WHERE {column}=%s", (lookup_value,))
        user = cursor.fetchone()
        if not user:
            return False
        if user["role"] == "admin":
            raise PermissionError("Administrator accounts cannot be deleted through this endpoint")
        cursor.execute("DELETE FROM app_users WHERE id=%s AND role <> 'admin'", (user["id"],))
        return cursor.rowcount == 1
