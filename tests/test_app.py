"""
Unit tests for StatusPulse Website Monitoring & Incident Management.
Uses pytest and Flask's test client.
"""

import pytest
from unittest.mock import patch, MagicMock
import requests

import app as statuspulse_app
from app import app, init_db, get_db, validate_url, calculate_latency, ping_target


@pytest.fixture
def client(tmp_path):
    """Set up an isolated SQLite test database and Flask test client."""
    test_db_path = str(tmp_path / "test_statuspulse.db")
    statuspulse_app.DB_PATH = test_db_path
    init_db()

    app.config["TESTING"] = True
    app.config["SECRET_KEY"] = "test-secret-key"

    with app.test_client() as test_client:
        yield test_client


# ──────────────────────────────────────────────────────────────────────────────
# 1. Home Dashboard Route Test
# ──────────────────────────────────────────────────────────────────────────────

def test_home_page_returns_200(client):
    """Verify that GET / renders successfully with HTTP 200."""
    response = client.get("/")
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert "Status" in html
    assert "Pulse" in html
    assert "Total Monitored Sites" in html
    assert "Healthy Sites" in html
    assert "Active Incidents" in html


# ──────────────────────────────────────────────────────────────────────────────
# 2. Add Target Route Test
# ──────────────────────────────────────────────────────────────────────────────

def test_add_target_success_and_db_persistence(client):
    """Verify adding a valid URL redirects successfully and stores it in the database."""
    with patch("threading.Thread"):
        response = client.post(
            "/add",
            data={"url": "https://example.com"},
            follow_redirects=False,
        )
        # Should redirect to home (302)
        assert response.status_code == 302
        assert response.headers["Location"] == "/"

        # Follow redirect and verify success message
        follow_response = client.get("/")
        assert follow_response.status_code == 200
        assert "https://example.com" in follow_response.get_data(as_text=True)

        # Confirm persisted in 'targets' SQLite table
        with get_db() as conn:
            row = conn.execute("SELECT * FROM targets WHERE url='https://example.com'").fetchone()
            assert row is not None
            assert row["url"] == "https://example.com"
            assert row["created_at"] is not None


def test_add_target_invalid_url(client):
    """Verify that invalid URL formats are rejected."""
    response = client.post("/add", data={"url": "not-a-valid-url"}, follow_redirects=True)
    assert response.status_code == 200
    assert "URL must start with http:// or https://" in response.get_data(as_text=True)


# ──────────────────────────────────────────────────────────────────────────────
# 3. Delete Target Route Test
# ──────────────────────────────────────────────────────────────────────────────

def test_delete_target(client):
    """Verify that POST /delete/<id> removes target from database."""
    with patch("threading.Thread"):
        client.post("/add", data={"url": "https://to-delete.com"})

    with get_db() as conn:
        t_id = conn.execute("SELECT id FROM targets WHERE url='https://to-delete.com'").fetchone()["id"]

    res = client.post(f"/delete/{t_id}", follow_redirects=True)
    assert res.status_code == 200
    assert "deleted" in res.get_data(as_text=True)

    with get_db() as conn:
        assert conn.execute("SELECT * FROM targets WHERE id=?", (t_id,)).fetchone() is None


# ──────────────────────────────────────────────────────────────────────────────
# 4. Monitoring Check Logic (Success, Failure, and Incident Lifecycle)
# ──────────────────────────────────────────────────────────────────────────────

def test_monitoring_check_successful_response(client):
    """Verify check logic on HTTP 200 OK records check and marks target as UP."""
    with patch("threading.Thread"):
        client.post("/add", data={"url": "https://healthy-service.org"})

    with get_db() as conn:
        t_id = conn.execute("SELECT id FROM targets WHERE url='https://healthy-service.org'").fetchone()["id"]

    with patch("requests.get") as mock_get:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_get.return_value = mock_resp

        result = ping_target(t_id, "https://healthy-service.org")
        assert result["is_up"] is True
        assert result["status_code"] == 200
        assert result["latency_ms"] is not None

        # Verify check row in DB
        with get_db() as conn:
            check_row = conn.execute("SELECT * FROM checks WHERE target_id=?", (t_id,)).fetchone()
            assert check_row is not None
            assert check_row["status_code"] == 200


