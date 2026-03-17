#!/usr/bin/env python3
"""
AP Coaching Dashboard - Local web server for managing coaching calls

RUN: python coaching_dashboard.py
OPEN: http://localhost:5000

Features:
- Today's calls at a glance
- This week's schedule
- Student details + question files
- Slack message status tracking
- Background sending of reminders
"""

import os
import sys
import json
import signal
import logging
import threading
import smtplib
from logging.handlers import RotatingFileHandler
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime, timedelta
from pathlib import Path
from functools import wraps

try:
    from zoneinfo import ZoneInfo
except ImportError:
    from backports.zoneinfo import ZoneInfo

from flask import Flask, render_template_string, jsonify, request, redirect, url_for
import markdown

# =============================================================================
# TIMEZONE SETTINGS
# =============================================================================

# Students are in US Central, you're in London
STUDENT_TZ = ZoneInfo('America/Chicago')
COACH_TZ = ZoneInfo('Europe/London')

def convert_time_to_london(date_tuple, time_str):
    """Convert a Central time to London time."""
    year, month, day = date_tuple
    hour, minute = map(int, time_str.split(':'))

    # Create datetime in Central time
    central_dt = datetime(year, month, day, hour, minute, tzinfo=STUDENT_TZ)

    # Convert to London
    london_dt = central_dt.astimezone(COACH_TZ)

    return london_dt.strftime('%H:%M')

def get_london_now():
    """Get current time in London."""
    return datetime.now(COACH_TZ)

# Optional Slack integration
try:
    from slack_sdk import WebClient
    from slack_sdk.errors import SlackApiError
    SLACK_AVAILABLE = True
except ImportError:
    SLACK_AVAILABLE = False

# Optional Slack Bolt for interactive bot
try:
    from slack_bolt import App as SlackBoltApp
    from slack_bolt.adapter.socket_mode import SocketModeHandler
    SLACK_BOLT_AVAILABLE = True
except ImportError:
    SLACK_BOLT_AVAILABLE = False

# =============================================================================
# CONFIGURATION
# =============================================================================

app = Flask(__name__)
BASE_DIR = Path(__file__).parent
QUESTIONS_DIR = BASE_DIR / 'student_plans_v3' / 'questions'
PLANS_DIR = BASE_DIR / 'student_plans_v3'
STATUS_FILE = BASE_DIR / 'message_status.json'
CONFIG_FILE = BASE_DIR / 'dashboard_config.json'
LOGS_DIR = BASE_DIR / 'logs'

# =============================================================================
# LOGGING SETUP
# =============================================================================

LOGS_DIR.mkdir(exist_ok=True)
log_handler = RotatingFileHandler(
    LOGS_DIR / 'app.log',
    maxBytes=10_000_000,
    backupCount=5
)
log_handler.setFormatter(logging.Formatter(
    '%(asctime)s %(levelname)s %(name)s: %(message)s'
))
app.logger.addHandler(log_handler)
app.logger.setLevel(logging.INFO)

def load_config():
    if CONFIG_FILE.exists():
        with open(CONFIG_FILE, 'r') as f:
            return json.load(f)
    return {}

def save_config(config):
    with open(CONFIG_FILE, 'w') as f:
        json.dump(config, f, indent=2)

def get_slack_token():
    # First check config file, then environment variable
    config = load_config()
    return config.get('slack_token') or os.environ.get('SLACK_BOT_TOKEN', '')

# =============================================================================
# DATA
# =============================================================================

STUDENTS = {
    'Gus Castillo': {'email': 'gus.castillo@alpha.school', 'course': 'AP Human Geography', 'tier': 'Critical'},
    'Emma Cotner': {'email': 'emma.cotner@alpha.school', 'course': 'AP World History', 'tier': 'Intensive'},
    'Jackson Price': {'email': 'jackson.price@alpha.school', 'course': 'AP World History', 'tier': 'Light'},
    'Boris Dudarev': {'email': 'boris.dudarev@alpha.school', 'course': 'AP Human Geography', 'tier': 'Moderate'},
    'Sydney Barba': {'email': 'sydney.barba@alpha.school', 'course': 'AP Human Geography', 'tier': 'Moderate'},
    'Branson Pfiester': {'email': 'branson.pfiester@alpha.school', 'course': 'AP Human Geography', 'tier': 'Moderate'},
    'Saeed Tarawneh': {'email': 'said.tarawneh@alpha.school', 'course': 'AP World History', 'tier': 'Intensive'},
    'Aheli Shah': {'email': 'aheli.shah@alpha.school', 'course': 'AP Human Geography', 'tier': 'Light'},
    'Ella Dietz': {'email': 'ella.dietz@alpha.school', 'course': 'AP World History', 'tier': 'Light'},
    'Stella Cole': {'email': 'stella.cole@alpha.school', 'course': 'AP World History', 'tier': 'Moderate'},
    'Erika Rigby': {'email': 'erika.rigby@alpha.school', 'course': 'AP Human Geography', 'tier': 'Maintenance'},
    'Grady Swanson': {'email': 'grady.swanson@alpha.school', 'course': 'AP Human Geography', 'tier': 'Maintenance'},
    'Zayen Szpitalak': {'email': 'zayen.szpitalak@alpha.school', 'course': 'AP Human Geography', 'tier': 'Moderate'},
    'Adrienne Laswell': {'email': 'adrienne.laswell@alpha.school', 'course': 'AP Human Geography', 'tier': 'Maintenance'},
    'Austin Lin': {'email': 'austin.lin@alpha.school', 'course': 'AP Human Geography', 'tier': 'Maintenance'},
    'Jessica Owenby': {'email': 'jessica.owenby@alpha.school', 'course': 'AP Human Geography', 'tier': 'Maintenance'},
    'Cruce Saunders IV': {'email': 'cruce.saunders@alpha.school', 'course': 'AP US History', 'tier': 'Maintenance'},
    'Kavin Lingham': {'email': 'kavin.lingham@alpha.school', 'course': 'AP World History', 'tier': 'Maintenance'},
    'Stella Grams': {'email': 'stella.grams@alpha.school', 'course': 'AP World History', 'tier': 'Maintenance'},
    'Jacob Kuchinsky': {'email': 'jacob.kuchinsky@alpha.school', 'course': 'AP Human Geography', 'tier': 'Maintenance'},
    'Luca Sanchez': {'email': 'luca.sanchez@alpha.school', 'course': 'AP Human Geography', 'tier': 'Maintenance'},
    'Ali Romman': {'email': 'ali.romman@alpha.school', 'course': 'AP Human Geography', 'tier': 'Maintenance'},
    'Benny Valles': {'email': 'benjamin.valles@alpha.school', 'course': 'AP Human Geography', 'tier': 'Maintenance'},
    'Vera Li': {'email': 'vera.li@alpha.school', 'course': 'AP Human Geography', 'tier': 'Maintenance'},
    'Emily Smith': {'email': 'emily.smith@alpha.school', 'course': 'AP US Government', 'tier': 'Maintenance'},
    'Paty Margain-Junco': {'email': 'paty.margainjunco@alpha.school', 'course': 'AP US History', 'tier': 'Maintenance'},
    'Michael Cai': {'email': 'michael.cai@alpha.school', 'course': 'AP World History', 'tier': 'Maintenance'},
}

# SCHEDULE is the source of truth for coaching calls.
# After editing, run: python sync_schedule.py
# This keeps MASTER_COACHING_SCHEDULE.md in sync.
SCHEDULE = [
    {'date': (2026, 3, 12), 'time': '09:00', 'student': 'Gus Castillo', 'week': 1},
    {'date': (2026, 3, 17), 'time': '08:30', 'student': 'Emma Cotner', 'week': 1},
    {'date': (2026, 3, 11), 'time': '09:45', 'student': 'Jackson Price', 'week': 1},
    {'date': (2026, 3, 11), 'time': '08:35', 'student': 'Boris Dudarev', 'week': 1},
    {'date': (2026, 3, 11), 'time': '09:05', 'student': 'Sydney Barba', 'week': 1},
    {'date': (2026, 3, 12), 'time': '08:00', 'student': 'Saeed Tarawneh', 'week': 1},
    {'date': (2026, 3, 16), 'time': '09:25', 'student': 'Aheli Shah', 'week': 1},
    {'date': (2026, 3, 17), 'time': '10:00', 'student': 'Ella Dietz', 'week': 1},
    {'date': (2026, 3, 17), 'time': '08:00', 'student': 'Stella Cole', 'week': 1},
    {'date': (2026, 3, 17), 'time': '08:45', 'student': 'Erika Rigby', 'week': 1},
    {'date': (2026, 3, 16), 'time': '09:00', 'student': 'Grady Swanson', 'week': 1},
    {'date': (2026, 3, 16), 'time': '08:50', 'student': 'Zayen Szpitalak', 'week': 1},
    {'date': (2026, 3, 18), 'time': '09:40', 'student': 'Branson Pfiester', 'week': 1},
    {'date': (2026, 3, 19), 'time': '08:20', 'student': 'Gus Castillo', 'week': 2},
    {'date': (2026, 3, 19), 'time': '09:35', 'student': 'Emma Cotner', 'week': 2},
    {'date': (2026, 3, 20), 'time': '08:00', 'student': 'Saeed Tarawneh', 'week': 2},
    {'date': (2026, 3, 20), 'time': '08:35', 'student': 'Adrienne Laswell', 'week': 1},
    {'date': (2026, 3, 20), 'time': '09:05', 'student': 'Austin Lin', 'week': 1},
    {'date': (2026, 3, 25), 'time': '08:00', 'student': 'Boris Dudarev', 'week': 2},
    {'date': (2026, 3, 25), 'time': '08:35', 'student': 'Sydney Barba', 'week': 2},
    {'date': (2026, 3, 26), 'time': '08:20', 'student': 'Gus Castillo', 'week': 3},
    {'date': (2026, 3, 26), 'time': '09:35', 'student': 'Emma Cotner', 'week': 3},
    {'date': (2026, 3, 27), 'time': '08:00', 'student': 'Saeed Tarawneh', 'week': 3},
    {'date': (2026, 3, 27), 'time': '08:35', 'student': 'Jessica Owenby', 'week': 1},
    {'date': (2026, 3, 27), 'time': '09:05', 'student': 'Cruce Saunders IV', 'week': 1},
    {'date': (2026, 4, 1), 'time': '08:00', 'student': 'Zayen Szpitalak', 'week': 2},
    {'date': (2026, 4, 1), 'time': '08:35', 'student': 'Stella Cole', 'week': 2},
    {'date': (2026, 4, 1), 'time': '09:10', 'student': 'Branson Pfiester', 'week': 2},
    {'date': (2026, 4, 2), 'time': '08:20', 'student': 'Gus Castillo', 'week': 4},
    {'date': (2026, 4, 2), 'time': '09:35', 'student': 'Emma Cotner', 'week': 4},
    {'date': (2026, 4, 3), 'time': '08:00', 'student': 'Saeed Tarawneh', 'week': 4},
    {'date': (2026, 4, 3), 'time': '08:35', 'student': 'Kavin Lingham', 'week': 1},
    {'date': (2026, 4, 3), 'time': '09:05', 'student': 'Stella Grams', 'week': 1},
    {'date': (2026, 4, 7), 'time': '08:00', 'student': 'Sydney Barba', 'week': 3},
    {'date': (2026, 4, 8), 'time': '08:00', 'student': 'Jacob Kuchinsky', 'week': 1},
    {'date': (2026, 4, 8), 'time': '08:35', 'student': 'Luca Sanchez', 'week': 1},
    {'date': (2026, 4, 8), 'time': '09:05', 'student': 'Boris Dudarev', 'week': 3},
    {'date': (2026, 4, 9), 'time': '08:20', 'student': 'Gus Castillo', 'week': 5},
    {'date': (2026, 4, 9), 'time': '08:35', 'student': 'Aheli Shah', 'week': 2},
    {'date': (2026, 4, 9), 'time': '09:05', 'student': 'Ella Dietz', 'week': 2},
    {'date': (2026, 4, 10), 'time': '08:00', 'student': 'Jackson Price', 'week': 2},
    {'date': (2026, 4, 10), 'time': '08:35', 'student': 'Ali Romman', 'week': 1},
    {'date': (2026, 4, 10), 'time': '09:05', 'student': 'Benny Valles', 'week': 1},
    {'date': (2026, 4, 15), 'time': '08:00', 'student': 'Zayen Szpitalak', 'week': 3},
    {'date': (2026, 4, 15), 'time': '08:35', 'student': 'Stella Cole', 'week': 3},
    {'date': (2026, 4, 16), 'time': '08:20', 'student': 'Gus Castillo', 'week': 6},
    {'date': (2026, 4, 16), 'time': '08:35', 'student': 'Branson Pfiester', 'week': 3},
    {'date': (2026, 4, 16), 'time': '09:35', 'student': 'Emma Cotner', 'week': 5},
    {'date': (2026, 4, 17), 'time': '08:00', 'student': 'Saeed Tarawneh', 'week': 5},
    {'date': (2026, 4, 17), 'time': '08:35', 'student': 'Vera Li', 'week': 1},
    {'date': (2026, 4, 17), 'time': '09:05', 'student': 'Emily Smith', 'week': 1},
    {'date': (2026, 4, 28), 'time': '08:00', 'student': 'Stella Cole', 'week': 4},
    {'date': (2026, 4, 29), 'time': '08:00', 'student': 'Boris Dudarev', 'week': 4},
    {'date': (2026, 4, 29), 'time': '08:35', 'student': 'Sydney Barba', 'week': 4},
    {'date': (2026, 4, 29), 'time': '09:05', 'student': 'Zayen Szpitalak', 'week': 4},
    {'date': (2026, 4, 29), 'time': '09:40', 'student': 'Branson Pfiester', 'week': 4},
    {'date': (2026, 4, 30), 'time': '08:20', 'student': 'Gus Castillo', 'week': 7},
    {'date': (2026, 4, 30), 'time': '09:35', 'student': 'Emma Cotner', 'week': 6},
    {'date': (2026, 5, 1), 'time': '08:00', 'student': 'Saeed Tarawneh', 'week': 6},
    {'date': (2026, 5, 1), 'time': '08:35', 'student': 'Paty Margain-Junco', 'week': 1},
    {'date': (2026, 5, 1), 'time': '09:05', 'student': 'Michael Cai', 'week': 1},
]

EXAM_DATES = {
    'AP Human Geography': 'May 5',
    'AP World History': 'May 7',
    'AP US History': 'May 8',
    'AP US Government': 'May 11',
}

