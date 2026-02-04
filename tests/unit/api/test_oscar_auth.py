"""
Unit tests for OSCAR API Key Authentication (PRD-MySQL-Auth).

Tests cover:
- Core logic: permission checks, format validation
- Middleware integration: validation via /api/v1/auth/validate-internal
- Upstream failures: middleware timeouts, circuit breaker behavior
- Generic error handling: all failures return same "Access denied" message

Architecture:
All authentication goes through OSCAR middleware's /api/v1/auth/validate-internal
endpoint. Kore does NOT access Vault or Redis directly.
"""

import pytest
import threading
import time
from unittest.mock import Mock, patch

from mindsdb.api.common.oscar_auth import (
    # Core functions
    has_permission,
    check_oscar_auth,
    _auth_failure,
    # Classes
    ThreadSafeCircuitBreaker,
    AuthDeadline,
    AuthDeadlineExceeded,
    BoundedSessionContextCache,
)


# =============================================================================
# Test Fixtures and Constants
# =============================================================================

TEST_CONFIG = {
    "enabled": True,
    "middleware_url": "https://middleware:5200",
    "middleware_timeout_ms": 4000,
    "total_auth_budget_ms": 5000,
    "circuit_breaker_threshold": 5,
    "circuit_breaker_reset_s": 30,
    "session_cache_max_size": 10000,
    "session_cache_ttl_seconds": 3600,
    "ssl_verify": False,
    "required_permission": "access:kore:mysql",
}

VALID_API_KEY = "oscar_test1234567890abcdefghijklmnopqrstuv"
VALID_USER_ID = "550e8400-e29b-41d4-a716-446655440000"


# Helper to create mock middleware response
def create_middleware_response(
    status_code: int = 200,
    valid: bool = True,
    user_id: str = VALID_USER_ID,
    username: str = "oscaruser",
    user_type: str = "user",
    has_permission: bool = True,
    permissions: list = None,
    error: str = None,
):
    """Create a mock response object mimicking middleware's validate-internal endpoint."""
    response = Mock()
    response.status_code = status_code

    if status_code == 200:
        data = {
            "valid": valid,
            "user_id": user_id if valid else None,
            "username": username if valid else None,
            "user_type": user_type if valid else None,
            "key_id": "key123" if valid else None,
            "key_suffix": "...wxyz" if valid else None,
            "has_permission": has_permission if valid else None,
            "permissions": permissions or (["access:kore:mysql"] if valid else []),
        }
        if error:
            data["error"] = error
        response.json.return_value = data
    else:
        response.text = f"Error {status_code}"
        response.json.return_value = {"error": f"Error {status_code}"}

    return response


# =============================================================================
# API Key Format Validation Tests
# =============================================================================


class TestApiKeyFormatValidation:
    """Test API key format validation in check_oscar_auth."""

    @patch("mindsdb.api.common.oscar_auth.requests.post")
    def test_rejects_invalid_prefix(self, mock_post):
        """Test that API keys without oscar_ prefix are rejected."""
        # Middleware should not be called - format check happens first
        result = check_oscar_auth(
            username="testuser",
            api_key="invalid_key_no_prefix",
            config=TEST_CONFIG,
            client_host="127.0.0.1",
        )

        assert result["success"] is False
        assert "Access denied" in result["msg"]
        mock_post.assert_not_called()

    @patch("mindsdb.api.common.oscar_auth.requests.post")
    def test_rejects_empty_api_key(self, mock_post):
        """Test that empty API key is rejected."""
        result = check_oscar_auth(
            username="testuser",
            api_key="",
            config=TEST_CONFIG,
            client_host="127.0.0.1",
        )

        assert result["success"] is False
        assert "Access denied" in result["msg"]
        mock_post.assert_not_called()

    @patch("mindsdb.api.common.oscar_auth.requests.post")
    def test_handles_bytes_api_key(self, mock_post):
        """Test that bytes API key is properly decoded."""
        # Bytes with invalid prefix still fails format check
        result = check_oscar_auth(
            username="testuser",
            api_key=b"invalid_bytes_key",
            config=TEST_CONFIG,
            client_host="127.0.0.1",
        )

        assert result["success"] is False
        assert "Access denied" in result["msg"]
        mock_post.assert_not_called()

    @patch("mindsdb.api.common.oscar_auth.requests.post")
    def test_rejects_non_utf8_bytes(self, mock_post):
        """Test that non-UTF8 bytes are rejected gracefully."""
        # Invalid UTF-8 sequence
        result = check_oscar_auth(
            username="testuser",
            api_key=b"\xff\xfe\x00\x01",
            config=TEST_CONFIG,
            client_host="127.0.0.1",
        )

        assert result["success"] is False
        assert "Access denied" in result["msg"]
        mock_post.assert_not_called()


