bash

cat /mnt/user-data/outputs/fara_lda_monitor.py
Output

#!/usr/bin/env python3
"""
FARA + LDA Filing Monitor
Daily email digest of new foreign lobbying and Senate lobbying filings.
"""

import os
import re
import json
import csv
import io
import zipfile
import smtplib
import logging
import time
from datetime import date, timedelta, datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

import requests
import anthropic
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# ─────────────────────────────────────────────
#  YOUR WATCHLIST
# ─────────────────────────────────────────────

# FARA: Firm names to monitor as registrants
FARA_REGISTRANT_NAMES = [
    "Ballard Partners",
    "Mercury Public Affairs",
    "BGR",
    "Akin Gump",
    "Brownstein Hyatt",
    "Miller Strategies LLC",
    "Checkmate Government Relations",
    "Continental Strategy",
    "Tactic COC",
    "DCI Group",
    "Cornerstone Government Affairs",
    "Holland & Knight",
    "Williams & Jensen",
    "Invariant",
    "Drake Ventures",
    "Cassidy & Associates",
    "Harbinger Strategies",
]

# FARA: Individual people to monitor as registrants
FARA_INDIVIDUAL_REGISTRANTS = [
    "Roger Stone",
    "Paul Manafort",
    "Brian Ballard",
    "Jeff Miller",
    "Jason Miller",
    "Brett Tolman",
    "Robert Stryker",
]

# FARA: Countries to monitor as foreign principals
FARA_COUNTRIES = [
    "Qatar",
    "Saudi Arabia",
    "Israel",
    "UAE",
    "United Arab Emirates",
    "Venezuela",
    "Iran",
    "India",
    "Nigeria",
    "Ukraine",
    "Kazakhstan",
    "Turkey",
    "China",
    "Russia",
]

# LDA: Lobbying firm names to monitor as registrants
LDA_REGISTRANT_NAMES = [
    "Ballard Partners",
    "Mercury Public Affairs",
    "BGR Group",
    "Akin Gump",
    "Brownstein Hyatt",
    "Miller Strategies LLC",
    "Checkmate Government Relations",
    "Continental Strategy",
    "Tactic COC",
    "DCI Group",
    "Cornerstone Government Affairs",
    "Holland & Knight",
    "Williams & Jensen",
    "Invariant",
    "Drake Ventures",
    "Cassidy & Associates",
    "Harbinger Strategies",
]

# LDA: Client/company names to monitor
LDA_CLIENT_NAMES = [
    "OpenAI",
    "Anthropic",
    "Meta",
    "Tesla",
    "World Liberty Financial",
    "Binance",
    "Cantor Fitzgerald",
]

# LDA: Individual lobbyist names to monitor
LDA_LOBBYIST_NAMES = [
    "Roger Stone",
    "Jason Miller",
    "Robert Stryker",
    "Brett Tolman",
    "Paul Manafort",
    "Brian Ballard",
    "Jeff Miller",
    "Corey Lewandowski",
    "David Urban",
    "Tom Barrack",
]

# ─────────────────────────────────────────────
#  CONFIG
# ─────────────────────────────────────────────

ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
EMAIL_FROM        = os.environ["EMAIL_FROM"]
EMAIL_TO          = os.environ["EMAIL_TO"]
EMAIL_PASSWORD    = os.environ["EMAIL_PASSWORD"]
SMTP_HOST         = "smtp.gmail.com"
SMTP_PORT         = 587

STATE_FILE    = Path("monitor_state.json")
LOOKBACK_DAYS = 2

# ─────────────────────────────────────────────
#  STATE
# ─────────────────────────────────────────────

def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"seen_fara": [], "seen_lda": [], "last_run": None}

def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2))

# ─────────────────────────────────────────────
#  HELPERS
# ─────────────────────────────────────────────