# =============================================================================
# HELPERS
# =============================================================================

def load_status():
    if STATUS_FILE.exists():
        with open(STATUS_FILE, 'r') as f:
            return json.load(f)
    return {'sent_messages': [], 'last_weekly': None, 'last_questions': None}

def save_status(status):
    with open(STATUS_FILE, 'w') as f:
        json.dump(status, f, indent=2, default=str)

def get_today():
    # For testing, you can override this
    return get_london_now().replace(tzinfo=None)

def get_calls_today():
    today = get_today().date()
    calls = []
    for c in SCHEDULE:
        call_date = datetime(*c['date']).date()
        if call_date == today:
            calls.append({
                **c,
                'date_obj': datetime(*c['date']),
                'info': STUDENTS.get(c['student'], {}),
                'time_london': convert_time_to_london(c['date'], c['time'])
            })
    return sorted(calls, key=lambda x: x['time'])

def get_calls_this_week():
    today = get_today()
    week_start = today - timedelta(days=today.weekday())
    week_end = week_start + timedelta(days=7)

    calls = []
    for c in SCHEDULE:
        call_date = datetime(*c['date'])
        if week_start <= call_date < week_end:
            calls.append({
                **c,
                'date_obj': call_date,
                'info': STUDENTS.get(c['student'], {}),
                'is_today': call_date.date() == today.date(),
                'time_london': convert_time_to_london(c['date'], c['time'])
            })
    return sorted(calls, key=lambda x: (x['date_obj'], x['time']))

def get_question_file_content(student_name, week):
    clean_name = student_name.lower().replace(" ", "_").replace("-", "_")
    filename = f"{clean_name}_week{week}.md"
    filepath = QUESTIONS_DIR / filename

    if filepath.exists():
        with open(filepath, 'r', encoding='utf-8') as f:
            return f.read(), filename
    return None, filename

def get_student_plan_content(student_name):
    filename = student_name.replace(" ", "_") + ".md"
    filepath = PLANS_DIR / filename

    if filepath.exists():
        with open(filepath, 'r', encoding='utf-8') as f:
            return f.read()
    return None

def get_student_plan_path(student_name):
    filename = student_name.replace(" ", "_") + ".md"
    return PLANS_DIR / filename

def get_call_records(student_name):
    """Parse call records from student's .md file."""
    content = get_student_plan_content(student_name)
    if not content:
        return []

    records = []
    in_records_section = False
    in_table = False

    for line in content.split('\n'):
        if '## Coaching Call Records' in line or '## Call Records' in line:
            in_records_section = True
            continue
        if in_records_section:
            if line.startswith('## '):  # New section, stop parsing
                break
            if line.startswith('|') and 'Date' in line:  # Header row
                in_table = True
                continue
            if line.startswith('|---'):  # Separator row
                continue
            if in_table and line.startswith('|'):
                parts = [p.strip() for p in line.split('|')[1:-1]]
                if len(parts) >= 4:
                    # Extract link from markdown if present
                    recording = parts[3] if len(parts) > 3 else ''
                    if '[' in recording and '](' in recording:
                        # Extract URL from [text](url)
                        import re
                        match = re.search(r'\[.*?\]\((.*?)\)', recording)
                        recording = match.group(1) if match else recording

                    records.append({
                        'date': parts[0],
                        'week': parts[1],
                        'attended': '✓' in parts[2] or 'Yes' in parts[2],
                        'recording': recording
                    })

    return records

def save_call_record(student_name, call_date, week, attended, recording_url=''):
    """Save a call record to the student's .md file."""
    filepath = get_student_plan_path(student_name)
    if not filepath.exists():
        return False, "Plan file not found"

    content = filepath.read_text(encoding='utf-8')

    # Format the new record
    date_str = call_date.strftime('%b %d, %Y')
    attended_str = '✓ Yes' if attended else '✗ No'
    recording_str = f'[Recording]({recording_url})' if recording_url else '-'
    new_row = f"| {date_str} | {week} | {attended_str} | {recording_str} |"

    # Check if Call Records section exists
    if '## Coaching Call Records' not in content and '## Call Records' not in content:
        # Add the section at the end
        records_section = f"""

---

## Coaching Call Records

| Date | Week | Attended | Recording |
|------|------|----------|-----------|
{new_row}
"""
        content = content.rstrip() + records_section
    else:
        # Find and update existing section
        lines = content.split('\n')
        new_lines = []
        in_records = False
        found_table = False
        record_added = False

        for i, line in enumerate(lines):
            if '## Coaching Call Records' in line or '## Call Records' in line:
                in_records = True
                new_lines.append(line)
                continue

            if in_records and line.startswith('|---'):
                found_table = True
                new_lines.append(line)
                # Check if we need to update existing record or add new one
                # Look ahead to see if this date/week combo exists
                existing_found = False
                for j in range(i+1, len(lines)):
                    if lines[j].startswith('|') and date_str in lines[j] and f'| {week} |' in lines[j]:
                        existing_found = True
                        break
                    if lines[j].startswith('## ') or not lines[j].startswith('|'):
                        break

                if not existing_found and not record_added:
                    new_lines.append(new_row)
                    record_added = True
                continue

            if in_records and found_table and line.startswith('|'):
                # Check if this is the row to update
                if date_str in line and f'| {week} |' in line:
                    new_lines.append(new_row)  # Replace with updated row
                    record_added = True
                    continue

            if in_records and line.startswith('## '):
                in_records = False

            new_lines.append(line)

        content = '\n'.join(new_lines)

    # Write back
    filepath.write_text(content, encoding='utf-8')
    return True, "Record saved"

def get_call_record(student_name, call_date, week):
    """Get a specific call record."""
    records = get_call_records(student_name)
    date_str = call_date.strftime('%b %d, %Y')

    for r in records:
        if r['date'] == date_str and str(r['week']) == str(week):
            return r
    return None

# =============================================================================
# SLACK FUNCTIONS
# =============================================================================

slack_client = None

def init_slack():
    global slack_client
    token = get_slack_token()
    if SLACK_AVAILABLE and token:
        try:
            slack_client = WebClient(token=token)
            # Test the connection
            slack_client.auth_test()
            return True
        except Exception as e:
            print(f"Slack connection failed: {e}")
            slack_client = None
            return False
    return False

def reconnect_slack():
    """Reconnect Slack with current token."""
    return init_slack()

def send_slack_dm(email, message):
    if not slack_client:
        return False, "Slack not configured"

    try:
        # Look up user by email
        response = slack_client.users_lookupByEmail(email=email)
        user_id = response['user']['id']

        # Send DM
        slack_client.chat_postMessage(channel=user_id, text=message)
        return True, "Sent"
    except SlackApiError as e:
        return False, str(e.response['error'])

def send_question_to_student(student_name, week):
    info = STUDENTS.get(student_name, {})
    content, filename = get_question_file_content(student_name, week)

    if not content:
        return False, f"No question file: {filename}"

    # Extract questions only (before rubric)
    questions_only = content.split('<details>')[0].strip()
    if len(questions_only) > 3500:
        questions_only = questions_only[:3500] + "\n\n_(truncated)_"

    message = f"Hey {student_name.split()[0]}! Here are your practice questions for our upcoming call:\n\n{questions_only}\n\nComplete these before we meet!"

    success, msg = send_slack_dm(info.get('email', ''), message)

    # Log it
    status = load_status()
    status['sent_messages'].append({
        'student': student_name,
        'type': 'questions',
        'week': week,
        'time': datetime.now().isoformat(),
        'success': success,
        'message': msg
    })
    save_status(status)

    return success, msg

# =============================================================================
# EMAIL FUNCTIONS
# =============================================================================

def get_email_config():
    """Get email SMTP configuration."""
    config = load_config()
    return {
        'smtp_server': config.get('smtp_server', 'smtp.gmail.com'),
        'smtp_port': config.get('smtp_port', 587),
        'smtp_username': config.get('smtp_username', ''),
        'smtp_password': config.get('smtp_password', ''),
        'from_email': config.get('from_email', ''),
        'from_name': config.get('from_name', 'AP Coaching')
    }

def is_email_configured():
    """Check if email is properly configured."""
    config = get_email_config()
    return bool(config['smtp_username'] and config['smtp_password'] and config['from_email'])

def send_email(to_email, subject, body_text, body_html=None):
    """Send an email using configured SMTP settings."""
    config = get_email_config()

    if not is_email_configured():
        return False, "Email not configured"

    try:
        msg = MIMEMultipart('alternative')
        msg['Subject'] = subject
        msg['From'] = f"{config['from_name']} <{config['from_email']}>"
        msg['To'] = to_email

        # Attach plain text version
        msg.attach(MIMEText(body_text, 'plain'))

        # Attach HTML version if provided
        if body_html:
            msg.attach(MIMEText(body_html, 'html'))

        # Connect and send
        with smtplib.SMTP(config['smtp_server'], config['smtp_port']) as server:
            server.starttls()
            server.login(config['smtp_username'], config['smtp_password'])
            server.send_message(msg)

        return True, "Sent"
    except smtplib.SMTPAuthenticationError:
        return False, "Authentication failed - check username/password"
    except smtplib.SMTPException as e:
        return False, f"SMTP error: {str(e)}"
    except Exception as e:
        return False, str(e)

def send_question_via_email(student_name, week):
    """Send question file to student via email."""
    info = STUDENTS.get(student_name, {})
    content, filename = get_question_file_content(student_name, week)

    if not content:
        return False, f"No question file: {filename}"

    # Extract questions only (before rubric)
    questions_only = content.split('<details')[0].strip()

    # Create email subject and body
    first_name = student_name.split()[0]
    subject = f"AP Practice Questions - Week {week}"

    body_text = f"""Hey {first_name}!

Here are your practice questions for our upcoming coaching call:

{questions_only}

Complete these before we meet!

Best,
AP Coaching
"""

    # Simple HTML version
    body_html = f"""
    <html>
    <body style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; padding: 20px;">
        <h2>Hey {first_name}!</h2>
        <p>Here are your practice questions for our upcoming coaching call:</p>
        <div style="background: #f5f5f5; padding: 20px; border-radius: 8px; margin: 20px 0;">
            <pre style="white-space: pre-wrap; font-family: inherit;">{questions_only}</pre>
        </div>
        <p>Complete these before we meet!</p>
        <p>Best,<br>AP Coaching</p>
    </body>
    </html>
    """

    success, msg = send_email(info.get('email', ''), subject, body_text, body_html)

    # Log it
    status = load_status()
    status['sent_messages'].append({
        'student': student_name,
        'type': 'email-questions',
        'week': week,
        'time': datetime.now().isoformat(),
        'success': success,
        'message': msg
    })
    save_status(status)

    return success, msg

def send_plan_intro_email(student_name):
    """Send the coaching plan introduction email to a student."""
    info = STUDENTS.get(student_name, {})
    if not info:
        return False, "Student not found"

    first_name = student_name.split()[0]
    course = info.get('course', 'AP Social Science')
    exam_date = EXAM_DATES.get(course, 'May')

    # Get their plan content
    plan_content = get_student_plan_content(student_name)
    if not plan_content:
        return False, "No plan file found"

    # Clean up markdown for email (basic conversion)
    plan_text = plan_content.replace('# ', '\n').replace('## ', '\n').replace('### ', '\n')
    plan_text = plan_text.replace('**', '').replace('*', '').replace('`', '')

    subject = f"Your {course} Coaching Plan - Let's Get You Ready!"

    body_text = f"""Hey {first_name}!

I'm Adam, and I'll be your coach for {course} as we prepare for your AP exam on {exam_date}.

I've put together a personalized coaching plan just for you based on your current progress and the areas where we can make the biggest impact before exam day.

HERE'S WHAT THIS PLAN INCLUDES:
- Your current standing and target score
- A week-by-week breakdown of what we'll cover
- Scheduled coaching calls (you'll get calendar invites)
- Practice questions I'll send before each call

WHAT YOU NEED TO DO:
1. Review your plan below so you know what to expect
2. Accept the calendar invites when they arrive
3. Complete the practice questions I send before each coaching call
4. Come to calls ready to discuss your answers and ask questions

I'll send you practice questions the morning before each call, giving you plenty of time to work through them.

Let's do this! Looking forward to working with you.

- Adam

---

YOUR PERSONALIZED COACHING PLAN:

{plan_text}
"""

    # HTML version
    plan_html = plan_content.replace('\n', '<br>')

    body_html = f"""
    <html>
    <body style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; padding: 20px; max-width: 700px; margin: 0 auto;">
        <h2 style="color: #667eea;">Hey {first_name}!</h2>

        <p>I'm <strong>Adam</strong>, and I'll be your coach for <strong>{course}</strong> as we prepare for your AP exam on <strong>{exam_date}</strong>.</p>

        <p>I've put together a personalized coaching plan just for you based on your current progress and the areas where we can make the biggest impact before exam day.</p>

        <div style="background: #f0f4ff; padding: 20px; border-radius: 8px; margin: 20px 0;">
            <h3 style="color: #667eea; margin-top: 0;">Here's What This Plan Includes:</h3>
            <ul>
                <li>Your current standing and target score</li>
                <li>A week-by-week breakdown of what we'll cover</li>
                <li>Scheduled coaching calls (you'll get calendar invites)</li>
                <li>Practice questions I'll send before each call</li>
            </ul>
        </div>

        <div style="background: #f0fff4; padding: 20px; border-radius: 8px; margin: 20px 0;">
            <h3 style="color: #059669; margin-top: 0;">What You Need To Do:</h3>
            <ol>
                <li><strong>Review your plan below</strong> so you know what to expect</li>
                <li><strong>Accept the calendar invites</strong> when they arrive</li>
                <li><strong>Complete the practice questions</strong> I send before each coaching call</li>
                <li><strong>Come to calls ready</strong> to discuss your answers and ask questions</li>
            </ol>
        </div>

        <p>I'll send you practice questions the morning before each call, giving you plenty of time to work through them.</p>

        <p><strong>Let's do this!</strong> Looking forward to working with you.</p>

        <p>- Adam</p>

        <hr style="border: none; border-top: 2px solid #667eea; margin: 30px 0;">

        <h2 style="color: #667eea;">Your Personalized Coaching Plan</h2>

        <div style="background: #fafafa; padding: 20px; border-radius: 8px; border-left: 4px solid #667eea;">
            {plan_html}
        </div>
    </body>
    </html>
    """

    success, msg = send_email(info.get('email', ''), subject, body_text, body_html)

    # Log it
    status = load_status()
    status['sent_messages'].append({
        'student': student_name,
        'type': 'plan-intro',
        'time': datetime.now().isoformat(),
        'success': success,
        'message': msg
    })
    save_status(status)

    return success, msg

