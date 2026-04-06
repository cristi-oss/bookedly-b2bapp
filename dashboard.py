"""
Bookedly Command Center — Lead Gen Dashboard
Run with: python dashboard.py
"""
from __future__ import annotations

import sqlite3
import os
import sys
from datetime import datetime, timedelta

try:
    from flask import Flask, render_template_string, jsonify, send_from_directory, abort
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

app = Flask(__name__)


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def query(sql: str, params: tuple = ()) -> list[dict]:
    conn = get_conn()
    try:
        rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def scalar(sql: str, params: tuple = (), default=0):
    conn = get_conn()
    try:
        row = conn.execute(sql, params).fetchone()
        return row[0] if row and row[0] is not None else default
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Data helpers
# ---------------------------------------------------------------------------

def get_overview() -> dict:
    total = scalar("SELECT COUNT(*) FROM leads")
    with_email = scalar("SELECT COUNT(*) FROM leads WHERE email IS NOT NULL AND email != ''")
    personal_email = scalar(
        "SELECT COUNT(*) FROM leads WHERE email_type IN ('decision_maker', 'personal_pattern')"
    )
    with_dm = scalar(
        "SELECT COUNT(*) FROM leads WHERE decision_maker_name IS NOT NULL AND decision_maker_name != ''"
    )
    delivered = scalar("SELECT COUNT(*) FROM leads WHERE delivered_at IS NOT NULL")
    fb_checked = scalar("SELECT COUNT(*) FROM leads WHERE has_facebook_ads IS NOT NULL")
    verified_good = scalar("SELECT COUNT(*) FROM leads WHERE email_verified = 'good'")
    verified_risky = scalar("SELECT COUNT(*) FROM leads WHERE email_verified = 'risky'")
    verified_bad = scalar("SELECT COUNT(*) FROM leads WHERE email_verified = 'bad'")
    unverified = scalar(
        "SELECT COUNT(*) FROM leads WHERE email IS NOT NULL AND email != '' AND (email_verified IS NULL OR email_verified = '')"
    )

    # Today's leads
    today = datetime.utcnow().strftime("%Y-%m-%d")
    today_count = scalar("SELECT COUNT(*) FROM leads WHERE scraped_at LIKE ?", (today + "%",))

    # This week
    week_ago = (datetime.utcnow() - timedelta(days=7)).strftime("%Y-%m-%d")
    week_count = scalar("SELECT COUNT(*) FROM leads WHERE scraped_at >= ?", (week_ago,))

    return {
        "total": total,
        "with_email": with_email,
        "personal_email": personal_email,
        "with_dm": with_dm,
        "delivered": delivered,
        "fb_checked": fb_checked,
        "verified_good": verified_good,
        "verified_risky": verified_risky,
        "verified_bad": verified_bad,
        "unverified": unverified,
        "today": today_count,
        "this_week": week_count,
        "email_pct": round(with_email / total * 100) if total else 0,
        "dm_pct": round(with_dm / total * 100) if total else 0,
        "personal_pct": round(personal_email / with_email * 100) if with_email else 0,
    }


def get_by_state() -> list[dict]:
    rows = query("""
        SELECT
            state,
            COUNT(*) AS total,
            SUM(CASE WHEN email IS NOT NULL AND email != '' THEN 1 ELSE 0 END) AS with_email,
            SUM(CASE WHEN decision_maker_name IS NOT NULL AND decision_maker_name != '' THEN 1 ELSE 0 END) AS with_dm,
            SUM(CASE WHEN email_verified = 'good' THEN 1 ELSE 0 END) AS verified
        FROM leads
        WHERE state IS NOT NULL AND state != ''
        GROUP BY state
        ORDER BY total DESC
    """)
    for r in rows:
        r["email_pct"] = round(r["with_email"] / r["total"] * 100) if r["total"] else 0
    return rows