def name_matches(actual_name, watchlist_names):
    """
    Strict matching: watchlist term must appear as a meaningful phrase.
    Short terms require exact full match to avoid noise.
    """
    actual = (actual_name or "").lower().strip()
    for term in watchlist_names:
        t = term.lower().strip()
        if len(t) < 6:
            if actual == t:
                return True
        else:
            if t in actual:
                return True
    return False

def strip_markdown(text):
    """Remove markdown so it renders cleanly in HTML email."""
    text = re.sub(r'\*\*(.+?)\*\*', r'\1', text)
    text = re.sub(r'\*(.+?)\*', r'\1', text)
    text = re.sub(r'#{1,6}\s+', '', text)
    text = re.sub(r'^\s*[-*]\s+', '', text, flags=re.MULTILINE)
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()

def format_amount(income, expenses):
    if income:
        try:
            return "${:,.0f}".format(float(income))
        except Exception:
            return str(income)
    elif expenses:
        try:
            return "${:,.0f}".format(float(expenses))
        except Exception:
            return str(expenses)
    return None

# ─────────────────────────────────────────────
#  FARA
# ─────────────────────────────────────────────

def download_fara_csv(filename, retries=3):
    url = "https://efile.fara.gov/bulk/zip/{}".format(filename)
    for attempt in range(1, retries + 1):
        try:
            log.info("Downloading FARA bulk data (attempt {}): {}".format(attempt, url))
            r = requests.get(url, timeout=60)
            r.raise_for_status()
            with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
                inner = zf.namelist()[0]
                with zf.open(inner) as f:
                    text = f.read().decode("iso-8859-1")
            reader = csv.DictReader(io.StringIO(text))
            return list(reader)
        except Exception as e:
            log.error("FARA download attempt {} failed: {}".format(attempt, e))
            if attempt < retries:
                log.info("Retrying in 5 seconds...")
                time.sleep(5)
    log.error("All FARA download attempts failed — skipping FARA this run.")
    return []

def get_new_fara_filings(seen_ids):
    rows = download_fara_csv("FARA_All_RegistrantDocs.csv.zip")
    if not rows:
        return []

    cutoff = date.today() - timedelta(days=LOOKBACK_DAYS)
    new_filings = []

    for row in rows:
        doc_id = row.get("DocumentLink", "") or row.get("Url", "") or row.get("Link", "")
        if not doc_id:
            doc_id = "{}-{}-{}".format(
                row.get("RegistrationNumber", ""),
                row.get("DateStamped", ""),
                row.get("DocType", ""))

        if doc_id in seen_ids:
            continue

        date_str = row.get("DateStamped", "") or row.get("ReceivedDate", "")
        try:
            filed_date = datetime.strptime(date_str[:10], "%Y-%m-%d").date()
        except Exception:
            try:
                filed_date = datetime.strptime(date_str[:10], "%m/%d/%Y").date()
            except Exception:
                continue

        if filed_date < cutoff:
            continue

        registrant = row.get("Registrant", "") or ""
        country    = (row.get("ForeignPrincipalCountryOfFormation", "") or
                      row.get("Country", "") or "")

        firm_match       = name_matches(registrant, FARA_REGISTRANT_NAMES)
        individual_match = name_matches(registrant, FARA_INDIVIDUAL_REGISTRANTS)
        country_match    = name_matches(country, FARA_COUNTRIES)

        if firm_match or individual_match or country_match:
            row["_doc_id"]     = doc_id
            row["_filed_date"] = filed_date.isoformat()
            if individual_match:
                row["_match_type"] = "individual"
            elif firm_match:
                row["_match_type"] = "firm"
            else:
                row["_match_type"] = "country"
            new_filings.append(row)

    log.info("Found {} new FARA filings matching watchlist".format(len(new_filings)))
    return new_filings

def fetch_fara_pdf_url(row):
    link = row.get("DocumentLink", "")
    if link:
        if link.startswith("http"):
            return link
        return "https://efile.fara.gov{}".format(link)
    return None

# ─────────────────────────────────────────────
#  LDA
# ─────────────────────────────────────────────