def send_all_plan_intros():
    """Send plan intro emails to all students."""
    results = {'sent': [], 'failed': [], 'skipped': []}

    status = load_status()
    already_sent = set()
    for msg in status.get('sent_messages', []):
        if msg.get('type') == 'plan-intro' and msg.get('success'):
            already_sent.add(msg.get('student'))

    for student_name in STUDENTS.keys():
        if student_name in already_sent:
            results['skipped'].append(student_name)
            continue

        success, msg = send_plan_intro_email(student_name)
        if success:
            results['sent'].append(student_name)
        else:
            results['failed'].append((student_name, msg))

    return results

def send_question_smart(student_name, week):
    """Try Slack first, fall back to email if Slack unavailable or fails."""
    method_used = None

    # Try Slack first if configured
    if slack_client:
        success, msg = send_question_to_student(student_name, week)
        if success:
            return True, "Sent via Slack", "slack"
        # Slack failed - try email as fallback
        method_used = f"Slack failed ({msg})"
    else:
        method_used = "Slack not configured"

    # Fall back to email
    if is_email_configured():
        success, msg = send_question_via_email(student_name, week)
        if success:
            return True, f"{method_used}, sent via Email", "email"
        return False, f"{method_used}, Email also failed: {msg}", None

    return False, f"{method_used}, Email not configured", None

# =============================================================================
# AUTO-SEND SCHEDULER
# =============================================================================

import time
from threading import Thread, Event

scheduler_stop = Event()
scheduler_log = []  # Recent scheduler activity for dashboard display

def log_scheduler(message):
    """Log scheduler activity."""
    entry = {'time': datetime.now().isoformat(), 'message': message}
    scheduler_log.append(entry)
    # Keep only last 50 entries
    while len(scheduler_log) > 50:
        scheduler_log.pop(0)
    print(f"[Scheduler] {message}")

def get_calls_tomorrow():
    """Get calls that need questions sent today (accounting for weekends).

    - Friday: returns Monday's calls (send before weekend)
    - Saturday/Sunday: returns nothing (already sent Friday)
    - Other days: returns tomorrow's calls
    """
    today = get_today()
    weekday = today.weekday()  # 0=Mon, 4=Fri, 5=Sat, 6=Sun

    if weekday == 4:  # Friday - send for Monday
        target_date = (today + timedelta(days=3)).date()
    elif weekday in (5, 6):  # Weekend - nothing to send
        return []
    else:  # Mon-Thu - send for tomorrow
        target_date = (today + timedelta(days=1)).date()

    calls = []
    for c in SCHEDULE:
        call_date = datetime(*c['date']).date()
        if call_date == target_date:
            calls.append({
                **c,
                'date_obj': datetime(*c['date']),
                'info': STUDENTS.get(c['student'], {})
            })
    return calls

def get_calls_for_week(week_start_date):
    """Get all calls for a specific week."""
    week_end = week_start_date + timedelta(days=7)
    calls = []
    for c in SCHEDULE:
        call_date = datetime(*c['date']).date()
        if week_start_date <= call_date < week_end:
            calls.append({
                **c,
                'date_obj': datetime(*c['date']),
                'info': STUDENTS.get(c['student'], {})
            })
    return calls

def was_questions_sent(student_name, week, call_date):
    """Check if questions were already sent for this specific call."""
    status = load_status()
    call_date_str = call_date.strftime('%Y-%m-%d')

    for msg in status.get('sent_messages', []):
        if (msg.get('student') == student_name and
            msg.get('week') == week and
            msg.get('type') in ('questions', 'email-questions', 'auto-questions') and
            msg.get('success') == True and
            msg.get('call_date', msg.get('time', '')[:10]) == call_date_str):
            return True
    return False

def was_weekly_sent(student_name, week_start_str):
    """Check if weekly reminder was sent this week for this student."""
    status = load_status()

    for msg in status.get('sent_messages', []):
        if (msg.get('student') == student_name and
            msg.get('type') == 'weekly-reminder' and
            msg.get('success') == True and
            msg.get('week_start') == week_start_str):
            return True
    return False

def send_weekly_reminder(student_name, calls_this_week):
    """Send weekly schedule reminder to a student."""
    info = STUDENTS.get(student_name, {})
    first_name = student_name.split()[0]

    # Build schedule summary
    schedule_lines = []
    for call in sorted(calls_this_week, key=lambda x: (x['date_obj'], x['time'])):
        day = call['date_obj'].strftime('%A %b %d')
        schedule_lines.append(f"  • {day} at {call['time']} CT - Week {call['week']}")

    schedule_text = "\n".join(schedule_lines)

    message = f"""Hey {first_name}!

Here's your AP coaching schedule for this week:

{schedule_text}

I'll send you practice questions the day before each call. See you soon!"""

    # Try Slack first, fall back to email
    success = False
    method = None

    if slack_client:
        success, msg = send_slack_dm(info.get('email', ''), message)
        if success:
            method = 'slack'

    if not success and is_email_configured():
        subject = f"Your AP Coaching Schedule This Week"
        success, msg = send_email(info.get('email', ''), subject, message)
        if success:
            method = 'email'

    return success, method

# =============================================================================
# BRANSON SPECIAL WEEKLY FRQ PRACTICE
# =============================================================================

BRANSON_FRQS = [
    # Week 1 - Unit 1 & 2 basics
    """**FRQ Practice - Week 1**

**Question 1:** Define the concept of "sense of place" and explain how it differs from "location." Provide a specific example of how sense of place develops in a community.

**Question 2:** Explain how the friction of distance affects human interaction. Give an example of how technology has reduced friction of distance in the modern world.

**Question 3:** Define "scale of analysis" in geography. Explain why the same phenomenon might be interpreted differently at local vs. global scales, using urbanization as your example.""",

    # Week 2 - Population & Migration
    """**FRQ Practice - Week 2**

**Question 1:** Using the Demographic Transition Model, explain why Stage 2 countries experience rapid population growth. Identify ONE country currently in Stage 2 and describe its demographic characteristics.

**Question 2:** Distinguish between push factors and pull factors in migration. Provide TWO specific examples of each and explain how they interact to cause migration flows.

**Question 3:** Define "chain migration" and explain how it creates ethnic enclaves. Use a specific real-world example to illustrate your answer.""",

    # Week 3 - Culture
    """**FRQ Practice - Week 3**

**Question 1:** Define "cultural diffusion" and explain the difference between relocation diffusion and expansion diffusion. Provide a specific example of each type.

**Question 2:** Explain how language can act as both a unifying and a divisive force within a country. Use a specific country example to support your answer.

**Question 3:** Define "acculturation" and "assimilation." Explain why immigrant communities might resist full assimilation while still undergoing acculturation.""",

    # Week 4 - Political
    """**FRQ Practice - Week 4**

**Question 1:** Define "sovereignty" and explain TWO challenges to state sovereignty in the modern world. Provide specific examples.

**Question 2:** Explain how gerrymandering affects political representation. Describe TWO techniques used in gerrymandering and their effects.

**Question 3:** Define "supranationalism" and explain how the European Union exemplifies this concept. Identify ONE benefit and ONE challenge of supranational organizations.""",

    # Week 5 - Agriculture
    """**FRQ Practice - Week 5**

**Question 1:** Using the Von Thünen model, explain why market gardening is located closest to the city center. Identify TWO assumptions of the model and explain how real-world conditions might alter the pattern.

**Question 2:** Define the "Green Revolution" and explain TWO positive and TWO negative consequences of its implementation in developing countries.

**Question 3:** Distinguish between subsistence agriculture and commercial agriculture. Explain how globalization is changing traditional subsistence farming practices.""",

    # Week 6 - Urban
    """**FRQ Practice - Week 6**

**Question 1:** Compare and contrast the Burgess Concentric Zone Model with the Hoyt Sector Model. Explain why the Hoyt model might better explain modern urban patterns.

**Question 2:** Define "gentrification" and explain its effects on both the built environment and the existing community. Provide a specific example.

**Question 3:** Explain the concept of "urban sprawl" and identify TWO environmental consequences and TWO social consequences of this development pattern.""",

    # Week 7 - Industry & Development
    """**FRQ Practice - Week 7**

**Question 1:** Using Rostow's Stages of Economic Growth model, explain the characteristics of the "take-off" stage. Identify ONE criticism of this model.

**Question 2:** Define the Human Development Index (HDI) and explain why it is considered a better measure of development than GDP per capita alone.

**Question 3:** Explain how the "new international division of labor" has changed manufacturing patterns globally. Provide specific examples of countries affected.""",

    # Week 8 - Review
    """**FRQ Practice - Week 8 (Comprehensive Review)**

**Question 1:** Explain how population growth, urbanization, and industrialization are interconnected. Use the Demographic Transition Model and a specific country example in your response.

**Question 2:** Choose ONE model from the course (DTM, Von Thünen, Burgess, Hoyt, or Rostow) and explain its key concepts, assumptions, and ONE major limitation.

**Question 3:** Explain how globalization has affected cultural diffusion patterns. Provide specific examples of both cultural homogenization and cultural resistance."""
]

def was_branson_frq_sent(week_str):
    """Check if Branson's weekly FRQ was sent this week."""
    status = load_status()
    for msg in status.get('sent_messages', []):
        if (msg.get('student') == 'Branson Pfiester' and
            msg.get('type') == 'weekly-frq-practice' and
            msg.get('success') == True and
            msg.get('week_str') == week_str):
            return True
    return False

def get_branson_frq_week():
    """Calculate which FRQ week we're on (1-8, cycling)."""
    # Start from March 9, 2026 (week 1)
    start_date = datetime(2026, 3, 9).date()
    today = get_today().date()
    weeks_elapsed = (today - start_date).days // 7
    return (weeks_elapsed % 8) + 1  # Cycles 1-8

def send_branson_weekly_frqs():
    """Send weekly FRQ practice to Branson."""
    info = STUDENTS.get('Branson Pfiester', {})
    frq_week = get_branson_frq_week()

    # Get the FRQs for this week (0-indexed)
    frq_content = BRANSON_FRQS[frq_week - 1]

    message = f"""Hey Branson!

Here are your 3 FRQ practice questions for this week. Remember: define, explain, give example.

{frq_content}

**Instructions:**
1. Set a timer for 15 minutes per question (45 min total)
2. Write out FULL responses - no outlines
3. Check your answers against the rubric
4. Note which points you missed

Your FRQ score on the practice test was 24% - these weekly practices will get that up! Consistency is key.

- Adam"""

    # Try Slack first, fall back to email
    success = False
    method = None

    if slack_client:
        success, msg = send_slack_dm(info.get('email', ''), message)
        if success:
            method = 'slack'

    if not success and is_email_configured():
        subject = f"Weekly FRQ Practice - Week {frq_week}"
        success, msg = send_email(info.get('email', ''), subject, message)
        if success:
            method = 'email'

    return success, method, frq_week

def auto_send_branson_frqs():
    """Send Branson's weekly FRQs (Monday)."""
    today = get_today()
    week_start = (today - timedelta(days=today.weekday())).date()
    week_str = week_start.strftime('%Y-%m-%d')

    if was_branson_frq_sent(week_str):
        log_scheduler("Branson weekly FRQs: Already sent this week")
        return

    log_scheduler("Sending Branson's weekly FRQ practice...")
    success, method, frq_week = send_branson_weekly_frqs()

    # Log it
    status = load_status()
    status['sent_messages'].append({
        'student': 'Branson Pfiester',
        'type': 'weekly-frq-practice',
        'week_str': week_str,
        'frq_week': frq_week,
        'time': datetime.now().isoformat(),
        'success': success,
        'message': f"Weekly FRQs Week {frq_week} via {method}" if success else "Failed",
        'method': method
    })
    save_status(status)

    if success:
        log_scheduler(f"  Branson: FRQ Week {frq_week} sent via {method}")
    else:
        log_scheduler(f"  Branson: FRQ send FAILED")

def auto_send_questions_for_tomorrow():
    """Send questions to students with upcoming calls (accounting for weekends)."""
    today = get_today()
    weekday = today.weekday()

    # Determine what we're sending for
    if weekday == 4:
        target_desc = "Monday (sending Friday before weekend)"
    elif weekday in (5, 6):
        target_desc = "weekend - skipping"
    else:
        target_desc = "tomorrow"

    tomorrow_calls = get_calls_tomorrow()

    if not tomorrow_calls:
        log_scheduler(f"No calls for {target_desc} - nothing to send")
        return

    log_scheduler(f"Checking {len(tomorrow_calls)} calls for {target_desc}...")

    for call in tomorrow_calls:
        student = call['student']
        week = call['week']
        call_date = call['date_obj'].date()

        if was_questions_sent(student, week, call_date):
            log_scheduler(f"  {student} Week {week}: Already sent")
            continue

        # Send questions
        success, msg, method = send_question_smart(student, week)

        # Log with call_date for tracking
        status = load_status()
        status['sent_messages'].append({
            'student': student,
            'type': 'auto-questions',
            'week': week,
            'call_date': call_date.strftime('%Y-%m-%d'),
            'time': datetime.now().isoformat(),
            'success': success,
            'message': f"Auto-sent: {msg}",
            'method': method
        })
        save_status(status)

        if success:
            log_scheduler(f"  {student} Week {week}: Sent via {method}")
        else:
            log_scheduler(f"  {student} Week {week}: FAILED - {msg}")

def auto_send_weekly_reminders():
    """Send weekly reminders to all students with calls this week (Monday)."""
    today = get_today()
    week_start = (today - timedelta(days=today.weekday())).date()
    week_start_str = week_start.strftime('%Y-%m-%d')

    # Get all students with calls this week
    week_calls = get_calls_for_week(week_start)

    # Group by student
    students_calls = {}
    for call in week_calls:
        student = call['student']
        if student not in students_calls:
            students_calls[student] = []
        students_calls[student].append(call)

    if not students_calls:
        log_scheduler("No calls this week - no reminders to send")
        return

    log_scheduler(f"Checking weekly reminders for {len(students_calls)} students...")

    for student, calls in students_calls.items():
        if was_weekly_sent(student, week_start_str):
            log_scheduler(f"  {student}: Weekly already sent")
            continue

        success, method = send_weekly_reminder(student, calls)

        # Log it
        status = load_status()
        status['sent_messages'].append({
            'student': student,
            'type': 'weekly-reminder',
            'week_start': week_start_str,
            'time': datetime.now().isoformat(),
            'success': success,
            'message': f"Weekly reminder via {method}" if success else "Failed to send",
            'method': method
        })
        save_status(status)

        if success:
            log_scheduler(f"  {student}: Weekly sent via {method}")
        else:
            log_scheduler(f"  {student}: Weekly FAILED")