def get_by_niche() -> list[dict]:
    rows = query("""
        SELECT
            niche,
            COUNT(*) AS total,
            SUM(CASE WHEN email IS NOT NULL AND email != '' THEN 1 ELSE 0 END) AS with_email,
            SUM(CASE WHEN decision_maker_name IS NOT NULL AND decision_maker_name != '' THEN 1 ELSE 0 END) AS with_dm
        FROM leads
        WHERE niche IS NOT NULL AND niche != ''
        GROUP BY niche
        ORDER BY total DESC
    """)
    for r in rows:
        r["email_pct"] = round(r["with_email"] / r["total"] * 100) if r["total"] else 0
    return rows


def get_by_angle() -> list[dict]:
    return query("""
        SELECT
            COALESCE(angle_type, 'unanalyzed') AS angle_type,
            COUNT(*) AS total
        FROM leads
        GROUP BY angle_type
        ORDER BY total DESC
    """)


def get_email_methods() -> list[dict]:
    return query("""
        SELECT
            COALESCE(email_method, 'none') AS method,
            COUNT(*) AS total
        FROM leads
        WHERE email IS NOT NULL AND email != ''
        GROUP BY email_method
        ORDER BY total DESC
    """)


def get_daily_trend() -> list[dict]:
    return query("""
        SELECT
            DATE(scraped_at) AS day,
            COUNT(*) AS total,
            SUM(CASE WHEN email IS NOT NULL AND email != '' THEN 1 ELSE 0 END) AS with_email
        FROM leads
        GROUP BY DATE(scraped_at)
        ORDER BY day DESC
        LIMIT 30
    """)


def get_recent_leads(limit: int = 30) -> list[dict]:
    return query("""
        SELECT
            name, city, state, niche,
            decision_maker_name, email, email_verified,
            angle_type, email_method, scraped_at
        FROM leads
        ORDER BY scraped_at DESC
        LIMIT ?
    """, (limit,))


def get_scrape_files() -> list[dict]:
    """List CSV files in the data/ directory, newest first."""
    data_dir = os.path.join(_HERE, config.CSV_OUTPUT_DIR if hasattr(config, "CSV_OUTPUT_DIR") else "data")
    if not os.path.isdir(data_dir):
        return []
    files = []
    for fname in os.listdir(data_dir):
        if not fname.endswith(".csv"):
            continue
        fpath = os.path.join(data_dir, fname)
        stat = os.stat(fpath)
        size_kb = stat.st_size / 1024
        # Count rows (minus header)
        try:
            with open(fpath, "r", encoding="utf-8") as f:
                row_count = max(sum(1 for _ in f) - 1, 0)
        except Exception:
            row_count = 0
        files.append({
            "name": fname,
            "size": f"{size_kb:.0f} KB" if size_kb < 1024 else f"{size_kb/1024:.1f} MB",
            "rows": row_count,
            "modified": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M"),
        })
    files.sort(key=lambda f: f["modified"], reverse=True)
    return files


# ---------------------------------------------------------------------------
# Template
# ---------------------------------------------------------------------------

