#!/usr/bin/env python3
"""
NSE Premarket Report - Sends top 5 advances & bottom 5 declines at 9:10 AM IST
Fetches data snapshots at 9:00 AM and 9:02 AM.

Setup:
    pip install requests pytz schedule

Gmail Setup (OAuth2):
    pip install google-auth google-auth-oauthlib google-auth-httplib2 google-api-python-client
    Follow: https://developers.google.com/gmail/api/quickstart/python

OR use App Password (simpler):
    Enable 2FA on Google → Create App Password → set GMAIL_APP_PASSWORD below

Cron (if you prefer cron over the built-in scheduler):
    9 10 * * 1-5 /usr/bin/python3 /path/to/premarket_report.py --run-once
"""

import os
import sys
import time
import logging
import smtplib
import argparse
import requests
import schedule
import pytz
from datetime import date, datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
import json

# ─────────────────────────────────────────────
#  CONFIG  — edit these
# ─────────────────────────────────────────────
GMAIL_SENDER   = os.environ["GMAIL_SENDER"]
GMAIL_APP_PASS = os.environ["GMAIL_APP_PASS"]
REPORT_TO      = os.environ["REPORT_TO"].split(",")  # recipient list
TOP_N           = 5                        # advances + declines count

IST = pytz.timezone("Asia/Kolkata")

# NSE 2025 & 2026 market holidays (add/update annually)
# Source: https://www.nseindia.com/resources/exchange-communication-holidays
NSE_HOLIDAYS: set[date] = {
    # 2025
    date(2025, 1, 26),  # Republic Day
    date(2025, 2, 26),  # Mahashivratri
    date(2025, 3, 14),  # Holi
    date(2025, 3, 31),  # Id-Ul-Fitr (Ramzan Eid)
    date(2025, 4, 10),  # Shri Ram Navami
    date(2025, 4, 14),  # Dr. Baba Saheb Ambedkar Jayanti
    date(2025, 4, 18),  # Good Friday
    date(2025, 5, 1),   # Maharashtra Day
    date(2025, 8, 15),  # Independence Day
    date(2025, 8, 27),  # Ganesh Chaturthi
    date(2025, 10, 2),  # Mahatma Gandhi Jayanti
    date(2025, 10, 2),  # Dussehra
    date(2025, 10, 20), # Diwali - Laxmi Pujan
    date(2025, 10, 21), # Diwali - Balipratipada
    date(2025, 11, 5),  # Prakash Gurpurb Sri Guru Nanak Dev Ji
    date(2025, 12, 25), # Christmas
    # 2026
    date(2026, 1, 26),  # Republic Day
    date(2026, 3, 20),  # Holi
    date(2026, 4, 3),   # Good Friday
    date(2026, 4, 14),  # Dr. Baba Saheb Ambedkar Jayanti
    date(2026, 5, 1),   # Maharashtra Day
    date(2026, 8, 15),  # Independence Day
    date(2026, 8, 17),  # Ganesh Chaturthi / Parsi New Year
    date(2026, 10, 2),  # Mahatma Gandhi Jayanti
    date(2026, 11, 9),  # Diwali
    date(2026, 12, 25), # Christmas
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)s  %(message)s",
    handlers=[logging.StreamHandler()]
)
log = logging.getLogger(__name__)


# ─────────────────────────────────────────────
#  HOLIDAY / WEEKDAY CHECK
# ─────────────────────────────────────────────

def is_trading_day(d: date = None) -> bool:
    d = d or datetime.now(IST).date()
    if d.weekday() >= 5:        # Saturday=5, Sunday=6
        return False
    if d in NSE_HOLIDAYS:
        return False
    return True


# ─────────────────────────────────────────────
#  DATA FETCH  — NSE premarket endpoint
# ─────────────────────────────────────────────

NSE_PREMARKET_URL = "https://www.nseindia.com/api/market-data-pre-open?key=ALL"


def get_nse_session() -> requests.Session:
    session = requests.Session()
    session.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.nseindia.com/",
    })
    # Step 1: warm up — get cookies from homepage
    try:
        session.get("https://www.nseindia.com", timeout=10)
    except Exception as e:
        log.warning("Homepage warm-up failed (non-fatal): %s", e)
    # Step 2: second cookie set from market-data page
    try:
        session.get("https://www.nseindia.com/market-data/live-equity-market", timeout=10)
    except Exception as e:
        log.warning("Market-data warm-up failed (non-fatal): %s", e)
    return session