LDA_API_BASE = "https://lda.senate.gov/api/v1"

def lda_search(params):
    """Single page search — never paginates to avoid pulling years of history."""
    url = "{}/filings/".format(LDA_API_BASE)
    params = dict(params)
    params["page_size"] = 25
    params["page"] = 1
    try:
        r = requests.get(url, params=params, timeout=30)
        r.raise_for_status()
        return r.json().get("results", [])
    except Exception as e:
        log.error("LDA API error: {}".format(e))
        return []

def get_new_lda_filings(seen_ids):
    cutoff = (date.today() - timedelta(days=LOOKBACK_DAYS)).isoformat()
    seen = set(seen_ids)

    lda_firm_filings     = []
    lda_company_filings  = []
    lda_lobbyist_filings = []

    def add_results(rows, match_type, target_list, check_field=None, watchlist=None):
        for row in rows:
            fid = str(row.get("filing_uuid", ""))
            if not fid or fid in seen:
                continue
            if check_field and watchlist:
                if check_field == "registrant":
                    actual = row.get("registrant", {}).get("name", "")
                elif check_field == "client":
                    actual = row.get("client", {}).get("name", "")
                else:
                    actual = ""
                if not name_matches(actual, watchlist):
                    continue
            seen.add(fid)
            row["_match_type"] = match_type
            target_list.append(row)

    for name in LDA_REGISTRANT_NAMES:
        rows = lda_search({"registrant_name": name, "filing_date_after": cutoff})
        add_results(rows, "registrant:{}".format(name),
                    lda_firm_filings, "registrant", LDA_REGISTRANT_NAMES)
        time.sleep(0.3)

    for name in LDA_CLIENT_NAMES:
        rows = lda_search({"client_name": name, "filing_date_after": cutoff})
        add_results(rows, "client:{}".format(name),
                    lda_company_filings, "client", LDA_CLIENT_NAMES)
        time.sleep(0.3)

    for name in LDA_LOBBYIST_NAMES:
        rows = lda_search({"lobbyist_name": name, "filing_date_after": cutoff})
        add_results(rows, "lobbyist:{}".format(name), lda_lobbyist_filings)
        time.sleep(0.3)

    log.info("LDA: {} firm, {} company, {} lobbyist filings".format(
        len(lda_firm_filings), len(lda_company_filings), len(lda_lobbyist_filings)))

    return lda_firm_filings, lda_company_filings, lda_lobbyist_filings

# ─────────────────────────────────────────────
#  CLAUDE ANALYSIS
# ─────────────────────────────────────────────

claude_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

FARA_SYSTEM = """You are an expert analyst covering foreign lobbying for an investigative journalist.
Write a tight plain-text summary with no markdown, no asterisks, no bullet points, no headers.
Extract and state clearly: registrant name, foreign principal, country, compensation amount,
scope of services, government contacts targeted, and any notable political activity or conflicts of interest.
Flag anything unusual or newsworthy. 3-5 sentences. Start directly with the facts."""

LDA_SYSTEM = """You are an expert analyst covering lobbying disclosures for an investigative journalist.
Write a tight plain-text summary with no markdown, no asterisks, no bullet points, no headers.
Extract and state clearly: lobbying firm, client, dollar amount, specific issues lobbied,
bills or agencies targeted, lobbyist names, any foreign entity connections, and revolving-door hires.
Flag anything notable or unusual. 3-5 sentences. Start directly with the facts."""

