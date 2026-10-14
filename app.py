"""
StatusPulse - DevOps Website Monitoring & Automated Incident Management
Flask Backend with SQLite, APScheduler, CSV Telemetry Export, and Webhook Notifications
"""

import os
import csv
import io
import time
import sqlite3
import logging
import threading
from datetime import datetime, timezone

import requests as req
from flask import Flask, render_template, request, redirect, url_for, flash, jsonify, Response
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

# ──────────────────────────────────────────────────────────────────────────────
# Configuration
# ──────────────────────────────────────────────────────────────────────────────

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
os.makedirs(DATA_DIR, exist_ok=True)

DB_PATH  = os.environ.get("DB_PATH", os.path.join(DATA_DIR, "statuspulse.db"))
POLL_INTERVAL_SECONDS = int(os.environ.get("POLL_INTERVAL", 30))
REQUEST_TIMEOUT       = int(os.environ.get("REQUEST_TIMEOUT", 5))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s – %(message)s",
)
logger = logging.getLogger("statuspulse")

# ──────────────────────────────────────────────────────────────────────────────
# Flask App
# ──────────────────────────────────────────────────────────────────────────────

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "statuspulse-dev-secret-change-in-prod")

# ──────────────────────────────────────────────────────────────────────────────
# Database Setup & Helpers
# ──────────────────────────────────────────────────────────────────────────────

def get_db() -> sqlite3.Connection:
    """Return a thread-safe SQLite connection with Row factory."""
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn


def init_db() -> None:
    """Initialize SQLite tables according to specification."""
    os.makedirs(os.path.dirname(os.path.abspath(DB_PATH)), exist_ok=True)
    with get_db() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS targets (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                url        TEXT    NOT NULL UNIQUE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS checks (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                target_id   INTEGER NOT NULL REFERENCES targets(id) ON DELETE CASCADE,
                status_code INTEGER,
                latency_ms  REAL,
                timestamp   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS incidents (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                target_id       INTEGER NOT NULL REFERENCES targets(id) ON DELETE CASCADE,
                start_time      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                resolution_time TIMESTAMP,
                status          TEXT DEFAULT 'OPEN'
            );

            CREATE TABLE IF NOT EXISTS settings (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
        """)
    logger.info("Database initialized at %s", DB_PATH)


def get_setting(key: str, default: str = "") -> str:
    """Read a setting from the settings table."""
    try:
        with get_db() as conn:
            row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
            if row:
                return row["value"]
    except Exception as exc:
        logger.warning("Could not read setting %s: %s", key, exc)
    return default


def set_setting(key: str, value: str) -> None:
    """Write or update a setting in the settings table."""
    with get_db() as conn:
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )


# ──────────────────────────────────────────────────────────────────────────────
# Webhook Notifications
# ──────────────────────────────────────────────────────────────────────────────

def send_webhook_alert(event_type: str, url: str, status_code: int | None, timestamp: str) -> None:
    """
    Dispatch non-blocking alert to configured Discord / Slack / HTTP webhook.
    """
    webhook_url = get_setting("webhook_url", os.environ.get("WEBHOOK_URL", "")).strip()
    if not webhook_url:
        return

    if event_type == "DOWN":
        title = f"🚨 StatusPulse Outage Alert: {url}"
        desc = f"Target is DOWN!\nHTTP Status: {status_code or 'Timeout / Connection Failure'}\nTime: {timestamp}"
        color = 15548997  # Red
    else:
        title = f"✅ StatusPulse Recovery: {url}"
        desc = f"Target has RECOVERED!\nHTTP Status: {status_code or '200 OK'}\nTime: {timestamp}"
        color = 5763719   # Green

    payload = {
        "content": title,
        "text": f"{title}\n{desc}",
        "embeds": [
            {
                "title": title,
                "description": desc,
                "color": color,
                "timestamp": timestamp,
                "footer": {"text": "StatusPulse Automated Incident Management"}
            }
        ]
    }

    def _post():
        try:
            req.post(
                webhook_url,
                json=payload,
                timeout=5,
                headers={"User-Agent": "StatusPulse-Webhook/1.0", "Content-Type": "application/json"}
            )
            logger.info("Webhook alert sent to %s for %s (%s)", webhook_url, url, event_type)
        except Exception as exc:
            logger.warning("Failed to dispatch webhook alert to %s: %s", webhook_url, exc)

    threading.Thread(target=_post, daemon=True).start()


# ──────────────────────────────────────────────────────────────────────────────
# Monitoring Engine & Incident Management
# ──────────────────────────────────────────────────────────────────────────────

def ping_target(target_id: int, url: str) -> dict:
    """
    Sends an HTTP GET request with a 5-second timeout, measures round-trip
    latency in ms, records the check, and manages incident lifecycle:
    - If check fails (status != 200 or exception) and no open incident exists -> CREATE incident row with status 'OPEN'.
    - If check succeeds (status == 200) and open incident exists -> UPDATE incident resolution_time and status 'RESOLVED'.
    """
    status_code = None
    latency_ms  = None
    is_success  = False

    t0 = time.perf_counter()
    try:
        response = req.get(
            url,
            timeout=REQUEST_TIMEOUT,
            allow_redirects=True,
            headers={"User-Agent": "StatusPulse/1.0"}
        )
        latency_ms  = round((time.perf_counter() - t0) * 1000, 2)
        status_code = response.status_code
        is_success  = (response.status_code == 200)
    except req.exceptions.Timeout:
        latency_ms  = round((time.perf_counter() - t0) * 1000, 2)
        status_code = 0  # 0 indicates connection timeout
        logger.warning("Timeout while checking %s", url)
    except req.exceptions.RequestException as exc:
        latency_ms  = round((time.perf_counter() - t0) * 1000, 2)
        status_code = 500  # 500 indicates connection/network error
        logger.warning("Connection failure checking %s: %s", url, exc)

    now = datetime.now(timezone.utc).isoformat()

    with get_db() as conn:
        # Record check telemetry
        conn.execute(
            "INSERT INTO checks (target_id, status_code, latency_ms, timestamp) "
            "VALUES (?, ?, ?, ?)",
            (target_id, status_code, latency_ms, now),
        )

        # Check for currently open incident
        open_incident = conn.execute(
            "SELECT id FROM incidents WHERE target_id=? AND status='OPEN'",
            (target_id,),
        ).fetchone()

        if not is_success and open_incident is None:
            # Check failed and no OPEN incident exists -> Create new incident
            conn.execute(
                "INSERT INTO incidents (target_id, start_time, status) VALUES (?, ?, 'OPEN')",
                (target_id, now),
            )
            logger.warning("INCIDENT OPENED  – target_id=%s url=%s (status=%s)", target_id, url, status_code)
            send_webhook_alert("DOWN", url, status_code, now)

        elif is_success and open_incident is not None:
            # Check succeeded and OPEN incident exists -> Resolve incident
            conn.execute(
                "UPDATE incidents SET resolution_time=?, status='RESOLVED' WHERE id=?",
                (now, open_incident["id"]),
            )
            logger.info("INCIDENT RESOLVED – target_id=%s url=%s", target_id, url)
            send_webhook_alert("UP", url, status_code, now)

    return {
        "status_code": status_code,
        "latency_ms": latency_ms,
        "is_up": is_success,
    }


def poll_all_targets() -> None:
    """Scheduled background job: iterates through all active targets in the database."""
    logger.debug("Polling active targets …")
    try:
        with get_db() as conn:
            targets = conn.execute("SELECT id, url FROM targets").fetchall()
        for t in targets:
            ping_target(t["id"], t["url"])
    except Exception as exc:
        logger.error("Poll job failed: %s", exc)


# ──────────────────────────────────────────────────────────────────────────────
# APScheduler Setup
# ──────────────────────────────────────────────────────────────────────────────

scheduler = BackgroundScheduler(daemon=True)


def start_scheduler() -> None:
    scheduler.add_job(
        poll_all_targets,
        trigger=IntervalTrigger(seconds=POLL_INTERVAL_SECONDS),
        id="poll_all",
        replace_existing=True,
        max_instances=1,
    )
    if not scheduler.running:
        scheduler.start()
        logger.info("Scheduler started (interval=%ss, timeout=%ss)", POLL_INTERVAL_SECONDS, REQUEST_TIMEOUT)


# ──────────────────────────────────────────────────────────────────────────────
# Helper Utilities
# ──────────────────────────────────────────────────────────────────────────────

def calculate_latency(url: str, timeout: int = REQUEST_TIMEOUT) -> float | None:
    """Helper for testing: times a single HTTP GET and returns latency in ms."""
    try:
        t0 = time.perf_counter()
        req.get(url, timeout=timeout, allow_redirects=True, headers={"User-Agent": "StatusPulse/1.0"})
        return round((time.perf_counter() - t0) * 1000, 2)
    except req.exceptions.RequestException:
        return None


def validate_url(url: str) -> tuple[bool, str]:
    """Validate HTTP/HTTPS scheme and length."""
    if not url:
        return False, "URL cannot be empty."
    if not (url.startswith("http://") or url.startswith("https://")):
        return False, "URL must start with http:// or https://"
    if len(url) > 2048:
        return False, "URL is too long (max 2048 characters)."
    return True, ""


def _get_dashboard_data() -> dict:
    """Assemble targets, checks, incidents, and summary metrics for the dashboard."""
    with get_db() as conn:
        targets_raw = conn.execute(
            "SELECT id, url, created_at FROM targets ORDER BY created_at DESC"
        ).fetchall()

        enriched_targets = []
        for t in targets_raw:
            latest = conn.execute(
                "SELECT status_code, latency_ms, timestamp "
                "FROM checks WHERE target_id=? ORDER BY timestamp DESC LIMIT 1",
                (t["id"],),
            ).fetchone()

            avg_row = conn.execute(
                "SELECT ROUND(AVG(latency_ms), 2) as avg_lat "
                "FROM checks WHERE target_id=? AND latency_ms IS NOT NULL",
                (t["id"],),
            ).fetchone()

            uptime_row = conn.execute(
                "SELECT "
                "  COUNT(*) as total, "
                "  SUM(CASE WHEN status_code = 200 THEN 1 ELSE 0 END) as up_count "
                "FROM checks WHERE target_id=?",
                (t["id"],),
            ).fetchone()

            total   = uptime_row["total"] or 0
            up_cnt  = uptime_row["up_count"] or 0
            uptime  = round((up_cnt / total * 100), 1) if total else None

            open_inc = conn.execute(
                "SELECT COUNT(*) as cnt FROM incidents WHERE target_id=? AND status='OPEN'",
                (t["id"],),
            ).fetchone()

            is_up = None
            if latest and latest["status_code"] is not None:
                is_up = (latest["status_code"] == 200)

            enriched_targets.append({
                "id":           t["id"],
                "url":          t["url"],
                "created_at":   t["created_at"],
                "status_code":  latest["status_code"]  if latest else None,
                "latency_ms":   latest["latency_ms"]   if latest else None,
                "is_up":        is_up,
                "last_checked": latest["timestamp"]    if latest else None,
                "avg_latency":  avg_row["avg_lat"]     if avg_row else None,
                "uptime_pct":   uptime,
                "has_incident": bool(open_inc["cnt"])  if open_inc else False,
            })

        incidents_raw = conn.execute(
            "SELECT i.id, t.url, i.start_time, i.resolution_time, i.status "
            "FROM incidents i JOIN targets t ON t.id = i.target_id "
            "ORDER BY i.start_time DESC LIMIT 50"
        ).fetchall()

        incidents = [dict(r) for r in incidents_raw]

        total_monitored  = len(enriched_targets)
        healthy_sites    = sum(1 for t in enriched_targets if t["is_up"] is True)
        active_incidents = sum(1 for t in enriched_targets if t["has_incident"])
        webhook_url      = get_setting("webhook_url", os.environ.get("WEBHOOK_URL", ""))

    return {
        "targets":          enriched_targets,
        "incidents":        incidents,
        "total_monitored":  total_monitored,
        "healthy_sites":    healthy_sites,
        "active_incidents": active_incidents,
        "webhook_url":      webhook_url,
    }


# ──────────────────────────────────────────────────────────────────────────────
# Routes
# ──────────────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    """Renders the dashboard with active targets, check history, and incidents."""
    data = _get_dashboard_data()
    return render_template("index.html", **data)


@app.route("/add", methods=["POST"])
def add_target():
    """Reads URL from form data, validates it, inserts it into 'targets', and redirects to home."""
    url = request.form.get("url", "").strip()

    valid, err = validate_url(url)
    if not valid:
        flash(err, "danger")
        return redirect(url_for("index"))

    try:
        with get_db() as conn:
            conn.execute(
                "INSERT INTO targets (url, created_at) VALUES (?, ?)",
                (url, datetime.now(timezone.utc).isoformat()),
            )
        flash(f"✅ Target '{url}' added successfully!", "success")
        t_id = get_db().execute("SELECT id FROM targets WHERE url=?", (url,)).fetchone()["id"]
        threading.Thread(target=ping_target, args=(t_id, url), daemon=True).start()
    except sqlite3.IntegrityError:
        flash(f"⚠️ Target '{url}' is already being monitored.", "warning")

    return redirect(url_for("index"))


@app.route("/delete/<int:target_id>", methods=["POST"])
def delete_target(target_id: int):
    """Deletes a target and all its associated telemetry and incidents."""
    with get_db() as conn:
        row = conn.execute("SELECT url FROM targets WHERE id=?", (target_id,)).fetchone()
        if row:
            conn.execute("DELETE FROM targets WHERE id=?", (target_id,))
            flash(f"🗑️ Target '{row['url']}' deleted.", "info")
        else:
            flash("Target not found.", "danger")
    return redirect(url_for("index"))


@app.route("/export/history")
def export_history():
    """Download check telemetry and incident logs as CSV."""
    output = io.StringIO()
    writer = csv.writer(output)

    # 1. Section: Check Telemetry
    writer.writerow(["=== STATUSPULSE CHECK TELEMETRY ==="])
    writer.writerow(["Check ID", "Target ID", "Target URL", "HTTP Status", "Latency (ms)", "State", "Timestamp (UTC)"])

    with get_db() as conn:
        checks = conn.execute("""
            SELECT c.id, c.target_id, t.url, c.status_code, c.latency_ms, c.timestamp
            FROM checks c
            JOIN targets t ON t.id = c.target_id
            ORDER BY c.timestamp DESC
        """).fetchall()

        for c in checks:
            state = "UP" if c["status_code"] == 200 else "DOWN"
            writer.writerow([
                c["id"],
                c["target_id"],
                c["url"],
                c["status_code"] if c["status_code"] is not None else "ERROR",
                c["latency_ms"] if c["latency_ms"] is not None else "",
                state,
                c["timestamp"]
            ])

        writer.writerow([])

        # 2. Section: Incident History Logs
        writer.writerow(["=== STATUSPULSE INCIDENT HISTORY LOG ==="])
        writer.writerow(["Incident ID", "Target ID", "Target URL", "Status", "Started At (UTC)", "Resolved At (UTC)"])

        incidents = conn.execute("""
            SELECT i.id, i.target_id, t.url, i.status, i.start_time, i.resolution_time
            FROM incidents i
            JOIN targets t ON t.id = i.target_id
            ORDER BY i.start_time DESC
        """).fetchall()

        for inc in incidents:
            writer.writerow([
                inc["id"],
                inc["target_id"],
                inc["url"],
                inc["status"],
                inc["start_time"],
                inc["resolution_time"] or "ONGOING"
            ])

    filename = f"statuspulse_telemetry_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.csv"
    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"}
    )


@app.route("/settings/webhook", methods=["POST"])
def update_webhook():
    """Configure or clear the Discord/Slack webhook URL."""
    webhook_url = request.form.get("webhook_url", "").strip()
    if webhook_url and not (webhook_url.startswith("http://") or webhook_url.startswith("https://")):
        flash("Invalid Webhook URL. It must begin with http:// or https://", "danger")
        return redirect(url_for("index"))

    set_setting("webhook_url", webhook_url)
    if webhook_url:
        flash("🔔 Webhook notification URL updated successfully!", "success")
    else:
        flash("🔕 Webhook notifications disabled.", "info")
    return redirect(url_for("index"))


@app.route("/settings/webhook/test", methods=["POST"])
def test_webhook():
    """Trigger an immediate test alert to the configured webhook."""
    webhook_url = get_setting("webhook_url", "").strip()
    if not webhook_url:
        return jsonify({"error": "No webhook URL configured."}), 400

    now = datetime.now(timezone.utc).isoformat()
    send_webhook_alert("UP", "https://statuspulse.local/test", 200, now)
    return jsonify({"success": True, "message": "Test webhook alert dispatched."})


@app.route("/api/status")
def api_status():
    """JSON endpoint for live dashboard polling."""
    data = _get_dashboard_data()
    return jsonify(data)


@app.route("/api/check/<int:target_id>", methods=["POST"])
def api_check_now(target_id: int):
    """Trigger an immediate check for a single target."""
    with get_db() as conn:
        row = conn.execute("SELECT url FROM targets WHERE id=?", (target_id,)).fetchone()
    if not row:
        return jsonify({"error": "Target not found"}), 404
    result = ping_target(target_id, row["url"])
    return jsonify(result)


@app.route("/healthz")
def healthz():
    """Liveness probe."""
    return jsonify({"status": "ok"})


# ──────────────────────────────────────────────────────────────────────────────
# App Bootstrap
# ──────────────────────────────────────────────────────────────────────────────

def create_app():
    init_db()
    start_scheduler()
    return app


if __name__ == "__main__":
    create_app()
    app.run(host="0.0.0.0", port=5000, debug=False)