def catchup_check():
    """
    Check if we missed any sends and catch up.
    - Questions should be sent day before call (Friday for Monday calls)
    - Weekly reminders should be sent on Monday
    - Branson's weekly FRQs should be sent on Monday
    """
    today = get_today()
    log_scheduler("Running catch-up check...")

    # Check for any calls today that don't have questions sent
    todays_calls = get_calls_today()
    for call in todays_calls:
        student = call['student']
        week = call['week']
        call_date = call['date_obj'].date()

        if not was_questions_sent(student, week, call_date):
            log_scheduler(f"  CATCH-UP: {student} has call TODAY but questions not sent!")
            success, msg, method = send_question_smart(student, week)

            status = load_status()
            status['sent_messages'].append({
                'student': student,
                'type': 'auto-questions',
                'week': week,
                'call_date': call_date.strftime('%Y-%m-%d'),
                'time': datetime.now().isoformat(),
                'success': success,
                'message': f"CATCH-UP: {msg}",
                'method': method
            })
            save_status(status)

            log_scheduler(f"    -> {'Sent' if success else 'FAILED'}")

    # Check for tomorrow's calls too
    auto_send_questions_for_tomorrow()

    # If it's Monday (or later in week and weekly not sent), send weekly reminders
    week_start = (today - timedelta(days=today.weekday())).date()
    week_start_str = week_start.strftime('%Y-%m-%d')

    week_calls = get_calls_for_week(week_start)
    students_this_week = set(c['student'] for c in week_calls)

    for student in students_this_week:
        if not was_weekly_sent(student, week_start_str):
            log_scheduler(f"  CATCH-UP: {student} missing weekly reminder")
            student_calls = [c for c in week_calls if c['student'] == student]
            success, method = send_weekly_reminder(student, student_calls)

            status = load_status()
            status['sent_messages'].append({
                'student': student,
                'type': 'weekly-reminder',
                'week_start': week_start_str,
                'time': datetime.now().isoformat(),
                'success': success,
                'message': f"CATCH-UP weekly via {method}" if success else "CATCH-UP failed",
                'method': method
            })
            save_status(status)

    # Branson's weekly FRQs catch-up
    if not was_branson_frq_sent(week_start_str):
        log_scheduler("  CATCH-UP: Branson missing weekly FRQs")
        success, method, frq_week = send_branson_weekly_frqs()

        status = load_status()
        status['sent_messages'].append({
            'student': 'Branson Pfiester',
            'type': 'weekly-frq-practice',
            'week_str': week_start_str,
            'frq_week': frq_week,
            'time': datetime.now().isoformat(),
            'success': success,
            'message': f"CATCH-UP FRQs Week {frq_week} via {method}" if success else "CATCH-UP failed",
            'method': method
        })
        save_status(status)

def scheduler_loop():
    """Background scheduler that runs periodically."""
    log_scheduler("Scheduler started")

    # Run catch-up immediately on start
    try:
        catchup_check()
    except Exception as e:
        log_scheduler(f"Catch-up error: {e}")

    last_daily_check = None

    while not scheduler_stop.is_set():
        now = datetime.now()
        today_str = now.strftime('%Y-%m-%d')

        # Run daily check once per day (around 8 AM for next-day questions)
        # Gives students all day + evening to work on them
        if last_daily_check != today_str:
            hour = now.hour
            # Run at 8 AM or later (catch up if we missed it)
            if hour >= 8:
                try:
                    log_scheduler(f"Daily auto-send check ({now.strftime('%H:%M')})")
                    auto_send_questions_for_tomorrow()

                    # Monday check for weekly reminders + Branson FRQs
                    if now.weekday() == 0:  # Monday
                        auto_send_weekly_reminders()
                        auto_send_branson_frqs()

                    last_daily_check = today_str
                except Exception as e:
                    log_scheduler(f"Daily check error: {e}")

        # Sleep for 30 minutes between checks
        scheduler_stop.wait(1800)

    log_scheduler("Scheduler stopped")

def start_scheduler():
    """Start the background scheduler thread."""
    thread = Thread(target=scheduler_loop, daemon=True)
    thread.start()
    return thread

# =============================================================================
# HTML TEMPLATE
# =============================================================================

DASHBOARD_HTML = """
<!DOCTYPE html>
<html>
<head>
    <title>AP Coaching Dashboard</title>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <style>
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background: #f5f5f5;
            color: #333;
            line-height: 1.5;
        }
        .container { max-width: 1200px; margin: 0 auto; padding: 20px; }

        header {
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            color: white;
            padding: 30px;
            margin-bottom: 30px;
            border-radius: 12px;
        }
        header h1 { font-size: 2em; margin-bottom: 5px; }
        header .date { opacity: 0.9; font-size: 1.1em; }

        .stats {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
            gap: 20px;
            margin-bottom: 30px;
        }
        .stat-card {
            background: white;
            padding: 20px;
            border-radius: 12px;
            box-shadow: 0 2px 8px rgba(0,0,0,0.1);
        }
        .stat-card .number { font-size: 2.5em; font-weight: bold; color: #667eea; }
        .stat-card .label { color: #666; font-size: 0.9em; }

        .section { margin-bottom: 30px; }
        .section h2 {
            font-size: 1.4em;
            margin-bottom: 15px;
            padding-bottom: 10px;
            border-bottom: 2px solid #667eea;
        }

        .calls-grid { display: grid; gap: 15px; }
        .call-card {
            background: white;
            border-radius: 12px;
            padding: 20px;
            box-shadow: 0 2px 8px rgba(0,0,0,0.1);
            display: grid;
            grid-template-columns: auto 1fr auto;
            gap: 20px;
            align-items: center;
        }
        .call-card.today { border-left: 4px solid #667eea; }
        .call-card.upcoming { border-left: 4px solid #10b981; }

        .call-time {
            background: #f0f0f0;
            padding: 10px 15px;
            border-radius: 8px;
            text-align: center;
            min-width: 80px;
        }
        .call-time .time { font-size: 1.3em; font-weight: bold; }
        .call-time .day { font-size: 0.8em; color: #666; }

        .call-info h3 { font-size: 1.1em; margin-bottom: 5px; }
        .call-info .course { color: #666; font-size: 0.9em; }
        .call-info .tier {
            display: inline-block;
            padding: 2px 8px;
            border-radius: 4px;
            font-size: 0.75em;
            font-weight: bold;
            margin-top: 5px;
        }
        .tier.Critical { background: #fee2e2; color: #dc2626; }
        .tier.Intensive { background: #fef3c7; color: #d97706; }
        .tier.Moderate { background: #dbeafe; color: #2563eb; }
        .tier.Light { background: #d1fae5; color: #059669; }
        .tier.Maintenance { background: #f3f4f6; color: #6b7280; }

        .call-actions { display: flex; gap: 10px; }
        .btn {
            padding: 8px 16px;
            border: none;
            border-radius: 6px;
            cursor: pointer;
            font-size: 0.85em;
            text-decoration: none;
            display: inline-block;
        }
        .btn-primary { background: #667eea; color: white; }
        .btn-primary:hover { background: #5a67d8; }
        .btn-secondary { background: #e5e7eb; color: #374151; }
        .btn-secondary:hover { background: #d1d5db; }
        .btn-success { background: #10b981; color: white; }
        .btn-warning { background: #f59e0b; color: white; }

        .status-bar {
            background: white;
            padding: 15px 20px;
            border-radius: 12px;
            margin-bottom: 20px;
            display: flex;
            justify-content: space-between;
            align-items: center;
            box-shadow: 0 2px 8px rgba(0,0,0,0.1);
        }
        .status-item { display: flex; align-items: center; gap: 8px; }
        .status-dot { width: 10px; height: 10px; border-radius: 50%; }
        .status-dot.green { background: #10b981; }
        .status-dot.yellow { background: #f59e0b; }
        .status-dot.red { background: #ef4444; }

        .message-log {
            background: white;
            border-radius: 12px;
            padding: 20px;
            box-shadow: 0 2px 8px rgba(0,0,0,0.1);
            max-height: 300px;
            overflow-y: auto;
        }
        .log-entry {
            padding: 10px;
            border-bottom: 1px solid #f0f0f0;
            display: flex;
            justify-content: space-between;
        }
        .log-entry:last-child { border-bottom: none; }
        .log-entry .success { color: #10b981; }
        .log-entry .failed { color: #ef4444; }

        .modal {
            display: none;
            position: fixed;
            top: 0; left: 0; right: 0; bottom: 0;
            background: rgba(0,0,0,0.5);
            z-index: 1000;
            align-items: center;
            justify-content: center;
        }
        .modal.active { display: flex; }
        .modal-content {
            background: white;
            border-radius: 12px;
            padding: 30px;
            max-width: 800px;
            max-height: 80vh;
            overflow-y: auto;
            width: 90%;
        }
        .modal-content h2 { margin-bottom: 20px; }
        .modal-content pre {
            background: #f5f5f5;
            padding: 15px;
            border-radius: 8px;
            overflow-x: auto;
            font-size: 0.85em;
        }
        .close-btn {
            float: right;
            font-size: 1.5em;
            cursor: pointer;
            color: #666;
        }

        .no-calls {
            text-align: center;
            padding: 40px;
            color: #666;
        }
    </style>
</head>
<body>
    <div class="container">
        <header>
            <h1>AP Coaching Dashboard</h1>
            <div class="date">{{ today.strftime('%A, %B %d, %Y') }} | Your time: {{ london_time }}</div>
        </header>

        <div class="status-bar">
            <div class="status-item">
                <span class="status-dot {{ 'green' if slack_connected else 'red' }}"></span>
                <a href="/settings" style="color: inherit; text-decoration: none;">
                    Slack: {{ 'Connected' if slack_connected else 'Not configured' }}
                </a>
            </div>
            <div class="status-item">
                <span class="status-dot {{ 'green' if email_configured else 'yellow' }}"></span>
                <a href="/settings" style="color: inherit; text-decoration: none;">
                    Email: {{ 'Configured' if email_configured else 'Not configured' }}
                </a>
            </div>
            <div class="status-item">
                <span>{{ calls_today|length }} calls today</span>
            </div>
            <div class="status-item">
                <span>{{ calls_week|length }} calls this week</span>
            </div>
            <div>
                <a href="/interactions" class="btn btn-secondary">Interactions</a>
                <a href="/scheduler" class="btn btn-secondary">Scheduler</a>
                <a href="/settings" class="btn btn-secondary">Settings</a>
            </div>
        </div>

        <div class="stats">
            <div class="stat-card">
                <div class="number">{{ calls_today|length }}</div>
                <div class="label">Calls Today</div>
            </div>
            <div class="stat-card">
                <div class="number">{{ calls_week|length }}</div>
                <div class="label">Calls This Week</div>
            </div>
            <div class="stat-card">
                <div class="number">{{ messages_sent_today }}</div>
                <div class="label">Messages Sent Today</div>
            </div>
            <div class="stat-card">
                <div class="number">63</div>
                <div class="label">Total Scheduled Calls</div>
            </div>
        </div>

        <div class="section">
            <h2>Today's Calls</h2>
            <div class="calls-grid">
                {% if calls_today %}
                    {% for call in calls_today %}
                    <div class="call-card today">
                        <div class="call-time">
                            <div class="time">{{ call.time_london }}</div>
                            <div class="day">UK</div>
                            <div style="font-size: 0.7em; color: #999; margin-top: 2px;">{{ call.time }} CT</div>
                        </div>
                        <div class="call-info">
                            <h3>{{ call.student }}</h3>
                            <div class="course">{{ call.info.course }} - Week {{ call.week }}</div>
                            <span class="tier {{ call.info.tier }}">{{ call.info.tier }}</span>
                        </div>
                        <div class="call-actions">
                            <a href="/student/{{ call.student|urlencode }}" class="btn btn-secondary">View Plan</a>
                            <a href="/questions/{{ call.student|urlencode }}/{{ call.week }}" class="btn btn-secondary">Questions</a>
                            <a href="/send/{{ call.student|urlencode }}/{{ call.week }}" class="btn btn-primary">Send Questions</a>
                            <a href="/record/{{ call.student|urlencode }}/{{ call.week }}" class="btn btn-success">Record</a>
                            <a href="/reschedule/{{ call.student|urlencode }}/{{ call.week }}" class="btn btn-warning" title="Reschedule">🗓</a>
                        </div>
                    </div>
                    {% endfor %}
                {% else %}
                    <div class="no-calls">No calls scheduled for today</div>
                {% endif %}
            </div>
        </div>

        <div class="section">
            <h2>This Week</h2>
            <div class="calls-grid">
                {% for call in calls_week %}
                <div class="call-card {{ 'today' if call.is_today else 'upcoming' }}">
                    <div class="call-time">
                        <div class="time">{{ call.time_london }}</div>
                        <div class="day">{{ call.date_obj.strftime('%a %d') }}</div>
                        <div style="font-size: 0.65em; color: #999;">{{ call.time }} CT</div>
                    </div>
                    <div class="call-info">
                        <h3>{{ call.student }}</h3>
                        <div class="course">{{ call.info.course }} - Week {{ call.week }}</div>
                        <span class="tier {{ call.info.tier }}">{{ call.info.tier }}</span>
                    </div>
                    <div class="call-actions">
                        <a href="/student/{{ call.student|urlencode }}" class="btn btn-secondary">Plan</a>
                        <a href="/questions/{{ call.student|urlencode }}/{{ call.week }}" class="btn btn-secondary">Q's</a>
                        <a href="/record/{{ call.student|urlencode }}/{{ call.week }}" class="btn btn-secondary" title="Record attendance">📝</a>
                        <a href="/reschedule/{{ call.student|urlencode }}/{{ call.week }}" class="btn btn-secondary" title="Reschedule">🗓</a>
                    </div>
                </div>
                {% endfor %}
            </div>
        </div>

        <div class="section">
            <h2>Message Log (Today)</h2>
            <div class="message-log">
                {% if recent_messages %}
                    {% for msg in recent_messages %}
                    <div class="log-entry">
                        <span>{{ msg.time[:16] }} - {{ msg.student }} ({{ msg.type }})</span>
                        <span class="{{ 'success' if msg.success else 'failed' }}">
                            {{ 'Sent' if msg.success else msg.message }}
                        </span>
                    </div>
                    {% endfor %}
                {% else %}
                    <div class="no-calls">No messages sent today</div>
                {% endif %}
            </div>
        </div>
    </div>
</body>
</html>
"""