def analyze_fara_filing(row):
    summary_text = """
FARA Filing:
- Registrant: {}
- Registration Number: {}
- Foreign Principal: {}
- Country: {}
- Document Type: {}
- Date Filed: {}
- Match Type: {}
""".format(
        row.get("Registrant", "Unknown"),
        row.get("RegistrationNumber", "Unknown"),
        row.get("ForeignPrincipal", "Unknown"),
        row.get("ForeignPrincipalCountryOfFormation", row.get("Country", "Unknown")),
        row.get("DocType", "Unknown"),
        row.get("_filed_date", "Unknown"),
        row.get("_match_type", "Unknown"),
    )

    pdf_content = []
    pdf_url = fetch_fara_pdf_url(row)
    if pdf_url:
        try:
            resp = requests.get(pdf_url, timeout=30)
            if resp.status_code == 200 and "pdf" in resp.headers.get("content-type", "").lower():
                import base64
                pdf_b64 = base64.standard_b64encode(resp.content).decode()
                pdf_content = [{"type": "document",
                                "source": {"type": "base64",
                                           "media_type": "application/pdf",
                                           "data": pdf_b64}}]
        except Exception as e:
            log.warning("Could not fetch FARA PDF: {}".format(e))

    content = pdf_content + [{"type": "text", "text": summary_text}]
    try:
        response = claude_client.messages.create(
            model="claude-opus-4-5",
            max_tokens=500,
            system=FARA_SYSTEM,
            messages=[{"role": "user", "content": content}]
        )
        return strip_markdown(response.content[0].text.strip())
    except Exception as e:
        log.error("Claude FARA analysis failed: {}".format(e))
        return "Analysis unavailable."

def analyze_lda_filing(row):
    activities = row.get("lobbying_activities") or []
    lobbyists = []
    if activities:
        lobbyists = [
            l.get("lobbyist", {}).get("name", "")
            for l in activities[0].get("lobbyists", [])
            if l.get("lobbyist")
        ]

    amount = format_amount(row.get("income"), row.get("expenses"))

    filing_text = """
LDA Filing:
- Filing Type: {}
- Year/Period: {} {}
- Date Filed: {}
- Registrant: {}
- Client: {}
- Client Description: {}
- Amount: {}
- Match Reason: {}

LOBBYING ACTIVITIES:
{}

FOREIGN ENTITIES:
{}

LOBBYISTS: {}
""".format(
        row.get("filing_type_display", row.get("filing_type", "Unknown")),
        row.get("filing_year", ""),
        row.get("filing_period_display", ""),
        row.get("dt_posted", "Unknown"),
        row.get("registrant", {}).get("name", "Unknown"),
        row.get("client", {}).get("name", "Unknown"),
        row.get("client", {}).get("general_description", ""),
        amount or "Not reported",
        row.get("_match_type", ""),
        json.dumps(activities, indent=2)[:2000],
        json.dumps(row.get("foreign_entities", []), indent=2)[:500],
        ", ".join(lobbyists) if lobbyists else "Not listed",
    )

    try:
        response = claude_client.messages.create(
            model="claude-opus-4-5",
            max_tokens=500,
            system=LDA_SYSTEM,
            messages=[{"role": "user", "content": filing_text}]
        )
        return strip_markdown(response.content[0].text.strip())
    except Exception as e:
        log.error("Claude LDA analysis failed: {}".format(e))
        return "Analysis unavailable."

# ─────────────────────────────────────────────
#  EMAIL BUILDER
# ─────────────────────────────────────────────

def filing_card(title, subtitle, amount, analysis, link, accent_color):
    amount_html = ""
    if amount:
        amount_html = '<div style="margin:6px 0;"><span style="background:{color};color:white;font-weight:700;font-size:13px;padding:3px 12px;border-radius:12px;">{amount}</span></div>'.format(
            color=accent_color, amount=amount)

    link_html = ""
    if link:
        link_html = '<a href="{}" style="color:{};font-size:12px;font-weight:600;text-decoration:none;">View original filing &rarr;</a>'.format(link, accent_color)

    return """
    <div style="background:#ffffff;border:1px solid #e8e8e8;border-radius:8px;padding:18px 20px;margin:12px 0;border-left:4px solid {accent};">
        <div style="font-weight:700;font-size:16px;color:#111;">{title}</div>
        <div style="font-size:12px;color:#888;margin:3px 0 8px;text-transform:uppercase;letter-spacing:0.5px;">{subtitle}</div>
        {amount_html}
        <div style="font-size:14px;color:#333;line-height:1.7;margin-top:8px;">{analysis}</div>
        <div style="margin-top:12px;">{link_html}</div>
    </div>""".format(
        accent=accent_color,
        title=title,
        subtitle=subtitle,
        amount_html=amount_html,
        analysis=analysis,
        link_html=link_html,
    )