# =============================================================================
# Permission Check Tests
# =============================================================================


class TestHasPermission:
    """Test permission checking logic."""

    def test_exact_match(self):
        """Test exact permission match."""
        permissions = {"read:alerts", "access:kore:mysql"}
        assert has_permission(permissions, "access:kore:mysql") is True

    def test_no_match(self):
        """Test no permission match."""
        permissions = {"read:alerts", "read:inventory"}
        assert has_permission(permissions, "access:kore:mysql") is False

    def test_manage_all_wildcard(self):
        """Test that manage:all grants all permissions."""
        permissions = {"manage:all"}
        assert has_permission(permissions, "access:kore:mysql") is True
        assert has_permission(permissions, "delete:users") is True
        assert has_permission(permissions, "anything:anything") is True

    def test_manage_subject_wildcard(self):
        """Test that manage:kore grants all kore permissions."""
        permissions = {"manage:kore"}
        assert has_permission(permissions, "access:kore:mysql") is True
        assert has_permission(permissions, "read:kore:databases") is True
        assert has_permission(permissions, "manage:alerts") is False

    def test_empty_permissions(self):
        """Test empty permission set."""
        permissions = set()
        assert has_permission(permissions, "access:kore:mysql") is False

    def test_invalid_permission_format(self):
        """Test invalid permission format (no colon)."""
        permissions = {"manage:all"}
        # Single-part permission should return False gracefully
        assert has_permission(permissions, "nocolon") is False
        # But manage:all still grants it because of admin check
        # Actually nocolon has no parts, so it returns False before manage:all check
        permissions_without_wildcard = {"read:alerts"}
        assert has_permission(permissions_without_wildcard, "nocolon") is False


# =============================================================================
# Thread-Safe Circuit Breaker Tests
# =============================================================================


class TestThreadSafeCircuitBreaker:
    """Test circuit breaker behavior."""

    def test_initial_state_closed(self):
        """Test that circuit breaker starts closed (healthy)."""
        cb = ThreadSafeCircuitBreaker("test", failure_threshold=3, reset_timeout=10)
        assert cb.get_state() == "closed"
        assert cb.is_open() is False

    def test_opens_after_threshold_failures(self):
        """Test that circuit opens after threshold failures."""
        cb = ThreadSafeCircuitBreaker("test", failure_threshold=3, reset_timeout=10)

        cb.record_failure()
        assert cb.get_state() == "closed"
        cb.record_failure()
        assert cb.get_state() == "closed"
        cb.record_failure()  # Third failure triggers open
        assert cb.get_state() == "open"
        assert cb.is_open() is True

    def test_success_resets_failure_count(self):
        """Test that success resets failure count."""
        cb = ThreadSafeCircuitBreaker("test", failure_threshold=3, reset_timeout=10)

        cb.record_failure()
        cb.record_failure()
        cb.record_success()  # Reset
        cb.record_failure()
        cb.record_failure()
        # Still closed because success reset the count
        assert cb.get_state() == "closed"

    def test_half_open_after_timeout(self):
        """Test transition to half-open after reset timeout."""
        cb = ThreadSafeCircuitBreaker("test", failure_threshold=2, reset_timeout=0)  # 0 for instant

        cb.record_failure()
        cb.record_failure()
        assert cb.get_state() == "open"

        # After timeout, is_open() should return False (allowing test request)
        # and state should be half-open
        time.sleep(0.01)  # Small delay to ensure timeout passed
        assert cb.is_open() is False
        assert cb.get_state() == "half-open"

    def test_success_closes_from_half_open(self):
        """Test that success in half-open state closes circuit."""
        cb = ThreadSafeCircuitBreaker("test", failure_threshold=2, reset_timeout=0)

        # Open the circuit
        cb.record_failure()
        cb.record_failure()

        # Transition to half-open
        time.sleep(0.01)
        cb.is_open()  # Triggers half-open
        assert cb.get_state() == "half-open"

        # Success closes it
        cb.record_success()
        assert cb.get_state() == "closed"

    def test_thread_safety(self):
        """Test circuit breaker is thread-safe under concurrent access."""
        cb = ThreadSafeCircuitBreaker("test", failure_threshold=100, reset_timeout=60)
        errors = []

        def record_failures():
            try:
                for _ in range(50):
                    cb.record_failure()
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=record_failures) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0
        # Should have recorded 200 failures across 4 threads
        # Circuit should be open since threshold is 100
        assert cb.get_state() == "open"