TEMPLATE = """
<!DOCTYPE html>
<html lang="en" data-bs-theme="dark">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>Bookedly Command Center</title>
  <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css" rel="stylesheet" />
  <link href="https://cdn.jsdelivr.net/npm/bootstrap-icons@1.11.3/font/bootstrap-icons.min.css" rel="stylesheet" />
  <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
  <style>
    :root {
      --bg-main: #0f1117;
      --bg-card: #1a1d27;
      --bg-sidebar: #141620;
      --accent-blue: #4f8cff;
      --accent-green: #34d399;
      --accent-orange: #fb923c;
      --accent-red: #f87171;
      --accent-purple: #a78bfa;
      --accent-cyan: #22d3ee;
      --border: #2a2d3a;
      --text-muted: #6b7280;
      --text-secondary: #9ca3af;
    }

    * { box-sizing: border-box; }

    body {
      background: var(--bg-main);
      color: #e5e7eb;
      font-family: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
      font-size: 0.875rem;
      margin: 0;
      min-height: 100vh;
    }

    /* ── Sidebar ── */
    .sidebar {
      position: fixed;
      top: 0; left: 0;
      width: 240px;
      height: 100vh;
      background: var(--bg-sidebar);
      border-right: 1px solid var(--border);
      padding: 0;
      z-index: 100;
      display: flex;
      flex-direction: column;
    }

    .sidebar-brand {
      padding: 20px 20px 16px;
      border-bottom: 1px solid var(--border);
    }

    .sidebar-brand h5 {
      margin: 0;
      font-size: 1.1rem;
      font-weight: 700;
      background: linear-gradient(135deg, var(--accent-blue), var(--accent-purple));
      -webkit-background-clip: text;
      -webkit-text-fill-color: transparent;
    }

    .sidebar-brand small {
      color: var(--text-muted);
      font-size: 0.7rem;
      text-transform: uppercase;
      letter-spacing: 1.5px;
    }

    .sidebar-nav { padding: 12px 0; flex: 1; }

    .sidebar-section {
      padding: 8px 20px 4px;
      font-size: 0.65rem;
      text-transform: uppercase;
      letter-spacing: 1.5px;
      color: var(--text-muted);
      font-weight: 600;
    }

    .sidebar-link {
      display: flex;
      align-items: center;
      gap: 10px;
      padding: 9px 20px;
      color: var(--text-secondary);
      text-decoration: none;
      font-size: 0.82rem;
      transition: all .15s;
      border-left: 3px solid transparent;
    }

    .sidebar-link:hover {
      background: rgba(79, 140, 255, 0.08);
      color: #fff;
    }

    .sidebar-link.active {
      background: rgba(79, 140, 255, 0.12);
      color: var(--accent-blue);
      border-left-color: var(--accent-blue);
      font-weight: 600;
    }

    .sidebar-link i { font-size: 1rem; width: 20px; text-align: center; }

    .sidebar-link .badge {
      margin-left: auto;
      font-size: 0.65rem;
      padding: 2px 6px;
    }

    .sidebar-footer {
      padding: 16px 20px;
      border-top: 1px solid var(--border);
      font-size: 0.72rem;
      color: var(--text-muted);
    }

    /* ── Main Content ── */
    .main-content {
      margin-left: 240px;
      padding: 24px 28px;
      min-height: 100vh;
    }

    .page-header {
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin-bottom: 24px;
    }

    .page-header h4 {
      font-weight: 700;
      margin: 0;
    }

    .page-header .last-update {
      font-size: 0.75rem;
      color: var(--text-muted);
    }

    /* ── Stat Cards ── */
    .stat-card {
      background: var(--bg-card);
      border: 1px solid var(--border);
      border-radius: 12px;
      padding: 18px 20px;
      position: relative;
      overflow: hidden;
      transition: transform .15s, border-color .15s;
    }

    .stat-card:hover {
      transform: translateY(-2px);
      border-color: rgba(79, 140, 255, 0.3);
    }

    .stat-card .stat-icon {
      width: 38px;
      height: 38px;
      border-radius: 10px;
      display: flex;
      align-items: center;
      justify-content: center;
      font-size: 1.1rem;
      margin-bottom: 12px;
    }

    .stat-card .stat-value {
      font-size: 1.6rem;
      font-weight: 700;
      line-height: 1;
      margin-bottom: 4px;
    }

    .stat-card .stat-label {
      font-size: 0.72rem;
      color: var(--text-muted);
      text-transform: uppercase;
      letter-spacing: 0.5px;
    }

    .stat-card .stat-sub {
      font-size: 0.72rem;
      margin-top: 6px;
    }

    /* ── Cards ── */
    .dash-card {
      background: var(--bg-card);
      border: 1px solid var(--border);
      border-radius: 12px;
      padding: 20px;
    }

    .dash-card .card-title {
      font-size: 0.78rem;
      font-weight: 600;
      text-transform: uppercase;
      letter-spacing: 0.8px;
      color: var(--text-secondary);
      margin-bottom: 16px;
      display: flex;
      align-items: center;
      gap: 8px;
    }

    .dash-card .card-title i { color: var(--accent-blue); }

    /* ── Tables ── */
    .table-dark-custom {
      --bs-table-bg: transparent;
      --bs-table-hover-bg: rgba(79, 140, 255, 0.05);
      font-size: 0.8rem;
      margin: 0;
    }

    .table-dark-custom th {
      font-size: 0.7rem;
      text-transform: uppercase;
      letter-spacing: 0.5px;
      color: var(--text-muted);
      font-weight: 600;
      border-bottom: 1px solid var(--border);
      padding: 8px 10px;
    }

    .table-dark-custom td {
      border-bottom: 1px solid rgba(42, 45, 58, 0.5);
      padding: 8px 10px;
      vertical-align: middle;
    }

    /* ── Tags ── */
    .tag {
      display: inline-block;
      padding: 2px 8px;
      border-radius: 6px;
      font-size: 0.7rem;
      font-weight: 600;
    }

    .tag-good { background: rgba(52, 211, 153, 0.15); color: var(--accent-green); }
    .tag-risky { background: rgba(251, 146, 60, 0.15); color: var(--accent-orange); }
    .tag-bad { background: rgba(248, 113, 113, 0.15); color: var(--accent-red); }
    .tag-unverified { background: rgba(107, 114, 128, 0.15); color: var(--text-muted); }
    .tag-niche { background: rgba(79, 140, 255, 0.12); color: var(--accent-blue); }
    .tag-angle { background: rgba(167, 139, 250, 0.12); color: var(--accent-purple); }
    .tag-dm { background: rgba(52, 211, 153, 0.12); color: var(--accent-green); }

    /* ── Progress bar ── */
    .mini-progress {
      height: 6px;
      background: rgba(255,255,255,0.06);
      border-radius: 3px;
      overflow: hidden;
    }
    .mini-progress-fill {
      height: 100%;
      border-radius: 3px;
      transition: width .3s;
    }

    /* ── Charts ── */
    .chart-container {
      position: relative;
      height: 200px;
    }

    /* ── Email cell ── */
    .email-cell {
      max-width: 180px;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
      color: var(--accent-cyan);
    }

    /* ── Responsive ── */
    @media (max-width: 992px) {
      .sidebar { width: 60px; }
      .sidebar-brand h5, .sidebar-brand small,
      .sidebar-section, .sidebar-link span, .sidebar-link .badge,
      .sidebar-footer { display: none; }
      .sidebar-link { justify-content: center; padding: 12px; }
      .main-content { margin-left: 60px; }
    }
  </style>
</head>
<body>

<!-- ═══ Sidebar ═══ -->
<nav class="sidebar">
  <div class="sidebar-brand">
    <h5>Bookedly</h5>
    <small>Command Center</small>
  </div>

  <div class="sidebar-nav">
    <div class="sidebar-section">Acquisition</div>
    <a href="#" class="sidebar-link active">
      <i class="bi bi-envelope-fill"></i>
      <span>Cold Email</span>
      <span class="badge bg-primary rounded-pill">{{ overview.total }}</span>
    </a>
    <a href="#" class="sidebar-link">
      <i class="bi bi-chat-dots-fill"></i>
      <span>DM Outreach</span>
      <span class="badge bg-secondary rounded-pill">Soon</span>
    </a>
    <a href="#" class="sidebar-link">
      <i class="bi bi-telephone-fill"></i>
      <span>Cold Calling</span>
      <span class="badge bg-secondary rounded-pill">Soon</span>
    </a>
    <a href="#" class="sidebar-link">
      <i class="bi bi-linkedin"></i>
      <span>LinkedIn</span>
      <span class="badge bg-secondary rounded-pill">Soon</span>
    </a>

    <div class="sidebar-section" style="margin-top:16px;">Analytics</div>
    <a href="#" class="sidebar-link">
      <i class="bi bi-bar-chart-fill"></i>
      <span>Reports</span>
    </a>
    <a href="#" class="sidebar-link">
      <i class="bi bi-gear-fill"></i>
      <span>Settings</span>
    </a>
  </div>

  <div class="sidebar-footer">
    <div style="display:flex; align-items:center; gap:8px;">
      <div style="width:8px; height:8px; border-radius:50%; background:var(--accent-green);"></div>
      System Online
    </div>
  </div>
</nav>

<!-- ═══ Main Content ═══ -->
<div class="main-content">

  <!-- Header -->
  <div class="page-header">
    <div>
      <h4>Cold Email Pipeline</h4>
      <span class="last-update">Last refresh: {{ now }}</span>
    </div>
    <div style="display:flex; gap:8px;">
      <a href="/" class="btn btn-sm btn-outline-primary"><i class="bi bi-arrow-clockwise"></i> Refresh</a>
    </div>
  </div>

  <!-- ── KPI Cards ── -->
  <div class="row g-3 mb-4">
    <div class="col-6 col-md-4 col-xl-2">
      <div class="stat-card">
        <div class="stat-icon" style="background:rgba(79,140,255,.12); color:var(--accent-blue);">
          <i class="bi bi-people-fill"></i>
        </div>
        <div class="stat-value">{{ overview.total | commify }}</div>
        <div class="stat-label">Total Leads</div>
        <div class="stat-sub text-info">+{{ overview.today }} today</div>
      </div>
    </div>
    <div class="col-6 col-md-4 col-xl-2">
      <div class="stat-card">
        <div class="stat-icon" style="background:rgba(52,211,153,.12); color:var(--accent-green);">
          <i class="bi bi-envelope-check-fill"></i>
        </div>
        <div class="stat-value">{{ overview.with_email | commify }}</div>
        <div class="stat-label">With Email</div>
        <div class="stat-sub" style="color:var(--accent-green);">{{ overview.email_pct }}% rate</div>
      </div>
    </div>
    <div class="col-6 col-md-4 col-xl-2">
      <div class="stat-card">
        <div class="stat-icon" style="background:rgba(34,211,238,.12); color:var(--accent-cyan);">
          <i class="bi bi-person-badge-fill"></i>
        </div>
        <div class="stat-value">{{ overview.personal_email | commify }}</div>
        <div class="stat-label">Personal Emails</div>
        <div class="stat-sub" style="color:var(--accent-cyan);">{{ overview.personal_pct }}% of emails</div>
      </div>
    </div>
    <div class="col-6 col-md-4 col-xl-2">
      <div class="stat-card">
        <div class="stat-icon" style="background:rgba(167,139,250,.12); color:var(--accent-purple);">
          <i class="bi bi-person-fill-check"></i>
        </div>
        <div class="stat-value">{{ overview.with_dm | commify }}</div>
        <div class="stat-label">Decision Makers</div>
        <div class="stat-sub" style="color:var(--accent-purple);">{{ overview.dm_pct }}% found</div>
      </div>
    </div>
    <div class="col-6 col-md-4 col-xl-2">
      <div class="stat-card">
        <div class="stat-icon" style="background:rgba(52,211,153,.12); color:var(--accent-green);">
          <i class="bi bi-shield-check"></i>
        </div>
        <div class="stat-value">{{ overview.verified_good | commify }}</div>
        <div class="stat-label">Verified Good</div>
        <div class="stat-sub"><span style="color:var(--accent-orange);">{{ overview.verified_risky }} risky</span> &middot; <span style="color:var(--accent-red);">{{ overview.verified_bad }} bad</span></div>
      </div>
    </div>
    <div class="col-6 col-md-4 col-xl-2">
      <div class="stat-card">
        <div class="stat-icon" style="background:rgba(251,146,60,.12); color:var(--accent-orange);">
          <i class="bi bi-send-fill"></i>
        </div>
        <div class="stat-value">{{ overview.delivered | commify }}</div>
        <div class="stat-label">Delivered</div>
        <div class="stat-sub" style="color:var(--accent-orange);">to Instantly</div>
      </div>
    </div>
  </div>

  <!-- ── Charts Row ── -->
  <div class="row g-3 mb-4">
    <!-- Email Verification Breakdown -->
    <div class="col-12 col-lg-4">
      <div class="dash-card h-100">
        <div class="card-title"><i class="bi bi-shield-check"></i> Email Verification</div>
        <div class="chart-container">
          <canvas id="verifyChart"></canvas>
        </div>
      </div>
    </div>

    <!-- Email Source Methods -->
    <div class="col-12 col-lg-4">
      <div class="dash-card h-100">
        <div class="card-title"><i class="bi bi-diagram-3-fill"></i> Email Sources</div>
        <div class="chart-container">
          <canvas id="methodChart"></canvas>
        </div>
      </div>
    </div>

    <!-- Outreach Angles -->
    <div class="col-12 col-lg-4">
      <div class="dash-card h-100">
        <div class="card-title"><i class="bi bi-bullseye"></i> Outreach Angles</div>
        <div class="chart-container">
          <canvas id="angleChart"></canvas>
        </div>
      </div>
    </div>
  </div>

  <!-- ── Tables Row ── -->
  <div class="row g-3 mb-4">
    <!-- By State -->
    <div class="col-12 col-lg-6">
      <div class="dash-card">
        <div class="card-title"><i class="bi bi-geo-alt-fill"></i> Leads by State</div>
        <div style="max-height:320px; overflow-y:auto;">
          <table class="table table-dark-custom">
            <thead>
              <tr>
                <th>State</th>
                <th class="text-end">Leads</th>
                <th>Email Rate</th>
                <th class="text-end">Verified</th>
              </tr>
            </thead>
            <tbody>
              {% for r in by_state %}
              <tr>
                <td><strong>{{ r.state }}</strong></td>
                <td class="text-end">{{ r.total | commify }}</td>
                <td>
                  <div style="display:flex; align-items:center; gap:8px;">
                    <div class="mini-progress" style="width:60px;">
                      <div class="mini-progress-fill" style="width:{{ r.email_pct }}%; background:var(--accent-blue);"></div>
                    </div>
                    <span style="font-size:0.75rem; color:var(--text-secondary);">{{ r.email_pct }}%</span>
                  </div>
                </td>
                <td class="text-end" style="color:var(--accent-green);">{{ r.verified }}</td>
              </tr>
              {% endfor %}
            </tbody>
          </table>
        </div>
      </div>
    </div>

    <!-- By Niche -->
    <div class="col-12 col-lg-6">
      <div class="dash-card">
        <div class="card-title"><i class="bi bi-grid-3x3-gap-fill"></i> Leads by Niche</div>
        <div style="max-height:320px; overflow-y:auto;">
          <table class="table table-dark-custom">
            <thead>
              <tr>
                <th>Niche</th>
                <th class="text-end">Leads</th>
                <th>Email Rate</th>
                <th class="text-end">DM Found</th>
              </tr>
            </thead>
            <tbody>
              {% for r in by_niche %}
              <tr>
                <td><span class="tag tag-niche">{{ r.niche }}</span></td>
                <td class="text-end">{{ r.total | commify }}</td>
                <td>
                  <div style="display:flex; align-items:center; gap:8px;">
                    <div class="mini-progress" style="width:60px;">
                      <div class="mini-progress-fill" style="width:{{ r.email_pct }}%; background:var(--accent-cyan);"></div>
                    </div>
                    <span style="font-size:0.75rem; color:var(--text-secondary);">{{ r.email_pct }}%</span>
                  </div>
                </td>
                <td class="text-end">{{ r.with_dm }}</td>
              </tr>
              {% endfor %}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  </div>

  <!-- ── Scrape Files ── -->
  <div class="dash-card mb-4">
    <div class="card-title"><i class="bi bi-file-earmark-spreadsheet"></i> Scrape Files</div>
    {% if scrape_files %}
    <div style="max-height:360px; overflow-y:auto;">
      <table class="table table-dark-custom">
        <thead>
          <tr>
            <th>File</th>
            <th class="text-end">Leads</th>
            <th class="text-end">Size</th>
            <th>Date</th>
            <th class="text-center">Download</th>
          </tr>
        </thead>
        <tbody>
          {% for f in scrape_files %}
          <tr>
            <td style="font-size:0.8rem;">
              <i class="bi bi-filetype-csv" style="color:var(--accent-green);"></i>
              {{ f.name }}
            </td>
            <td class="text-end">{{ f.rows | commify }}</td>
            <td class="text-end" style="color:var(--text-secondary);">{{ f.size }}</td>
            <td style="font-size:0.75rem; color:var(--text-secondary);">{{ f.modified }}</td>
            <td class="text-center">
              <a href="/download/{{ f.name }}" class="btn btn-sm" style="background:var(--accent-blue); color:#fff; padding:2px 12px; font-size:0.75rem; border-radius:6px;">
                <i class="bi bi-download"></i> CSV
              </a>
            </td>
          </tr>
          {% endfor %}
        </tbody>
      </table>
    </div>
    {% else %}
    <p style="color:var(--text-muted); padding:16px 0;">No scrape files yet. Run the pipeline to generate CSVs.</p>
    {% endif %}
  </div>

  <!-- ── Recent Leads ── -->
  <div class="dash-card mb-4">
    <div class="card-title"><i class="bi bi-clock-history"></i> Recent Leads</div>
    <div style="overflow-x:auto;">
      <table class="table table-dark-custom">
        <thead>
          <tr>
            <th>Company</th>
            <th>Location</th>
            <th>Niche</th>
            <th>Decision Maker</th>
            <th>Email</th>
            <th>Verified</th>
            <th>Angle</th>
            <th>Source</th>
          </tr>
        </thead>
        <tbody>
          {% for r in recent %}
          <tr>
            <td><strong>{{ r.name or '-' }}</strong></td>
            <td style="white-space:nowrap;">{{ r.city or '' }}{% if r.city and r.state %}, {% endif %}{{ r.state or '' }}</td>
            <td>{% if r.niche %}<span class="tag tag-niche">{{ r.niche }}</span>{% else %}-{% endif %}</td>
            <td>
              {% if r.decision_maker_name %}
                <span class="tag tag-dm">{{ r.decision_maker_name }}</span>
              {% else %}<span style="color:var(--text-muted);">-</span>{% endif %}
            </td>
            <td class="email-cell" title="{{ r.email or '' }}">{{ r.email or '-' }}</td>
            <td>
              {% if r.email_verified == 'good' %}<span class="tag tag-good">Good</span>
              {% elif r.email_verified == 'risky' %}<span class="tag tag-risky">Risky</span>
              {% elif r.email_verified == 'bad' %}<span class="tag tag-bad">Bad</span>
              {% else %}<span class="tag tag-unverified">-</span>{% endif %}
            </td>
            <td>
              {% if r.angle_type and r.angle_type != 'unanalyzed' %}
                <span class="tag tag-angle">{{ r.angle_type }}</span>
              {% else %}<span style="color:var(--text-muted);">-</span>{% endif %}
            </td>
            <td style="font-size:0.7rem; color:var(--text-muted);">{{ r.email_method or '-' }}</td>
          </tr>
          {% endfor %}
        </tbody>
      </table>
    </div>
  </div>

</div><!-- /main-content -->

<!-- ═══ Charts JS ═══ -->
<script>
Chart.defaults.color = '#6b7280';
Chart.defaults.borderColor = 'rgba(42, 45, 58, 0.5)';

const chartFont = { family: "'Inter', sans-serif", size: 11 };

// ── Verification Donut ──
new Chart(document.getElementById('verifyChart'), {
  type: 'doughnut',
  data: {
    labels: ['Good', 'Risky', 'Bad', 'Unverified'],
    datasets: [{
      data: [{{ overview.verified_good }}, {{ overview.verified_risky }}, {{ overview.verified_bad }}, {{ overview.unverified }}],
      backgroundColor: ['#34d399', '#fb923c', '#f87171', '#374151'],
      borderWidth: 0,
      hoverOffset: 6,
    }]
  },
  options: {
    responsive: true,
    maintainAspectRatio: false,
    cutout: '65%',
    plugins: {
      legend: { position: 'bottom', labels: { font: chartFont, padding: 12, usePointStyle: true, pointStyleWidth: 8 } }
    }
  }
});

// ── Email Methods Bar ──
const methods = {{ email_methods | tojson }};
new Chart(document.getElementById('methodChart'), {
  type: 'bar',
  data: {
    labels: methods.map(m => m.method),
    datasets: [{
      data: methods.map(m => m.total),
      backgroundColor: ['#4f8cff', '#22d3ee', '#a78bfa', '#34d399', '#fb923c', '#f87171'],
      borderRadius: 6,
      barPercentage: 0.6,
    }]
  },
  options: {
    responsive: true,
    maintainAspectRatio: false,
    indexAxis: 'y',
    plugins: { legend: { display: false } },
    scales: {
      x: { grid: { display: false }, ticks: { font: chartFont } },
      y: { grid: { display: false }, ticks: { font: chartFont } },
    }
  }
});

// ── Angles Donut ──
const angles = {{ by_angle | tojson }};
const angleColors = {
  'no_ads': '#f87171', 'has_ads': '#34d399', 'unanalyzed': '#374151', 'none': '#374151',
  'few_ads': '#fb923c', 'stale_ads': '#facc15', 'generic_ads': '#a78bfa', 'audit_opportunity': '#22d3ee',
};
new Chart(document.getElementById('angleChart'), {
  type: 'doughnut',
  data: {
    labels: angles.map(a => a.angle_type),
    datasets: [{
      data: angles.map(a => a.total),
      backgroundColor: angles.map(a => angleColors[a.angle_type] || '#4f8cff'),
      borderWidth: 0,
      hoverOffset: 6,
    }]
  },
  options: {
    responsive: true,
    maintainAspectRatio: false,
    cutout: '65%',
    plugins: {
      legend: { position: 'bottom', labels: { font: chartFont, padding: 12, usePointStyle: true, pointStyleWidth: 8 } }
    }
  }
});
</script>

</body>
</html>
"""