STUDENT_HTML = """
<!DOCTYPE html>
<html>
<head>
    <title>{{ student_name }} - AP Coaching</title>
    <meta charset="utf-8">
    <style>
        * { box-sizing: border-box; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background: #f5f5f5;
            padding: 20px;
            line-height: 1.6;
        }
        .container { max-width: 900px; margin: 0 auto; }
        .back { margin-bottom: 20px; }
        .back a { color: #667eea; text-decoration: none; }
        .card {
            background: white;
            border-radius: 12px;
            padding: 30px;
            box-shadow: 0 2px 8px rgba(0,0,0,0.1);
            margin-bottom: 20px;
        }
        h1 { color: #333; margin-bottom: 10px; }
        .meta { color: #666; margin-bottom: 20px; padding-bottom: 15px; border-bottom: 1px solid #eee; }
        .btn {
            padding: 10px 20px;
            background: #667eea;
            color: white;
            border: none;
            border-radius: 6px;
            cursor: pointer;
            text-decoration: none;
            display: inline-block;
            margin-right: 10px;
            margin-bottom: 10px;
        }
        /* Markdown content styling */
        .md-content h1 { font-size: 1.5em; color: #667eea; border-bottom: 2px solid #667eea; padding-bottom: 8px; margin-top: 25px; }
        .md-content h2 { font-size: 1.25em; color: #333; margin-top: 20px; }
        .md-content h3 { font-size: 1.1em; color: #555; }
        .md-content table { border-collapse: collapse; width: 100%; margin: 15px 0; }
        .md-content th, .md-content td { border: 1px solid #ddd; padding: 10px 12px; text-align: left; }
        .md-content th { background: #f5f5f5; font-weight: 600; }
        .md-content tr:nth-child(even) { background: #fafafa; }
        .md-content ul, .md-content ol { margin: 10px 0; padding-left: 25px; }
        .md-content li { margin: 5px 0; }
        .md-content code { background: #f0f0f0; padding: 2px 6px; border-radius: 4px; font-size: 0.9em; }
        .md-content pre { background: #f5f5f5; padding: 15px; border-radius: 8px; overflow-x: auto; }
        .md-content strong { color: #333; }
        .md-content hr { border: none; border-top: 1px solid #eee; margin: 20px 0; }
        .md-content p { margin: 10px 0; }
    </style>
</head>
<body>
    <div class="container">
        <div class="back"><a href="/">← Back to Dashboard</a></div>
        <div class="card">
            <h1>{{ student_name }}</h1>
            <div class="meta">
                {{ info.course }} | {{ info.tier }} | Exam: {{ exam_date }}
            </div>
            <div class="md-content">{{ plan_content|safe }}</div>
        </div>
        <div class="card" style="margin-top: 20px;">
            <h2 style="margin-bottom: 15px;">Coaching Calls</h2>
            <table style="width: 100%; border-collapse: collapse;">
                <tr style="background: #f5f5f5;">
                    <th style="padding: 10px; text-align: left; border-bottom: 2px solid #ddd;">Date</th>
                    <th style="padding: 10px; text-align: left; border-bottom: 2px solid #ddd;">Week</th>
                    <th style="padding: 10px; text-align: left; border-bottom: 2px solid #ddd;">Time (UK)</th>
                    <th style="padding: 10px; text-align: left; border-bottom: 2px solid #ddd;">Time (CT)</th>
                    <th style="padding: 10px; text-align: left; border-bottom: 2px solid #ddd;">Actions</th>
                </tr>
                {% for call in student_calls %}
                <tr>
                    <td style="padding: 10px; border-bottom: 1px solid #eee;">{{ call.date_str }}</td>
                    <td style="padding: 10px; border-bottom: 1px solid #eee;">Week {{ call.week }}</td>
                    <td style="padding: 10px; border-bottom: 1px solid #eee; font-weight: bold;">{{ call.time_london }}</td>
                    <td style="padding: 10px; border-bottom: 1px solid #eee; color: #666;">{{ call.time }}</td>
                    <td style="padding: 10px; border-bottom: 1px solid #eee;">
                        <a href="/questions/{{ student_name|urlencode }}/{{ call.week }}" class="btn" style="padding: 5px 10px; font-size: 0.85em;">Questions</a>
                        <a href="/record/{{ student_name|urlencode }}/{{ call.week }}" class="btn" style="padding: 5px 10px; font-size: 0.85em; background: #10b981;">Record</a>
                    </td>
                </tr>
                {% endfor %}
            </table>
        </div>
        <div style="margin-top: 20px;">
            <a href="mailto:{{ info.email }}" class="btn">Email Student</a>
        </div>
    </div>
</body>
</html>
"""

QUESTIONS_HTML = """
<!DOCTYPE html>
<html>
<head>
    <title>{{ student_name }} - Week {{ week }} Questions</title>
    <meta charset="utf-8">
    <style>
        * { box-sizing: border-box; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background: #f5f5f5;
            padding: 20px;
            line-height: 1.6;
        }
        .container { max-width: 900px; margin: 0 auto; }
        .back { margin-bottom: 20px; }
        .back a { color: #667eea; text-decoration: none; }
        .card {
            background: white;
            border-radius: 12px;
            padding: 30px;
            box-shadow: 0 2px 8px rgba(0,0,0,0.1);
        }
        h1 { color: #333; margin-bottom: 20px; border-bottom: 2px solid #667eea; padding-bottom: 10px; }
        .btn {
            padding: 10px 20px;
            background: #667eea;
            color: white;
            border: none;
            border-radius: 6px;
            cursor: pointer;
            text-decoration: none;
            display: inline-block;
            margin-top: 20px;
            margin-right: 10px;
        }
        .btn-secondary { background: #6b7280; }
        /* Markdown content styling */
        .md-content h1 { font-size: 1.4em; color: #667eea; border-bottom: 2px solid #667eea; padding-bottom: 8px; margin-top: 30px; }
        .md-content h2 { font-size: 1.2em; color: #333; margin-top: 25px; background: #f5f5f5; padding: 10px; border-radius: 6px; }
        .md-content h3 { font-size: 1.05em; color: #555; margin-top: 15px; }
        .md-content p { margin: 12px 0; }
        .md-content strong { color: #333; }
        .md-content em { color: #666; }
        .md-content ul, .md-content ol { margin: 10px 0; padding-left: 25px; }
        .md-content li { margin: 8px 0; }
        .md-content hr { border: none; border-top: 2px solid #eee; margin: 25px 0; }
        .md-content details { background: #f9f9f9; border-radius: 8px; padding: 15px; margin: 20px 0; }
        .md-content summary { cursor: pointer; font-weight: bold; color: #667eea; }
        .md-content table { border-collapse: collapse; width: 100%; margin: 15px 0; }
        .md-content th, .md-content td { border: 1px solid #ddd; padding: 10px; text-align: left; }
        .md-content th { background: #f5f5f5; }
        .md-content code { background: #f0f0f0; padding: 2px 6px; border-radius: 4px; }
        .md-content pre { background: #f5f5f5; padding: 15px; border-radius: 8px; overflow-x: auto; }
        .md-content input[type="checkbox"] { margin-right: 8px; }
        .error { color: #dc2626; background: #fee2e2; padding: 20px; border-radius: 8px; }
    </style>
</head>
<body>
    <div class="container">
        <div class="back"><a href="/">← Back to Dashboard</a></div>
        <div class="card">
            <h1>{{ student_name }} - Week {{ week }}</h1>
            {% if content %}
            <div class="md-content">{{ content|safe }}</div>
            {% else %}
            <div class="error">Question file not found: {{ filename }}</div>
            {% endif %}
            <a href="/send/{{ student_name|urlencode }}/{{ week }}" class="btn">Send to Student</a>
            <a href="/send-question/{{ student_name|urlencode }}/{{ week }}" class="btn btn-secondary" title="Force Slack only">Slack</a>
            <a href="/send-email/{{ student_name|urlencode }}/{{ week }}" class="btn btn-secondary" title="Force Email only">Email</a>
            <a href="/student/{{ student_name|urlencode }}" class="btn btn-secondary">View Plan</a>
        </div>
    </div>
</body>
</html>
"""

RESULT_HTML = """
<!DOCTYPE html>
<html>
<head>
    <title>{{ title }}</title>
    <meta charset="utf-8">
    <meta http-equiv="refresh" content="3;url=/">
    <style>
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            display: flex; align-items: center; justify-content: center;
            min-height: 100vh; background: #f5f5f5;
        }
        .card {
            background: white; padding: 40px; border-radius: 12px;
            box-shadow: 0 2px 8px rgba(0,0,0,0.1); text-align: center;
        }
        .success { color: #10b981; }
        .error { color: #ef4444; }
    </style>
</head>
<body>
    <div class="card">
        <h1 class="{{ 'success' if success else 'error' }}">{{ title }}</h1>
        <p>{{ message }}</p>
        <p><small>Redirecting to dashboard...</small></p>
    </div>
</body>
</html>
"""

SETTINGS_HTML = """
<!DOCTYPE html>
<html>
<head>
    <title>Settings - AP Coaching Dashboard</title>
    <meta charset="utf-8">
    <style>
        * { box-sizing: border-box; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background: #f5f5f5;
            padding: 20px;
            line-height: 1.6;
        }
        .container { max-width: 700px; margin: 0 auto; }
        .back { margin-bottom: 20px; }
        .back a { color: #667eea; text-decoration: none; }
        .card {
            background: white;
            border-radius: 12px;
            padding: 30px;
            box-shadow: 0 2px 8px rgba(0,0,0,0.1);
            margin-bottom: 20px;
        }
        h1 { color: #333; margin-bottom: 20px; }
        h2 { color: #333; margin-bottom: 15px; font-size: 1.2em; }

        .status-badge {
            display: inline-block;
            padding: 4px 12px;
            border-radius: 20px;
            font-size: 0.85em;
            font-weight: 500;
        }
        .status-badge.connected { background: #d1fae5; color: #059669; }
        .status-badge.disconnected { background: #fee2e2; color: #dc2626; }

        .form-group { margin-bottom: 20px; }
        .form-group label { display: block; margin-bottom: 8px; font-weight: 500; color: #374151; }
        .form-group input[type="text"],
        .form-group input[type="password"] {
            width: 100%;
            padding: 12px;
            border: 1px solid #d1d5db;
            border-radius: 8px;
            font-size: 1em;
            font-family: monospace;
        }
        .form-group input:focus {
            outline: none;
            border-color: #667eea;
            box-shadow: 0 0 0 3px rgba(102, 126, 234, 0.1);
        }
        .form-group small { color: #6b7280; display: block; margin-top: 6px; }

        .btn {
            padding: 12px 24px;
            border: none;
            border-radius: 8px;
            cursor: pointer;
            font-size: 1em;
            font-weight: 500;
        }
        .btn-primary { background: #667eea; color: white; }
        .btn-primary:hover { background: #5a67d8; }
        .btn-secondary { background: #e5e7eb; color: #374151; margin-left: 10px; }
        .btn-danger { background: #ef4444; color: white; margin-left: 10px; }

        .help-section {
            background: #f9fafb;
            border-radius: 8px;
            padding: 20px;
            margin-top: 20px;
        }
        .help-section h3 { font-size: 1em; margin-bottom: 10px; }
        .help-section ol { margin-left: 20px; }
        .help-section li { margin-bottom: 8px; }
        .help-section code { background: #e5e7eb; padding: 2px 6px; border-radius: 4px; font-size: 0.9em; }

        .alert {
            padding: 15px;
            border-radius: 8px;
            margin-bottom: 20px;
        }
        .alert-success { background: #d1fae5; color: #059669; }
        .alert-error { background: #fee2e2; color: #dc2626; }
    </style>
</head>
<body>
    <div class="container">
        <div class="back"><a href="/">← Back to Dashboard</a></div>

        {% if message %}
        <div class="alert {{ 'alert-success' if success else 'alert-error' }}">
            {{ message }}
        </div>
        {% endif %}

        <div class="card">
            <h1>Settings</h1>

            <h2>Slack Integration
                <span class="status-badge {{ 'connected' if slack_connected else 'disconnected' }}">
                    {{ 'Connected' if slack_connected else 'Not Connected' }}
                </span>
            </h2>

            <form method="POST" action="/settings/slack">
                <div class="form-group">
                    <label for="slack_token">Bot User OAuth Token</label>
                    <input type="password" id="slack_token" name="slack_token"
                           value="{{ current_token }}"
                           placeholder="xoxb-your-token-here">
                    <small>Starts with <code>xoxb-</code>. Get this from OAuth & Permissions.</small>
                </div>

                <div class="form-group">
                    <label for="slack_app_token">App-Level Token (for interactive bot)</label>
                    <input type="password" id="slack_app_token" name="slack_app_token"
                           value="{{ current_app_token }}"
                           placeholder="xapp-your-token-here">
                    <small>Starts with <code>xapp-</code>. Required for receiving student messages. Get this from Basic Information → App-Level Tokens.</small>
                </div>

                <button type="submit" class="btn btn-primary">Save & Connect</button>
                {% if current_token %}
                <a href="/settings/slack/test" class="btn btn-secondary">Test Connection</a>
                <a href="/settings/slack/clear" class="btn btn-danger">Disconnect</a>
                {% endif %}
            </form>

            <div class="help-section">
                <h3>Setup Instructions:</h3>
                <p><strong>Bot Token (required for sending messages):</strong></p>
                <ol>
                    <li>Go to <a href="https://api.slack.com/apps" target="_blank">api.slack.com/apps</a></li>
                    <li>Click <strong>Create New App</strong> → <strong>From scratch</strong></li>
                    <li>Name it "AP Coaching Bot", select your workspace</li>
                    <li>Go to <strong>OAuth & Permissions</strong> → add scopes:
                        <code>chat:write</code>, <code>users:read</code>, <code>users:read.email</code>, <code>im:history</code>, <code>im:read</code>, <code>im:write</code></li>
                    <li>Click <strong>Install to Workspace</strong></li>
                    <li>Copy the <strong>Bot User OAuth Token</strong> (<code>xoxb-...</code>)</li>
                </ol>
                <p><strong>App Token (required for receiving student messages):</strong></p>
                <ol>
                    <li>In your app settings, go to <strong>Socket Mode</strong> → Enable it</li>
                    <li>Go to <strong>Event Subscriptions</strong> → Enable → Subscribe to <code>message.im</code></li>
                    <li>Go to <strong>Basic Information</strong> → scroll to <strong>App-Level Tokens</strong></li>
                    <li>Click <strong>Generate Token</strong>, name it "socket", add scope <code>connections:write</code></li>
                    <li>Copy the token (<code>xapp-...</code>)</li>
                </ol>
            </div>
        </div>

        <div class="card">
            <h2>Email Integration
                <span class="status-badge {{ 'connected' if email_configured else 'disconnected' }}">
                    {{ 'Configured' if email_configured else 'Not Configured' }}
                </span>
            </h2>

            <form method="POST" action="/settings/email">
                <div class="form-group">
                    <label for="smtp_server">SMTP Server</label>
                    <input type="text" id="smtp_server" name="smtp_server"
                           value="{{ email_config.smtp_server }}"
                           placeholder="smtp.gmail.com">
                    <small>e.g., smtp.gmail.com, smtp.office365.com</small>
                </div>

                <div class="form-group">
                    <label for="smtp_port">SMTP Port</label>
                    <input type="text" id="smtp_port" name="smtp_port"
                           value="{{ email_config.smtp_port }}"
                           placeholder="587">
                    <small>Usually 587 for TLS, 465 for SSL</small>
                </div>

                <div class="form-group">
                    <label for="smtp_username">Email / Username</label>
                    <input type="text" id="smtp_username" name="smtp_username"
                           value="{{ email_config.smtp_username }}"
                           placeholder="your.email@gmail.com">
                </div>

                <div class="form-group">
                    <label for="smtp_password">App Password</label>
                    <input type="password" id="smtp_password" name="smtp_password"
                           value="{{ email_config.smtp_password }}"
                           placeholder="your-app-password">
                    <small>For Gmail, use an <a href="https://support.google.com/accounts/answer/185833" target="_blank">App Password</a>, not your regular password</small>
                </div>

                <div class="form-group">
                    <label for="from_email">From Email Address</label>
                    <input type="text" id="from_email" name="from_email"
                           value="{{ email_config.from_email }}"
                           placeholder="your.email@gmail.com">
                </div>

                <div class="form-group">
                    <label for="from_name">From Name</label>
                    <input type="text" id="from_name" name="from_name"
                           value="{{ email_config.from_name }}"
                           placeholder="AP Coaching">
                </div>

                <button type="submit" class="btn btn-primary">Save Email Settings</button>
                {% if email_configured %}
                <a href="/settings/email/test" class="btn btn-secondary">Send Test Email</a>
                <a href="/settings/email/clear" class="btn btn-danger">Clear Settings</a>
                {% endif %}
            </form>

            <div class="help-section">
                <h3>Gmail Setup:</h3>
                <ol>
                    <li>Enable 2-Factor Authentication on your Google Account</li>
                    <li>Go to <a href="https://myaccount.google.com/apppasswords" target="_blank">App Passwords</a></li>
                    <li>Create an app password for "Mail"</li>
                    <li>Use that 16-character password above (not your Gmail password)</li>
                </ol>
            </div>
        </div>

        <div class="card">
            <h2>Bulk Actions</h2>

            <div style="margin-bottom: 20px;">
                <h3 style="font-size: 1em; margin-bottom: 10px;">Send Coaching Plans to All Students</h3>
                <p style="color: #6b7280; margin-bottom: 15px;">
                    Send an introduction email to all students with their personalized coaching plan,
                    explaining who you are, what to expect, and what they need to do.
                    Only sends to students who haven't received it yet.
                </p>
                <a href="/send-plans" class="btn btn-primary"
                   onclick="return confirm('This will email coaching plans to all students who haven\\'t received one yet. Continue?')">
                    Send Plans to All Students
                </a>
            </div>

            <hr style="border: none; border-top: 1px solid #eee; margin: 20px 0;">

            <div>
                <h3 style="font-size: 1em; margin-bottom: 10px;">Manual Sends</h3>
                <a href="/send-weekly" class="btn btn-secondary">Send Weekly Reminders</a>
                <a href="/send-questions" class="btn btn-secondary">Send Tomorrow's Questions</a>
            </div>
        </div>
    </div>
</body>
</html>
"""

