"""
OSCAR API Key Authentication for MySQL Protocol

PRD-MySQL-Auth: Enables MySQL protocol clients to authenticate using OSCAR API keys
instead of static username/password. When enabled via config, the MySQL password
field is treated as an OSCAR API key, validated via OSCAR middleware, and
permission-checked to ensure the user has `access:kore:mysql` permission.

This module provides synchronous authentication for the MySQL proxy, which cannot
use async functions in its check_auth() callback.

All authentication goes through OSCAR middleware's /api/v1/auth/validate-internal
endpoint, which handles Vault lookup, Redis caching, and permission checking.

Login audit logging is sent to OSCAR middleware's /api/v1/user-audit/login endpoint
for both successful and failed authentication attempts.

Circuit breaker scope: per-process global
Assumes MindsDB MySQL proxy uses thread-per-connection model.
"""

import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, Optional, Set

from mindsdb.utilities import log

logger = log.getLogger(__name__)

# Thread pool for fire-and-forget audit logging (daemon threads)
_audit_executor: Optional[ThreadPoolExecutor] = None
_audit_executor_lock = threading.Lock()

# =============================================================================
# Constants
# =============================================================================

USER_APIKEY_PREFIX = "oscar_"  # OSCAR API key prefix

# =============================================================================
# Thread-Safe Circuit Breaker
# =============================================================================


class ThreadSafeCircuitBreaker:
    """
    Thread-safe circuit breaker for upstream services.
    Scope: Global per process (shared across all connections in worker).

    States:
    - closed: Healthy, all requests go through
    - open: Failing, requests fast-fail immediately
    - half-open: Testing, allow one request to check recovery
    """

    def __init__(self, name: str, failure_threshold: int = 5, reset_timeout: int = 30):
        self._name = name
        self._failure_threshold = failure_threshold
        self._reset_timeout = reset_timeout
        self._lock = threading.Lock()
        self._failure_count = 0
        self._last_failure_time = 0.0
        self._state = "closed"  # closed = healthy, open = failing, half-open = testing

    def is_open(self) -> bool:
        """Check if circuit is open (failing). Returns False if requests should proceed."""
        with self._lock:
            if self._state == "open":
                # Check if we should try again (half-open)
                if time.time() - self._last_failure_time > self._reset_timeout:
                    self._state = "half-open"
                    logger.info(f"[OSCAR_AUTH] {self._name} circuit breaker HALF-OPEN, allowing test request")
                    return False
                return True
            return False

    def record_success(self) -> None:
        """Record a successful request, close the circuit."""
        with self._lock:
            if self._state == "half-open":
                logger.info(f"[OSCAR_AUTH] {self._name} circuit breaker CLOSED after successful test")
            self._failure_count = 0
            self._state = "closed"

    def record_failure(self) -> None:
        """Record a failed request, potentially open the circuit."""
        with self._lock:
            self._failure_count += 1
            self._last_failure_time = time.time()
            if self._failure_count >= self._failure_threshold:
                if self._state != "open":
                    logger.error(f"[OSCAR_AUTH] {self._name} circuit breaker OPEN after {self._failure_count} failures")
                self._state = "open"

    def get_state(self) -> str:
        """Get current circuit state."""
        with self._lock:
            return self._state


# Module-level singleton (per-process)
_middleware_circuit: Optional[ThreadSafeCircuitBreaker] = None


def _get_circuit_breaker(config: Dict[str, Any]) -> ThreadSafeCircuitBreaker:
    """Get or create circuit breaker with config-specified thresholds."""
    global _middleware_circuit

    threshold = config.get("circuit_breaker_threshold", 5)
    reset_s = config.get("circuit_breaker_reset_s", 30)

    if _middleware_circuit is None:
        _middleware_circuit = ThreadSafeCircuitBreaker("Middleware", threshold, reset_s)

    return _middleware_circuit


# =============================================================================
# Auth Deadline Timer
# =============================================================================