# =============================================================================
# Auth Deadline Tests
# =============================================================================


class TestAuthDeadline:
    """Test auth deadline timer behavior."""

    def test_remaining_ms_positive(self):
        """Test remaining_ms returns positive value when time remains."""
        deadline = AuthDeadline(total_budget_ms=5000)
        remaining = deadline.remaining_ms()
        assert remaining > 0
        assert remaining <= 5000

    def test_remaining_ms_zero_when_expired(self):
        """Test remaining_ms returns 0 when expired."""
        deadline = AuthDeadline(total_budget_ms=1)  # 1ms budget
        time.sleep(0.01)  # Wait for expiry
        assert deadline.remaining_ms() == 0

    def test_is_expired_false_initially(self):
        """Test is_expired returns False initially."""
        deadline = AuthDeadline(total_budget_ms=5000)
        assert deadline.is_expired() is False

    def test_is_expired_true_after_timeout(self):
        """Test is_expired returns True after timeout."""
        deadline = AuthDeadline(total_budget_ms=1)
        time.sleep(0.01)
        assert deadline.is_expired() is True

    def test_check_or_raise_passes_when_valid(self):
        """Test check_or_raise does not raise when time remains."""
        deadline = AuthDeadline(total_budget_ms=5000)
        deadline.check_or_raise("test_step")  # Should not raise

    def test_check_or_raise_raises_when_expired(self):
        """Test check_or_raise raises when deadline expired."""
        deadline = AuthDeadline(total_budget_ms=1)
        time.sleep(0.01)
        with pytest.raises(AuthDeadlineExceeded) as exc_info:
            deadline.check_or_raise("test_step")
        assert "test_step" in str(exc_info.value)

    def test_get_timeout_for_step_respects_remaining(self):
        """Test get_timeout_for_step returns min of remaining and max."""
        deadline = AuthDeadline(total_budget_ms=1000)
        timeout = deadline.get_timeout_for_step(max_step_timeout_ms=3000)
        # Should return remaining (< 1000), not 3000
        assert timeout < 1000

    def test_get_timeout_for_step_respects_max(self):
        """Test get_timeout_for_step caps at max when remaining is larger."""
        deadline = AuthDeadline(total_budget_ms=10000)  # 10 seconds
        timeout = deadline.get_timeout_for_step(max_step_timeout_ms=2000)
        # Should return 2000 (max), not remaining
        assert timeout == 2000


# =============================================================================
# Bounded Session Context Cache Tests
# =============================================================================