def section_header(emoji, title, count, color):
    return """
    <div style="margin-top:36px;margin-bottom:4px;padding-bottom:8px;border-bottom:2px solid {color};">
        <span style="font-size:18px;font-weight:800;color:#111;">{emoji} {title}</span>
        <span style="font-size:13px;color:#888;margin-left:8px;">{count} filing{s} today</span>
    </div>""".format(
        color=color, emoji=emoji, title=title,
        count=count, s="s" if count != 1 else "")

def empty_section():
    return '<p style="color:#bbb;font-size:13px;padding:8px 0 16px;font-style:italic;">No new filings today.</p>'

def build_email_html(fara_items, lda_firm_items, lda_company_items, lda_lobbyist_items):
    today_str = date.today().strftime("%A, %B %d, %Y")
    total = (len(fara_items) + len(lda_firm_items) +
             len(lda_company_items) + len(lda_lobbyist_items))

    # ── FARA section ──
    fara_html = section_header("🌐", "FARA — Foreign Agent Filings", len(fara_items), "#c0392b")
    if fara_items:
        for item in fara_items:
            registrant = item.get("Registrant", "Unknown")
            country    = item.get("ForeignPrincipalCountryOfFormation", item.get("Country", ""))
            principal  = item.get("ForeignPrincipal", "")
            doc_type   = item.get("DocType", "Filing")
            filed      = item.get("_filed_date", "")
            link       = fetch_fara_pdf_url(item)
            subtitle   = "{doc} &bull; {country}{princ}&bull; Filed {filed}".format(
                doc=doc_type,
                country=country,
                princ=" &bull; " + principal + " " if principal else " ",
                filed=filed)
            fara_html += filing_card(registrant, subtitle, None,
                                     item.get("_analysis", ""), link, "#c0392b")
    else:
        fara_html += empty_section()

    # ── LDA Firms section ──
    lda_html = section_header("🏛️", "LDA — Lobbying Firm Filings", len(lda_firm_items), "#2471a3")
    if lda_firm_items:
        for item in lda_firm_items:
            registrant  = item.get("registrant", {}).get("name", "Unknown")
            client_name = item.get("client", {}).get("name", "Unknown Client")
            period      = item.get("filing_period_display", item.get("filing_period", ""))
            year        = item.get("filing_year", "")
            ftype       = item.get("filing_type_display", item.get("filing_type", ""))
            amount      = format_amount(item.get("income"), item.get("expenses"))
            uuid        = item.get("filing_uuid", "")
            link        = "https://lda.senate.gov/filings/public/filing/{}/print/".format(uuid) if uuid else None
            subtitle    = "{ftype} &bull; {client} &bull; {period} {year}".format(
                ftype=ftype, client=client_name, period=period, year=year)
            lda_html += filing_card(registrant, subtitle, amount,
                                    item.get("_analysis", ""), link, "#2471a3")
    else:
        lda_html += empty_section()

    # ── Companies section ──
    company_html = section_header("🏢", "Companies to Watch", len(lda_company_items), "#1e8449")
    if lda_company_items:
        for item in lda_company_items:
            client_name = item.get("client", {}).get("name", "Unknown Client")
            registrant  = item.get("registrant", {}).get("name", "Unknown Firm")
            period      = item.get("filing_period_display", item.get("filing_period", ""))
            year        = item.get("filing_year", "")
            ftype       = item.get("filing_type_display", item.get("filing_type", ""))
            amount      = format_amount(item.get("income"), item.get("expenses"))
            uuid        = item.get("filing_uuid", "")
            link        = "https://lda.senate.gov/filings/public/filing/{}/print/".format(uuid) if uuid else None
            subtitle    = "{ftype} &bull; Lobbied by {reg} &bull; {period} {year}".format(
                ftype=ftype, reg=registrant, period=period, year=year)
            company_html += filing_card(client_name, subtitle, amount,
                                        item.get("_analysis", ""), link, "#1e8449")
    else:
        company_html += empty_section()

    # ── Individual Lobbyists section ──
    lobbyist_html = section_header("👤", "Individual Lobbyists", len(lda_lobbyist_items), "#7d3c98")
    if lda_lobbyist_items:
        for item in lda_lobbyist_items:
            registrant  = item.get("registrant", {}).get("name", "Unknown Firm")
            client_name = item.get("client", {}).get("name", "Unknown Client")
            period      = item.get("filing_period_display", item.get("filing_period", ""))
            year        = item.get("filing_year", "")
            ftype       = item.get("filing_type_display", item.get("filing_type", ""))
            amount      = format_amount(item.get("income"), item.get("expenses"))
            match_type  = item.get("_match_type", "")
            lobbyist_name = match_type.replace("lobbyist:", "") if "lobbyist:" in match_type else "Unknown"
            uuid        = item.get("filing_uuid", "")
            link        = "https://lda.senate.gov/filings/public/filing/{}/print/".format(uuid) if uuid else None
            title       = "{} &mdash; {} / {}".format(lobbyist_name, registrant, client_name)
            subtitle    = "{ftype} &bull; {period} {year}".format(
                ftype=ftype, period=period, year=year)
            lobbyist_html += filing_card(title, subtitle, amount,
                                         item.get("_analysis", ""), link, "#7d3c98")
    else:
        lobbyist_html += empty_section()

    return """<!DOCTYPE html>
<html>
<head><meta charset="UTF-8"></head>
<body style="margin:0;padding:0;background:#f0f0f0;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif;">
<div style="max-width:680px;margin:24px auto;padding:0 16px 40px;">

  <!-- Header -->
  <div style="background:#111;border-radius:8px;padding:24px 28px;margin-bottom:12px;">
    <div style="font-size:11px;color:#888;text-transform:uppercase;letter-spacing:2px;margin-bottom:6px;">Daily Filing Monitor</div>
    <div style="font-size:24px;font-weight:800;color:#fff;margin-bottom:10px;">{today}</div>
    <div style="font-size:15px;color:#ccc;line-height:1.6;">
      Good morning, Gabe. Here is your daily filings digest.<br>
      <span style="color:#fff;font-weight:700;">{total} new filing{s}</span> across 4 categories today.
    </div>
  </div>

  <!-- Count bar -->
  <div style="background:#fff;border-radius:8px;padding:14px 20px;margin-bottom:4px;border:1px solid #e0e0e0;font-size:13px;color:#555;">
    <span>🌐 FARA: <strong style="color:#c0392b;">{fara_count}</strong></span>
    &nbsp;&nbsp;&nbsp;
    <span>🏛️ LDA Firms: <strong style="color:#2471a3;">{lda_count}</strong></span>
    &nbsp;&nbsp;&nbsp;
    <span>🏢 Companies: <strong style="color:#1e8449;">{company_count}</strong></span>
    &nbsp;&nbsp;&nbsp;
    <span>👤 Lobbyists: <strong style="color:#7d3c98;">{lobbyist_count}</strong></span>
  </div>

  {fara_html}
  {lda_html}
  {company_html}
  {lobbyist_html}

  <!-- Footer -->
  <div style="margin-top:32px;padding-top:16px;border-top:1px solid #ddd;font-size:11px;color:#aaa;text-align:center;">
    Sources: <a href="https://efile.fara.gov" style="color:#aaa;">efile.fara.gov</a> &nbsp;&middot;&nbsp;
    <a href="https://lda.senate.gov" style="color:#aaa;">lda.senate.gov</a> &nbsp;&middot;&nbsp;
    AI analysis via Claude (Anthropic)
  </div>

</div>
</body>
</html>""".format(
        today=today_str,
        total=total,
        s="s" if total != 1 else "",
        fara_count=len(fara_items),
        lda_count=len(lda_firm_items),
        company_count=len(lda_company_items),
        lobbyist_count=len(lda_lobbyist_items),
        fara_html=fara_html,
        lda_html=lda_html,
        company_html=company_html,
        lobbyist_html=lobbyist_html,
    )