def fetch_premarket(session: requests.Session) -> list[dict]:
    """
    Returns list of dicts:
        { symbol, ltp, prevClose, pChange }
    """
    try:
        resp = session.get(NSE_PREMARKET_URL, timeout=10)
        resp.raise_for_status()

        # Guard: empty body
        if not resp.text.strip():
            log.error("Empty body | status=%s | url=%s", resp.status_code, NSE_PREMARKET_URL)
            return []

        # Guard: HTML instead of JSON (blocked / redirected)
        content_type = resp.headers.get("Content-Type", "")
        if "text/html" in content_type:
            log.error("Got HTML instead of JSON — likely blocked. snippet: %s", resp.text[:300])
            return []

        raw = resp.json().get("data", [])
        result = []
        for item in raw:
            md = item.get("metadata", {})
            symbol    = md.get("symbol", "")
            ltp       = md.get("lastPrice") or md.get("finalPrice") or 0
            prev      = md.get("previousClose", 0)
            p_change  = md.get("pChange", 0)
            if not symbol:
                continue
            try:
                ltp      = float(ltp)
                prev     = float(prev)
                p_change = float(p_change)
            except (TypeError, ValueError):
                continue
            result.append({
                "symbol":    symbol,
                "ltp":       ltp,
                "prevClose": prev,
                "pChange":   p_change,
            })
        return result

    except Exception as e:
        log.error("Fetch failed: %s", e)
        return []


# ─────────────────────────────────────────────
#  DATA PROCESSING
# ─────────────────────────────────────────────

def select_top_bottom(stocks: list[dict], n: int = 5):
    sorted_stocks = sorted(stocks, key=lambda x: x["pChange"], reverse=True)
    advances = sorted_stocks[:n]
    declines = sorted_stocks[-n:][::-1]   # worst first
    return advances, declines


def trend_arrow(c1: float, c2: float) -> str:
    if c2 > c1:
        return "▲ Bullish"
    elif c2 < c1:
        return "▼ Bearish"
    return "→ Neutral"


# ─────────────────────────────────────────────
#  EMAIL RENDERING
# ─────────────────────────────────────────────

def pct(val) -> str:
    if val is None:
        return "—"
    sign = "+" if val > 0 else ""
    return f"{sign}{val:.2f}%"


def price(val) -> str:
    return "—" if val is None else f"₹{val:,.2f}"


def build_row(
    symbol: str,
    s1: dict | None,
    s2: dict | None,
    row_class: str,
) -> str:
    c1 = s1["pChange"] if s1 else None
    c2 = s2["pChange"] if s2 else None
    trend = trend_arrow(c1, c2) if (c1 is not None and c2 is not None) else "—"
    trend_color = "#16a34a" if "Bullish" in trend else ("#dc2626" if "Bearish" in trend else "#6b7280")

    ltp1 = s1["ltp"] if s1 else None
    ltp2 = s2["ltp"] if s2 else None

    return f"""
    <tr class="{row_class}">
        <td class="sym">{symbol}</td>
        <td>{price(ltp1)}<br><span class="pct {'pos' if (c1 or 0)>0 else 'neg'}">{pct(c1)}</span></td>
        <td>{price(ltp2)}<br><span class="pct {'pos' if (c2 or 0)>0 else 'neg'}">{pct(c2)}</span></td>
        <td class="trend" style="color:{trend_color}">{trend}</td>
    </tr>"""


def build_table(title: str, stocks: list[tuple], section_class: str) -> str:
    rows = "".join(
        build_row(sym, s1, s2, "alt" if i % 2 else "")
        for i, (sym, s1, s2) in enumerate(stocks)
    )
    return f"""
    <div class="section {section_class}">
        <div class="section-title">{title}</div>
        <table>
            <thead>
                <tr>
                    <th>Stock</th>
                    <th>9:00 AM</th>
                    <th>9:02 AM</th>
                    <th>Trend</th>
                </tr>
            </thead>
            <tbody>{rows}</tbody>
        </table>
    </div>"""


CSS = """
<style>
  body { font-family: 'Segoe UI', Arial, sans-serif; background:#f4f6f9; margin:0; padding:20px; }
  .container { max-width:680px; margin:auto; background:#fff; border-radius:12px;
                box-shadow:0 2px 12px rgba(0,0,0,.1); overflow:hidden; }
  .header { background:linear-gradient(135deg,#1e3a5f,#2563eb); color:#fff;
             padding:24px 28px; }
  .header h1 { margin:0; font-size:20px; letter-spacing:.5px; }
  .header .sub { margin:4px 0 0; font-size:13px; opacity:.8; }
  .body { padding:24px 28px; }
  .section { margin-bottom:28px; }
  .section-title { font-size:14px; font-weight:700; letter-spacing:.4px;
                    text-transform:uppercase; margin-bottom:10px; padding:6px 10px;
                    border-radius:6px; }
  .advances .section-title { background:#dcfce7; color:#15803d; }
  .declines .section-title { background:#fee2e2; color:#b91c1c; }
  table { width:100%; border-collapse:collapse; font-size:13px; }
  th { background:#f1f5f9; text-align:left; padding:9px 10px; color:#475569;
        font-size:11px; text-transform:uppercase; letter-spacing:.5px; }
  td { padding:9px 10px; border-bottom:1px solid #f1f5f9; color:#1e293b; vertical-align:middle; }
  tr.alt td { background:#fafbfc; }
  .sym { font-weight:700; font-size:13px; color:#1e293b; }
  .pct { font-size:11px; font-weight:600; }
  .pos { color:#16a34a; }
  .neg { color:#dc2626; }
  .trend { font-weight:600; font-size:12px; }
  .footer { background:#f8fafc; padding:14px 28px; font-size:11px;
             color:#94a3b8; border-top:1px solid #e2e8f0; text-align:center; }
  @media (max-width:600px) {
    td, th { padding:7px 6px; font-size:12px; }
  }
</style>
"""