class TestBoundedSessionContextCache:
    """Test session context cache behavior."""

    def test_set_and_get(self):
        """Test basic set and get operations."""
        cache = BoundedSessionContextCache(max_size=100, ttl_seconds=3600)
        cache.set("session1", "user1", "user", "testuser")

        result = cache.get("session1")
        assert result["user_id"] == "user1"
        assert result["user_type"] == "user"
        assert result["username"] == "testuser"

    def test_get_nonexistent_returns_empty(self):
        """Test get returns empty dict for nonexistent session."""
        cache = BoundedSessionContextCache(max_size=100, ttl_seconds=3600)
        result = cache.get("nonexistent")
        assert result == {}

    def test_ttl_expiration(self):
        """Test entries expire after TTL."""
        cache = BoundedSessionContextCache(max_size=100, ttl_seconds=0)  # Immediate expiry
        cache.set("session1", "user1", "user", "testuser")

        time.sleep(0.01)
        result = cache.get("session1")
        assert result == {}

    def test_lru_eviction(self):
        """Test LRU eviction when cache is full."""
        cache = BoundedSessionContextCache(max_size=3, ttl_seconds=3600)

        cache.set("session1", "user1", "user", "user1")
        cache.set("session2", "user2", "user", "user2")
        cache.set("session3", "user3", "user", "user3")

        # Access session1 to make it recently used
        cache.get("session1")

        # Add session4, should evict session2 (least recently used)
        cache.set("session4", "user4", "user", "user4")

        assert cache.get("session1") != {}  # Still present (recently used)
        assert cache.get("session2") == {}  # Evicted
        assert cache.get("session3") != {}  # Still present
        assert cache.get("session4") != {}  # Just added

    def test_remove(self):
        """Test explicit removal of session."""
        cache = BoundedSessionContextCache(max_size=100, ttl_seconds=3600)
        cache.set("session1", "user1", "user", "testuser")
        cache.remove("session1")

        result = cache.get("session1")
        assert result == {}

    def test_thread_safety(self):
        """Test cache is thread-safe under concurrent access."""
        cache = BoundedSessionContextCache(max_size=1000, ttl_seconds=3600)
        errors = []

        def writer():
            try:
                for i in range(100):
                    cache.set(f"session_{threading.current_thread().name}_{i}", f"user{i}", "user", f"name{i}")
            except Exception as e:
                errors.append(e)

        def reader():
            try:
                for i in range(100):
                    cache.get(f"session_reader_{i}")
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=writer) for _ in range(4)]
        threads += [threading.Thread(target=reader) for _ in range(4)]

        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0


# =============================================================================
# Auth Failure Response Tests (Generic Error Messages)
# =============================================================================


class TestAuthFailureGenericErrors:
    """Test that auth failures return generic error messages."""

    def test_generic_error_format(self):
        """Test that _auth_failure returns MySQL-style generic error."""
        result = _auth_failure("testuser", "192.168.1.1", "Specific internal reason")

        assert result["success"] is False
        assert "Access denied" in result["msg"]
        assert "testuser" in result["msg"]
        assert "192.168.1.1" in result["msg"]
        # Specific reason should NOT be in the message
        assert "Specific internal reason" not in result["msg"]

    def test_handles_empty_username(self):
        """Test that empty username is replaced with 'unknown'."""
        result = _auth_failure("", "192.168.1.1", "reason")

        assert "unknown" in result["msg"]
        assert result["success"] is False

    def test_handles_none_username(self):
        """Test that None username is replaced with 'unknown'."""
        result = _auth_failure(None, "192.168.1.1", "reason")

        assert "unknown" in result["msg"]
        assert result["success"] is False

    def test_handles_empty_host(self):
        """Test that empty host is replaced with 'unknown'."""
        result = _auth_failure("testuser", "", "reason")

        assert "unknown" in result["msg"]
        assert result["success"] is False


# =============================================================================
# Middleware Integration Tests
# =============================================================================