RECORD_HTML = """
<!DOCTYPE html>
<html>
<head>
    <title>Record Call - {{ student_name }}</title>
    <meta charset="utf-8">
    <style>
        * { box-sizing: border-box; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background: #f5f5f5;
            padding: 20px;
            line-height: 1.6;
        }
        .container { max-width: 600px; margin: 0 auto; }
        .back { margin-bottom: 20px; }
        .back a { color: #667eea; text-decoration: none; }
        .card {
            background: white;
            border-radius: 12px;
            padding: 30px;
            box-shadow: 0 2px 8px rgba(0,0,0,0.1);
        }
        h1 { color: #333; margin-bottom: 5px; }
        .subtitle { color: #666; margin-bottom: 25px; padding-bottom: 15px; border-bottom: 1px solid #eee; }

        .form-group { margin-bottom: 25px; }
        .form-group label { display: block; margin-bottom: 10px; font-weight: 600; color: #374151; }

        .radio-group { display: flex; gap: 20px; }
        .radio-option {
            flex: 1;
            padding: 20px;
            border: 2px solid #e5e7eb;
            border-radius: 8px;
            cursor: pointer;
            text-align: center;
            transition: all 0.2s;
        }
        .radio-option:hover { border-color: #667eea; }
        .radio-option.selected { border-color: #667eea; background: #f0f4ff; }
        .radio-option.selected.yes { border-color: #10b981; background: #d1fae5; }
        .radio-option.selected.no { border-color: #ef4444; background: #fee2e2; }
        .radio-option input { display: none; }
        .radio-option .icon { font-size: 2em; margin-bottom: 5px; }
        .radio-option .label { font-weight: 500; }

        input[type="text"] {
            width: 100%;
            padding: 12px;
            border: 1px solid #d1d5db;
            border-radius: 8px;
            font-size: 1em;
        }
        input[type="text"]:focus {
            outline: none;
            border-color: #667eea;
            box-shadow: 0 0 0 3px rgba(102, 126, 234, 0.1);
        }
        small { color: #6b7280; display: block; margin-top: 6px; }

        .btn {
            padding: 12px 24px;
            border: none;
            border-radius: 8px;
            cursor: pointer;
            font-size: 1em;
            font-weight: 500;
            text-decoration: none;
            display: inline-block;
        }
        .btn-primary { background: #667eea; color: white; }
        .btn-primary:hover { background: #5a67d8; }
        .btn-secondary { background: #e5e7eb; color: #374151; margin-left: 10px; }

        .existing-record {
            background: #f0f4ff;
            border: 1px solid #667eea;
            border-radius: 8px;
            padding: 15px;
            margin-bottom: 20px;
        }
        .existing-record h3 { margin: 0 0 10px 0; color: #667eea; font-size: 0.9em; }
    </style>
</head>
<body>
    <div class="container">
        <div class="back"><a href="/">← Back to Dashboard</a></div>
        <div class="card">
            <h1>Record Call: {{ student_name }}</h1>
            <div class="subtitle">
                Week {{ week }} - {{ call.date_obj.strftime('%A, %B %d, %Y') }} at {{ call.time }} CT
            </div>

            {% if existing %}
            <div class="existing-record">
                <h3>Existing Record</h3>
                <p>
                    <strong>Attended:</strong> {{ '✓ Yes' if existing.attended else '✗ No' }}<br>
                    {% if existing.recording %}
                    <strong>Recording:</strong> <a href="{{ existing.recording }}" target="_blank">{{ existing.recording[:50] }}...</a>
                    {% else %}
                    <strong>Recording:</strong> Not added
                    {% endif %}
                </p>
            </div>
            {% endif %}

            <form method="POST">
                <div class="form-group">
                    <label>Did {{ student_name.split()[0] }} attend?</label>
                    <div class="radio-group">
                        <label class="radio-option yes {{ 'selected' if existing and existing.attended else '' }}" onclick="selectOption(this, 'yes')">
                            <input type="radio" name="attended" value="yes" {{ 'checked' if existing and existing.attended else '' }}>
                            <div class="icon">✓</div>
                            <div class="label">Yes, attended</div>
                        </label>
                        <label class="radio-option no {{ 'selected' if existing and not existing.attended else '' }}" onclick="selectOption(this, 'no')">
                            <input type="radio" name="attended" value="no" {{ 'checked' if existing and not existing.attended else '' }}>
                            <div class="icon">✗</div>
                            <div class="label">No show</div>
                        </label>
                    </div>
                </div>

                <div class="form-group">
                    <label for="recording_url">Recording Link (optional)</label>
                    <input type="text" id="recording_url" name="recording_url"
                           value="{{ existing.recording if existing else '' }}"
                           placeholder="https://zoom.us/rec/share/...">
                    <small>Paste the link to the call recording (Zoom, Google Meet, etc.)</small>
                </div>

                <button type="submit" class="btn btn-primary">Save Record</button>
                <a href="/student/{{ student_name }}" class="btn btn-secondary">Cancel</a>
            </form>
        </div>
    </div>

    <script>
        function selectOption(element, value) {
            // Remove selected from all
            document.querySelectorAll('.radio-option').forEach(el => el.classList.remove('selected'));
            // Add selected to clicked
            element.classList.add('selected');
            // Check the radio
            element.querySelector('input').checked = true;
        }
    </script>
</body>
</html>
"""

# =============================================================================
# ROUTES
# =============================================================================

@app.route('/health')
def health():
    """Health check endpoint for DashManager."""
    checks = {
        'slack': bool(slack_client),
        'email': is_email_configured(),
        'listener': slack_listener_status.get('running', False)
    }
    all_ok = checks['slack'] or checks['email']  # At least one messaging method
    status = 'ok' if all_ok else 'degraded'
    code = 200 if status == 'ok' else 503
    return jsonify({'status': status, 'checks': checks}), code

@app.route('/')
def dashboard():
    status = load_status()
    today = get_today()

    # Get today's messages
    today_str = today.strftime('%Y-%m-%d')
    recent_messages = [m for m in status.get('sent_messages', [])
                       if m.get('time', '').startswith(today_str)][-20:]

    return render_template_string(DASHBOARD_HTML,
        today=today,
        london_time=get_london_now().strftime('%H:%M %Z'),
        calls_today=get_calls_today(),
        calls_week=get_calls_this_week(),
        slack_connected=bool(slack_client),
        email_configured=is_email_configured(),
        messages_sent_today=len(recent_messages),
        recent_messages=reversed(recent_messages)
    )

@app.route('/student/<student_name>')
def student_detail(student_name):
    info = STUDENTS.get(student_name, {})
    plan_content_raw = get_student_plan_content(student_name)
    exam_date = EXAM_DATES.get(info.get('course', ''), 'TBD')

    # Convert markdown to HTML
    if plan_content_raw:
        md = markdown.Markdown(extensions=['tables', 'fenced_code', 'md_in_html'])
        plan_content = md.convert(plan_content_raw)
    else:
        plan_content = '<p class="error">No plan file found</p>'

    # Get student calls with formatted dates and London times
    student_calls = []
    for c in SCHEDULE:
        if c['student'] == student_name:
            call_date = datetime(*c['date'])
            student_calls.append({
                **c,
                'date_obj': call_date,
                'date_str': call_date.strftime('%a, %b %d'),
                'time_london': convert_time_to_london(c['date'], c['time'])
            })
    student_calls.sort(key=lambda x: x['date_obj'])

    return render_template_string(STUDENT_HTML,
        student_name=student_name,
        info=info,
        plan_content=plan_content,
        exam_date=exam_date,
        student_calls=student_calls
    )

@app.route('/questions/<student_name>/<int:week>')
def view_questions(student_name, week):
    content_raw, filename = get_question_file_content(student_name, week)

    # Convert markdown to HTML (with raw HTML passthrough for <details> tags)
    if content_raw:
        md = markdown.Markdown(extensions=['tables', 'fenced_code', 'md_in_html'])
        content = md.convert(content_raw)
    else:
        content = None

    return render_template_string(QUESTIONS_HTML,
        student_name=student_name,
        week=week,
        content=content,
        filename=filename
    )

@app.route('/send-question/<student_name>/<int:week>')
def send_question(student_name, week):
    success, msg = send_question_to_student(student_name, week)

    return render_template_string(RESULT_HTML,
        title="Message Sent!" if success else "Failed to Send",
        message=f"Questions for {student_name} Week {week}: {msg}",
        success=success
    )

@app.route('/send-email/<student_name>/<int:week>')
def send_email_route(student_name, week):
    success, msg = send_question_via_email(student_name, week)

    return render_template_string(RESULT_HTML,
        title="Email Sent!" if success else "Failed to Send Email",
        message=f"Questions for {student_name} Week {week}: {msg}",
        success=success
    )

@app.route('/send/<student_name>/<int:week>')
def send_smart_route(student_name, week):
    success, msg, method = send_question_smart(student_name, week)

    if success:
        title = f"Sent via {'Slack' if method == 'slack' else 'Email'}!"
    else:
        title = "Failed to Send"

    return render_template_string(RESULT_HTML,
        title=title,
        message=f"Questions for {student_name} Week {week}: {msg}",
        success=success
    )

@app.route('/send-weekly')
def send_weekly_page():
    auto_send_weekly_reminders()
    return render_template_string(RESULT_HTML,
        title="Weekly Reminders Sent",
        message="Sent weekly schedule reminders to all students with calls this week (who haven't received one yet).",
        success=True
    )

@app.route('/send-questions')
def send_questions_page():
    auto_send_questions_for_tomorrow()
    today = get_today()
    weekday = today.weekday()
    if weekday == 4:
        target = "Monday's calls (Friday send)"
    elif weekday in (5, 6):
        target = "nothing (weekend)"
    else:
        target = "tomorrow's calls"
    return render_template_string(RESULT_HTML,
        title="Questions Sent",
        message=f"Sent practice questions for {target} to students who haven't received them yet.",
        success=True
    )