class AuthDeadline:
    """
    Enforces total auth time budget across sequential operations.
    Aborts auth if deadline exceeded before any step.
    """

    def __init__(self, total_budget_ms: int = 5000):
        self._deadline = time.time() + (total_budget_ms / 1000.0)
        self._total_budget_ms = total_budget_ms

    def remaining_ms(self) -> int:
        """Returns remaining time in milliseconds, or 0 if expired."""
        remaining = self._deadline - time.time()
        return max(0, int(remaining * 1000))

    def is_expired(self) -> bool:
        """Check if deadline has passed."""
        return time.time() >= self._deadline

    def check_or_raise(self, step_name: str) -> None:
        """Raise if deadline expired before starting a step."""
        if self.is_expired():
            raise AuthDeadlineExceeded(f"Auth deadline exceeded before {step_name}")

    def get_timeout_for_step(self, max_step_timeout_ms: int) -> int:
        """Returns min(remaining, max_step_timeout) for a step."""
        return min(self.remaining_ms(), max_step_timeout_ms)


class AuthDeadlineExceeded(Exception):
    """Raised when auth total budget is exceeded."""

    pass


# =============================================================================
# Bounded Session Context Cache
# =============================================================================


class BoundedSessionContextCache:
    """
    Thread-safe, bounded cache for session user context.
    - Max entries prevent unbounded growth
    - TTL ensures stale entries are cleaned
    - LRU eviction when full
    """

    def __init__(self, max_size: int = 10000, ttl_seconds: int = 3600):
        self._cache: OrderedDict = OrderedDict()
        self._lock = threading.Lock()
        self._max_size = max_size
        self._ttl = ttl_seconds

    def set(self, session_id: str, user_id: str, user_type: str, username: str) -> None:
        """Store user context for a session."""
        with self._lock:
            # Remove if exists (for LRU ordering)
            self._cache.pop(session_id, None)
            # Add with timestamp
            self._cache[session_id] = {
                "user_id": user_id,
                "user_type": user_type,
                "username": username,
                "expires_at": time.time() + self._ttl,
            }
            # Evict oldest if over limit
            while len(self._cache) > self._max_size:
                self._cache.popitem(last=False)

    def get(self, session_id: str) -> Dict[str, str]:
        """Get user context for a session, or empty dict if not found/expired."""
        with self._lock:
            entry = self._cache.get(session_id)
            if entry is None:
                return {}
            # Check TTL
            if time.time() > entry["expires_at"]:
                self._cache.pop(session_id, None)
                return {}
            return {
                "user_id": entry["user_id"],
                "user_type": entry["user_type"],
                "username": entry["username"],
            }

    def remove(self, session_id: str) -> None:
        """Remove session context (call on disconnect)."""
        with self._lock:
            self._cache.pop(session_id, None)


# Module-level singleton
_session_contexts: Optional[BoundedSessionContextCache] = None


def _get_session_cache(config: Dict[str, Any]) -> BoundedSessionContextCache:
    """Lazy singleton with configurable limits."""
    global _session_contexts
    if _session_contexts is None:
        _session_contexts = BoundedSessionContextCache(
            max_size=config.get("session_cache_max_size", 10000),
            ttl_seconds=config.get("session_cache_ttl_seconds", 3600),
        )
    return _session_contexts


def set_user_context(session_id: str, user_id: str, user_type: str, username: str, config: Dict[str, Any]) -> None:
    """Store user context after successful auth."""
    cache = _get_session_cache(config)
    cache.set(session_id, user_id, user_type, username)


def get_user_context(session_id: str, config: Dict[str, Any]) -> Dict[str, str]:
    """Get stored user context for a session."""
    cache = _get_session_cache(config)
    return cache.get(session_id)


def clear_user_context(session_id: str, config: Dict[str, Any]) -> None:
    """Clear user context on disconnect."""
    cache = _get_session_cache(config)
    cache.remove(session_id)


# =============================================================================
# API Key Validation via Middleware
# =============================================================================