class TestMiddlewareIntegration:
    """Test authentication via middleware's /api/v1/auth/validate-internal endpoint."""

    @patch("mindsdb.api.common.oscar_auth.requests.post")
    def test_successful_auth_flow(self, mock_post):
        """Test successful authentication through middleware."""
        mock_post.return_value = create_middleware_response(
            valid=True,
            user_id=VALID_USER_ID,
            username="oscaruser",
            user_type="user",
            has_permission=True,
        )

        result = check_oscar_auth(
            username="testuser",
            api_key=VALID_API_KEY,
            config=TEST_CONFIG,
            client_host="127.0.0.1",
        )

        assert result["success"] is True
        assert result["username"] == "oscaruser"
        assert result["user_id"] == VALID_USER_ID
        assert result["user_type"] == "user"

        # Verify middleware was called with correct params
        mock_post.assert_called_once()
        call_args = mock_post.call_args
        assert "https://middleware:5200/api/v1/auth/validate-internal" in call_args[0][0]
        assert call_args[1]["headers"]["X-Internal-Service"] == "kore"
        assert call_args[1]["json"]["api_key"] == VALID_API_KEY
        assert call_args[1]["json"]["include_permissions"] is True
        assert call_args[1]["json"]["required_permission"] == "access:kore:mysql"

    @patch("mindsdb.api.common.oscar_auth.requests.post")
    def test_auth_fails_invalid_api_key(self, mock_post):
        """Test authentication fails for invalid API key."""
        mock_post.return_value = create_middleware_response(
            valid=False,
            error="API key not found",
        )

        result = check_oscar_auth(
            username="testuser",
            api_key=VALID_API_KEY,
            config=TEST_CONFIG,
            client_host="127.0.0.1",
        )

        assert result["success"] is False
        assert "Access denied" in result["msg"]

    @patch("mindsdb.api.common.oscar_auth.requests.post")
    def test_auth_fails_without_required_permission(self, mock_post):
        """Test that auth fails when user lacks required permission."""
        mock_post.return_value = create_middleware_response(
            valid=True,
            user_id=VALID_USER_ID,
            username="oscaruser",
            has_permission=False,  # User doesn't have the permission
            permissions=["read:alerts"],  # Missing access:kore:mysql
        )

        result = check_oscar_auth(
            username="testuser",
            api_key=VALID_API_KEY,
            config=TEST_CONFIG,
            client_host="127.0.0.1",
        )

        assert result["success"] is False
        assert "Access denied" in result["msg"]

    @patch("mindsdb.api.common.oscar_auth.requests.post")
    def test_auth_with_manage_all_permission(self, mock_post):
        """Test that manage:all grants access."""
        mock_post.return_value = create_middleware_response(
            valid=True,
            user_id=VALID_USER_ID,
            username="adminuser",
            has_permission=True,  # Middleware confirms permission
            permissions=["manage:all"],
        )

        result = check_oscar_auth(
            username="testuser",
            api_key=VALID_API_KEY,
            config=TEST_CONFIG,
            client_host="127.0.0.1",
        )

        assert result["success"] is True
        assert result["username"] == "adminuser"

    @patch("mindsdb.api.common.oscar_auth.requests.post")
    def test_auth_with_manage_kore_permission(self, mock_post):
        """Test that manage:kore grants access to kore:mysql."""
        mock_post.return_value = create_middleware_response(
            valid=True,
            user_id=VALID_USER_ID,
            username="koremanager",
            has_permission=True,  # Middleware confirms permission
            permissions=["manage:kore"],
        )

        result = check_oscar_auth(
            username="testuser",
            api_key=VALID_API_KEY,
            config=TEST_CONFIG,
            client_host="127.0.0.1",
        )

        assert result["success"] is True

    @patch("mindsdb.api.common.oscar_auth.requests.post")
    def test_auth_with_system_user(self, mock_post):
        """Test authentication with system user type."""
        mock_post.return_value = create_middleware_response(
            valid=True,
            user_id=VALID_USER_ID,
            username="service_account",
            user_type="system",
            has_permission=True,
        )

        result = check_oscar_auth(
            username="testuser",
            api_key=VALID_API_KEY,
            config=TEST_CONFIG,
            client_host="127.0.0.1",
        )

        assert result["success"] is True
        assert result["user_type"] == "system"

    @patch("mindsdb.api.common.oscar_auth.requests.post")
    def test_local_permission_check_when_middleware_doesnt_check(self, mock_post):
        """Test local permission check when middleware returns has_permission=None."""
        mock_post.return_value = create_middleware_response(
            valid=True,
            user_id=VALID_USER_ID,
            username="oscaruser",
            has_permission=None,  # Middleware didn't check
            permissions=["access:kore:mysql"],  # Has the permission
        )

        result = check_oscar_auth(
            username="testuser",
            api_key=VALID_API_KEY,
            config=TEST_CONFIG,
            client_host="127.0.0.1",
        )

        assert result["success"] is True

    @patch("mindsdb.api.common.oscar_auth.requests.post")
    def test_local_permission_check_fails_without_permission(self, mock_post):
        """Test local permission check fails when permission missing."""
        mock_post.return_value = create_middleware_response(
            valid=True,
            user_id=VALID_USER_ID,
            username="oscaruser",
            has_permission=None,  # Middleware didn't check
            permissions=["read:alerts"],  # Missing required permission
        )

        result = check_oscar_auth(
            username="testuser",
            api_key=VALID_API_KEY,
            config=TEST_CONFIG,
            client_host="127.0.0.1",
        )

        assert result["success"] is False
        assert "Access denied" in result["msg"]