@app.route('/scheduler')
def scheduler_status():
    return render_template_string("""
<!DOCTYPE html>
<html>
<head>
    <title>Scheduler Log - AP Coaching</title>
    <meta charset="utf-8">
    <meta http-equiv="refresh" content="30">
    <style>
        body { font-family: -apple-system, BlinkMacSystemFont, sans-serif; padding: 20px; background: #f5f5f5; }
        .container { max-width: 900px; margin: 0 auto; }
        .back { margin-bottom: 20px; }
        .back a { color: #667eea; text-decoration: none; }
        .card { background: white; border-radius: 12px; padding: 20px; box-shadow: 0 2px 8px rgba(0,0,0,0.1); }
        h1 { margin-bottom: 20px; }
        .log-entry { padding: 8px 0; border-bottom: 1px solid #eee; font-family: monospace; font-size: 0.9em; }
        .log-entry:last-child { border-bottom: none; }
        .time { color: #666; }
        .btn { padding: 10px 20px; background: #667eea; color: white; border: none; border-radius: 6px; text-decoration: none; display: inline-block; margin-right: 10px; }
        .btn-secondary { background: #6b7280; }
        .actions { margin-bottom: 20px; }
        .status { padding: 10px; background: #d1fae5; border-radius: 6px; margin-bottom: 20px; color: #059669; }
    </style>
</head>
<body>
    <div class="container">
        <div class="back"><a href="/">← Back to Dashboard</a></div>
        <div class="card">
            <h1>Scheduler Status</h1>
            <div class="status">Auto-refresh every 30 seconds</div>
            <div class="actions">
                <a href="/send-weekly" class="btn">Send Weekly Now</a>
                <a href="/send-questions" class="btn">Send Tomorrow's Questions Now</a>
                <a href="/scheduler/catchup" class="btn btn-secondary">Run Catch-up Check</a>
            </div>
            <h2>Recent Activity</h2>
            {% if log %}
                {% for entry in log|reverse %}
                <div class="log-entry">
                    <span class="time">{{ entry.time[:19] }}</span> - {{ entry.message }}
                </div>
                {% endfor %}
            {% else %}
                <p>No scheduler activity yet.</p>
            {% endif %}
        </div>
    </div>
</body>
</html>
    """, log=scheduler_log)

@app.route('/scheduler/catchup')
def run_catchup():
    catchup_check()
    return redirect('/scheduler')

@app.route('/record/<student_name>/<int:week>')
def record_call_page(student_name, week):
    """Show form to record call attendance."""
    # Find the call date for this student/week
    call_info = None
    for c in SCHEDULE:
        if c['student'] == student_name and c['week'] == week:
            call_info = {
                **c,
                'date_obj': datetime(*c['date']),
                'info': STUDENTS.get(student_name, {})
            }
            break

    if not call_info:
        return "Call not found", 404

    # Check for existing record
    existing = get_call_record(student_name, call_info['date_obj'], week)

    return render_template_string(RECORD_HTML,
        student_name=student_name,
        week=week,
        call=call_info,
        existing=existing
    )

@app.route('/record/<student_name>/<int:week>', methods=['POST'])
def save_record(student_name, week):
    """Save call attendance record."""
    # Find the call date
    call_date = None
    for c in SCHEDULE:
        if c['student'] == student_name and c['week'] == week:
            call_date = datetime(*c['date'])
            break

    if not call_date:
        return "Call not found", 404

    attended = request.form.get('attended') == 'yes'
    recording_url = request.form.get('recording_url', '').strip()

    success, msg = save_call_record(student_name, call_date, week, attended, recording_url)

    if success:
        return redirect(f'/student/{student_name}?message=Record+saved')
    else:
        return redirect(f'/record/{student_name}/{week}?error={msg}')

@app.route('/reschedule/<student_name>/<int:week>')
def reschedule_form(student_name, week):
    """Show form to reschedule a call."""
    # Find the current call
    call_info = None
    for c in SCHEDULE:
        if c['student'] == student_name and c['week'] == week:
            call_info = {
                **c,
                'date_obj': datetime(*c['date']),
                'info': STUDENTS.get(student_name, {})
            }
            break

    if not call_info:
        return "Call not found", 404

    return render_template_string("""
<!DOCTYPE html>
<html>
<head>
    <title>Reschedule Call - {{ student_name }}</title>
    <meta charset="utf-8">
    <style>
        body { font-family: -apple-system, BlinkMacSystemFont, sans-serif; padding: 20px; background: #f5f5f5; }
        .container { max-width: 500px; margin: 0 auto; }
        .back { margin-bottom: 20px; }
        .back a { color: #667eea; text-decoration: none; }
        .card { background: white; border-radius: 12px; padding: 24px; box-shadow: 0 2px 8px rgba(0,0,0,0.1); }
        h1 { margin: 0 0 8px 0; font-size: 1.5em; }
        .subtitle { color: #666; margin-bottom: 24px; }
        .current { background: #fef3c7; padding: 12px 16px; border-radius: 8px; margin-bottom: 24px; }
        .current strong { color: #92400e; }
        .form-group { margin-bottom: 20px; }
        label { display: block; font-weight: 600; margin-bottom: 6px; color: #374151; }
        input[type="date"], input[type="time"] {
            width: 100%; padding: 10px 12px; border: 1px solid #d1d5db; border-radius: 6px;
            font-size: 1em; box-sizing: border-box;
        }
        .time-note { font-size: 0.85em; color: #666; margin-top: 4px; }
        .btn { padding: 12px 24px; border: none; border-radius: 6px; font-size: 1em; cursor: pointer; }
        .btn-primary { background: #667eea; color: white; }
        .btn-secondary { background: #e5e7eb; color: #374151; margin-left: 10px; text-decoration: none; display: inline-block; }
        .actions { margin-top: 24px; }
    </style>
</head>
<body>
    <div class="container">
        <div class="back"><a href="/">← Back to Dashboard</a></div>
        <div class="card">
            <h1>Reschedule Call</h1>
            <p class="subtitle">{{ student_name }} - Week {{ week }}</p>

            <div class="current">
                <strong>Current:</strong> {{ call.date_obj.strftime('%A %b %d') }} at {{ call.time }} CT
            </div>

            <form method="POST">
                <div class="form-group">
                    <label for="new_date">New Date</label>
                    <input type="date" id="new_date" name="new_date" value="{{ call.date_obj.strftime('%Y-%m-%d') }}" required>
                </div>

                <div class="form-group">
                    <label for="new_time">New Time (Central Time)</label>
                    <input type="time" id="new_time" name="new_time" value="{{ call.time }}" required>
                    <div class="time-note">Enter time in US Central (student's timezone)</div>
                </div>

                <div class="actions">
                    <button type="submit" class="btn btn-primary">Save New Time</button>
                    <a href="/" class="btn btn-secondary">Cancel</a>
                </div>
            </form>
        </div>
    </div>
</body>
</html>
""", student_name=student_name, week=week, call=call_info)

@app.route('/reschedule/<student_name>/<int:week>', methods=['POST'])
def save_reschedule(student_name, week):
    """Save rescheduled call."""
    new_date_str = request.form.get('new_date', '')
    new_time = request.form.get('new_time', '')

    if not new_date_str or not new_time:
        return redirect(f'/reschedule/{student_name}/{week}?error=Missing+date+or+time')

    # Parse new date
    try:
        new_date = datetime.strptime(new_date_str, '%Y-%m-%d')
        new_date_tuple = (new_date.year, new_date.month, new_date.day)
    except ValueError:
        return redirect(f'/reschedule/{student_name}/{week}?error=Invalid+date')

    # Update SCHEDULE in memory
    for c in SCHEDULE:
        if c['student'] == student_name and c['week'] == week:
            old_date = c['date']
            old_time = c['time']
            c['date'] = new_date_tuple
            c['time'] = new_time

            # Log the change
            app.logger.info(f"Rescheduled {student_name} week {week}: {old_date} {old_time} -> {new_date_tuple} {new_time}")
            break

    # Update the dashboard source file
    try:
        dashboard_path = BASE_DIR / 'coaching_dashboard.py'
        with open(dashboard_path, 'r', encoding='utf-8') as f:
            content = f.read()

        # Find and replace the schedule entry
        import re
        old_pattern = rf"\{{'date': \(\d+, \d+, \d+\), 'time': '[^']+', 'student': '{re.escape(student_name)}', 'week': {week}\}}"
        new_entry = f"{{'date': {new_date_tuple}, 'time': '{new_time}', 'student': '{student_name}', 'week': {week}}}"

        content = re.sub(old_pattern, new_entry, content)

        with open(dashboard_path, 'w', encoding='utf-8') as f:
            f.write(content)
    except Exception as e:
        app.logger.error(f"Failed to update dashboard file: {e}")

    # Update student plan markdown
    try:
        clean_name = student_name.replace(' ', '_').replace('-', '_')
        plan_path = PLANS_DIR / f'{clean_name}.md'
        if plan_path.exists():
            with open(plan_path, 'r', encoding='utf-8') as f:
                plan_content = f.read()

            # Update the call table entry
            old_day = datetime(*old_date).strftime('%a %b %d')
            new_day = new_date.strftime('%a %b %d')
            old_line = f'| {week} | {old_day} | {old_time} |'
            new_line = f'| {week} | {new_day} | {new_time} |'
            plan_content = plan_content.replace(old_line, new_line)

            with open(plan_path, 'w', encoding='utf-8') as f:
                f.write(plan_content)
    except Exception as e:
        app.logger.error(f"Failed to update student plan: {e}")

    # Update master schedule
    try:
        master_path = PLANS_DIR / 'MASTER_COACHING_SCHEDULE.md'
        if master_path.exists():
            with open(master_path, 'r', encoding='utf-8') as f:
                master_content = f.read()

            # Update the entry
            old_day = datetime(*old_date).strftime('%a %b %d')
            new_day = new_date.strftime('%a %b %d')
            old_pattern = f'| {old_day} | {old_time} | {student_name} |'
            new_pattern = f'| {new_day} | {new_time} | {student_name} |'
            master_content = master_content.replace(old_pattern, new_pattern)

            with open(master_path, 'w', encoding='utf-8') as f:
                f.write(master_content)
    except Exception as e:
        app.logger.error(f"Failed to update master schedule: {e}")

    return redirect(f'/?message=Rescheduled+{student_name}+to+{new_date_str}+{new_time}')

@app.route('/send-plans')
def send_plans_page():
    """Send coaching plan intro emails to all students."""
    results = send_all_plan_intros()

    sent_count = len(results['sent'])
    skipped_count = len(results['skipped'])
    failed_count = len(results['failed'])

    message_parts = []
    if sent_count:
        message_parts.append(f"Sent to {sent_count} students: {', '.join(results['sent'])}")
    if skipped_count:
        message_parts.append(f"Skipped {skipped_count} (already sent): {', '.join(results['skipped'])}")
    if failed_count:
        failed_names = [f"{name} ({msg})" for name, msg in results['failed']]
        message_parts.append(f"Failed {failed_count}: {', '.join(failed_names)}")

    return render_template_string("""
<!DOCTYPE html>
<html>
<head>
    <title>Send Plans - AP Coaching</title>
    <meta charset="utf-8">
    <style>
        body { font-family: -apple-system, BlinkMacSystemFont, sans-serif; padding: 40px; background: #f5f5f5; }
        .card { background: white; max-width: 800px; margin: 0 auto; padding: 40px; border-radius: 12px; box-shadow: 0 2px 8px rgba(0,0,0,0.1); }
        h1 { color: {{ '#10b981' if sent_count else '#667eea' }}; }
        .stat { font-size: 1.2em; margin: 10px 0; }
        .sent { color: #10b981; }
        .skipped { color: #6b7280; }
        .failed { color: #ef4444; }
        .btn { display: inline-block; padding: 12px 24px; background: #667eea; color: white; text-decoration: none; border-radius: 6px; margin-top: 20px; }
        ul { margin: 10px 0 20px 20px; }
    </style>
</head>
<body>
    <div class="card">
        <h1>Plan Emails {{ 'Sent!' if sent_count else 'Complete' }}</h1>

        {% if sent_count %}
        <p class="stat sent">✓ Sent to {{ sent_count }} students:</p>
        <ul>{% for name in sent %}<li>{{ name }}</li>{% endfor %}</ul>
        {% endif %}

        {% if skipped_count %}
        <p class="stat skipped">↷ Skipped {{ skipped_count }} (already sent):</p>
        <ul>{% for name in skipped %}<li>{{ name }}</li>{% endfor %}</ul>
        {% endif %}

        {% if failed_count %}
        <p class="stat failed">✗ Failed {{ failed_count }}:</p>
        <ul>{% for name, msg in failed %}<li>{{ name }}: {{ msg }}</li>{% endfor %}</ul>
        {% endif %}

        <a href="/" class="btn">Back to Dashboard</a>
    </div>
</body>
</html>
    """,
        sent_count=sent_count,
        skipped_count=skipped_count,
        failed_count=failed_count,
        sent=results['sent'],
        skipped=results['skipped'],
        failed=results['failed']
    )

@app.route('/send-all-now')
def send_all_now_page():
    # TODO: Implement bulk questions send for tomorrow
    return render_template_string(RESULT_HTML,
        title="Tomorrow's Questions",
        message="This would send question files to all students with calls tomorrow. (Coming soon)",
        success=True
    )

@app.route('/settings')
def settings_page():
    config = load_config()
    current_token = config.get('slack_token', '')
    # Mask the token for display
    if current_token:
        masked = current_token[:10] + '...' + current_token[-4:] if len(current_token) > 20 else '****'
    else:
        masked = ''

    # Get app token and mask it
    current_app_token = config.get('slack_app_token', '')
    if current_app_token:
        masked_app = current_app_token[:10] + '...' + current_app_token[-4:] if len(current_app_token) > 20 else '****'
    else:
        masked_app = ''

    # Get email config (mask password for display)
    email_config = get_email_config()
    if email_config['smtp_password']:
        email_config['smtp_password'] = '********'

    return render_template_string(SETTINGS_HTML,
        slack_connected=bool(slack_client),
        current_token=masked,
        current_app_token=masked_app,
        email_configured=is_email_configured(),
        email_config=email_config,
        message=request.args.get('message'),
        success=request.args.get('success') == 'true'
    )

@app.route('/settings/slack', methods=['POST'])
def save_slack_settings():
    token = request.form.get('slack_token', '').strip()
    app_token = request.form.get('slack_app_token', '').strip()

    config = load_config()
    changed = False

    # If token looks masked, don't overwrite
    if token and '...' not in token and token != '****':
        config['slack_token'] = token
        changed = True

    # Save app token if provided and not masked
    if app_token and '...' not in app_token and app_token != '****':
        config['slack_app_token'] = app_token
        changed = True

    if changed:
        save_config(config)

        # Try to connect with bot token
        if reconnect_slack():
            return redirect('/settings?message=Slack+settings+saved+successfully!&success=true')
        else:
            return redirect('/settings?message=Settings+saved+but+bot+connection+failed.+Check+the+bot+token.&success=false')

    return redirect('/settings?message=No+changes+made&success=true')