def build_html(
    advances: list[tuple],
    declines: list[tuple],
    t1: str,
    t2: str,
    report_date: str,
) -> str:
    adv_table = build_table("🟢 Top 5 Advances", advances, "advances")
    dec_table = build_table("🔴 Bottom 5 Declines", declines, "declines")

    return f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8">{CSS}</head>
<body>
<div class="container">
  <div class="header">
    <h1>📊 NSE Premarket Report</h1>
    <div class="sub">{report_date} &nbsp;|&nbsp; Snapshots: {t1} &amp; {t2} IST</div>
  </div>
  <div class="body">
    {adv_table}
    {dec_table}
  </div>
  <div class="footer">
    Data sourced from NSE India Premarket Feed &nbsp;·&nbsp;
    Generated at 9:10 AM IST &nbsp;·&nbsp; For personal trading reference only.
  </div>
</div>
</body>
</html>"""


# ─────────────────────────────────────────────
#  EMAIL SENDER  (SMTP + App Password)
# ─────────────────────────────────────────────

def send_email(html_body: str, subject: str):
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = GMAIL_SENDER
    msg["To"]      = ", ".join(REPORT_TO)
    msg.attach(MIMEText(html_body, "html"))

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(GMAIL_SENDER, GMAIL_APP_PASS)
            server.sendmail(GMAIL_SENDER, REPORT_TO, msg.as_string())
        log.info("Email sent → %s", REPORT_TO)
    except Exception as e:
        log.error("Email failed: %s", e)
        raise


# ─────────────────────────────────────────────
#  MAIN REPORT FLOW
# ─────────────────────────────────────────────

def run_report():
    today = datetime.now(IST).date()
    if not is_trading_day(today):
        log.info("Non-trading day (%s) — skipping.", today)
        return

    log.info("=== Premarket Report — %s ===", today)

    # ── Build session once; reuse for both snapshots ──
    session = get_nse_session()

    # ── Snapshot 1 @ 9:00 AM ──
    log.info("Fetching snapshot 1 (9:00 AM)…")
    snap1_raw = fetch_premarket(session)
    t1_str = datetime.now(IST).strftime("%I:%M %p")

    # ── Wait 2 minutes ──
    log.info("Waiting 120 s for snapshot 2…")
    time.sleep(120)

    # ── Snapshot 2 @ 9:02 AM ──
    log.info("Fetching snapshot 2 (9:02 AM)…")
    snap2_raw = fetch_premarket(session)
    t2_str = datetime.now(IST).strftime("%I:%M %p")

    if not snap1_raw or not snap2_raw:
        log.error("Empty data — aborting report.")
        return

    # Select top/bottom — use snapshot 2 as primary ranking (more current)
    adv2, dec2 = select_top_bottom(snap2_raw, TOP_N)
    adv1, dec1 = select_top_bottom(snap1_raw, TOP_N)

    by1 = {s["symbol"]: s for s in snap1_raw}
    by2 = {s["symbol"]: s for s in snap2_raw}

    # Final symbol list: union deduplicated, snap2 ordering takes priority
    all_syms_adv = list(dict.fromkeys([s["symbol"] for s in adv2] + [s["symbol"] for s in adv1]))[:TOP_N]
    all_syms_dec = list(dict.fromkeys([s["symbol"] for s in dec2] + [s["symbol"] for s in dec1]))[:TOP_N]

    adv_rows = [(sym, by1.get(sym), by2.get(sym)) for sym in all_syms_adv]
    dec_rows = [(sym, by1.get(sym), by2.get(sym)) for sym in all_syms_dec]

    report_date = today.strftime("%A, %d %B %Y")
    html = build_html(adv_rows, dec_rows, t1_str, t2_str, report_date)
    subject = f"📊 NSE Premarket Report — {today.strftime('%d %b %Y')}"

    send_email(html, subject)
    log.info("Report done.")


# ─────────────────────────────────────────────
#  SCHEDULER  (runs inside the script process)
# ─────────────────────────────────────────────

def scheduler_loop():
    """
    Schedules run_report() at 9:00 AM IST every weekday.
    Snapshot 1 @ 9:00 AM → sleep 120s → Snapshot 2 @ 9:02 AM → send ~9:05–9:10 AM.
    """
    log.info("Scheduler started. Waiting for 09:00 IST on trading days…")
    schedule.every().monday.at("09:00").do(run_report)
    schedule.every().tuesday.at("09:00").do(run_report)
    schedule.every().wednesday.at("09:00").do(run_report)
    schedule.every().thursday.at("09:00").do(run_report)
    schedule.every().friday.at("09:00").do(run_report)

    while True:
        schedule.run_pending()
        time.sleep(10)


# ─────────────────────────────────────────────
#  ENTRY POINT
# ─────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="NSE Premarket Report")
    parser.add_argument(
        "--run-once",
        action="store_true",
        help="Run report immediately (for cron / manual test)",
    )
    args = parser.parse_args()

    if args.run_once:
        run_report()
    else:
        scheduler_loop()