# =============================================================================
# Upstream Failure Tests
# =============================================================================


class TestUpstreamFailures:
    """Test behavior when middleware fails."""

    @patch("mindsdb.api.common.oscar_auth.requests.post")
    def test_middleware_timeout(self, mock_post):
        """Test that middleware timeout causes auth failure."""
        import requests

        mock_post.side_effect = requests.exceptions.Timeout("Connection timed out")

        result = check_oscar_auth(
            username="testuser",
            api_key=VALID_API_KEY,
            config=TEST_CONFIG,
            client_host="127.0.0.1",
        )

        assert result["success"] is False
        assert "Access denied" in result["msg"]

    @patch("mindsdb.api.common.oscar_auth.requests.post")
    def test_middleware_connection_error(self, mock_post):
        """Test that middleware connection error causes auth failure."""
        import requests

        mock_post.side_effect = requests.exceptions.ConnectionError("Connection refused")

        result = check_oscar_auth(
            username="testuser",
            api_key=VALID_API_KEY,
            config=TEST_CONFIG,
            client_host="127.0.0.1",
        )

        assert result["success"] is False
        assert "Access denied" in result["msg"]

    @patch("mindsdb.api.common.oscar_auth.requests.post")
    def test_middleware_403_response(self, mock_post):
        """Test that middleware 403 (missing X-Internal-Service) causes auth failure."""
        mock_post.return_value = create_middleware_response(status_code=403)

        result = check_oscar_auth(
            username="testuser",
            api_key=VALID_API_KEY,
            config=TEST_CONFIG,
            client_host="127.0.0.1",
        )

        assert result["success"] is False
        assert "Access denied" in result["msg"]

    @patch("mindsdb.api.common.oscar_auth.requests.post")
    def test_middleware_500_response(self, mock_post):
        """Test that middleware 500 error causes auth failure."""
        mock_post.return_value = create_middleware_response(status_code=500)

        result = check_oscar_auth(
            username="testuser",
            api_key=VALID_API_KEY,
            config=TEST_CONFIG,
            client_host="127.0.0.1",
        )

        assert result["success"] is False
        assert "Access denied" in result["msg"]

    def test_auth_deadline_exceeded(self):
        """Test that exceeding total auth budget fails gracefully."""
        # Create a config with very short budget
        short_budget_config = {**TEST_CONFIG, "total_auth_budget_ms": 1}

        # Sleep to ensure deadline passes before middleware call
        time.sleep(0.01)

        result = check_oscar_auth(
            username="testuser",
            api_key=VALID_API_KEY,
            config=short_budget_config,
            client_host="127.0.0.1",
        )

        assert result["success"] is False
        assert "Access denied" in result["msg"]


# =============================================================================
# Circuit Breaker Integration Tests
# =============================================================================


