"""
Bookedly CRM — B2B Lead Management & Outreach Command Center

Run:    python3 crm.py

Default login:
    Email:    admin@bookedly.com
    Password: bookedly
"""
from __future__ import annotations

import json
import logging
import os
import secrets
import sqlite3
import subprocess
import sys
import threading
from datetime import datetime, timedelta
from functools import wraps

try:
    from flask import (
        Flask, abort, flash, jsonify, redirect, render_template_string,
        request, send_from_directory, session, url_for,
    )
    from werkzeug.security import check_password_hash, generate_password_hash
except ImportError:
    print("Flask not installed. Run: pip install flask")
    sys.exit(1)

try:
    import config
    DB_PATH = config.DB_PATH
except ImportError:
    DB_PATH = "leads.db"

_HERE = os.path.dirname(os.path.abspath(__file__))
if not os.path.isabs(DB_PATH):
    DB_PATH = os.path.join(_HERE, DB_PATH)

# Turso (hosted SQLite) for Vercel deployment
TURSO_URL = os.environ.get("TURSO_DATABASE_URL")
TURSO_TOKEN = os.environ.get("TURSO_AUTH_TOKEN")
_use_turso = bool(TURSO_URL and TURSO_TOKEN)

if _use_turso:
    import requests as _requests

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET", secrets.token_hex(32))

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

STAGES = ["new", "enriched", "contacted", "opened", "replied", "interested", "booked", "client", "lost"]


# ═══════════════════════════════════════════════════════════════════════
# DATABASE
# ═══════════════════════════════════════════════════════════════════════

def _turso_execute(sql: str, params: tuple = (), want_rows: bool = False):
    """Execute SQL against Turso via HTTP API."""
    url = TURSO_URL.replace("libsql://", "https://")
    args = []
    for p in params:
        if isinstance(p, int):
            args.append({"type": "integer", "value": str(p)})
        elif isinstance(p, float):
            args.append({"type": "float", "value": str(p)})
        elif p is None:
            args.append({"type": "null"})
        else:
            args.append({"type": "text", "value": str(p)})

    body = {
        "requests": [
            {"type": "execute", "stmt": {"sql": sql, "args": args}},
            {"type": "close"}
        ]
    }
    resp = _requests.post(
        f"{url}/v2/pipeline", json=body,
        headers={"Authorization": f"Bearer {TURSO_TOKEN}"}
    )
    resp.raise_for_status()
    data = resp.json()

    result = data.get("results", [{}])[0]
    if result.get("type") == "error":
        raise Exception(result["error"].get("message", "Turso error"))

    response = result.get("response", {}).get("result", {})

    if want_rows:
        cols = [c["name"] for c in response.get("cols", [])]
        rows = []
        for r in response.get("rows", []):
            rows.append({cols[i]: cell.get("value") for i, cell in enumerate(r)})
        return rows
    return response.get("last_insert_rowid", None)


def _turso_batch(statements: list[str]):
    """Execute multiple SQL statements against Turso."""
    url = TURSO_URL.replace("libsql://", "https://")
    reqs = [{"type": "execute", "stmt": {"sql": s}} for s in statements]
    reqs.append({"type": "close"})
    resp = _requests.post(
        f"{url}/v2/pipeline", json={"requests": reqs},
        headers={"Authorization": f"Bearer {TURSO_TOKEN}"}
    )
    resp.raise_for_status()


def get_conn():
    if _use_turso:
        return None  # Turso uses HTTP, no persistent connection
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def query(sql: str, params: tuple = ()) -> list[dict]:
    if _use_turso:
        return _turso_execute(sql, params, want_rows=True)
    conn = get_conn()
    try:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


def scalar(sql: str, params: tuple = (), default=0):
    if _use_turso:
        rows = _turso_execute(sql, params, want_rows=True)
        if rows:
            first_val = list(rows[0].values())[0]
            if first_val is None:
                return default
            if isinstance(default, (int, float)):
                try:
                    return type(default)(first_val)
                except (ValueError, TypeError):
                    return default
            return first_val
        return default
    conn = get_conn()
    try:
        row = conn.execute(sql, params).fetchone()
        return row[0] if row and row[0] is not None else default
    finally:
        conn.close()


def execute(sql: str, params: tuple = ()):
    if _use_turso:
        return _turso_execute(sql, params)
    conn = get_conn()
    try:
        conn.execute(sql, params)
        conn.commit()
    finally:
        conn.close()


def init_crm():
    """Create CRM tables and migrate leads table."""
    table_stmts = [
        """CREATE TABLE IF NOT EXISTS leads (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT, phone TEXT, website TEXT, address TEXT, city TEXT, state TEXT,
            rating REAL, review_count INTEGER, category TEXT, place_id TEXT,
            decision_maker_name TEXT, decision_maker_title TEXT,
            email TEXT, email_type TEXT, email_confidence TEXT, email_method TEXT,
            scraped_at TEXT, delivered_at TEXT, source TEXT, niche TEXT,
            has_facebook_ads INTEGER DEFAULT 0, facebook_ad_count INTEGER,
            facebook_ad_samples TEXT, ad_analysis TEXT,
            outreach_angle TEXT, angle_type TEXT,
            email_verified TEXT, email_verify_status TEXT,
            lead_stage TEXT DEFAULT 'new',
            instantly_campaign_id TEXT, email_sent_at TEXT, email_opened_at TEXT,
            email_replied_at TEXT, email_bounced INTEGER DEFAULT 0, reply_text TEXT,
            subject_line TEXT
        )""",
        """CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT UNIQUE NOT NULL, password_hash TEXT NOT NULL,
            name TEXT NOT NULL, role TEXT DEFAULT 'viewer', created_at TEXT NOT NULL
        )""",
        """CREATE TABLE IF NOT EXISTS notes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            lead_id INTEGER NOT NULL, user_id INTEGER, note_text TEXT NOT NULL,
            created_at TEXT NOT NULL, FOREIGN KEY (lead_id) REFERENCES leads(id)
        )""",
        """CREATE TABLE IF NOT EXISTS tags (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            lead_id INTEGER NOT NULL, tag_name TEXT NOT NULL,
            FOREIGN KEY (lead_id) REFERENCES leads(id), UNIQUE(lead_id, tag_name)
        )""",
        """CREATE TABLE IF NOT EXISTS activity_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            lead_id INTEGER, action TEXT NOT NULL, details TEXT,
            user_id INTEGER, created_at TEXT NOT NULL
        )""",
        """CREATE TABLE IF NOT EXISTS pipeline_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_date TEXT NOT NULL, run_type TEXT NOT NULL, status TEXT DEFAULT 'running',
            state_name TEXT, niches TEXT,
            leads_scraped INTEGER DEFAULT 0, leads_enriched INTEGER DEFAULT 0,
            emails_found INTEGER DEFAULT 0, emails_verified INTEGER DEFAULT 0,
            ads_checked INTEGER DEFAULT 0, angles_generated INTEGER DEFAULT 0,
            csvs_exported INTEGER DEFAULT 0, leads_uploaded INTEGER DEFAULT 0,
            errors TEXT, duration_sec REAL, details TEXT,
            started_at TEXT NOT NULL, finished_at TEXT
        )""",
        "CREATE INDEX IF NOT EXISTS idx_pipeline_runs_date ON pipeline_runs(run_date)",
        "CREATE INDEX IF NOT EXISTS idx_leads_stage ON leads(lead_stage)",
        "CREATE INDEX IF NOT EXISTS idx_notes_lead ON notes(lead_id)",
        "CREATE INDEX IF NOT EXISTS idx_tags_lead ON tags(lead_id)",
        "CREATE INDEX IF NOT EXISTS idx_activity_lead ON activity_log(lead_id)",
    ]

    if _use_turso:
        _turso_batch(table_stmts)
        # Check if admin exists
        users = _turso_execute("SELECT COUNT(*) as cnt FROM users", (), want_rows=True)
        if not users or int(users[0].get("cnt", 0)) == 0:
            _turso_execute(
                "INSERT INTO users (email, password_hash, name, role, created_at) VALUES (?,?,?,?,?)",
                ("admin@bookedly.com", generate_password_hash("bookedly", method="pbkdf2:sha256"),
                 "Admin", "admin", datetime.utcnow().isoformat()),
            )
            print("[crm] Created default admin: admin@bookedly.com / bookedly")
        return

    conn = get_conn()
    conn.executescript("; ".join(table_stmts))

    # Migrate leads table — add CRM columns if missing
    existing = {row[1] for row in conn.execute("PRAGMA table_info(leads)").fetchall()}
    crm_cols = {
        "lead_stage": "TEXT DEFAULT 'new'",
        "instantly_campaign_id": "TEXT",
        "email_sent_at": "TEXT",
        "email_opened_at": "TEXT",
        "email_replied_at": "TEXT",
        "email_bounced": "INTEGER DEFAULT 0",
        "reply_text": "TEXT",
    }
    for col, col_type in crm_cols.items():
        if col not in existing:
            conn.execute(f"ALTER TABLE leads ADD COLUMN {col} {col_type}")

    conn.commit()

    # Default admin user
    if conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0:
        conn.execute(
            "INSERT INTO users (email, password_hash, name, role, created_at) VALUES (?,?,?,?,?)",
            ("admin@bookedly.com", generate_password_hash("bookedly", method="pbkdf2:sha256"), "Admin", "admin",
             datetime.utcnow().isoformat()),
        )
        conn.commit()
        print("[crm] Created default admin: admin@bookedly.com / bookedly")

    conn.close()


# ═══════════════════════════════════════════════════════════════════════
# AUTH
# ═══════════════════════════════════════════════════════════════════════

def current_user():
    uid = session.get("user_id")
    if not uid:
        return None
    rows = query("SELECT * FROM users WHERE id = ?", (int(uid),))
    return rows[0] if rows else None


def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not current_user():
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated


def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        u = current_user()
        if not u or u["role"] != "admin":
            abort(403)
        return f(*args, **kwargs)
    return decorated


# ═══════════════════════════════════════════════════════════════════════
# INSTANTLY SYNC
# ═══════════════════════════════════════════════════════════════════════