# ─────────────────────────────────────────────
#  EMAIL SENDER
# ─────────────────────────────────────────────

def send_email(subject, html_body):
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = EMAIL_FROM
    msg["To"]      = EMAIL_TO
    msg.attach(MIMEText(html_body, "html"))
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
        server.ehlo()
        server.starttls()
        server.login(EMAIL_FROM, EMAIL_PASSWORD)
        server.sendmail(EMAIL_FROM, EMAIL_TO, msg.as_string())
    log.info("Email sent to {}".format(EMAIL_TO))

# ─────────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────────

def main():
    log.info("=== Filing Monitor starting ===")
    state     = load_state()
    seen_fara = set(state.get("seen_fara", []))
    seen_lda  = set(state.get("seen_lda",  []))

    # ── FARA ──
    fara_filings = get_new_fara_filings(list(seen_fara))
    for filing in fara_filings:
        log.info("Analyzing FARA: {} / {}".format(
            filing.get("Registrant"), filing.get("ForeignPrincipal")))
        filing["_analysis"] = analyze_fara_filing(filing)
        seen_fara.add(filing["_doc_id"])
        time.sleep(1)

    # ── LDA ──
    lda_firm_filings, lda_company_filings, lda_lobbyist_filings = get_new_lda_filings(list(seen_lda))

    for filing in lda_firm_filings:
        log.info("Analyzing LDA firm: {} / {}".format(
            filing.get("registrant", {}).get("name"),
            filing.get("client", {}).get("name")))
        filing["_analysis"] = analyze_lda_filing(filing)
        seen_lda.add(str(filing.get("filing_uuid", "")))
        time.sleep(1)

    for filing in lda_company_filings:
        log.info("Analyzing LDA company: {} / {}".format(
            filing.get("client", {}).get("name"),
            filing.get("registrant", {}).get("name")))
        filing["_analysis"] = analyze_lda_filing(filing)
        seen_lda.add(str(filing.get("filing_uuid", "")))
        time.sleep(1)

    for filing in lda_lobbyist_filings:
        log.info("Analyzing LDA lobbyist: {}".format(filing.get("_match_type", "")))
        filing["_analysis"] = analyze_lda_filing(filing)
        seen_lda.add(str(filing.get("filing_uuid", "")))
        time.sleep(1)

    # ── Build + send email ──
    total = (len(fara_filings) + len(lda_firm_filings) +
             len(lda_company_filings) + len(lda_lobbyist_filings))
    today_str = date.today().strftime("%B %d, %Y")

    if total == 0:
        subject = "Filing Monitor {} — No new filings today".format(today_str)
    else:
        subject = "Filing Monitor {} — {} new filing{}".format(
            today_str, total, "s" if total != 1 else "")

    html = build_email_html(fara_filings, lda_firm_filings,
                            lda_company_filings, lda_lobbyist_filings)
    try:
        send_email(subject, html)
        log.info("Email sent successfully.")
    except Exception as e:
        log.error("Failed to send email: {}".format(e))
        Path("digest.html").write_text(html)
        log.info("Saved digest to digest.html as fallback.")

    # ── Save state ──
    state["seen_fara"] = list(seen_fara)
    state["seen_lda"]  = list(seen_lda)
    state["last_run"]  = datetime.now().isoformat()
    save_state(state)
    log.info("Done. {} FARA + {} LDA firm + {} company + {} lobbyist filings.".format(
        len(fara_filings), len(lda_firm_filings),
        len(lda_company_filings), len(lda_lobbyist_filings)))

if __name__ == "__main__":
    main()