class TestCircuitBreakerIntegration:
    """Test circuit breaker behavior in authentication flow."""

    @patch("mindsdb.api.common.oscar_auth._middleware_circuit", None)  # Reset singleton
    @patch("mindsdb.api.common.oscar_auth.requests.post")
    def test_circuit_opens_after_failures(self, mock_post):
        """Test that circuit breaker opens after repeated failures."""
        import requests

        # Reset the module-level circuit breaker singleton
        import mindsdb.api.common.oscar_auth as auth_module

        auth_module._middleware_circuit = None

        # Config with low threshold for testing
        test_config = {**TEST_CONFIG, "circuit_breaker_threshold": 3, "circuit_breaker_reset_s": 60}

        mock_post.side_effect = requests.exceptions.Timeout("timeout")

        # Make 3 requests to trigger circuit breaker
        for _ in range(3):
            result = check_oscar_auth(
                username="testuser",
                api_key=VALID_API_KEY,
                config=test_config,
                client_host="127.0.0.1",
            )
            assert result["success"] is False

        # Circuit should now be open - next request should fast-fail without calling middleware
        mock_post.reset_mock()
        result = check_oscar_auth(
            username="testuser",
            api_key=VALID_API_KEY,
            config=test_config,
            client_host="127.0.0.1",
        )

        assert result["success"] is False
        # Middleware should NOT be called because circuit is open
        mock_post.assert_not_called()

        # Reset for other tests
        auth_module._middleware_circuit = None

    @patch("mindsdb.api.common.oscar_auth._middleware_circuit", None)  # Reset singleton
    @patch("mindsdb.api.common.oscar_auth.requests.post")
    def test_circuit_closes_after_success(self, mock_post):
        """Test that circuit breaker closes after successful request in half-open state."""
        import mindsdb.api.common.oscar_auth as auth_module

        auth_module._middleware_circuit = None

        # Config with low threshold and instant reset for testing
        test_config = {**TEST_CONFIG, "circuit_breaker_threshold": 2, "circuit_breaker_reset_s": 0}

        import requests

        # Open the circuit
        mock_post.side_effect = requests.exceptions.Timeout("timeout")
        for _ in range(2):
            check_oscar_auth(
                username="testuser",
                api_key=VALID_API_KEY,
                config=test_config,
                client_host="127.0.0.1",
            )

        # Wait for half-open transition
        time.sleep(0.01)

        # Now return success
        mock_post.side_effect = None
        mock_post.return_value = create_middleware_response(valid=True)

        result = check_oscar_auth(
            username="testuser",
            api_key=VALID_API_KEY,
            config=test_config,
            client_host="127.0.0.1",
        )

        assert result["success"] is True

        # Circuit should be closed now
        assert auth_module._middleware_circuit.get_state() == "closed"

        # Reset for other tests
        auth_module._middleware_circuit = None


# =============================================================================
# Session Context Tests
# =============================================================================


class TestSessionContext:
    """Test session context storage after successful auth."""

    @patch("mindsdb.api.common.oscar_auth._session_contexts", None)  # Reset singleton
    @patch("mindsdb.api.common.oscar_auth.requests.post")
    def test_session_context_stored_after_auth(self, mock_post):
        """Test that session context is stored after successful auth."""
        from mindsdb.api.common.oscar_auth import get_user_context
        import mindsdb.api.common.oscar_auth as auth_module

        auth_module._session_contexts = None

        mock_post.return_value = create_middleware_response(
            valid=True,
            user_id=VALID_USER_ID,
            username="oscaruser",
            user_type="user",
        )

        session_id = "test-session-123"

        result = check_oscar_auth(
            username="testuser",
            api_key=VALID_API_KEY,
            config=TEST_CONFIG,
            client_host="127.0.0.1",
            session_id=session_id,
        )

        assert result["success"] is True

        # Verify context was stored
        context = get_user_context(session_id, TEST_CONFIG)
        assert context["user_id"] == VALID_USER_ID
        assert context["username"] == "oscaruser"
        assert context["user_type"] == "user"

        # Reset for other tests
        auth_module._session_contexts = None

    @patch("mindsdb.api.common.oscar_auth._session_contexts", None)  # Reset singleton
    @patch("mindsdb.api.common.oscar_auth.requests.post")
    def test_no_session_context_on_failure(self, mock_post):
        """Test that session context is NOT stored on auth failure."""
        from mindsdb.api.common.oscar_auth import get_user_context
        import mindsdb.api.common.oscar_auth as auth_module

        auth_module._session_contexts = None

        mock_post.return_value = create_middleware_response(
            valid=False,
            error="Invalid API key",
        )

        session_id = "test-session-456"

        result = check_oscar_auth(
            username="testuser",
            api_key=VALID_API_KEY,
            config=TEST_CONFIG,
            client_host="127.0.0.1",
            session_id=session_id,
        )

        assert result["success"] is False

        # Context should be empty
        context = get_user_context(session_id, TEST_CONFIG)
        assert context == {}

        # Reset for other tests
        auth_module._session_contexts = None