def _validate_api_key_via_middleware(
    api_key: str,
    config: Dict[str, Any],
    deadline: AuthDeadline,
) -> Optional[Dict[str, Any]]:
    """
    Validate a user API key via OSCAR middleware's internal endpoint.

    This is the single point of authentication - middleware handles:
    - Vault lookup
    - Redis caching
    - User info retrieval
    - Permission checking

    Returns validation response dict if valid, None if invalid.
    """
    middleware_circuit = _get_circuit_breaker(config)

    # Quick format check - must start with expected prefix
    if not api_key.startswith(USER_APIKEY_PREFIX):
        logger.debug(f"[OSCAR_AUTH] API key does not start with expected prefix '{USER_APIKEY_PREFIX}'")
        return None

    # Check circuit breaker
    if middleware_circuit.is_open():
        logger.warning("[OSCAR_AUTH] Middleware circuit breaker open, fast-failing")
        return None

    # Call middleware's internal validation endpoint
    try:
        import requests

        deadline.check_or_raise("middleware_validate")
        middleware_timeout = deadline.get_timeout_for_step(config.get("middleware_timeout_ms", 4000))
        if middleware_timeout <= 0:
            raise AuthDeadlineExceeded("No time remaining for middleware_validate")

        middleware_url = config.get("middleware_url", "https://middleware:5200")
        url = f"{middleware_url}/api/v1/auth/validate-internal"

        # Get the required permission from config
        required_permission = config.get("required_permission", "access:kore:mysql")

        response = requests.post(
            url,
            headers={
                "X-Internal-Service": "kore",
                "Content-Type": "application/json",
            },
            json={
                "api_key": api_key,
                "include_permissions": True,
                "required_permission": required_permission,
            },
            verify=config.get("ssl_verify", False),
            timeout=middleware_timeout / 1000.0,
        )

        if response.status_code == 200:
            data = response.json()
            middleware_circuit.record_success()

            if not data.get("valid"):
                error_msg = data.get("error", "Unknown validation error")
                logger.warning(f"[OSCAR_AUTH] API key validation failed: {error_msg}")
                return None

            # Return the validation data
            return {
                "user_id": data.get("user_id"),
                "username": data.get("username"),
                "user_type": data.get("user_type", "user"),
                "key_id": data.get("key_id"),
                "key_suffix": data.get("key_suffix"),
                "description": data.get("description"),
                "permissions": data.get("permissions", []),
                "has_permission": data.get("has_permission"),
            }

        elif response.status_code == 403:
            # Internal service header required or not authorized
            logger.error(
                f"[OSCAR_AUTH] Middleware returned 403 - check X-Internal-Service header. "
                f"Response: {response.text[:200]}"
            )
            middleware_circuit.record_failure()
            return None

        else:
            logger.error(
                f"[OSCAR_AUTH] Middleware validation failed: status={response.status_code}, "
                f"response={response.text[:200]}"
            )
            middleware_circuit.record_failure()
            return None

    except AuthDeadlineExceeded:
        raise
    except requests.exceptions.Timeout:
        logger.error("[OSCAR_AUTH] Middleware request timed out")
        middleware_circuit.record_failure()
        return None
    except requests.exceptions.ConnectionError as e:
        logger.error(f"[OSCAR_AUTH] Cannot connect to middleware: {e}")
        middleware_circuit.record_failure()
        return None
    except Exception as e:
        logger.error(f"[OSCAR_AUTH] Middleware validation error: {e}")
        middleware_circuit.record_failure()
        return None


# =============================================================================
# Permission Check
# =============================================================================


def has_permission(permissions: Set[str], required: str) -> bool:
    """
    Check if permission set satisfies required permission.

    Supports:
    - Exact match
    - Admin wildcard (manage:all)
    - Subject-level manage (manage:kore grants all kore permissions)
    """
    # Admin wildcard
    if "manage:all" in permissions:
        return True

    # Exact match
    if required in permissions:
        return True

    # Parse required permission
    parts = required.split(":")
    if len(parts) < 2:
        return False

    _, subject = parts[0], parts[1]

    # Subject wildcard (manage:kore grants all kore:* permissions)
    base_subject = subject.split(":")[0] if ":" in subject else subject
    if f"manage:{base_subject}" in permissions:
        return True

    return False


# =============================================================================
# Auth Failure Response
# =============================================================================