@app.route('/settings/slack/test')
def test_slack():
    if reconnect_slack():
        return redirect('/settings?message=Slack+connection+successful!&success=true')
    else:
        return redirect('/settings?message=Slack+connection+failed.+Check+your+token.&success=false')

@app.route('/settings/slack/clear')
def clear_slack():
    global slack_client
    config = load_config()
    config['slack_token'] = ''
    config['slack_app_token'] = ''
    save_config(config)
    slack_client = None
    return redirect('/settings?message=Slack+disconnected&success=true')

@app.route('/settings/email', methods=['POST'])
def save_email_settings():
    config = load_config()

    # Only update password if it's not the masked placeholder
    new_password = request.form.get('smtp_password', '').strip()
    if new_password == '********':
        # Keep existing password
        new_password = config.get('smtp_password', '')

    config['smtp_server'] = request.form.get('smtp_server', 'smtp.gmail.com').strip()
    config['smtp_port'] = int(request.form.get('smtp_port', '587').strip() or 587)
    config['smtp_username'] = request.form.get('smtp_username', '').strip()
    config['smtp_password'] = new_password
    config['from_email'] = request.form.get('from_email', '').strip()
    config['from_name'] = request.form.get('from_name', 'AP Coaching').strip()

    save_config(config)

    if is_email_configured():
        return redirect('/settings?message=Email+settings+saved!&success=true')
    else:
        return redirect('/settings?message=Email+settings+saved+but+incomplete.+Fill+in+all+fields.&success=false')

@app.route('/settings/email/test')
def test_email():
    config = get_email_config()
    if not is_email_configured():
        return redirect('/settings?message=Email+not+configured&success=false')

    # Send test email to self
    success, msg = send_email(
        config['from_email'],
        "AP Coaching - Test Email",
        "This is a test email from the AP Coaching Dashboard.\n\nIf you receive this, email is working correctly!",
        "<h2>Test Email</h2><p>This is a test email from the AP Coaching Dashboard.</p><p>If you receive this, email is working correctly!</p>"
    )

    if success:
        return redirect(f'/settings?message=Test+email+sent+to+{config["from_email"]}!&success=true')
    else:
        return redirect(f'/settings?message=Email+failed:+{msg}&success=false')

@app.route('/settings/email/clear')
def clear_email():
    config = load_config()
    config['smtp_username'] = ''
    config['smtp_password'] = ''
    config['from_email'] = ''
    save_config(config)
    return redirect('/settings?message=Email+settings+cleared&success=true')

# =============================================================================
# SLACK INTERACTIONS VIEW
# =============================================================================

INTERACTIONS_FILE = BASE_DIR / 'slack_interactions.json'

def load_interactions():
    if INTERACTIONS_FILE.exists():
        with open(INTERACTIONS_FILE, 'r') as f:
            return json.load(f)
    return []

@app.route('/interactions')
def interactions_view():
    interactions = load_interactions()
    # Reverse to show newest first
    interactions = list(reversed(interactions))
    return render_template_string("""
<!DOCTYPE html>
<html>
<head>
    <title>Slack Interactions - AP Coaching</title>
    <meta charset="utf-8">
    <meta http-equiv="refresh" content="30">
    <style>
        body { font-family: -apple-system, BlinkMacSystemFont, sans-serif; padding: 20px; background: #f5f5f5; }
        .container { max-width: 1000px; margin: 0 auto; }
        .back { margin-bottom: 20px; }
        .back a { color: #667eea; text-decoration: none; }
        .card { background: white; border-radius: 12px; padding: 20px; box-shadow: 0 2px 8px rgba(0,0,0,0.1); margin-bottom: 20px; }
        h1 { margin-bottom: 10px; }
        .subtitle { color: #666; margin-bottom: 20px; }
        .interaction { padding: 15px; border-bottom: 1px solid #eee; }
        .interaction:last-child { border-bottom: none; }
        .interaction:hover { background: #f9fafb; }
        .meta { display: flex; justify-content: space-between; margin-bottom: 8px; }
        .name { font-weight: 600; color: #1f2937; }
        .time { color: #9ca3af; font-size: 0.85em; }
        .message { background: #e0f2fe; padding: 10px 14px; border-radius: 8px; margin-bottom: 8px; color: #0369a1; }
        .response { background: #f3f4f6; padding: 10px 14px; border-radius: 8px; color: #4b5563; font-size: 0.9em; }
        .non-student { opacity: 0.6; }
        .non-student .name::after { content: " (not enrolled)"; color: #ef4444; font-weight: normal; font-size: 0.85em; }
        .empty { text-align: center; padding: 40px; color: #9ca3af; }
        .stats { display: flex; gap: 20px; margin-bottom: 20px; }
        .stat { background: #f3f4f6; padding: 15px 20px; border-radius: 8px; }
        .stat-value { font-size: 1.5em; font-weight: 600; color: #667eea; }
        .stat-label { color: #6b7280; font-size: 0.85em; }
        .filter-bar { margin-bottom: 15px; }
        .filter-bar a { margin-right: 15px; color: #667eea; text-decoration: none; }
        .filter-bar a:hover { text-decoration: underline; }
    </style>
</head>
<body>
    <div class="container">
        <div class="back"><a href="/">Back to Dashboard</a></div>
        <div class="card">
            <h1>Slack Interactions</h1>
            <p class="subtitle">Student messages to the coaching bot (auto-refreshes every 30s)</p>

            <div class="stats">
                <div class="stat">
                    <div class="stat-value">{{ interactions|length }}</div>
                    <div class="stat-label">Total Messages</div>
                </div>
                <div class="stat">
                    <div class="stat-value">{{ interactions|selectattr('is_student')|list|length }}</div>
                    <div class="stat-label">From Students</div>
                </div>
            </div>

            {% if interactions %}
                {% for i in interactions[:50] %}
                <div class="interaction {{ '' if i.is_student else 'non-student' }}">
                    <div class="meta">
                        <span class="name">{{ i.user_name }}</span>
                        <span class="time">{{ i.timestamp[:16].replace('T', ' ') }}</span>
                    </div>
                    <div class="message">{{ i.message }}</div>
                    <div class="response">{{ i.response[:200] }}{% if i.response|length > 200 %}...{% endif %}</div>
                </div>
                {% endfor %}
                {% if interactions|length > 50 %}
                <p style="text-align: center; color: #9ca3af; padding: 20px;">Showing 50 of {{ interactions|length }} interactions</p>
                {% endif %}
            {% else %}
                <div class="empty">
                    <p>No interactions yet.</p>
                    <p style="font-size: 0.9em;">Make sure you've added the App Token in Settings to enable the interactive bot.</p>
                </div>
            {% endif %}
        </div>
    </div>
</body>
</html>
""", interactions=interactions)

# =============================================================================
# SLACK INTERACTIVE LISTENER
# =============================================================================

# Build email -> name lookup for listener
EMAIL_TO_NAME = {info['email']: name for name, info in STUDENTS.items()}

slack_bolt_app = None
slack_listener_status = {'running': False, 'error': None}

def get_student_schedule(student_name):
    """Get all upcoming calls for a student."""
    today = get_today().date()
    calls = []
    for c in SCHEDULE:
        call_date = datetime(*c['date']).date()
        if c['student'] == student_name and call_date >= today:
            calls.append({'date': call_date, 'time': c['time'], 'week': c['week']})
    return sorted(calls, key=lambda x: x['date'])

def get_next_call(student_name):
    """Get the next upcoming call for a student."""
    calls = get_student_schedule(student_name)
    return calls[0] if calls else None

def handle_help():
    return """Here are the commands I understand:

*schedule* - See all your upcoming coaching calls
*next* - See your next coaching call
*help* - Show this help message

You can also just chat with me and I'll pass your message along to your coach!"""

def handle_schedule_cmd(student_name):
    calls = get_student_schedule(student_name)
    if not calls:
        return "You don't have any upcoming calls scheduled."
    lines = ["Here are your upcoming coaching calls:\n"]
    for c in calls[:5]:
        lines.append(f"  *{c['date'].strftime('%A %b %d')}* at *{c['time']} CT* (Week {c['week']})")
    if len(calls) > 5:
        lines.append(f"\n...and {len(calls) - 5} more")
    return "\n".join(lines)

def handle_next_cmd(student_name):
    call = get_next_call(student_name)
    if not call:
        return "You don't have any upcoming calls scheduled."
    days_until = (call['date'] - get_today().date()).days
    if days_until == 0:
        when = "today"
    elif days_until == 1:
        when = "tomorrow"
    else:
        when = f"in {days_until} days"
    return f"Your next coaching call is *{when}*:\n\n  *{call['date'].strftime('%A %b %d')}* at *{call['time']} CT* (Week {call['week']})\n\nSee you then!"

def handle_unknown_student():
    return "Hi! I'm the AP Coaching Bot. It looks like you're not currently enrolled in the coaching program. If you think this is an error, please contact your learning coach."

def handle_general_message(student_name, message):
    return f"Thanks for your message! I've noted it down and your coach will see it. If you need something urgently, try emailing them directly.\n\nType *help* to see what I can do."

def save_interaction(user_email, user_name, message, response, is_student):
    interactions = load_interactions()
    interactions.append({
        'timestamp': datetime.now().isoformat(),
        'user_email': user_email,
        'user_name': user_name,
        'message': message,
        'response': response,
        'is_student': is_student
    })
    interactions = interactions[-500:]
    with open(INTERACTIONS_FILE, 'w') as f:
        json.dump(interactions, f, indent=2)

def init_slack_listener():
    """Initialize the Slack Bolt app for receiving messages."""
    global slack_bolt_app, slack_listener_status

    if not SLACK_BOLT_AVAILABLE:
        slack_listener_status = {'running': False, 'error': 'slack_bolt not installed (pip install slack_bolt)'}
        return None

    config = load_config()
    bot_token = config.get('slack_token', '')
    app_token = config.get('slack_app_token', '')

    if not bot_token or not app_token:
        slack_listener_status = {'running': False, 'error': 'Missing tokens (need both bot and app token)'}
        return None

    try:
        slack_bolt_app = SlackBoltApp(token=bot_token)

        @slack_bolt_app.event("message")
        def handle_message(event, say, client):
            if event.get('bot_id') or event.get('subtype'):
                return

            user_id = event.get('user')
            text = event.get('text', '').strip().lower()
            original_text = event.get('text', '').strip()

            try:
                user_info = client.users_info(user=user_id)
                user_email = user_info['user']['profile'].get('email', '')
                user_display = user_info['user']['profile'].get('real_name', 'Unknown')
            except:
                user_email = ''
                user_display = 'Unknown'

            student_name = EMAIL_TO_NAME.get(user_email)
            is_student = student_name is not None

            if text == 'help':
                response = handle_help()
            elif text == 'schedule':
                response = handle_schedule_cmd(student_name) if is_student else handle_unknown_student()
            elif text == 'next':
                response = handle_next_cmd(student_name) if is_student else handle_unknown_student()
            else:
                response = handle_general_message(student_name, original_text) if is_student else handle_unknown_student()

            save_interaction(user_email, student_name or user_display, original_text, response, is_student)
            say(response)
            print(f"[Slack] {student_name or user_display}: {original_text[:50]}...")

        return slack_bolt_app, app_token
    except Exception as e:
        slack_listener_status = {'running': False, 'error': str(e)}
        return None

def start_slack_listener():
    """Start the Slack listener in a background thread."""
    global slack_listener_status

    result = init_slack_listener()
    if not result:
        return False

    bolt_app, app_token = result

    def run_listener():
        global slack_listener_status
        try:
            slack_listener_status = {'running': True, 'error': None}
            handler = SocketModeHandler(bolt_app, app_token)
            handler.start()
        except Exception as e:
            slack_listener_status = {'running': False, 'error': str(e)}
            print(f"[Slack Listener] Error: {e}")

    listener_thread = threading.Thread(target=run_listener, daemon=True)
    listener_thread.start()
    return True

# =============================================================================
# GRACEFUL SHUTDOWN
# =============================================================================

def handle_sigterm(signum, frame):
    """Handle SIGTERM for graceful shutdown."""
    app.logger.info("Received SIGTERM, shutting down...")
    print("\nShutting down gracefully...")
    sys.exit(0)

signal.signal(signal.SIGTERM, handle_sigterm)

# =============================================================================
# MAIN
# =============================================================================

if __name__ == '__main__':
    # Configuration from environment
    PORT = int(os.environ.get('PORT', 5000))
    DEBUG = os.environ.get('DEBUG', 'false').lower() == 'true'

    print("\n" + "="*50)
    print("AP Coaching Dashboard")
    print("="*50)

    app.logger.info("Starting AP Coaching Dashboard")

    # Initialize Slack
    if init_slack():
        print("Slack: Connected")
        app.logger.info("Slack connected")
    else:
        print("Slack: Not configured (set SLACK_BOT_TOKEN)")

    # Check Email
    if is_email_configured():
        config = get_email_config()
        print(f"Email: Configured ({config['from_email']})")
        app.logger.info(f"Email configured: {config['from_email']}")
    else:
        print("Email: Not configured (configure in Settings)")

    # Start background services (only in main process, not reloader)
    if not DEBUG or os.environ.get('WERKZEUG_RUN_MAIN') == 'true':
        print("Scheduler: Starting background auto-send...")
        app.logger.info("Starting scheduler")
        start_scheduler()

        # Start Slack listener if configured
        config = load_config()
        if config.get('slack_token') and config.get('slack_app_token'):
            if start_slack_listener():
                print("Slack Listener: Running (students can DM the bot)")
                app.logger.info("Slack listener started")
            else:
                print(f"Slack Listener: Failed - {slack_listener_status.get('error', 'unknown error')}")
                app.logger.error(f"Slack listener failed: {slack_listener_status.get('error')}")
        else:
            print("Slack Listener: Not configured (need app token in Settings)")
    elif DEBUG:
        print("Scheduler: Will start after reload...")
        print("Slack Listener: Will start after reload...")

    print(f"\nStarting server at http://127.0.0.1:{PORT}")
    print(f"Health check: http://127.0.0.1:{PORT}/health")
    print("Press Ctrl+C to stop\n")

    app.logger.info(f"Server starting on port {PORT}")
    app.run(host='127.0.0.1', port=PORT, debug=DEBUG)