# ---------------------------------------------------------------------------
# Jinja filter
# ---------------------------------------------------------------------------

@app.template_filter("commify")
def commify(n) -> str:
    try:
        return f"{int(n):,}"
    except (TypeError, ValueError):
        return str(n)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    db_exists = os.path.exists(DB_PATH)
    empty = {
        "total": 0, "with_email": 0, "personal_email": 0, "with_dm": 0,
        "delivered": 0, "fb_checked": 0, "verified_good": 0, "verified_risky": 0,
        "verified_bad": 0, "unverified": 0, "today": 0, "this_week": 0,
        "email_pct": 0, "dm_pct": 0, "personal_pct": 0,
    }
    return render_template_string(
        TEMPLATE,
        overview=get_overview() if db_exists else empty,
        by_state=get_by_state() if db_exists else [],
        by_niche=get_by_niche() if db_exists else [],
        by_angle=get_by_angle() if db_exists else [],
        email_methods=get_email_methods() if db_exists else [],
        daily_trend=get_daily_trend() if db_exists else [],
        recent=get_recent_leads() if db_exists else [],
        scrape_files=get_scrape_files(),
        now=datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"),
    )


@app.route("/api/stats")
def api_stats():
    """JSON endpoint for external integrations."""
    return jsonify(get_overview() if os.path.exists(DB_PATH) else {})


@app.route("/download/<path:filename>")
def download_file(filename):
    """Download a CSV file from the data directory."""
    data_dir = os.path.join(_HERE, config.CSV_OUTPUT_DIR if hasattr(config, "CSV_OUTPUT_DIR") else "data")
    # Security: only allow .csv files, no path traversal
    if ".." in filename or "/" in filename or not filename.endswith(".csv"):
        abort(404)
    return send_from_directory(data_dir, filename, as_attachment=True)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    if not os.path.exists(DB_PATH):
        print(f"[warn] Database not found at {DB_PATH}")
    print(f"[bookedly] Command Center starting on http://127.0.0.1:5050")
    app.run(host="0.0.0.0", port=5050, debug=False)