def _auth_failure(
    username: str,
    host: str,
    reason: str,
    config: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Return generic auth failure to client, log specific reason server-side.

    SECURITY: All failures return the same generic message to prevent
    information leakage. Specific reasons are logged server-side only.

    Args:
        username: The username attempted (default to "unknown" if None/empty)
        host: Client host (default to "unknown" if None/empty)
        reason: Specific failure reason (logged only, never sent to client)
        config: OSCAR auth config (for audit logging)

    Returns:
        Auth failure dict for check_auth()
    """
    # Sanitize inputs - ensure we always have displayable values
    safe_username = username if username else "unknown"
    safe_host = host if host else "unknown"

    # Log specific reason server-side for debugging
    logger.warning(f"[OSCAR_AUTH] Auth failed for user '{safe_username}' from {safe_host}: {reason}")

    # Send audit event for failed login (fire-and-forget)
    if config:
        send_login_audit(
            config=config,
            username=safe_username,
            success=False,
            ip_address=safe_host if safe_host != "unknown" else None,
            error_message=reason,
        )

    # Return generic error to client (MySQL standard format)
    return {
        "success": False,
        "msg": f"Access denied for user '{safe_username}'@'{safe_host}' (using password: YES)",
    }


# =============================================================================
# Login Audit Logging (Fire-and-Forget)
# =============================================================================


def _get_audit_executor() -> ThreadPoolExecutor:
    """Get or create thread pool for audit logging."""
    global _audit_executor
    with _audit_executor_lock:
        if _audit_executor is None:
            # Small pool, daemon threads - won't block shutdown
            _audit_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="kore_audit")
    return _audit_executor


def _send_login_audit_sync(
    config: Dict[str, Any],
    username: str,
    success: bool,
    ip_address: Optional[str] = None,
    user_id: Optional[str] = None,
    session_id: Optional[str] = None,
    error_message: Optional[str] = None,
) -> None:
    """
    Send login audit event to OSCAR middleware (synchronous, for thread pool).

    This is a fire-and-forget operation - failures are logged but don't affect auth.
    Called in a background thread to avoid slowing down authentication.
    """
    try:
        import requests

        middleware_url = config.get("middleware_url", "https://middleware:5200")
        url = f"{middleware_url}/api/v1/user-audit/login"

        # Build audit payload matching LoginAuditCreate schema
        payload = {
            "username": username or "unknown",
            "auth_provider": "kore_mysql",
            "success": success,
            "resource_id": "kore_mysql",  # Identifies Kore MySQL protocol logins in UI
        }

        # Add optional fields
        if ip_address:
            payload["ip_address"] = ip_address
        if user_id:
            payload["user_id"] = user_id
        if session_id:
            payload["session_id"] = session_id
        if error_message:
            payload["error_message"] = error_message[:500]  # Truncate long messages

        # Short timeout - audit is best-effort
        response = requests.post(
            url,
            headers={
                "X-Internal-Service": "kore",
                "Content-Type": "application/json",
            },
            json=payload,
            verify=config.get("ssl_verify", False),
            timeout=2.0,  # Fast timeout for fire-and-forget
        )

        if response.status_code == 200:
            logger.debug(f"[OSCAR_AUTH] Login audit sent: username={username}, success={success}")
        else:
            logger.warning(
                f"[OSCAR_AUTH] Login audit failed: status={response.status_code}, response={response.text[:100]}"
            )

    except Exception as e:
        # Never fail auth due to audit issues
        logger.warning(f"[OSCAR_AUTH] Login audit error (non-fatal): {e}")


def send_login_audit(
    config: Dict[str, Any],
    username: str,
    success: bool,
    ip_address: Optional[str] = None,
    user_id: Optional[str] = None,
    session_id: Optional[str] = None,
    error_message: Optional[str] = None,
) -> None:
    """
    Send login audit event asynchronously (fire-and-forget).

    Uses a thread pool to send audit events without blocking authentication.
    Failures are logged but don't affect the auth flow.

    Args:
        config: OSCAR auth configuration
        username: Username attempting login
        success: Whether login succeeded
        ip_address: Client IP address
        user_id: OSCAR user UUID (for successful logins)
        session_id: Session ID (for successful logins)
        error_message: Error description (for failed logins)
    """
    # Check if audit logging is enabled (default: true)
    if not config.get("audit_enabled", True):
        return

    try:
        executor = _get_audit_executor()
        executor.submit(
            _send_login_audit_sync,
            config,
            username,
            success,
            ip_address,
            user_id,
            session_id,
            error_message,
        )
    except Exception as e:
        # Don't fail auth if we can't submit audit task
        logger.debug(f"[OSCAR_AUTH] Could not submit audit task: {e}")


# =============================================================================
# Main Entry Point
# =============================================================================


def check_oscar_auth(
    username: str,
    api_key: str,
    config: Dict[str, Any],
    client_host: str = "unknown",
    session_id: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Main entry point for OSCAR API key authentication.

    Called from middleware.py check_auth() when OSCAR auth is enabled.

    Authentication flow:
    1. Call middleware's /api/v1/auth/validate-internal endpoint
    2. Middleware validates API key via Vault (with Redis caching)
    3. Middleware returns user info and permission check result
    4. Store session context for audit

    Args:
        username: MySQL username (for logging, not used for auth)
        api_key: MySQL password field containing OSCAR API key
        config: oscar_mysql_auth configuration dict
        client_host: Client IP from mysql_proxy.self.client_address[0]
        session_id: Optional session ID for context storage

    Returns:
        Dict with keys:
        - success: True/False
        - username: OSCAR username (if success)
        - user_id: OSCAR user UUID (if success)
        - user_type: 'user' or 'system' (if success)
        - msg: Error message (if failure)
    """
    # Handle password as bytes (some MySQL clients send bytes)
    if isinstance(api_key, bytes):
        try:
            api_key = api_key.decode("utf-8")
        except UnicodeDecodeError:
            return _auth_failure(username, client_host, "Invalid API key encoding (not UTF-8)", config)

    # Empty password check
    if not api_key:
        return _auth_failure(username, client_host, "Empty password/API key", config)

    # Create deadline for total auth budget
    deadline = AuthDeadline(total_budget_ms=config.get("total_auth_budget_ms", 5000))

    try:
        # Step 1: Validate API key via middleware
        # This single call handles Vault lookup, caching, user info, and permission check
        validation_result = _validate_api_key_via_middleware(api_key, config, deadline)

        if validation_result is None:
            return _auth_failure(username, client_host, "Invalid or expired API key", config)

        user_id = validation_result.get("user_id")
        if not user_id:
            return _auth_failure(username, client_host, "API key missing user_id", config)

        # Step 2: Check if middleware already confirmed the required permission
        has_perm = validation_result.get("has_permission")
        if has_perm is False:
            # Middleware explicitly checked and user doesn't have permission
            required_permission = config.get("required_permission", "access:kore:mysql")
            return _auth_failure(
                username,
                client_host,
                f"Missing permission '{required_permission}', user_id={user_id}",
                config,
            )

        # If has_permission is None, middleware didn't check - do local check
        if has_perm is None:
            permissions = set(validation_result.get("permissions", []))
            required_permission = config.get("required_permission", "access:kore:mysql")
            if not has_permission(permissions, required_permission):
                return _auth_failure(
                    username,
                    client_host,
                    f"Missing permission '{required_permission}', user_id={user_id}",
                    config,
                )

        # Step 3: Get user info from validation result
        oscar_username = validation_result.get("username")
        if not oscar_username:
            return _auth_failure(
                username,
                client_host,
                f"Failed to resolve username for user_id={user_id}",
                config,
            )
        user_type = validation_result.get("user_type", "user")

        # Step 4: Store session context for audit
        if session_id:
            set_user_context(session_id, user_id, user_type, oscar_username, config)

        # Step 5: Send login audit for successful authentication (fire-and-forget)
        send_login_audit(
            config=config,
            username=oscar_username,
            success=True,
            ip_address=client_host if client_host != "unknown" else None,
            user_id=user_id,
            session_id=session_id,
        )

        logger.info(f"[OSCAR_AUTH] Auth successful for user '{oscar_username}' (user_id={user_id}, type={user_type})")

        return {
            "success": True,
            "username": oscar_username,
            "user_id": user_id,
            "user_type": user_type,
        }

    except AuthDeadlineExceeded as e:
        return _auth_failure(username, client_host, str(e), config)
    except Exception as e:
        logger.exception(f"[OSCAR_AUTH] Unexpected error during auth: {e}")
        return _auth_failure(username, client_host, f"Internal error: {type(e).__name__}", config)