def test_monitoring_check_failed_response_creates_incident(client):
    """Verify check logic on HTTP 500/error creates an OPEN incident row."""
    with patch("threading.Thread"):
        client.post("/add", data={"url": "https://failing-service.org"})

    with get_db() as conn:
        t_id = conn.execute("SELECT id FROM targets WHERE url='https://failing-service.org'").fetchone()["id"]

    # 1. Non-200 Response (HTTP 503)
    with patch("requests.get") as mock_get:
        mock_resp = MagicMock()
        mock_resp.status_code = 503
        mock_get.return_value = mock_resp

        result = ping_target(t_id, "https://failing-service.org")
        assert result["is_up"] is False
        assert result["status_code"] == 503

        # Confirm incident was opened with status 'OPEN'
        with get_db() as conn:
            incident = conn.execute("SELECT * FROM incidents WHERE target_id=?", (t_id,)).fetchone()
            assert incident is not None
            assert incident["status"] == "OPEN"
            assert incident["resolution_time"] is None


def test_monitoring_check_timeout_exception_creates_incident(client):
    """Verify check logic handles Timeout exception gracefully (status 0)."""
    with patch("threading.Thread"):
        client.post("/add", data={"url": "https://timeout-service.org"})

    with get_db() as conn:
        t_id = conn.execute("SELECT id FROM targets WHERE url='https://timeout-service.org'").fetchone()["id"]

    with patch("requests.get", side_effect=requests.exceptions.Timeout("Timed out")):
        result = ping_target(t_id, "https://timeout-service.org")
        assert result["is_up"] is False
        assert result["status_code"] == 0

        with get_db() as conn:
            incident = conn.execute("SELECT * FROM incidents WHERE target_id=?", (t_id,)).fetchone()
            assert incident is not None
            assert incident["status"] == "OPEN"


def test_monitoring_check_recovery_resolves_incident(client):
    """Verify check logic resolves an OPEN incident when service returns 200 OK."""
    with patch("threading.Thread"):
        client.post("/add", data={"url": "https://recovery-service.org"})

    with get_db() as conn:
        t_id = conn.execute("SELECT id FROM targets WHERE url='https://recovery-service.org'").fetchone()["id"]

    # Step 1: Outage (HTTP 500)
    with patch("requests.get") as mock_get:
        mock_fail = MagicMock()
        mock_fail.status_code = 500
        mock_get.return_value = mock_fail
        ping_target(t_id, "https://recovery-service.org")

    with get_db() as conn:
        open_inc = conn.execute("SELECT * FROM incidents WHERE target_id=? AND status='OPEN'", (t_id,)).fetchone()
        assert open_inc is not None

    # Step 2: Recovery (HTTP 200)
    with patch("requests.get") as mock_get:
        mock_ok = MagicMock()
        mock_ok.status_code = 200
        mock_get.return_value = mock_ok
        ping_target(t_id, "https://recovery-service.org")

    # Verify incident status updated to 'RESOLVED' and resolution_time is recorded
    with get_db() as conn:
        resolved_inc = conn.execute("SELECT * FROM incidents WHERE target_id=?", (t_id,)).fetchone()
        assert resolved_inc["status"] == "RESOLVED"
        assert resolved_inc["resolution_time"] is not None


# ──────────────────────────────────────────────────────────────────────────────
# 5. Helper & Export Route Tests
# ──────────────────────────────────────────────────────────────────────────────

def test_url_validation_helper():
    """Verify URL validation helper accepts valid schemes and rejects invalid."""
    assert validate_url("https://example.com")[0] is True
    assert validate_url("http://127.0.0.1:8080")[0] is True
    assert validate_url("")[0] is False
    assert validate_url("ftp://example.com")[0] is False


def test_calculate_latency_helper():
    """Verify calculate_latency helper returns ms on success and None on exception."""
    with patch("requests.get") as mock_get:
        mock_get.return_value.status_code = 200
        lat = calculate_latency("https://example.com")
        assert lat is not None and lat >= 0

    with patch("requests.get", side_effect=requests.exceptions.ConnectionError):
        lat = calculate_latency("https://bad-host.local")
        assert lat is None


def test_export_history_csv(client):
    """Verify CSV export endpoint returns text/csv with attachment header."""
    res = client.get("/export/history")
    assert res.status_code == 200
    assert res.mimetype == "text/csv"
    assert "attachment;" in res.headers["Content-Disposition"]