def sync_instantly() -> dict:
    """Pull lead engagement data back from Instantly API v2."""
    import requests

    api_key = getattr(config, "INSTANTLY_API_KEY", None) or os.getenv("INSTANTLY_API_KEY")
    if not api_key:
        return {"error": "No INSTANTLY_API_KEY set"}

    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    base = "https://api.instantly.ai/api/v2"

    # Fetch campaigns
    try:
        resp = requests.get(f"{base}/campaigns", headers=headers, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        campaigns = data if isinstance(data, list) else data.get("data", data.get("items", []))
    except Exception as e:
        return {"error": f"Failed to fetch campaigns: {e}"}

    synced = 0
    for camp in campaigns:
        cid = camp.get("id") or camp.get("campaign_id")
        if not cid:
            continue

        try:
            skip = 0
            while True:
                resp = requests.get(
                    f"{base}/leads",
                    headers=headers,
                    params={"campaign_id": cid, "limit": 100, "skip": skip},
                    timeout=30,
                )
                resp.raise_for_status()
                rdata = resp.json()
                items = rdata if isinstance(rdata, list) else rdata.get("items", rdata.get("data", rdata.get("leads", [])))

                if not items:
                    break

                conn = get_conn()
                for item in items:
                    email = (item.get("email") or "").strip().lower()
                    if not email:
                        continue

                    is_opened = item.get("opened") or item.get("open_count", 0) > 0
                    is_replied = item.get("replied") or item.get("reply_count", 0) > 0
                    is_bounced = item.get("bounced") or item.get("status") == "bounced"

                    sets = ["instantly_campaign_id = ?"]
                    params = [cid]

                    sent_at = item.get("sent_at") or item.get("last_email_sent_at")
                    if sent_at or item.get("status") in ("active", "contacted", "completed"):
                        sets.append("email_sent_at = COALESCE(email_sent_at, ?)")
                        params.append(sent_at or datetime.utcnow().isoformat())

                    if is_opened:
                        sets.append("email_opened_at = COALESCE(email_opened_at, ?)")
                        params.append(item.get("opened_at") or item.get("last_open_at") or datetime.utcnow().isoformat())

                    if is_replied:
                        sets.append("email_replied_at = COALESCE(email_replied_at, ?)")
                        params.append(item.get("replied_at") or datetime.utcnow().isoformat())
                        reply = item.get("reply_text") or item.get("reply") or ""
                        if reply:
                            sets.append("reply_text = ?")
                            params.append(reply)

                    if is_bounced:
                        sets.append("email_bounced = 1")

                    # Auto-stage based on engagement
                    if is_replied:
                        sets.append("lead_stage = CASE WHEN lead_stage IN ('new','enriched','contacted','opened') THEN 'replied' ELSE lead_stage END")
                    elif is_opened:
                        sets.append("lead_stage = CASE WHEN lead_stage IN ('new','enriched','contacted') THEN 'opened' ELSE lead_stage END")
                    elif sent_at:
                        sets.append("lead_stage = CASE WHEN lead_stage IN ('new','enriched') THEN 'contacted' ELSE lead_stage END")

                    params.append(email)
                    conn.execute(f"UPDATE leads SET {', '.join(sets)} WHERE LOWER(email) = ?", params)
                    synced += 1

                conn.commit()
                conn.close()

                if len(items) < 100:
                    break
                skip += 100

        except Exception as e:
            logger.warning(f"Instantly sync failed for campaign {cid}: {e}")

    return {"synced": synced, "campaigns": len(campaigns)}


# ═══════════════════════════════════════════════════════════════════════
# DATA QUERIES
# ═══════════════════════════════════════════════════════════════════════

def get_stage_counts() -> dict:
    rows = query("SELECT COALESCE(lead_stage, 'new') as stage, COUNT(*) as cnt FROM leads GROUP BY 1")
    counts = {s: 0 for s in STAGES}
    for r in rows:
        if r["stage"] in counts:
            counts[r["stage"]] = r["cnt"]
        else:
            counts["new"] += r["cnt"]
    return counts


def get_overview() -> dict:
    total = scalar("SELECT COUNT(*) FROM leads")
    with_email = scalar("SELECT COUNT(*) FROM leads WHERE email IS NOT NULL AND email != ''")
    verified = scalar("SELECT COUNT(*) FROM leads WHERE email_verified = 'good'")
    with_dm = scalar("SELECT COUNT(*) FROM leads WHERE decision_maker_name IS NOT NULL AND decision_maker_name != ''")
    with_angle = scalar("SELECT COUNT(*) FROM leads WHERE angle_type IS NOT NULL AND angle_type != ''")
    delivered = scalar("SELECT COUNT(*) FROM leads WHERE delivered_at IS NOT NULL")
    sent = scalar("SELECT COUNT(*) FROM leads WHERE email_sent_at IS NOT NULL")
    opened = scalar("SELECT COUNT(*) FROM leads WHERE email_opened_at IS NOT NULL")
    replied = scalar("SELECT COUNT(*) FROM leads WHERE email_replied_at IS NOT NULL")
    bounced = scalar("SELECT COUNT(*) FROM leads WHERE email_bounced = 1")
    today_str = datetime.utcnow().strftime("%Y-%m-%d")
    today = scalar("SELECT COUNT(*) FROM leads WHERE scraped_at LIKE ?", (today_str + "%",))
    return {
        "total": total, "with_email": with_email, "verified": verified,
        "with_dm": with_dm, "with_angle": with_angle, "delivered": delivered,
        "sent": sent, "opened": opened, "replied": replied, "bounced": bounced,
        "today": today,
        "open_rate": round(opened / sent * 100, 1) if sent else 0,
        "reply_rate": round(replied / sent * 100, 1) if sent else 0,
        "bounce_rate": round(bounced / sent * 100, 1) if sent else 0,
    }


def get_leads_page(page=1, per_page=50, **filters):
    conds, params = [], []
    if filters.get("stage"):
        conds.append("COALESCE(lead_stage, 'new') = ?")
        params.append(filters["stage"])
    if filters.get("niche"):
        conds.append("niche = ?")
        params.append(filters["niche"])
    if filters.get("state"):
        conds.append("state = ?")
        params.append(filters["state"])
    if filters.get("search"):
        conds.append("(name LIKE ? OR email LIKE ? OR city LIKE ? OR decision_maker_name LIKE ?)")
        s = f"%{filters['search']}%"
        params.extend([s, s, s, s])
    if filters.get("email_status"):
        conds.append("email_verified = ?")
        params.append(filters["email_status"])
    if filters.get("has_ads") == "yes":
        conds.append("has_facebook_ads = 1")
    elif filters.get("has_ads") == "no":
        conds.append("(has_facebook_ads = 0 OR has_facebook_ads IS NULL)")
    if filters.get("angle"):
        conds.append("angle_type = ?")
        params.append(filters["angle"])

    where = " WHERE " + " AND ".join(conds) if conds else ""
    total = scalar(f"SELECT COUNT(*) FROM leads{where}", tuple(params))
    offset = (page - 1) * per_page
    leads = query(
        f"SELECT * FROM leads{where} ORDER BY scraped_at DESC LIMIT ? OFFSET ?",
        tuple(params) + (per_page, offset),
    )
    return {"leads": leads, "total": total, "page": page, "per_page": per_page,
            "pages": max(1, (total + per_page - 1) // per_page)}


def get_lead_detail(lead_id: int):
    rows = query("SELECT * FROM leads WHERE id = ?", (lead_id,))
    if not rows:
        return None
    lead = rows[0]
    lead["notes"] = query(
        "SELECT n.*, u.name as user_name FROM notes n LEFT JOIN users u ON n.user_id=u.id "
        "WHERE n.lead_id=? ORDER BY n.created_at DESC", (lead_id,))
    lead["tags"] = [r["tag_name"] for r in query("SELECT tag_name FROM tags WHERE lead_id=?", (lead_id,))]
    lead["activity"] = query(
        "SELECT a.*, u.name as user_name FROM activity_log a LEFT JOIN users u ON a.user_id=u.id "
        "WHERE a.lead_id=? ORDER BY a.created_at DESC LIMIT 50", (lead_id,))
    return lead


def get_funnel() -> dict:
    return {
        "scraped": scalar("SELECT COUNT(*) FROM leads"),
        "has_email": scalar("SELECT COUNT(*) FROM leads WHERE email IS NOT NULL AND email != ''"),
        "verified": scalar("SELECT COUNT(*) FROM leads WHERE email_verified = 'good'"),
        "has_dm": scalar("SELECT COUNT(*) FROM leads WHERE decision_maker_name IS NOT NULL AND decision_maker_name != ''"),
        "has_angle": scalar("SELECT COUNT(*) FROM leads WHERE angle_type IS NOT NULL AND angle_type != ''"),
        "sent": scalar("SELECT COUNT(*) FROM leads WHERE email_sent_at IS NOT NULL"),
        "opened": scalar("SELECT COUNT(*) FROM leads WHERE email_opened_at IS NOT NULL"),
        "replied": scalar("SELECT COUNT(*) FROM leads WHERE email_replied_at IS NOT NULL"),
        "booked": scalar("SELECT COUNT(*) FROM leads WHERE lead_stage = 'booked'"),
    }


def get_filter_options() -> dict:
    niches = [r["niche"] for r in query(
        "SELECT DISTINCT niche FROM leads WHERE niche IS NOT NULL AND niche != '' ORDER BY niche")]
    states = [r["state"] for r in query(
        "SELECT DISTINCT state FROM leads WHERE state IS NOT NULL AND state != '' ORDER BY state")]
    angles = [r["angle_type"] for r in query(
        "SELECT DISTINCT angle_type FROM leads WHERE angle_type IS NOT NULL AND angle_type != '' ORDER BY angle_type")]
    return {"niches": niches, "states": states, "angles": angles}


def get_analytics() -> dict:
    by_niche = query("""
        SELECT niche, COUNT(*) as total,
            SUM(CASE WHEN email_sent_at IS NOT NULL THEN 1 ELSE 0 END) as sent,
            SUM(CASE WHEN email_opened_at IS NOT NULL THEN 1 ELSE 0 END) as opened,
            SUM(CASE WHEN email_replied_at IS NOT NULL THEN 1 ELSE 0 END) as replied
        FROM leads WHERE niche IS NOT NULL AND niche != '' GROUP BY niche ORDER BY total DESC
    """)
    by_angle = query("""
        SELECT COALESCE(angle_type, 'none') as angle_type, COUNT(*) as total,
            SUM(CASE WHEN email_sent_at IS NOT NULL THEN 1 ELSE 0 END) as sent,
            SUM(CASE WHEN email_replied_at IS NOT NULL THEN 1 ELSE 0 END) as replied
        FROM leads GROUP BY 1 ORDER BY total DESC
    """)
    by_state = query("""
        SELECT state, COUNT(*) as total,
            SUM(CASE WHEN email IS NOT NULL AND email != '' THEN 1 ELSE 0 END) as with_email,
            SUM(CASE WHEN email_verified = 'good' THEN 1 ELSE 0 END) as verified
        FROM leads WHERE state IS NOT NULL AND state != '' GROUP BY state ORDER BY total DESC
    """)
    daily = query("""
        SELECT DATE(scraped_at) as day, COUNT(*) as scraped,
            SUM(CASE WHEN email IS NOT NULL AND email != '' THEN 1 ELSE 0 END) as emails
        FROM leads GROUP BY 1 ORDER BY day DESC LIMIT 30
    """)
    verification = {
        "good": scalar("SELECT COUNT(*) FROM leads WHERE email_verified = 'good'"),
        "risky": scalar("SELECT COUNT(*) FROM leads WHERE email_verified = 'risky'"),
        "bad": scalar("SELECT COUNT(*) FROM leads WHERE email_verified = 'bad'"),
        "unverified": scalar("SELECT COUNT(*) FROM leads WHERE email IS NOT NULL AND email != '' AND (email_verified IS NULL OR email_verified = '')"),
    }
    return {"by_niche": by_niche, "by_angle": by_angle, "by_state": by_state,
            "daily": daily, "verification": verification}


def get_cold_email_data() -> dict:
    """Get data for the Cold Email channel page — agent activity reports."""
    # Pipeline run history
    runs = query("""
        SELECT * FROM pipeline_runs ORDER BY started_at DESC LIMIT 30
    """)

    # Daily activity (last 14 days)
    daily_activity = query("""
        SELECT DATE(scraped_at) as day,
            COUNT(*) as scraped,
            SUM(CASE WHEN email IS NOT NULL AND email != '' THEN 1 ELSE 0 END) as got_email,
            SUM(CASE WHEN decision_maker_name IS NOT NULL AND decision_maker_name != '' THEN 1 ELSE 0 END) as got_dm,
            SUM(CASE WHEN email_verified = 'good' THEN 1 ELSE 0 END) as verified,
            SUM(CASE WHEN outreach_angle IS NOT NULL AND outreach_angle != '' THEN 1 ELSE 0 END) as got_angle,
            SUM(CASE WHEN has_facebook_ads = 1 THEN 1 ELSE 0 END) as has_ads
        FROM leads
        GROUP BY 1 ORDER BY day DESC LIMIT 14
    """)

    # Email outreach stats
    outreach = {
        "total_sent": scalar("SELECT COUNT(*) FROM leads WHERE email_sent_at IS NOT NULL"),
        "total_opened": scalar("SELECT COUNT(*) FROM leads WHERE email_opened_at IS NOT NULL"),
        "total_replied": scalar("SELECT COUNT(*) FROM leads WHERE email_replied_at IS NOT NULL"),
        "total_bounced": scalar("SELECT COUNT(*) FROM leads WHERE email_bounced = 1"),
        "ready_to_send": scalar("""SELECT COUNT(*) FROM leads
            WHERE email IS NOT NULL AND email != ''
            AND email_verified = 'good'
            AND outreach_angle IS NOT NULL AND outreach_angle != ''
            AND email_sent_at IS NULL"""),
        "pending_enrich": scalar("""SELECT COUNT(*) FROM leads
            WHERE (email IS NULL OR email = '')"""),
        "pending_verify": scalar("""SELECT COUNT(*) FROM leads
            WHERE email IS NOT NULL AND email != ''
            AND (email_verified IS NULL OR email_verified = '')"""),
        "pending_angles": scalar("""SELECT COUNT(*) FROM leads
            WHERE email IS NOT NULL AND email != ''
            AND (outreach_angle IS NULL OR outreach_angle = '')"""),
        "pending_ads": scalar("SELECT COUNT(*) FROM leads WHERE has_facebook_ads IS NULL"),
    }
    open_rate = round(outreach["total_opened"] / outreach["total_sent"] * 100, 1) if outreach["total_sent"] else 0
    reply_rate = round(outreach["total_replied"] / outreach["total_sent"] * 100, 1) if outreach["total_sent"] else 0
    bounce_rate = round(outreach["total_bounced"] / outreach["total_sent"] * 100, 1) if outreach["total_sent"] else 0
    outreach["open_rate"] = open_rate
    outreach["reply_rate"] = reply_rate
    outreach["bounce_rate"] = bounce_rate

    # Recent replies
    replies = query("""
        SELECT id, name, email, decision_maker_name, reply_text, email_replied_at, lead_stage, niche
        FROM leads WHERE email_replied_at IS NOT NULL
        ORDER BY email_replied_at DESC LIMIT 10
    """)

    # Today's summary
    today_str = datetime.utcnow().strftime("%Y-%m-%d")
    today = {
        "scraped": scalar("SELECT COUNT(*) FROM leads WHERE scraped_at LIKE ?", (today_str + "%",)),
        "emails_found": scalar("SELECT COUNT(*) FROM leads WHERE scraped_at LIKE ? AND email IS NOT NULL AND email != ''", (today_str + "%",)),
        "enriched": scalar("SELECT COUNT(*) FROM leads WHERE scraped_at LIKE ? AND decision_maker_name IS NOT NULL", (today_str + "%",)),
    }

    # Last pipeline run
    last_run = runs[0] if runs else None

    return {
        "runs": runs,
        "daily_activity": daily_activity,
        "outreach": outreach,
        "replies": replies,
        "today": today,
        "last_run": last_run,
    }


def log_pipeline_run(run_type: str, **kwargs) -> int:
    """Log a pipeline run start. Returns the run ID."""
    conn = get_conn()
    try:
        cur = conn.execute(
            """INSERT INTO pipeline_runs (run_date, run_type, status, state_name, started_at)
               VALUES (?, ?, 'running', ?, ?)""",
            (datetime.utcnow().strftime("%Y-%m-%d"), run_type,
             kwargs.get("state_name"), datetime.utcnow().isoformat()),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def finish_pipeline_run(run_id: int, status: str = "completed", **kwargs):
    """Update a pipeline run with results."""
    sets = ["status = ?", "finished_at = ?"]
    params = [status, datetime.utcnow().isoformat()]
    for key in ("leads_scraped", "leads_enriched", "emails_found", "emails_verified",
                "ads_checked", "angles_generated", "csvs_exported", "leads_uploaded",
                "errors", "duration_sec", "details", "niches"):
        if key in kwargs:
            sets.append(f"{key} = ?")
            params.append(kwargs[key])
    params.append(run_id)
    execute(f"UPDATE pipeline_runs SET {', '.join(sets)} WHERE id = ?", tuple(params))


# ═══════════════════════════════════════════════════════════════════════
# JINJA FILTERS
# ═══════════════════════════════════════════════════════════════════════

@app.template_filter("commas")
def commas_filter(n):
    try:
        return f"{int(n):,}"
    except (TypeError, ValueError):
        return str(n)


@app.template_filter("stage_cls")
def stage_cls_filter(stage):
    m = {
        "new": "bg-slate-500/20 text-slate-300",
        "enriched": "bg-blue-500/20 text-blue-400",
        "contacted": "bg-indigo-500/20 text-indigo-400",
        "opened": "bg-cyan-500/20 text-cyan-400",
        "replied": "bg-emerald-500/20 text-emerald-400",
        "interested": "bg-amber-500/20 text-amber-400",
        "booked": "bg-purple-500/20 text-purple-400",
        "client": "bg-green-500/20 text-green-400",
        "lost": "bg-rose-500/20 text-rose-400",
    }
    return m.get(stage or "new", m["new"])


@app.template_filter("nl2br")
def nl2br_filter(text):
    if not text:
        return ""
    from markupsafe import Markup
    return Markup(str(text).replace("\n", "<br>"))


@app.template_filter("timeago")
def timeago_filter(dt_str):
    if not dt_str:
        return ""
    try:
        dt = datetime.fromisoformat(dt_str.replace("Z", "+00:00").split("+")[0])
        diff = datetime.utcnow() - dt
        if diff.days > 30:
            return f"{diff.days // 30}mo ago"
        if diff.days > 0:
            return f"{diff.days}d ago"
        hours = diff.seconds // 3600
        if hours > 0:
            return f"{hours}h ago"
        return f"{diff.seconds // 60}m ago"
    except Exception:
        return dt_str[:10] if dt_str else ""


# ═══════════════════════════════════════════════════════════════════════
# TEMPLATES
# ═══════════════════════════════════════════════════════════════════════

# ── Base layout: head + sidebar + footer ──
# Page-specific content replaces <!--BODY-->

BASE_HEAD = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{{ title }} | Bookedly CRM</title>
<script src="https://cdn.tailwindcss.com"></script>
<script>tailwind.config={darkMode:'class',theme:{extend:{colors:{brand:'#3b82f6'}}}}</script>
<link href="https://cdn.jsdelivr.net/npm/bootstrap-icons@1.11.3/font/bootstrap-icons.min.css" rel="stylesheet">
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
<style>
html{color-scheme:dark}
body{background:#0f172a;color:#e2e8f0;font-family:Inter,-apple-system,BlinkMacSystemFont,sans-serif}
.sidebar-link{display:flex;align-items:center;gap:10px;padding:9px 16px;margin:2px 8px;border-radius:8px;font-size:.82rem;color:#94a3b8;transition:all .15s}
.sidebar-link:hover{background:rgba(59,130,246,.08);color:#fff;text-decoration:none}
.sidebar-link.active{background:rgba(59,130,246,.15);color:#60a5fa;font-weight:600}
.card{background:#1e293b;border:1px solid #334155;border-radius:12px}
.stat-card{background:#1e293b;border:1px solid #334155;border-radius:12px;padding:16px 20px;transition:transform .15s,border-color .15s}
.stat-card:hover{transform:translateY(-2px);border-color:rgba(59,130,246,.3)}
.tag{display:inline-block;padding:2px 8px;border-radius:6px;font-size:.7rem;font-weight:600}
.input{background:#334155;border:1px solid #475569;border-radius:8px;padding:7px 12px;font-size:.82rem;color:#e2e8f0;outline:none}
.input:focus{border-color:#3b82f6;box-shadow:0 0 0 2px rgba(59,130,246,.2)}
.btn{padding:7px 16px;border-radius:8px;font-size:.82rem;font-weight:500;cursor:pointer;transition:all .15s;border:none}
.btn-primary{background:#3b82f6;color:#fff}.btn-primary:hover{background:#2563eb}
.btn-sm{padding:5px 12px;font-size:.75rem;border-radius:6px}
.btn-ghost{background:transparent;color:#94a3b8;border:1px solid #475569}.btn-ghost:hover{background:#334155;color:#fff}
select.input{appearance:none;background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='12' viewBox='0 0 24 24' fill='none' stroke='%2394a3b8' stroke-width='2'%3E%3Cpath d='M6 9l6 6 6-6'/%3E%3C/svg%3E");background-repeat:no-repeat;background-position:right 10px center;padding-right:30px}
table{width:100%;border-collapse:collapse}
th{text-align:left;padding:10px 16px;font-size:.7rem;text-transform:uppercase;letter-spacing:.5px;color:#64748b;font-weight:600;border-bottom:1px solid #334155}
td{padding:10px 16px;border-bottom:1px solid rgba(51,65,85,.5);vertical-align:middle}
tr:hover{background:rgba(59,130,246,.03)}
.truncate-email{max-width:180px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
@media(max-width:768px){.sidebar{width:0!important;overflow:hidden}.main-area{margin-left:0!important}}
</style>
</head>
<body>
"""

BASE_SIDEBAR = """
<nav class="sidebar" style="position:fixed;top:0;left:0;width:220px;height:100vh;background:#020617;border-right:1px solid #1e293b;display:flex;flex-direction:column;z-index:50">
  <div style="padding:20px 16px 16px;border-bottom:1px solid #1e293b">
    <h1 style="font-size:1.1rem;font-weight:700;background:linear-gradient(135deg,#60a5fa,#a78bfa);-webkit-background-clip:text;-webkit-text-fill-color:transparent;margin:0">Bookedly</h1>
    <small style="font-size:.6rem;text-transform:uppercase;letter-spacing:2px;color:#475569">CRM</small>
  </div>
  <div style="flex:1;padding:8px 0;overflow-y:auto">
    <div style="padding:8px 20px 4px;font-size:.6rem;text-transform:uppercase;letter-spacing:1.5px;color:#475569;font-weight:600">Main</div>
    <a href="/" class="sidebar-link {{ 'active' if page=='pipeline' }}"><i class="bi bi-kanban" style="width:18px"></i> Pipeline</a>
    <a href="/leads" class="sidebar-link {{ 'active' if page=='leads' }}"><i class="bi bi-people" style="width:18px"></i> Leads</a>
    <a href="/analytics" class="sidebar-link {{ 'active' if page=='analytics' }}"><i class="bi bi-graph-up" style="width:18px"></i> Analytics</a>
    <div style="padding:12px 20px 4px;font-size:.6rem;text-transform:uppercase;letter-spacing:1.5px;color:#475569;font-weight:600">Channels</div>
    <a href="/channels/cold-email" class="sidebar-link {{ 'active' if page=='cold-email' }}"><i class="bi bi-envelope" style="width:18px"></i> Cold Email</a>
    <a href="/leads?has_ads=yes" class="sidebar-link"><i class="bi bi-megaphone" style="width:18px"></i> FB Ads Intel</a>
    <a href="#" class="sidebar-link" style="opacity:.4"><i class="bi bi-chat-dots" style="width:18px"></i> DMs <span style="margin-left:auto;font-size:.6rem;background:#334155;padding:1px 6px;border-radius:4px">soon</span></a>
    {% if user and user.role == 'admin' %}
    <div style="padding:12px 20px 4px;font-size:.6rem;text-transform:uppercase;letter-spacing:1.5px;color:#475569;font-weight:600">Admin</div>
    <a href="/settings" class="sidebar-link {{ 'active' if page=='settings' }}"><i class="bi bi-gear" style="width:18px"></i> Settings</a>
    {% endif %}
  </div>
  <div style="padding:12px 16px;border-top:1px solid #1e293b">
    {% if user %}
    <div style="display:flex;align-items:center;gap:10px">
      <div style="width:32px;height:32px;border-radius:50%;background:rgba(59,130,246,.2);display:flex;align-items:center;justify-content:center;color:#60a5fa;font-size:.75rem;font-weight:700">{{ user.name[0] }}</div>
      <div style="flex:1;min-width:0"><div style="font-size:.75rem;font-weight:500">{{ user.name }}</div><div style="font-size:.6rem;color:#475569">{{ user.role }}</div></div>
      <a href="/logout" style="color:#475569" title="Logout"><i class="bi bi-box-arrow-right"></i></a>
    </div>
    {% endif %}
  </div>
</nav>
"""

BASE_FOOT = """
</body></html>
"""

# ── Login page (no sidebar) ──
LOGIN_PAGE = (
    BASE_HEAD
    + """
<div style="min-height:100vh;display:flex;align-items:center;justify-content:center">
  <div style="width:100%;max-width:380px;padding:0 20px">
    <div style="text-align:center;margin-bottom:32px">
      <h1 style="font-size:1.5rem;font-weight:700;background:linear-gradient(135deg,#60a5fa,#a78bfa);-webkit-background-clip:text;-webkit-text-fill-color:transparent">Bookedly</h1>
      <p style="font-size:.65rem;text-transform:uppercase;letter-spacing:2px;color:#475569;margin-top:4px">CRM Login</p>
    </div>
    {% if error %}<div style="background:rgba(239,68,68,.1);border:1px solid rgba(239,68,68,.3);color:#f87171;padding:10px 14px;border-radius:8px;font-size:.8rem;margin-bottom:16px">{{ error }}</div>{% endif %}
    <form method="POST" class="card" style="padding:24px">
      <div style="margin-bottom:16px">
        <label style="display:block;font-size:.7rem;color:#94a3b8;margin-bottom:6px;text-transform:uppercase;letter-spacing:.5px">Email</label>
        <input type="email" name="email" class="input" style="width:100%" required value="{{ req_email or '' }}">
      </div>
      <div style="margin-bottom:20px">
        <label style="display:block;font-size:.7rem;color:#94a3b8;margin-bottom:6px;text-transform:uppercase;letter-spacing:.5px">Password</label>
        <input type="password" name="password" class="input" style="width:100%" required>
      </div>
      <button type="submit" class="btn btn-primary" style="width:100%">Sign In</button>
    </form>
  </div>
</div>
"""
    + BASE_FOOT
)

# ── Pipeline page ──
PIPELINE_BODY = """
<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:20px">
  <div><h2 style="font-size:1.4rem;font-weight:700;margin:0">Pipeline</h2><p style="font-size:.75rem;color:#64748b;margin:4px 0 0">{{ overview.total | commas }} total leads</p></div>
  <div style="display:flex;gap:8px">
    {% if user and user.role == 'admin' %}
    <button onclick="syncInstantly()" class="btn btn-sm" style="background:#7c3aed;color:#fff" id="syncBtn"><i class="bi bi-arrow-repeat"></i> Sync Instantly</button>
    {% endif %}
    <a href="/" class="btn btn-sm btn-ghost"><i class="bi bi-arrow-clockwise"></i> Refresh</a>
  </div>
</div>

<!-- Stage pills -->
<div style="display:flex;gap:8px;margin-bottom:20px;overflow-x:auto;padding-bottom:4px">
  <a href="/" class="btn btn-sm {{ 'btn-primary' if not active_stage else 'btn-ghost' }}">All <span style="margin-left:4px;font-size:.65rem;opacity:.7">{{ overview.total }}</span></a>
  {% for stg in stages %}
  <a href="/?stage={{ stg }}" class="btn btn-sm {{ 'btn-primary' if active_stage == stg else 'btn-ghost' }}">
    {{ stg | capitalize }} <span style="margin-left:4px;font-size:.65rem;opacity:.7">{{ stage_counts[stg] }}</span>
  </a>
  {% endfor %}
</div>

<!-- KPI cards -->
<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin-bottom:20px">
  <div class="stat-card">
    <div style="font-size:.65rem;color:#64748b;text-transform:uppercase;letter-spacing:.5px">With Email</div>
    <div style="font-size:1.4rem;font-weight:700;margin:4px 0">{{ overview.with_email | commas }}</div>
    <div style="font-size:.7rem;color:#34d399">{{ (overview.with_email * 100 // overview.total if overview.total else 0) }}% rate</div>
  </div>
  <div class="stat-card">
    <div style="font-size:.65rem;color:#64748b;text-transform:uppercase;letter-spacing:.5px">Verified Good</div>
    <div style="font-size:1.4rem;font-weight:700;margin:4px 0">{{ overview.verified | commas }}</div>
    <div style="font-size:.7rem;color:#34d399">safe to send</div>
  </div>
  <div class="stat-card">
    <div style="font-size:.65rem;color:#64748b;text-transform:uppercase;letter-spacing:.5px">Sent</div>
    <div style="font-size:1.4rem;font-weight:700;margin:4px 0">{{ overview.sent | commas }}</div>
    <div style="font-size:.7rem;color:#818cf8">via Instantly</div>
  </div>
  <div class="stat-card">
    <div style="font-size:.65rem;color:#64748b;text-transform:uppercase;letter-spacing:.5px">Open Rate</div>
    <div style="font-size:1.4rem;font-weight:700;margin:4px 0">{{ overview.open_rate }}%</div>
    <div style="font-size:.7rem;color:#22d3ee">{{ overview.opened }} opened</div>
  </div>
  <div class="stat-card">
    <div style="font-size:.65rem;color:#64748b;text-transform:uppercase;letter-spacing:.5px">Reply Rate</div>
    <div style="font-size:1.4rem;font-weight:700;margin:4px 0">{{ overview.reply_rate }}%</div>
    <div style="font-size:.7rem;color:#34d399">{{ overview.replied }} replies</div>
  </div>
  <div class="stat-card">
    <div style="font-size:.65rem;color:#64748b;text-transform:uppercase;letter-spacing:.5px">Bounce Rate</div>
    <div style="font-size:1.4rem;font-weight:700;margin:4px 0">{{ overview.bounce_rate }}%</div>
    <div style="font-size:.7rem;color:#f87171">{{ overview.bounced }} bounced</div>
  </div>
</div>

<!-- Leads table -->
<div class="card" style="overflow:hidden">
  <div style="padding:14px 16px;border-bottom:1px solid #334155;display:flex;justify-content:space-between;align-items:center">
    <span style="font-size:.8rem;font-weight:600">{% if active_stage %}{{ active_stage | capitalize }} Leads{% else %}All Leads{% endif %}</span>
    <span style="font-size:.7rem;color:#64748b">{{ leads_data.total }} results</span>
  </div>
  <div style="overflow-x:auto">
    <table>
      <thead><tr>
        <th>Company</th><th>Contact</th><th>Email</th><th>Stage</th><th>Niche</th><th>Ads</th><th>Angle</th>
      </tr></thead>
      <tbody>
        {% for lead in leads_data.leads %}
        <tr style="cursor:pointer" onclick="location='/lead/{{ lead.id }}'">
          <td><div style="font-weight:500">{{ lead.name or '-' }}</div><div style="font-size:.7rem;color:#64748b">{{ lead.city or '' }}{% if lead.state %}, {{ lead.state }}{% endif %}</div></td>
          <td>{% if lead.decision_maker_name %}<span style="color:#34d399;font-size:.75rem">{{ lead.decision_maker_name }}</span>{% else %}<span style="color:#475569">-</span>{% endif %}</td>
          <td>{% if lead.email %}<div class="truncate-email" style="color:#22d3ee;font-size:.75rem" title="{{ lead.email }}">{{ lead.email }}</div>
            <span class="tag {% if lead.email_verified=='good' %}style="background:rgba(52,211,153,.15);color:#34d399"{% elif lead.email_verified=='risky' %}style="background:rgba(251,146,60,.15);color:#fb923c"{% elif lead.email_verified=='bad' %}style="background:rgba(248,113,113,.15);color:#f87171"{% else %}style="background:rgba(100,116,139,.15);color:#64748b"{% endif %}">{{ lead.email_verified or 'unverified' }}</span>
          {% else %}<span style="color:#475569">-</span>{% endif %}</td>
          <td><span class="tag {{ (lead.lead_stage or 'new') | stage_cls }}">{{ (lead.lead_stage or 'new') | capitalize }}</span></td>
          <td>{% if lead.niche %}<span class="tag" style="background:rgba(59,130,246,.12);color:#60a5fa">{{ lead.niche }}</span>{% endif %}</td>
          <td>{% if lead.has_facebook_ads == 1 %}<span style="color:#34d399;font-size:.75rem">{{ lead.facebook_ad_count or 0 }} ads</span>{% elif lead.has_facebook_ads == 0 %}<span style="font-size:.75rem;color:#475569">No ads</span>{% else %}<span style="color:#334155">-</span>{% endif %}</td>
          <td>{% if lead.angle_type %}<span class="tag" style="background:rgba(167,139,250,.12);color:#a78bfa">{{ lead.angle_type }}</span>{% endif %}</td>
        </tr>
        {% endfor %}
        {% if not leads_data.leads %}
        <tr><td colspan="7" style="text-align:center;padding:40px;color:#475569">No leads found</td></tr>
        {% endif %}
      </tbody>
    </table>
  </div>
  {% if leads_data.pages > 1 %}
  <div style="padding:12px 16px;border-top:1px solid #334155;display:flex;justify-content:space-between;align-items:center">
    <span style="font-size:.7rem;color:#64748b">Page {{ leads_data.page }} of {{ leads_data.pages }}</span>
    <div style="display:flex;gap:4px">
      {% if leads_data.page > 1 %}<a href="?page={{ leads_data.page - 1 }}&stage={{ active_stage or '' }}" class="btn btn-sm btn-ghost">Prev</a>{% endif %}
      {% if leads_data.page < leads_data.pages %}<a href="?page={{ leads_data.page + 1 }}&stage={{ active_stage or '' }}" class="btn btn-sm btn-ghost">Next</a>{% endif %}
    </div>
  </div>
  {% endif %}
</div>

<script>
function syncInstantly(){
  const btn=document.getElementById('syncBtn');
  btn.innerHTML='<i class="bi bi-arrow-repeat spin"></i> Syncing...';btn.disabled=true;
  fetch('/api/sync-instantly',{method:'POST'}).then(r=>r.json()).then(d=>{
    if(d.error)alert('Sync error: '+d.error);
    else alert('Synced '+d.synced+' leads from '+d.campaigns+' campaigns');
    location.reload();
  }).catch(e=>{alert('Sync failed: '+e);btn.innerHTML='<i class="bi bi-arrow-repeat"></i> Sync Instantly';btn.disabled=false});
}
</script>
<style>.spin{animation:spin 1s linear infinite}@keyframes spin{to{transform:rotate(360deg)}}</style>
"""

# ── Leads browser page ──
LEADS_BODY = """
<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:20px">
  <div><h2 style="font-size:1.4rem;font-weight:700;margin:0">Leads</h2><p style="font-size:.75rem;color:#64748b;margin:4px 0 0">Search and filter all leads</p></div>
</div>

<!-- Filters -->
<form method="GET" action="/leads" style="display:flex;gap:8px;margin-bottom:16px;flex-wrap:wrap">
  <input type="text" name="search" class="input" placeholder="Search company, email, city..." value="{{ filters.search or '' }}" style="flex:1;min-width:200px">
  <select name="stage" class="input">
    <option value="">All Stages</option>
    {% for s in stages %}<option value="{{ s }}" {{ 'selected' if filters.stage == s }}>{{ s | capitalize }}</option>{% endfor %}
  </select>
  <select name="niche" class="input">
    <option value="">All Niches</option>
    {% for n in options.niches %}<option value="{{ n }}" {{ 'selected' if filters.niche == n }}>{{ n }}</option>{% endfor %}
  </select>
  <select name="state" class="input">
    <option value="">All States</option>
    {% for s in options.states %}<option value="{{ s }}" {{ 'selected' if filters.state == s }}>{{ s }}</option>{% endfor %}
  </select>
  <select name="email_status" class="input">
    <option value="">All Email Status</option>
    <option value="good" {{ 'selected' if filters.email_status == 'good' }}>Good</option>
    <option value="risky" {{ 'selected' if filters.email_status == 'risky' }}>Risky</option>
    <option value="bad" {{ 'selected' if filters.email_status == 'bad' }}>Bad</option>
  </select>
  <select name="has_ads" class="input">
    <option value="">All Ad Status</option>
    <option value="yes" {{ 'selected' if filters.has_ads == 'yes' }}>Has Ads</option>
    <option value="no" {{ 'selected' if filters.has_ads == 'no' }}>No Ads</option>
  </select>
  <button type="submit" class="btn btn-primary btn-sm"><i class="bi bi-search"></i> Filter</button>
  <a href="/leads" class="btn btn-sm btn-ghost">Clear</a>
</form>

<!-- Results table -->
<div class="card" style="overflow:hidden">
  <div style="padding:14px 16px;border-bottom:1px solid #334155;display:flex;justify-content:space-between;align-items:center">
    <span style="font-size:.8rem;font-weight:600">{{ leads_data.total }} leads found</span>
  </div>
  <div style="overflow-x:auto">
    <table>
      <thead><tr>
        <th>Company</th><th>Contact</th><th>Email</th><th>Stage</th><th>Niche</th><th>Location</th><th>Ads</th><th>Rating</th>
      </tr></thead>
      <tbody>
        {% for lead in leads_data.leads %}
        <tr style="cursor:pointer" onclick="location='/lead/{{ lead.id }}'">
          <td style="font-weight:500">{{ lead.name or '-' }}</td>
          <td>{% if lead.decision_maker_name %}<span style="color:#34d399;font-size:.75rem">{{ lead.decision_maker_name }}</span><br><span style="font-size:.65rem;color:#64748b">{{ lead.decision_maker_title or '' }}</span>{% else %}<span style="color:#475569">-</span>{% endif %}</td>
          <td>{% if lead.email %}<div class="truncate-email" style="color:#22d3ee;font-size:.75rem" title="{{ lead.email }}">{{ lead.email }}</div>
            <span class="tag" {% if lead.email_verified=='good' %}style="background:rgba(52,211,153,.15);color:#34d399"{% elif lead.email_verified=='risky' %}style="background:rgba(251,146,60,.15);color:#fb923c"{% elif lead.email_verified=='bad' %}style="background:rgba(248,113,113,.15);color:#f87171"{% else %}style="background:rgba(100,116,139,.15);color:#64748b"{% endif %}>{{ lead.email_verified or 'unverified' }}</span>
          {% else %}<span style="color:#475569">-</span>{% endif %}</td>
          <td><span class="tag {{ (lead.lead_stage or 'new') | stage_cls }}">{{ (lead.lead_stage or 'new') | capitalize }}</span></td>
          <td>{% if lead.niche %}<span class="tag" style="background:rgba(59,130,246,.12);color:#60a5fa">{{ lead.niche }}</span>{% endif %}</td>
          <td style="font-size:.75rem;color:#94a3b8;white-space:nowrap">{{ lead.city or '' }}{% if lead.state %}, {{ lead.state }}{% endif %}</td>
          <td>{% if lead.has_facebook_ads == 1 %}<span style="color:#34d399;font-size:.75rem">{{ lead.facebook_ad_count or 0 }}</span>{% elif lead.has_facebook_ads == 0 %}<span style="color:#475569;font-size:.75rem">No</span>{% else %}-{% endif %}</td>
          <td style="font-size:.75rem">{% if lead.rating %}<span style="color:#fbbf24">{{ lead.rating }}</span> <span style="color:#64748b">({{ lead.review_count or 0 }})</span>{% endif %}</td>
        </tr>
        {% endfor %}
        {% if not leads_data.leads %}
        <tr><td colspan="8" style="text-align:center;padding:40px;color:#475569">No leads match your filters</td></tr>
        {% endif %}
      </tbody>
    </table>
  </div>
  {% if leads_data.pages > 1 %}
  <div style="padding:12px 16px;border-top:1px solid #334155;display:flex;justify-content:space-between;align-items:center">
    <span style="font-size:.7rem;color:#64748b">Page {{ leads_data.page }} of {{ leads_data.pages }} ({{ leads_data.total }} total)</span>
    <div style="display:flex;gap:4px">
      {% if leads_data.page > 1 %}<a href="?page={{ leads_data.page - 1 }}&search={{ filters.search or '' }}&stage={{ filters.stage or '' }}&niche={{ filters.niche or '' }}&state={{ filters.state or '' }}&email_status={{ filters.email_status or '' }}&has_ads={{ filters.has_ads or '' }}" class="btn btn-sm btn-ghost">Prev</a>{% endif %}
      {% if leads_data.page < leads_data.pages %}<a href="?page={{ leads_data.page + 1 }}&search={{ filters.search or '' }}&stage={{ filters.stage or '' }}&niche={{ filters.niche or '' }}&state={{ filters.state or '' }}&email_status={{ filters.email_status or '' }}&has_ads={{ filters.has_ads or '' }}" class="btn btn-sm btn-ghost">Next</a>{% endif %}
    </div>
  </div>
  {% endif %}
</div>
"""

# ── Lead detail page ──
LEAD_DETAIL_BODY = """
<div style="margin-bottom:16px">
  <a href="javascript:history.back()" style="color:#64748b;font-size:.8rem;text-decoration:none"><i class="bi bi-arrow-left"></i> Back</a>
</div>

<div style="display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:20px;flex-wrap:wrap;gap:12px">
  <div>
    <h2 style="font-size:1.4rem;font-weight:700;margin:0">{{ lead.name }}</h2>
    <p style="font-size:.8rem;color:#64748b;margin:4px 0 0">{{ lead.city or '' }}{% if lead.state %}, {{ lead.state }}{% endif %} &middot; {{ lead.niche or '' }}</p>
  </div>
  <div style="display:flex;gap:8px;align-items:center">
    <span class="tag {{ (lead.lead_stage or 'new') | stage_cls }}" style="font-size:.8rem;padding:4px 12px">{{ (lead.lead_stage or 'new') | capitalize }}</span>
    <select onchange="updateStage({{ lead.id }}, this.value)" class="input" style="width:auto;padding:4px 28px 4px 10px;font-size:.75rem">
      {% for s in stages %}<option value="{{ s }}" {{ 'selected' if (lead.lead_stage or 'new') == s }}>Move to: {{ s | capitalize }}</option>{% endfor %}
    </select>
  </div>
</div>

<div style="display:grid;grid-template-columns:1fr 1fr;gap:16px">
  <!-- LEFT COLUMN -->
  <div style="display:flex;flex-direction:column;gap:16px">

    <!-- Contact Info -->
    <div class="card" style="padding:20px">
      <h3 style="font-size:.75rem;text-transform:uppercase;letter-spacing:.5px;color:#64748b;margin:0 0 14px"><i class="bi bi-person-vcard" style="color:#60a5fa"></i> Contact Info</h3>
      <div style="display:grid;grid-template-columns:auto 1fr;gap:8px 16px;font-size:.82rem">
        {% if lead.decision_maker_name %}
        <span style="color:#64748b">Decision Maker</span><span style="color:#34d399;font-weight:500">{{ lead.decision_maker_name }}{% if lead.decision_maker_title %} <span style="color:#64748b;font-weight:400">- {{ lead.decision_maker_title }}</span>{% endif %}</span>
        {% endif %}
        <span style="color:#64748b">Email</span>
        <span>{% if lead.email %}<span style="color:#22d3ee">{{ lead.email }}</span>
          <span class="tag" {% if lead.email_verified=='good' %}style="background:rgba(52,211,153,.15);color:#34d399"{% elif lead.email_verified=='risky' %}style="background:rgba(251,146,60,.15);color:#fb923c"{% elif lead.email_verified=='bad' %}style="background:rgba(248,113,113,.15);color:#f87171"{% else %}style="background:rgba(100,116,139,.15);color:#64748b"{% endif %}>{{ lead.email_verified or 'unverified' }}</span>
          {% if lead.email_method %}<span style="font-size:.65rem;color:#475569">({{ lead.email_method }})</span>{% endif %}
        {% else %}<span style="color:#475569">Not found</span>{% endif %}</span>
        {% if lead.phone %}<span style="color:#64748b">Phone</span><span>{{ lead.phone }}</span>{% endif %}
        {% if lead.website %}<span style="color:#64748b">Website</span><span><a href="{{ lead.website if lead.website.startswith('http') else 'https://' + lead.website }}" target="_blank" style="color:#60a5fa;text-decoration:none">{{ lead.website }}</a></span>{% endif %}
        {% if lead.address %}<span style="color:#64748b">Address</span><span>{{ lead.address }}</span>{% endif %}
        {% if lead.rating %}<span style="color:#64748b">Rating</span><span style="color:#fbbf24">{{ lead.rating }} <span style="color:#64748b">({{ lead.review_count or 0 }} reviews)</span></span>{% endif %}
      </div>
    </div>

    <!-- Facebook Ads -->
    <div class="card" style="padding:20px">
      <h3 style="font-size:.75rem;text-transform:uppercase;letter-spacing:.5px;color:#64748b;margin:0 0 14px"><i class="bi bi-megaphone" style="color:#a78bfa"></i> Facebook Ads</h3>
      {% if lead.has_facebook_ads == 1 %}
      <div style="font-size:.82rem">
        <div style="margin-bottom:8px"><span style="color:#34d399;font-weight:600">{{ lead.facebook_ad_count or 0 }} active ads</span></div>
        {% if lead.ad_analysis %}<div style="margin-bottom:8px;color:#94a3b8">{{ lead.ad_analysis }}</div>{% endif %}
      </div>
      {% elif lead.has_facebook_ads == 0 %}
      <p style="color:#64748b;font-size:.82rem">No active Facebook ads found.</p>
      {% else %}
      <p style="color:#475569;font-size:.82rem">Not checked yet.</p>
      {% endif %}
    </div>

    <!-- Outreach -->
    {% if lead.outreach_angle %}
    <div class="card" style="padding:20px">
      <h3 style="font-size:.75rem;text-transform:uppercase;letter-spacing:.5px;color:#64748b;margin:0 0 14px"><i class="bi bi-envelope-paper" style="color:#22d3ee"></i> Outreach Email</h3>
      {% if lead.angle_type %}<span class="tag" style="background:rgba(167,139,250,.12);color:#a78bfa;margin-bottom:10px;display:inline-block">{{ lead.angle_type }}</span>{% endif %}
      {% if lead.subject_line %}<div style="font-size:.8rem;font-weight:600;margin-bottom:10px;color:#e2e8f0">Subject: {{ lead.subject_line }}</div>{% endif %}
      <div style="font-size:.8rem;color:#94a3b8;line-height:1.6;background:#0f172a;padding:14px;border-radius:8px">{{ lead.outreach_angle | nl2br }}</div>
    </div>
    {% endif %}

    <!-- Campaign Status -->
    {% if lead.email_sent_at or lead.instantly_campaign_id %}
    <div class="card" style="padding:20px">
      <h3 style="font-size:.75rem;text-transform:uppercase;letter-spacing:.5px;color:#64748b;margin:0 0 14px"><i class="bi bi-send" style="color:#818cf8"></i> Campaign Status</h3>
      <div style="display:grid;grid-template-columns:auto 1fr;gap:6px 16px;font-size:.8rem">
        {% if lead.email_sent_at %}<span style="color:#64748b">Sent</span><span style="color:#818cf8">{{ lead.email_sent_at | timeago }}</span>{% endif %}
        {% if lead.email_opened_at %}<span style="color:#64748b">Opened</span><span style="color:#22d3ee">{{ lead.email_opened_at | timeago }}</span>{% endif %}
        {% if lead.email_replied_at %}<span style="color:#64748b">Replied</span><span style="color:#34d399">{{ lead.email_replied_at | timeago }}</span>{% endif %}
        {% if lead.email_bounced %}<span style="color:#64748b">Status</span><span style="color:#f87171">Bounced</span>{% endif %}
      </div>
      {% if lead.reply_text %}
      <div style="margin-top:12px;padding:12px;background:#0f172a;border-radius:8px;border-left:3px solid #34d399">
        <div style="font-size:.65rem;text-transform:uppercase;color:#64748b;margin-bottom:6px">Reply</div>
        <div style="font-size:.8rem;color:#e2e8f0">{{ lead.reply_text | nl2br }}</div>
      </div>
      {% endif %}
    </div>
    {% endif %}
  </div>

  <!-- RIGHT COLUMN -->
  <div style="display:flex;flex-direction:column;gap:16px">

    <!-- Tags -->
    <div class="card" style="padding:20px">
      <h3 style="font-size:.75rem;text-transform:uppercase;letter-spacing:.5px;color:#64748b;margin:0 0 14px"><i class="bi bi-tags" style="color:#fbbf24"></i> Tags</h3>
      <div style="display:flex;flex-wrap:wrap;gap:6px;margin-bottom:12px" id="tagList">
        {% for tag in lead.tags %}
        <span class="tag" style="background:rgba(251,191,36,.12);color:#fbbf24;cursor:pointer" onclick="removeTag({{ lead.id }},'{{ tag }}')" title="Click to remove">{{ tag }} &times;</span>
        {% endfor %}
        {% if not lead.tags %}<span style="font-size:.75rem;color:#475569">No tags</span>{% endif %}
      </div>
      <form onsubmit="addTag(event,{{ lead.id }})" style="display:flex;gap:6px">
        <input type="text" id="tagInput" class="input" style="flex:1" placeholder="Add tag...">
        <button type="submit" class="btn btn-sm btn-primary">Add</button>
      </form>
      <div style="margin-top:8px;display:flex;gap:4px;flex-wrap:wrap">
        {% for qt in ['hot','callback','wrong contact','DNC','interested','needs follow-up'] %}
        <button onclick="quickTag({{ lead.id }},'{{ qt }}')" class="btn btn-sm btn-ghost" style="font-size:.65rem;padding:2px 8px">{{ qt }}</button>
        {% endfor %}
      </div>
    </div>

    <!-- Add Note -->
    <div class="card" style="padding:20px">
      <h3 style="font-size:.75rem;text-transform:uppercase;letter-spacing:.5px;color:#64748b;margin:0 0 14px"><i class="bi bi-chat-left-text" style="color:#34d399"></i> Notes</h3>
      <form onsubmit="addNote(event,{{ lead.id }})">
        <textarea id="noteInput" class="input" style="width:100%;min-height:80px;resize:vertical;margin-bottom:8px" placeholder="Add a note..."></textarea>
        <button type="submit" class="btn btn-sm btn-primary">Add Note</button>
      </form>
    </div>

    <!-- Notes list -->
    {% if lead.notes %}
    <div class="card" style="padding:20px">
      <h3 style="font-size:.75rem;text-transform:uppercase;letter-spacing:.5px;color:#64748b;margin:0 0 14px"><i class="bi bi-journal-text" style="color:#94a3b8"></i> History ({{ lead.notes | length }})</h3>
      {% for note in lead.notes %}
      <div style="padding:10px 0;{% if not loop.last %}border-bottom:1px solid #1e293b{% endif %}">
        <div style="display:flex;justify-content:space-between;margin-bottom:4px">
          <span style="font-size:.7rem;color:#60a5fa;font-weight:500">{{ note.user_name or 'System' }}</span>
          <span style="font-size:.65rem;color:#475569">{{ note.created_at | timeago }}</span>
        </div>
        <div style="font-size:.8rem;color:#cbd5e1">{{ note.note_text | nl2br }}</div>
      </div>
      {% endfor %}
    </div>
    {% endif %}

    <!-- Activity Log -->
    {% if lead.activity %}
    <div class="card" style="padding:20px">
      <h3 style="font-size:.75rem;text-transform:uppercase;letter-spacing:.5px;color:#64748b;margin:0 0 14px"><i class="bi bi-clock-history" style="color:#64748b"></i> Activity</h3>
      {% for act in lead.activity %}
      <div style="display:flex;gap:10px;padding:6px 0;font-size:.75rem;{% if not loop.last %}border-bottom:1px solid rgba(30,41,59,.5){% endif %}">
        <span style="color:#475569;white-space:nowrap">{{ act.created_at | timeago }}</span>
        <span style="color:#94a3b8">{{ act.action }}</span>
        {% if act.details %}<span style="color:#64748b">{{ act.details }}</span>{% endif %}
      </div>
      {% endfor %}
    </div>
    {% endif %}
  </div>
</div>

<script>
function updateStage(id, stage){
  fetch('/api/lead/'+id+'/stage',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({stage:stage})})
    .then(r=>r.json()).then(()=>location.reload());
}
function addTag(e,id){
  e.preventDefault();const v=document.getElementById('tagInput').value.trim();if(!v)return;
  fetch('/api/lead/'+id+'/tag',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({tag:v})})
    .then(r=>r.json()).then(()=>location.reload());
}
function quickTag(id,tag){
  fetch('/api/lead/'+id+'/tag',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({tag:tag})})
    .then(r=>r.json()).then(()=>location.reload());
}
function removeTag(id,tag){
  if(!confirm('Remove tag "'+tag+'"?'))return;
  fetch('/api/lead/'+id+'/tag',{method:'DELETE',headers:{'Content-Type':'application/json'},body:JSON.stringify({tag:tag})})
    .then(r=>r.json()).then(()=>location.reload());
}
function addNote(e,id){
  e.preventDefault();const v=document.getElementById('noteInput').value.trim();if(!v)return;
  fetch('/api/lead/'+id+'/note',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({text:v})})
    .then(r=>r.json()).then(()=>location.reload());
}
</script>
<style>@media(max-width:900px){div[style*="grid-template-columns: 1fr 1fr"]{grid-template-columns:1fr!important}}</style>
"""

# ── Analytics page ──
ANALYTICS_BODY = """
<div style="margin-bottom:20px"><h2 style="font-size:1.4rem;font-weight:700;margin:0">Analytics</h2><p style="font-size:.75rem;color:#64748b;margin:4px 0 0">Pipeline performance and insights</p></div>

<!-- Funnel -->
<div class="card" style="padding:20px;margin-bottom:16px">
  <h3 style="font-size:.75rem;text-transform:uppercase;letter-spacing:.5px;color:#64748b;margin:0 0 16px"><i class="bi bi-funnel" style="color:#60a5fa"></i> Lead Funnel</h3>
  <div style="display:flex;flex-direction:column;gap:8px">
    {% set max_val = funnel.scraped if funnel.scraped else 1 %}
    {% for label, key, color in [('Scraped','scraped','#3b82f6'),('Has Email','has_email','#22d3ee'),('Verified','verified','#34d399'),('Decision Maker','has_dm','#a78bfa'),('Has Angle','has_angle','#fbbf24'),('Sent','sent','#818cf8'),('Opened','opened','#06b6d4'),('Replied','replied','#10b981'),('Booked','booked','#8b5cf6')] %}
    <div style="display:flex;align-items:center;gap:12px">
      <span style="width:100px;font-size:.75rem;color:#94a3b8;text-align:right">{{ label }}</span>
      <div style="flex:1;height:28px;background:#0f172a;border-radius:6px;overflow:hidden;position:relative">
        <div style="height:100%;width:{{ (funnel[key] / max_val * 100) | round(1) }}%;background:{{ color }};border-radius:6px;transition:width .5s"></div>
      </div>
      <span style="width:60px;font-size:.8rem;font-weight:600;color:#e2e8f0">{{ funnel[key] | commas }}</span>
      <span style="width:45px;font-size:.7rem;color:#64748b">{{ (funnel[key] / max_val * 100) | round(1) if max_val else 0 }}%</span>
    </div>
    {% endfor %}
  </div>
</div>

<div style="display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-bottom:16px">
  <!-- Email Verification -->
  <div class="card" style="padding:20px">
    <h3 style="font-size:.75rem;text-transform:uppercase;letter-spacing:.5px;color:#64748b;margin:0 0 12px"><i class="bi bi-shield-check" style="color:#34d399"></i> Email Verification</h3>
    <div style="height:200px"><canvas id="verifyChart"></canvas></div>
  </div>

  <!-- Angle Performance -->
  <div class="card" style="padding:20px">
    <h3 style="font-size:.75rem;text-transform:uppercase;letter-spacing:.5px;color:#64748b;margin:0 0 12px"><i class="bi bi-bullseye" style="color:#a78bfa"></i> Angles Distribution</h3>
    <div style="height:200px"><canvas id="angleChart"></canvas></div>
  </div>
</div>

<!-- By Niche -->
<div class="card" style="padding:20px;margin-bottom:16px">
  <h3 style="font-size:.75rem;text-transform:uppercase;letter-spacing:.5px;color:#64748b;margin:0 0 14px"><i class="bi bi-grid-3x3-gap" style="color:#60a5fa"></i> Performance by Niche</h3>
  <div style="overflow-x:auto">
    <table>
      <thead><tr><th>Niche</th><th style="text-align:right">Leads</th><th style="text-align:right">Sent</th><th style="text-align:right">Opened</th><th style="text-align:right">Replied</th><th style="text-align:right">Reply Rate</th></tr></thead>
      <tbody>
      {% for r in analytics.by_niche %}
      <tr>
        <td><span class="tag" style="background:rgba(59,130,246,.12);color:#60a5fa">{{ r.niche }}</span></td>
        <td style="text-align:right">{{ r.total | commas }}</td>
        <td style="text-align:right">{{ r.sent }}</td>
        <td style="text-align:right">{{ r.opened }}</td>
        <td style="text-align:right;color:#34d399">{{ r.replied }}</td>
        <td style="text-align:right;font-weight:600;color:{% if r.sent %}{{ '#34d399' if r.replied/r.sent > 0.05 else '#fbbf24' if r.replied/r.sent > 0.02 else '#f87171' }}{% else %}#475569{% endif %}">{{ '%0.1f' | format(r.replied/r.sent*100) if r.sent else '0' }}%</td>
      </tr>
      {% endfor %}
      </tbody>
    </table>
  </div>
</div>

<!-- By State -->
<div class="card" style="padding:20px;margin-bottom:16px">
  <h3 style="font-size:.75rem;text-transform:uppercase;letter-spacing:.5px;color:#64748b;margin:0 0 14px"><i class="bi bi-geo-alt" style="color:#fb923c"></i> Leads by State</h3>
  <div style="overflow-x:auto">
    <table>
      <thead><tr><th>State</th><th style="text-align:right">Total</th><th style="text-align:right">With Email</th><th style="text-align:right">Verified</th><th>Email Rate</th></tr></thead>
      <tbody>
      {% for r in analytics.by_state %}
      <tr>
        <td style="font-weight:500">{{ r.state }}</td>
        <td style="text-align:right">{{ r.total | commas }}</td>
        <td style="text-align:right">{{ r.with_email }}</td>
        <td style="text-align:right;color:#34d399">{{ r.verified }}</td>
        <td>
          <div style="display:flex;align-items:center;gap:8px">
            <div style="width:80px;height:6px;background:#0f172a;border-radius:3px;overflow:hidden">
              <div style="height:100%;width:{{ (r.with_email / r.total * 100) | round if r.total else 0 }}%;background:#3b82f6;border-radius:3px"></div>
            </div>
            <span style="font-size:.7rem;color:#64748b">{{ (r.with_email / r.total * 100) | round if r.total else 0 }}%</span>
          </div>
        </td>
      </tr>
      {% endfor %}
      </tbody>
    </table>
  </div>
</div>

<script>
Chart.defaults.color='#64748b';Chart.defaults.borderColor='rgba(51,65,85,.5)';
const cf={family:"'Inter',sans-serif",size:11};

// Verification donut
new Chart(document.getElementById('verifyChart'),{type:'doughnut',data:{
  labels:['Good','Risky','Bad','Unverified'],
  datasets:[{data:[{{ analytics.verification.good }},{{ analytics.verification.risky }},{{ analytics.verification.bad }},{{ analytics.verification.unverified }}],
    backgroundColor:['#34d399','#fb923c','#f87171','#334155'],borderWidth:0}]},
  options:{responsive:true,maintainAspectRatio:false,cutout:'65%',plugins:{legend:{position:'bottom',labels:{font:cf,padding:10,usePointStyle:true}}}}});

// Angles donut
const angles={{ analytics.by_angle | tojson }};
const aCols=['#3b82f6','#a78bfa','#22d3ee','#34d399','#fb923c','#f87171','#fbbf24','#818cf8','#64748b'];
new Chart(document.getElementById('angleChart'),{type:'doughnut',data:{
  labels:angles.map(a=>a.angle_type),datasets:[{data:angles.map(a=>a.total),backgroundColor:aCols.slice(0,angles.length),borderWidth:0}]},
  options:{responsive:true,maintainAspectRatio:false,cutout:'65%',plugins:{legend:{position:'bottom',labels:{font:cf,padding:10,usePointStyle:true}}}}});
</script>
"""

# ── Settings page (admin) ──
SETTINGS_BODY = """
<div style="margin-bottom:20px"><h2 style="font-size:1.4rem;font-weight:700;margin:0">Settings</h2><p style="font-size:.75rem;color:#64748b;margin:4px 0 0">Admin controls and pipeline actions</p></div>

<div style="display:grid;grid-template-columns:1fr 1fr;gap:16px">
  <!-- Pipeline Actions -->
  <div class="card" style="padding:20px">
    <h3 style="font-size:.75rem;text-transform:uppercase;letter-spacing:.5px;color:#64748b;margin:0 0 16px"><i class="bi bi-play-circle" style="color:#34d399"></i> Pipeline Actions</h3>
    <div style="display:flex;flex-direction:column;gap:8px">
      <button onclick="runAction('enrich')" class="btn btn-ghost" style="text-align:left"><i class="bi bi-search"></i> Run Enrichment</button>
      <button onclick="runAction('ads')" class="btn btn-ghost" style="text-align:left"><i class="bi bi-megaphone"></i> Check Facebook Ads</button>
      <button onclick="runAction('verify')" class="btn btn-ghost" style="text-align:left"><i class="bi bi-shield-check"></i> Verify Emails</button>
      <button onclick="runAction('export')" class="btn btn-ghost" style="text-align:left"><i class="bi bi-download"></i> Export CSVs</button>
      <button onclick="syncInstantly()" class="btn btn-ghost" style="text-align:left"><i class="bi bi-arrow-repeat"></i> Sync Instantly</button>
    </div>
    <div id="actionStatus" style="margin-top:12px;font-size:.8rem;color:#64748b"></div>
  </div>

  <!-- User Management -->
  <div class="card" style="padding:20px">
    <h3 style="font-size:.75rem;text-transform:uppercase;letter-spacing:.5px;color:#64748b;margin:0 0 16px"><i class="bi bi-people" style="color:#60a5fa"></i> Users</h3>
    <table style="font-size:.8rem">
      <thead><tr><th>Name</th><th>Email</th><th>Role</th></tr></thead>
      <tbody>
      {% for u in users %}
      <tr><td>{{ u.name }}</td><td style="color:#22d3ee">{{ u.email }}</td><td><span class="tag" style="background:rgba(59,130,246,.12);color:#60a5fa">{{ u.role }}</span></td></tr>
      {% endfor %}
      </tbody>
    </table>

    <h4 style="font-size:.7rem;text-transform:uppercase;color:#64748b;margin:20px 0 10px">Add User</h4>
    <form method="POST" action="/settings/add-user" style="display:flex;flex-direction:column;gap:8px">
      <input type="text" name="name" class="input" placeholder="Name" required>
      <input type="email" name="email" class="input" placeholder="Email" required>
      <input type="password" name="password" class="input" placeholder="Password" required>
      <select name="role" class="input"><option value="viewer">Viewer</option><option value="admin">Admin</option></select>
      <button type="submit" class="btn btn-primary btn-sm">Create User</button>
    </form>
  </div>
</div>

<!-- Scrape Files -->
<div class="card" style="padding:20px;margin-top:16px">
  <h3 style="font-size:.75rem;text-transform:uppercase;letter-spacing:.5px;color:#64748b;margin:0 0 14px"><i class="bi bi-file-earmark-spreadsheet" style="color:#34d399"></i> CSV Files</h3>
  {% if csv_files %}
  <table style="font-size:.8rem">
    <thead><tr><th>File</th><th style="text-align:right">Rows</th><th style="text-align:right">Size</th><th>Modified</th><th></th></tr></thead>
    <tbody>
    {% for f in csv_files %}
    <tr>
      <td><i class="bi bi-filetype-csv" style="color:#34d399"></i> {{ f.name }}</td>
      <td style="text-align:right">{{ f.rows | commas }}</td>
      <td style="text-align:right;color:#64748b">{{ f.size }}</td>
      <td style="font-size:.7rem;color:#64748b">{{ f.modified }}</td>
      <td><a href="/download/{{ f.name }}" class="btn btn-sm btn-primary" style="font-size:.65rem"><i class="bi bi-download"></i></a></td>
    </tr>
    {% endfor %}
    </tbody>
  </table>
  {% else %}
  <p style="color:#475569;font-size:.8rem">No CSV files yet.</p>
  {% endif %}
</div>

<script>
function runAction(action){
  const s=document.getElementById('actionStatus');
  s.innerHTML='<i class="bi bi-arrow-repeat spin"></i> Running '+action+'...';
  fetch('/api/pipeline/'+action,{method:'POST'}).then(r=>r.json()).then(d=>{
    s.innerHTML='<span style="color:#34d399"><i class="bi bi-check-circle"></i> '+JSON.stringify(d)+'</span>';
  }).catch(e=>{s.innerHTML='<span style="color:#f87171">Error: '+e+'</span>'});
}
function syncInstantly(){
  const s=document.getElementById('actionStatus');
  s.innerHTML='<i class="bi bi-arrow-repeat spin"></i> Syncing Instantly...';
  fetch('/api/sync-instantly',{method:'POST'}).then(r=>r.json()).then(d=>{
    if(d.error) s.innerHTML='<span style="color:#f87171">'+d.error+'</span>';
    else s.innerHTML='<span style="color:#34d399">Synced '+d.synced+' leads from '+d.campaigns+' campaigns</span>';
  }).catch(e=>{s.innerHTML='<span style="color:#f87171">'+e+'</span>'});
}
</script>
<style>.spin{animation:spin 1s linear infinite}@keyframes spin{to{transform:rotate(360deg)}}</style>
"""

# ── Cold Email channel page ──
COLD_EMAIL_BODY = """
<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:20px">
  <div>
    <h2 style="font-size:1.4rem;font-weight:700;margin:0"><i class="bi bi-envelope" style="color:#60a5fa"></i> Cold Email</h2>
    <p style="font-size:.75rem;color:#64748b;margin:4px 0 0">Your AI agent's daily work report</p>
  </div>
  <div style="display:flex;gap:8px">
    {% if user and user.role == 'admin' %}
    <button onclick="runPipeline('full')" class="btn btn-sm btn-primary" id="runBtn"><i class="bi bi-play-fill"></i> Run Pipeline Now</button>
    <button onclick="syncInstantly()" class="btn btn-sm" style="background:#7c3aed;color:#fff"><i class="bi bi-arrow-repeat"></i> Sync Instantly</button>
    {% endif %}
  </div>
</div>

<!-- Today's Report Card -->
<div class="card" style="padding:20px;margin-bottom:16px;border-left:3px solid #60a5fa">
  <div style="display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:16px">
    <div>
      <h3 style="font-size:.85rem;font-weight:700;margin:0;color:#e2e8f0"><i class="bi bi-calendar-check" style="color:#60a5fa"></i> Today's Report</h3>
      <p style="font-size:.65rem;color:#475569;margin:4px 0 0">{{ now.strftime('%A, %B %d %Y') }}</p>
    </div>
    {% if data.last_run %}
    <span class="tag {% if data.last_run.status == 'completed' %}style="background:rgba(52,211,153,.15);color:#34d399"{% elif data.last_run.status == 'running' %}style="background:rgba(96,165,250,.15);color:#60a5fa"{% else %}style="background:rgba(248,113,113,.15);color:#f87171"{% endif %}">
      {% if data.last_run.status == 'completed' %}<i class="bi bi-check-circle"></i>{% elif data.last_run.status == 'running' %}<i class="bi bi-arrow-repeat spin"></i>{% else %}<i class="bi bi-x-circle"></i>{% endif %}
      Last run: {{ data.last_run.status }} {{ data.last_run.finished_at | timeago if data.last_run.finished_at else '' }}
    </span>
    {% endif %}
  </div>
  <div style="display:grid;grid-template-columns:repeat(3,1fr);gap:12px">
    <div style="background:#0f172a;border-radius:8px;padding:14px;text-align:center">
      <div style="font-size:1.8rem;font-weight:800;color:#60a5fa">{{ data.today.scraped }}</div>
      <div style="font-size:.7rem;color:#64748b;margin-top:2px">Leads Scraped</div>
    </div>
    <div style="background:#0f172a;border-radius:8px;padding:14px;text-align:center">
      <div style="font-size:1.8rem;font-weight:800;color:#22d3ee">{{ data.today.emails_found }}</div>
      <div style="font-size:.7rem;color:#64748b;margin-top:2px">Emails Found</div>
    </div>
    <div style="background:#0f172a;border-radius:8px;padding:14px;text-align:center">
      <div style="font-size:1.8rem;font-weight:800;color:#a78bfa">{{ data.today.enriched }}</div>
      <div style="font-size:.7rem;color:#64748b;margin-top:2px">Decision Makers</div>
    </div>
  </div>
</div>

<!-- Outreach Performance + Queue Status -->
<div style="display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-bottom:16px">

  <!-- Outreach Performance -->
  <div class="card" style="padding:20px">
    <h3 style="font-size:.75rem;text-transform:uppercase;letter-spacing:.5px;color:#64748b;margin:0 0 16px"><i class="bi bi-send-check" style="color:#34d399"></i> Outreach Performance</h3>
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px">
      <div style="background:#0f172a;border-radius:8px;padding:12px">
        <div style="font-size:.65rem;color:#64748b;text-transform:uppercase">Sent</div>
        <div style="font-size:1.3rem;font-weight:700;color:#818cf8">{{ data.outreach.total_sent | commas }}</div>
      </div>
      <div style="background:#0f172a;border-radius:8px;padding:12px">
        <div style="font-size:.65rem;color:#64748b;text-transform:uppercase">Opened</div>
        <div style="font-size:1.3rem;font-weight:700;color:#22d3ee">{{ data.outreach.total_opened | commas }}</div>
        <div style="font-size:.65rem;color:#22d3ee">{{ data.outreach.open_rate }}% rate</div>
      </div>
      <div style="background:#0f172a;border-radius:8px;padding:12px">
        <div style="font-size:.65rem;color:#64748b;text-transform:uppercase">Replied</div>
        <div style="font-size:1.3rem;font-weight:700;color:#34d399">{{ data.outreach.total_replied | commas }}</div>
        <div style="font-size:.65rem;color:#34d399">{{ data.outreach.reply_rate }}% rate</div>
      </div>
      <div style="background:#0f172a;border-radius:8px;padding:12px">
        <div style="font-size:.65rem;color:#64748b;text-transform:uppercase">Bounced</div>
        <div style="font-size:1.3rem;font-weight:700;color:#f87171">{{ data.outreach.total_bounced | commas }}</div>
        <div style="font-size:.65rem;color:#f87171">{{ data.outreach.bounce_rate }}% rate</div>
      </div>
    </div>
  </div>

  <!-- Agent Queue -->
  <div class="card" style="padding:20px">
    <h3 style="font-size:.75rem;text-transform:uppercase;letter-spacing:.5px;color:#64748b;margin:0 0 16px"><i class="bi bi-robot" style="color:#a78bfa"></i> Agent Work Queue</h3>
    <div style="display:flex;flex-direction:column;gap:10px">
      <div style="display:flex;justify-content:space-between;align-items:center;padding:8px 12px;background:#0f172a;border-radius:8px">
        <span style="font-size:.8rem;color:#94a3b8"><i class="bi bi-search" style="color:#60a5fa"></i> Need enrichment</span>
        <span style="font-size:.9rem;font-weight:700;color:#60a5fa">{{ data.outreach.pending_enrich | commas }}</span>
      </div>
      <div style="display:flex;justify-content:space-between;align-items:center;padding:8px 12px;background:#0f172a;border-radius:8px">
        <span style="font-size:.8rem;color:#94a3b8"><i class="bi bi-shield-check" style="color:#22d3ee"></i> Need verification</span>
        <span style="font-size:.9rem;font-weight:700;color:#22d3ee">{{ data.outreach.pending_verify | commas }}</span>
      </div>
      <div style="display:flex;justify-content:space-between;align-items:center;padding:8px 12px;background:#0f172a;border-radius:8px">
        <span style="font-size:.8rem;color:#94a3b8"><i class="bi bi-megaphone" style="color:#fb923c"></i> Need ad check</span>
        <span style="font-size:.9rem;font-weight:700;color:#fb923c">{{ data.outreach.pending_ads | commas }}</span>
      </div>
      <div style="display:flex;justify-content:space-between;align-items:center;padding:8px 12px;background:#0f172a;border-radius:8px">
        <span style="font-size:.8rem;color:#94a3b8"><i class="bi bi-pencil-square" style="color:#a78bfa"></i> Need outreach angle</span>
        <span style="font-size:.9rem;font-weight:700;color:#a78bfa">{{ data.outreach.pending_angles | commas }}</span>
      </div>
      <div style="display:flex;justify-content:space-between;align-items:center;padding:10px 12px;background:rgba(52,211,153,.08);border:1px solid rgba(52,211,153,.2);border-radius:8px">
        <span style="font-size:.8rem;color:#34d399;font-weight:600"><i class="bi bi-check2-all"></i> Ready to send</span>
        <span style="font-size:1rem;font-weight:800;color:#34d399">{{ data.outreach.ready_to_send | commas }}</span>
      </div>
    </div>
  </div>
</div>

<!-- Daily Activity Chart -->
<div class="card" style="padding:20px;margin-bottom:16px">
  <h3 style="font-size:.75rem;text-transform:uppercase;letter-spacing:.5px;color:#64748b;margin:0 0 14px"><i class="bi bi-bar-chart-line" style="color:#60a5fa"></i> Daily Agent Activity (Last 14 Days)</h3>
  <div style="height:220px"><canvas id="dailyChart"></canvas></div>
</div>

<!-- Recent Replies -->
<div class="card" style="padding:20px;margin-bottom:16px">
  <h3 style="font-size:.75rem;text-transform:uppercase;letter-spacing:.5px;color:#64748b;margin:0 0 14px"><i class="bi bi-reply" style="color:#34d399"></i> Recent Replies</h3>
  {% if data.replies %}
  <div style="display:flex;flex-direction:column;gap:8px">
    {% for r in data.replies %}
    <div style="background:#0f172a;border-radius:8px;padding:14px;border-left:3px solid {% if r.lead_stage in ('interested','booked','client') %}#34d399{% elif r.lead_stage == 'lost' %}#f87171{% else %}#60a5fa{% endif %};cursor:pointer" onclick="location='/lead/{{ r.id }}'">
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px">
        <div>
          <span style="font-weight:600;font-size:.82rem">{{ r.name }}</span>
          {% if r.decision_maker_name %}<span style="color:#34d399;font-size:.75rem;margin-left:8px">{{ r.decision_maker_name }}</span>{% endif %}
        </div>
        <div style="display:flex;gap:6px;align-items:center">
          {% if r.niche %}<span class="tag" style="background:rgba(59,130,246,.12);color:#60a5fa">{{ r.niche }}</span>{% endif %}
          <span class="tag {{ (r.lead_stage or 'replied') | stage_cls }}">{{ (r.lead_stage or 'replied') | capitalize }}</span>
          <span style="font-size:.65rem;color:#475569">{{ r.email_replied_at | timeago }}</span>
        </div>
      </div>
      {% if r.reply_text %}
      <div style="font-size:.8rem;color:#94a3b8;line-height:1.5">{{ r.reply_text[:200] }}{% if r.reply_text|length > 200 %}...{% endif %}</div>
      {% else %}
      <div style="font-size:.8rem;color:#475569;font-style:italic">Reply received (no content synced yet)</div>
      {% endif %}
    </div>
    {% endfor %}
  </div>
  {% else %}
  <div style="text-align:center;padding:24px;color:#475569">
    <i class="bi bi-inbox" style="font-size:2rem;display:block;margin-bottom:8px"></i>
    <p style="font-size:.82rem">No replies yet. Sync Instantly to pull reply data.</p>
  </div>
  {% endif %}
</div>

<!-- Pipeline Run History -->
<div class="card" style="padding:20px">
  <h3 style="font-size:.75rem;text-transform:uppercase;letter-spacing:.5px;color:#64748b;margin:0 0 14px"><i class="bi bi-clock-history" style="color:#818cf8"></i> Pipeline Run History</h3>
  {% if data.runs %}
  <div style="overflow-x:auto">
    <table>
      <thead><tr>
        <th>Date</th><th>Type</th><th>Status</th><th>Scraped</th><th>Emails</th><th>Verified</th><th>Angles</th><th>Duration</th><th>Details</th>
      </tr></thead>
      <tbody>
      {% for run in data.runs %}
      <tr>
        <td style="font-size:.75rem;white-space:nowrap">{{ run.started_at | timeago }}</td>
        <td><span class="tag" style="background:rgba(96,165,250,.12);color:#60a5fa">{{ run.run_type }}</span></td>
        <td>
          {% if run.status == 'completed' %}<span style="color:#34d399"><i class="bi bi-check-circle-fill"></i> Done</span>
          {% elif run.status == 'running' %}<span style="color:#60a5fa"><i class="bi bi-arrow-repeat spin"></i> Running</span>
          {% else %}<span style="color:#f87171"><i class="bi bi-x-circle-fill"></i> {{ run.status }}</span>{% endif %}
        </td>
        <td style="text-align:right;font-weight:500">{{ run.leads_scraped or '-' }}</td>
        <td style="text-align:right;font-weight:500;color:#22d3ee">{{ run.emails_found or '-' }}</td>
        <td style="text-align:right;font-weight:500;color:#34d399">{{ run.emails_verified or '-' }}</td>
        <td style="text-align:right;font-weight:500;color:#a78bfa">{{ run.angles_generated or '-' }}</td>
        <td style="font-size:.75rem;color:#64748b">{% if run.duration_sec %}{{ '%.0f' | format(run.duration_sec) }}s{% else %}-{% endif %}</td>
        <td style="font-size:.7rem;color:#475569;max-width:200px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="{{ run.details or '' }}">{{ run.details or run.state_name or '-' }}</td>
      </tr>
      {% endfor %}
      </tbody>
    </table>
  </div>
  {% else %}
  <div style="text-align:center;padding:24px;color:#475569">
    <i class="bi bi-robot" style="font-size:2rem;display:block;margin-bottom:8px"></i>
    <p style="font-size:.82rem">No pipeline runs recorded yet.</p>
    <p style="font-size:.7rem;color:#334155">Run the pipeline to see activity here: <code style="background:#334155;padding:2px 6px;border-radius:4px">python3 main.py</code></p>
  </div>
  {% endif %}
</div>

<script>
Chart.defaults.color='#64748b';Chart.defaults.borderColor='rgba(51,65,85,.5)';
const daily={{ data.daily_activity | tojson }};
daily.reverse();
new Chart(document.getElementById('dailyChart'),{
  type:'bar',
  data:{
    labels:daily.map(d=>d.day.slice(5)),
    datasets:[
      {label:'Scraped',data:daily.map(d=>d.scraped),backgroundColor:'rgba(96,165,250,.6)',borderRadius:4},
      {label:'Got Email',data:daily.map(d=>d.got_email),backgroundColor:'rgba(34,211,238,.6)',borderRadius:4},
      {label:'Decision Maker',data:daily.map(d=>d.got_dm),backgroundColor:'rgba(167,139,250,.6)',borderRadius:4},
      {label:'Verified',data:daily.map(d=>d.verified),backgroundColor:'rgba(52,211,153,.6)',borderRadius:4}
    ]
  },
  options:{responsive:true,maintainAspectRatio:false,
    scales:{x:{grid:{display:false}},y:{beginAtZero:true,grid:{color:'rgba(51,65,85,.3)'}}},
    plugins:{legend:{position:'top',labels:{font:{size:11},usePointStyle:true,padding:12}}}}
});

{% if user and user.role == 'admin' %}
function runPipeline(type){
  const btn=document.getElementById('runBtn');
  btn.innerHTML='<i class="bi bi-arrow-repeat spin"></i> Running...';btn.disabled=true;
  fetch('/api/pipeline/enrich',{method:'POST'}).then(r=>r.json()).then(d=>{
    btn.innerHTML='<i class="bi bi-check"></i> Started';
    setTimeout(()=>{btn.innerHTML='<i class="bi bi-play-fill"></i> Run Pipeline Now';btn.disabled=false},3000);
  }).catch(e=>{btn.innerHTML='<i class="bi bi-play-fill"></i> Run Pipeline Now';btn.disabled=false;alert(e)});
}
function syncInstantly(){
  fetch('/api/sync-instantly',{method:'POST'}).then(r=>r.json()).then(d=>{
    if(d.error)alert('Error: '+d.error);
    else alert('Synced '+d.synced+' leads from '+d.campaigns+' campaigns');
    location.reload();
  });
}
{% endif %}
</script>
<style>.spin{animation:spin 1s linear infinite}@keyframes spin{to{transform:rotate(360deg)}}</style>
"""


# ═══════════════════════════════════════════════════════════════════════
# RENDER HELPER
# ═══════════════════════════════════════════════════════════════════════

def render_page(body_template: str, **ctx):
    """Wrap a body template in the base layout (head + sidebar + footer)."""
    full = BASE_HEAD + BASE_SIDEBAR + '<div class="main-area" style="margin-left:220px;padding:24px;min-height:100vh">' + body_template + '</div>' + BASE_FOOT
    ctx.setdefault("user", current_user())
    ctx.setdefault("stages", STAGES)
    return render_template_string(full, **ctx)


# ═══════════════════════════════════════════════════════════════════════
# PAGE ROUTES
# ═══════════════════════════════════════════════════════════════════════

@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    req_email = ""
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        req_email = email
        rows = query("SELECT * FROM users WHERE LOWER(email) = ?", (email,))
        if rows and check_password_hash(rows[0]["password_hash"], password):
            session["user_id"] = int(rows[0]["id"])
            return redirect(url_for("pipeline"))
        error = "Invalid email or password"
    return render_template_string(LOGIN_PAGE, title="Login", error=error, req_email=req_email)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/")
@login_required
def pipeline():
    stage = request.args.get("stage", "")
    page = int(request.args.get("page", 1))
    leads_data = get_leads_page(page=page, stage=stage or None)
    return render_page(
        PIPELINE_BODY,
        title="Pipeline",
        page="pipeline",
        overview=get_overview(),
        stage_counts=get_stage_counts(),
        active_stage=stage,
        leads_data=leads_data,
    )


@app.route("/leads")
@login_required
def leads_browser():
    filters = {
        "search": request.args.get("search", ""),
        "stage": request.args.get("stage", ""),
        "niche": request.args.get("niche", ""),
        "state": request.args.get("state", ""),
        "email_status": request.args.get("email_status", ""),
        "has_ads": request.args.get("has_ads", ""),
        "angle": request.args.get("angle", ""),
    }
    page = int(request.args.get("page", 1))
    leads_data = get_leads_page(page=page, **{k: v for k, v in filters.items() if v})
    return render_page(
        LEADS_BODY,
        title="Leads",
        page="leads",
        leads_data=leads_data,
        filters=filters,
        options=get_filter_options(),
    )


@app.route("/lead/<int:lead_id>")
@login_required
def lead_detail(lead_id):
    lead = get_lead_detail(lead_id)
    if not lead:
        abort(404)
    return render_page(
        LEAD_DETAIL_BODY,
        title=lead["name"] or f"Lead #{lead_id}",
        page="leads",
        lead=lead,
    )


@app.route("/analytics")
@login_required
def analytics():
    return render_page(
        ANALYTICS_BODY,
        title="Analytics",
        page="analytics",
        funnel=get_funnel(),
        analytics=get_analytics(),
    )


@app.route("/channels/cold-email")
@login_required
def cold_email():
    return render_page(
        COLD_EMAIL_BODY,
        title="Cold Email",
        page="cold-email",
        data=get_cold_email_data(),
        now=datetime.utcnow(),
    )


@app.route("/settings", methods=["GET"])
@login_required
@admin_required
def settings():
    users = query("SELECT * FROM users ORDER BY created_at")

    # CSV files
    data_dir = os.path.join(_HERE, getattr(config, "CSV_OUTPUT_DIR", "data"))
    csv_files = []
    if os.path.isdir(data_dir):
        for fname in os.listdir(data_dir):
            if not fname.endswith(".csv"):
                continue
            fpath = os.path.join(data_dir, fname)
            stat = os.stat(fpath)
            size_kb = stat.st_size / 1024
            try:
                with open(fpath, "r", encoding="utf-8") as f:
                    rows = max(sum(1 for _ in f) - 1, 0)
            except Exception:
                rows = 0
            csv_files.append({
                "name": fname,
                "size": f"{size_kb:.0f} KB" if size_kb < 1024 else f"{size_kb / 1024:.1f} MB",
                "rows": rows,
                "modified": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M"),
            })
        csv_files.sort(key=lambda f: f["modified"], reverse=True)

    return render_page(
        SETTINGS_BODY,
        title="Settings",
        page="settings",
        users=users,
        csv_files=csv_files,
    )


@app.route("/settings/add-user", methods=["POST"])
@login_required
@admin_required
def add_user():
    name = request.form.get("name", "").strip()
    email = request.form.get("email", "").strip().lower()
    password = request.form.get("password", "")
    role = request.form.get("role", "viewer")
    if name and email and password:
        try:
            execute(
                "INSERT INTO users (email, password_hash, name, role, created_at) VALUES (?,?,?,?,?)",
                (email, generate_password_hash(password, method="pbkdf2:sha256"), name, role, datetime.utcnow().isoformat()),
            )
        except sqlite3.IntegrityError:
            pass  # duplicate email
    return redirect(url_for("settings"))


@app.route("/download/<path:filename>")
@login_required
def download_file(filename):
    data_dir = os.path.join(_HERE, getattr(config, "CSV_OUTPUT_DIR", "data"))
    if ".." in filename or "/" in filename or not filename.endswith(".csv"):
        abort(404)
    return send_from_directory(data_dir, filename, as_attachment=True)


# ═══════════════════════════════════════════════════════════════════════
# API ROUTES
# ═══════════════════════════════════════════════════════════════════════

@app.route("/api/lead/<int:lead_id>/stage", methods=["POST"])
@login_required
def api_update_stage(lead_id):
    data = request.get_json(force=True)
    new_stage = data.get("stage", "")
    if new_stage not in STAGES:
        return jsonify({"error": "Invalid stage"}), 400
    old = scalar("SELECT COALESCE(lead_stage, 'new') FROM leads WHERE id = ?", (lead_id,), "new")
    execute("UPDATE leads SET lead_stage = ? WHERE id = ?", (new_stage, lead_id))
    user = current_user()
    execute(
        "INSERT INTO activity_log (lead_id, action, details, user_id, created_at) VALUES (?,?,?,?,?)",
        (lead_id, "Stage changed", f"{old} -> {new_stage}", user["id"] if user else None, datetime.utcnow().isoformat()),
    )
    return jsonify({"ok": True})


@app.route("/api/lead/<int:lead_id>/note", methods=["POST"])
@login_required
def api_add_note(lead_id):
    data = request.get_json(force=True)
    text = (data.get("text") or "").strip()
    if not text:
        return jsonify({"error": "Empty note"}), 400
    user = current_user()
    execute(
        "INSERT INTO notes (lead_id, user_id, note_text, created_at) VALUES (?,?,?,?)",
        (lead_id, user["id"] if user else None, text, datetime.utcnow().isoformat()),
    )
    execute(
        "INSERT INTO activity_log (lead_id, action, details, user_id, created_at) VALUES (?,?,?,?,?)",
        (lead_id, "Note added", text[:100], user["id"] if user else None, datetime.utcnow().isoformat()),
    )
    return jsonify({"ok": True})


@app.route("/api/lead/<int:lead_id>/tag", methods=["POST"])
@login_required
def api_add_tag(lead_id):
    data = request.get_json(force=True)
    tag = (data.get("tag") or "").strip().lower()
    if not tag:
        return jsonify({"error": "Empty tag"}), 400
    try:
        execute("INSERT INTO tags (lead_id, tag_name) VALUES (?,?)", (lead_id, tag))
        user = current_user()
        execute(
            "INSERT INTO activity_log (lead_id, action, details, user_id, created_at) VALUES (?,?,?,?,?)",
            (lead_id, "Tag added", tag, user["id"] if user else None, datetime.utcnow().isoformat()),
        )
    except sqlite3.IntegrityError:
        pass  # already tagged
    return jsonify({"ok": True})


@app.route("/api/lead/<int:lead_id>/tag", methods=["DELETE"])
@login_required
def api_remove_tag(lead_id):
    data = request.get_json(force=True)
    tag = (data.get("tag") or "").strip().lower()
    execute("DELETE FROM tags WHERE lead_id = ? AND tag_name = ?", (lead_id, tag))
    return jsonify({"ok": True})


@app.route("/api/sync-instantly", methods=["POST"])
@login_required
@admin_required
def api_sync_instantly():
    result = sync_instantly()
    return jsonify(result)


@app.route("/api/pipeline/<action>", methods=["POST"])
@login_required
@admin_required
def api_pipeline_action(action):
    """Run pipeline actions in background thread and log them."""
    if _use_turso:
        return jsonify({"error": "Pipeline actions are not available in cloud deployment. Run them from your local machine."}), 400
    allowed = {"enrich": "--enrich-only", "ads": "--ads-only", "verify": "--verify-only",
               "export": "--deliver-only", "full": None, "scrape": "--scrape-only"}
    if action not in allowed:
        return jsonify({"error": f"Unknown action: {action}"}), 400

    flag = allowed[action]
    run_id = log_pipeline_run(action)

    def _run():
        import time as _time
        start = _time.time()
        try:
            cmd = [sys.executable, os.path.join(_HERE, "main.py")]
            if flag:
                cmd.append(flag)
            result = subprocess.run(cmd, cwd=_HERE, capture_output=True, text=True, timeout=600)
            elapsed = _time.time() - start
            details = (result.stdout or "")[-500:]  # last 500 chars of output
            errors = (result.stderr or "")[-300:] if result.returncode != 0 else None
            finish_pipeline_run(run_id, status="completed" if result.returncode == 0 else "failed",
                                duration_sec=elapsed, details=details, errors=errors)
        except Exception as e:
            elapsed = _time.time() - start
            finish_pipeline_run(run_id, status="failed", duration_sec=elapsed, errors=str(e))
            logger.error(f"Pipeline action {action} failed: {e}")

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    return jsonify({"status": "started", "action": action, "run_id": run_id})


@app.route("/api/stats")
@login_required
def api_stats():
    return jsonify(get_overview())


# ═══════════════════════════════════════════════════════════════════════
# ENTRY POINT
# ═══════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    if not os.path.exists(DB_PATH):
        print(f"[warn] Database not found at {DB_PATH}")
        print("[warn] Run 'python3 main.py --stats' first to initialize the database")
    init_crm()
    print(f"[bookedly] CRM starting on http://127.0.0.1:5050")
    print(f"[bookedly] Login: admin@bookedly.com / bookedly")
    app.run(host="0.0.0.0", port=5050, debug=True)
