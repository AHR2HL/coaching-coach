#!/usr/bin/env python3
"""
Unified AP Dashboard

Main entry point for:
- AP Social Studies tracking (home page)
- Coaching schedule and calls (/coaching)
- Communications center (/comms)

Run: python ap_socsci_dashboard.py
Open: http://localhost:5000
"""

import os
import re
import csv
import json
import time
import smtplib
import requests
import pandas as pd
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from pathlib import Path
from datetime import datetime, timedelta
from flask import Flask, render_template_string, jsonify, request, redirect, url_for
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from dotenv import load_dotenv

# Optional Slack integration
try:
    from slack_sdk import WebClient
    from slack_sdk.errors import SlackApiError
    SLACK_AVAILABLE = True
except ImportError:
    SLACK_AVAILABLE = False

# Optional OpenAI integration
try:
    import openai
    OPENAI_AVAILABLE = True
except ImportError:
    OPENAI_AVAILABLE = False

load_dotenv()

# Also try loading credentials from JSON-format .env if standard dotenv failed
def load_env_from_json():
    """Try to load credentials from JSON-format .env file."""
    env_file = Path(__file__).parent / '.env'
    if env_file.exists():
        try:
            content = env_file.read_text().strip()
            if content.startswith('{'):
                data = json.loads(content)
            else:
                # Parse JSON-like format without braces
                data = {}
                for line in content.split('\n'):
                    line = line.strip().rstrip(',')
                    if ':' in line:
                        key, val = line.split(':', 1)
                        key = key.strip().strip('"')
                        val = val.strip().strip('"')
                        data[key] = val

            if 'client_id' in data and not os.environ.get('TIMEBACK_CLIENT_ID'):
                os.environ['TIMEBACK_CLIENT_ID'] = data['client_id']
            if 'client_secret' in data and not os.environ.get('TIMEBACK_CLIENT_SECRET'):
                os.environ['TIMEBACK_CLIENT_SECRET'] = data['client_secret']
        except Exception:
            pass

load_env_from_json()

app = Flask(__name__)
BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / 'adam_ss_bundle'
CONFIG_FILE = BASE_DIR / 'dashboard_config.json'
COMMS_HISTORY_FILE = BASE_DIR / 'ap_comms_history.json'
RECOMMENDATION_LOCK_FILE = BASE_DIR / 'recommendation_lock.json'

# =============================================================================
# CONFIG MANAGEMENT
# =============================================================================

def load_config():
    """Load dashboard configuration."""
    if CONFIG_FILE.exists():
        with open(CONFIG_FILE, 'r') as f:
            return json.load(f)
    return {}

def save_config(config):
    """Save dashboard configuration."""
    with open(CONFIG_FILE, 'w') as f:
        json.dump(config, f, indent=2)

# =============================================================================
# SLACK INTEGRATION
# =============================================================================

slack_client = None

def init_slack():
    """Initialize Slack client."""
    global slack_client
    if not SLACK_AVAILABLE:
        return False
    config = load_config()
    token = config.get('slack_token') or os.environ.get('SLACK_BOT_TOKEN', '')
    if token:
        slack_client = WebClient(token=token)
        return True
    return False

def send_slack_dm(email, message):
    """Send a direct message to a user via Slack."""
    if not slack_client:
        return False, "Slack not configured"
    try:
        response = slack_client.users_lookupByEmail(email=email)
        user_id = response['user']['id']
        slack_client.chat_postMessage(channel=user_id, text=message)
        return True, "Sent"
    except SlackApiError as e:
        return False, str(e.response['error'])
    except Exception as e:
        return False, str(e)

# =============================================================================
# EMAIL INTEGRATION
# =============================================================================

def get_email_config():
    """Get email SMTP configuration."""
    config = load_config()
    return {
        'smtp_server': config.get('smtp_server', 'smtp.sendgrid.net'),
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
        msg.attach(MIMEText(body_text, 'plain'))
        if body_html:
            msg.attach(MIMEText(body_html, 'html'))
        with smtplib.SMTP(config['smtp_server'], config['smtp_port']) as server:
            server.starttls()
            server.login(config['smtp_username'], config['smtp_password'])
            server.send_message(msg)
        return True, "Sent"
    except smtplib.SMTPAuthenticationError:
        return False, "Authentication failed"
    except smtplib.SMTPException as e:
        return False, f"SMTP error: {str(e)}"
    except Exception as e:
        return False, str(e)

# =============================================================================
# STUDENT & GUIDE DATA (for messaging)
# =============================================================================

STUDENTS = {
    'Gus Castillo': {'email': 'gus.castillo@alpha.school', 'course': 'AP Human Geography'},
    'Emma Cotner': {'email': 'emma.cotner@alpha.school', 'course': 'AP World History'},
    'Jackson Price': {'email': 'jackson.price@alpha.school', 'course': 'AP World History'},
    'Boris Dudarev': {'email': 'boris.dudarev@alpha.school', 'course': 'AP Human Geography'},
    'Sydney Barba': {'email': 'sydney.barba@alpha.school', 'course': 'AP Human Geography'},
    'Branson Pfiester': {'email': 'branson.pfiester@alpha.school', 'course': 'AP Human Geography'},
    'Saeed Tarawneh': {'email': 'said.tarawneh@alpha.school', 'course': 'AP World History'},
    'Aheli Shah': {'email': 'aheli.shah@alpha.school', 'course': 'AP Human Geography'},
    'Ella Dietz': {'email': 'ella.dietz@alpha.school', 'course': 'AP World History'},
    'Stella Cole': {'email': 'stella.cole@alpha.school', 'course': 'AP World History'},
    'Erika Rigby': {'email': 'erika.rigby@alpha.school', 'course': 'AP Human Geography'},
    'Grady Swanson': {'email': 'grady.swanson@alpha.school', 'course': 'AP Human Geography'},
    'Zayen Szpitalak': {'email': 'zayen.szpitalak@alpha.school', 'course': 'AP Human Geography'},
    'Adrienne Laswell': {'email': 'adrienne.laswell@alpha.school', 'course': 'AP Human Geography'},
    'Austin Lin': {'email': 'austin.lin@alpha.school', 'course': 'AP Human Geography'},
    'Jessica Owenby': {'email': 'jessica.owenby@alpha.school', 'course': 'AP Human Geography'},
    'Cruce Saunders IV': {'email': 'cruce.saunders@alpha.school', 'course': 'AP US History'},
    'Kavin Lingham': {'email': 'kavin.lingham@alpha.school', 'course': 'AP World History'},
    'Stella Grams': {'email': 'stella.grams@alpha.school', 'course': 'AP World History'},
    'Jacob Kuchinsky': {'email': 'jacob.kuchinsky@alpha.school', 'course': 'AP Human Geography'},
    'Luca Sanchez': {'email': 'luca.sanchez@alpha.school', 'course': 'AP Human Geography'},
    'Ali Romman': {'email': 'ali.romman@alpha.school', 'course': 'AP Human Geography'},
    'Benny Valles': {'email': 'benjamin.valles@alpha.school', 'course': 'AP Human Geography'},
    'Vera Li': {'email': 'vera.li@alpha.school', 'course': 'AP Human Geography'},
    'Emily Smith': {'email': 'emily.smith@alpha.school', 'course': 'AP US Government'},
    'Paty Margain-Junco': {'email': 'paty.margainjunco@alpha.school', 'course': 'AP US History'},
    'Michael Cai': {'email': 'michael.cai@alpha.school', 'course': 'AP World History'},
}

GUIDES = {
    'Gus Castillo': {'name': 'Jebin Justin', 'email': 'jebin.justin@alpha.school'},
    'Austin Lin': {'name': 'Jebin Justin', 'email': 'jebin.justin@alpha.school'},
    'Zayen Szpitalak': {'name': 'Jebin Justin', 'email': 'jebin.justin@alpha.school'},
    'Cruce Saunders IV': {'name': 'Jebin Justin', 'email': 'jebin.justin@alpha.school'},
    'Vera Li': {'name': 'Jebin Justin', 'email': 'jebin.justin@alpha.school'},
    'Emma Cotner': {'name': 'Chloe Belvin', 'email': 'chloe.belvin@alpha.school'},
    'Aheli Shah': {'name': 'Chloe Belvin', 'email': 'chloe.belvin@alpha.school'},
    'Ella Dietz': {'name': 'Chloe Belvin', 'email': 'chloe.belvin@alpha.school'},
    'Erika Rigby': {'name': 'Chloe Belvin', 'email': 'chloe.belvin@alpha.school'},
    'Paty Margain-Junco': {'name': 'Chloe Belvin', 'email': 'chloe.belvin@alpha.school'},
    'Stella Cole': {'name': 'Chloe Belvin', 'email': 'chloe.belvin@alpha.school'},
    'Branson Pfiester': {'name': 'Cameron Sorsby', 'email': 'cameron.sorsby@alpha.school'},
    'Saeed Tarawneh': {'name': 'Cameron Sorsby', 'email': 'cameron.sorsby@alpha.school'},
    'Grady Swanson': {'name': 'Cameron Sorsby', 'email': 'cameron.sorsby@alpha.school'},
    'Jackson Price': {'name': 'Cameron Sorsby', 'email': 'cameron.sorsby@alpha.school'},
    'Stella Grams': {'name': 'Cameron Sorsby', 'email': 'cameron.sorsby@alpha.school'},
    'Boris Dudarev': {'name': 'Logan Higuera', 'email': 'logan.higuera@alpha.school'},
    'Sydney Barba': {'name': 'Logan Higuera', 'email': 'logan.higuera@alpha.school'},
    'Luca Sanchez': {'name': 'Logan Higuera', 'email': 'logan.higuera@alpha.school'},
    'Adrienne Laswell': {'name': 'Emily Findley', 'email': 'emily.findley@alpha.school'},
    'Jessica Owenby': {'name': 'Emily Findley', 'email': 'emily.findley@alpha.school'},
    'Kavin Lingham': {'name': 'Emily Findley', 'email': 'emily.findley@alpha.school'},
    'Jacob Kuchinsky': {'name': 'Emily Findley', 'email': 'emily.findley@alpha.school'},
    'Ali Romman': {'name': 'Emily Findley', 'email': 'emily.findley@alpha.school'},
    'Benny Valles': {'name': 'Emily Findley', 'email': 'emily.findley@alpha.school'},
    'Emily Smith': {'name': 'Emily Findley', 'email': 'emily.findley@alpha.school'},
    'Michael Cai': {'name': 'Emily Findley', 'email': 'emily.findley@alpha.school'},
}

# =============================================================================
# COMMS HISTORY TRACKING
# =============================================================================

def load_comms_history():
    """Load recommendation message history."""
    if COMMS_HISTORY_FILE.exists():
        with open(COMMS_HISTORY_FILE, 'r') as f:
            return json.load(f)
    return {}

def save_comms_history(history):
    """Save recommendation message history."""
    with open(COMMS_HISTORY_FILE, 'w') as f:
        json.dump(history, f, indent=2)

def record_comms_send(student_name, recommendation_type):
    """Record that a recommendation message was sent to a student."""
    history = load_comms_history()
    if student_name not in history:
        history[student_name] = {}
    if recommendation_type not in history[student_name]:
        history[student_name][recommendation_type] = []
    history[student_name][recommendation_type].append(datetime.now().isoformat())
    save_comms_history(history)


# =============================================================================
# RECOMMENDATION LOCK FUNCTIONS
# =============================================================================

def get_week_start(date):
    """Return the Monday of the week containing the given date."""
    # weekday() returns 0 for Monday, 6 for Sunday
    days_since_monday = date.weekday()
    return date - timedelta(days=days_since_monday)


def is_weekend(date):
    """Return True if date is Saturday (5) or Sunday (6)."""
    return date.weekday() >= 5


def load_recommendation_lock():
    """Load the recommendation lock file."""
    if RECOMMENDATION_LOCK_FILE.exists():
        try:
            with open(RECOMMENDATION_LOCK_FILE, 'r') as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            return None
    return None


def save_recommendation_lock(students):
    """
    Save current recommendations and progress to lock file.
    students: list of student dicts from build_unified_table
    """
    today = datetime.now().date()
    lock_data = {
        'locked_at': datetime.now().isoformat(),
        'week_start': get_week_start(today).isoformat(),
        'students': {}
    }

    for s in students:
        key = f"{s['student']}|{s['course']}"
        lock_data['students'][key] = {
            'rec': s.get('recommendation', ''),
            'detail': s.get('rec_detail', ''),
            'combined_progress': s.get('combined_progress', 0),
            'xp': s.get('current_xp', 0)
        }

    with open(RECOMMENDATION_LOCK_FILE, 'w') as f:
        json.dump(lock_data, f, indent=2)

    return lock_data


def delete_recommendation_lock():
    """Delete the lock file (unlock)."""
    if RECOMMENDATION_LOCK_FILE.exists():
        RECOMMENDATION_LOCK_FILE.unlink()


def get_lock_state():
    """
    Determine current lock state.
    Returns: dict with 'locked' (bool), 'lock_date' (str or None), 'reason' (str)
    """
    today = datetime.now().date()

    # Weekend = always unlocked
    if is_weekend(today):
        return {
            'locked': False,
            'lock_date': None,
            'reason': 'weekend'
        }

    lock_data = load_recommendation_lock()

    # No lock file = unlocked
    if lock_data is None:
        return {
            'locked': False,
            'lock_date': None,
            'reason': 'no_lock_file'
        }

    # Check if lock is from current week
    try:
        lock_week_start = datetime.fromisoformat(lock_data['week_start']).date()
    except (KeyError, ValueError):
        return {
            'locked': False,
            'lock_date': None,
            'reason': 'invalid_lock_file'
        }

    current_week_start = get_week_start(today)

    # Lock from previous week = stale, unlocked
    if lock_week_start < current_week_start:
        return {
            'locked': False,
            'lock_date': lock_data.get('locked_at'),
            'reason': 'stale_lock'
        }

    # Valid lock from current week
    return {
        'locked': True,
        'lock_date': lock_data.get('locked_at'),
        'reason': 'active'
    }


def get_locked_data(student_name, course):
    """
    Get locked recommendation and progress for a student.
    Returns: dict with 'rec', 'detail', 'combined_progress', 'xp' or None if not found.
    """
    lock_data = load_recommendation_lock()
    if lock_data is None:
        return None

    key = f"{student_name}|{course}"
    return lock_data.get('students', {}).get(key)

def get_students_by_recommendation(students):
    """Group students by their current recommendation, with history annotations."""
    by_rec = {
        'PT': [], 'Stay': [], 'FRQ': [], 'Hole-Fill': [],
        'Hole+FRQ': [], 'Speed': [], 'Holes': [], 'Impossible': []
    }
    history = load_comms_history()

    for s in students:
        rec = s.get('recommendation', 'Unknown')
        if rec in by_rec:
            student_history = history.get(s['student'], {}).get(rec, [])
            s['last_sent'] = student_history[-1] if student_history else None
            s['is_new'] = len(student_history) == 0
            by_rec[rec].append(s)

    return by_rec

# =============================================================================
# OPENAI MESSAGE GENERATION
# =============================================================================

REC_PROMPTS = {
    'PT': "Student has completed the course content. Encourage them to take a practice test to establish a baseline before the AP exam.",
    'Stay': "Student is on track. Encourage them to maintain their current pace and keep up the good work.",
    'FRQ': "Student's FRQ accuracy is notably lower than MCQ. Recommend focused FRQ practice with specific strategies.",
    'Hole-Fill': "Student has content gaps in earlier units. Recommend revisiting specific units before moving forward.",
    'Hole+FRQ': "Student has both content gaps AND FRQ weakness. Prioritize filling holes first, then FRQ practice.",
    'Speed': "Student needs to increase their daily pace to meet the deadline. Be encouraging but honest about the effort needed.",
    'Holes': "Late student with content gaps. Recommend targeted mini-courses to efficiently fill gaps.",
    'Impossible': "Student cannot realistically complete the course in time. Be supportive, focus on maximizing what's achievable, celebrate effort."
}

def generate_recommendation_message(student, additional_context=''):
    """Generate personalized message using OpenAI."""
    if not OPENAI_AVAILABLE:
        return None, "OpenAI not available (pip install openai)"

    config = load_config()
    api_key = config.get('openai_api_key') or os.environ.get('OPENAI_API_KEY', '')
    if not api_key:
        return None, "OpenAI API key not configured"

    rec = student.get('recommendation', 'Stay')
    rec_detail = student.get('rec_detail', '')
    weak_units = student.get('weak_units', [])
    rec_courses = student.get('rec_courses', [])

    student_context = f"""
Student: {student.get('student', 'Unknown')}
Course: {student.get('course', 'Unknown')}
Progress: {student.get('combined_progress', 'N/A')}%
Daily XP Rate: {student.get('daily_xp', 'N/A')} XP/day
XP to 90%: {student.get('xp_to_90', 'N/A')}
Projected 90% Date: {student.get('projected_90', 'N/A')}
Practice Test Score: {student.get('pt_score') or 'Not taken'}
MCQ Accuracy: {student.get('mcq') or 'N/A'}%
FRQ Accuracy: {student.get('frq') or 'N/A'}%
Recommendation: {rec}
Recommendation Detail: {rec_detail}
Weak Units: {', '.join([u.get('unit_name', u.get('unit_id', '')) for u in weak_units]) if weak_units else 'None'}
Recommended Courses: {', '.join([c.get('name', '') for c in rec_courses]) if rec_courses else 'None'}
"""

    # Add coach's additional context if provided
    if additional_context:
        student_context += f"\nAdditional context from coach (IMPORTANT - incorporate these specific details):\n{additional_context}\n"

    system_prompt = """You are Coach Adam, an AP exam prep coach. Write a brief, encouraging
Slack message to a student about their AP course progress. Be warm but direct.
Keep the message under 200 words. Use the student's first name only.
Include specific action items based on their situation.
If additional context is provided by the coach, make sure to incorporate those specific details naturally into your message.
End with encouragement. Don't use emojis excessively (one or two max)."""

    user_prompt = f"""{student_context}

Recommendation guidance: {REC_PROMPTS.get(rec, '')}

Write a personalized message for this student."""

    try:
        client = openai.OpenAI(api_key=api_key)
        response = client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ],
            temperature=0.7,
            max_tokens=500
        )
        return response.choices[0].message.content, None
    except Exception as e:
        return None, str(e)

# =============================================================================
# MULTI-CHANNEL SEND
# =============================================================================

def send_recommendation_message(student, message_text):
    """
    Send recommendation message to all 4 channels:
    1. Slack DM to student
    2. Slack DM to coach (record keeping)
    3. Email to student
    4. Email to guide
    """
    results = {
        'slack_student': {'success': False, 'message': 'Not attempted'},
        'slack_coach': {'success': False, 'message': 'Not attempted'},
        'email_student': {'success': False, 'message': 'Not attempted'},
        'email_guide': {'success': False, 'message': 'Not attempted'}
    }

    student_name = student.get('student', '')
    student_info = STUDENTS.get(student_name, {})
    student_email = student_info.get('email', '')
    # Fallback: generate email from name if not in STUDENTS dict
    if not student_email and student_name:
        student_email = student_name.lower().replace(' ', '.') + '@alpha.school'
    guide_info = GUIDES.get(student_name, {})
    guide_email = guide_info.get('email', '')
    config = load_config()
    coach_email = config.get('from_email', '')

    # 1. Slack to student
    if student_email:
        success, msg = send_slack_dm(student_email, message_text)
        results['slack_student'] = {'success': success, 'message': msg}

    # 2. Slack to coach
    if coach_email:
        record_msg = f"[SENT TO {student_name}]\n\n{message_text}"
        success, msg = send_slack_dm(coach_email, record_msg)
        results['slack_coach'] = {'success': success, 'message': msg}

    # 3. Email to student
    if student_email:
        course = student.get('course', 'AP Course')
        subject = f"{course} - Quick Check-in"
        html_body = '<br><br>'.join(f'<p>{p}</p>' for p in message_text.split('\n\n') if p.strip())
        success, msg = send_email(student_email, subject, message_text, html_body)
        results['email_student'] = {'success': success, 'message': msg}

    # 4. Email to guide
    if guide_email:
        guide_name = guide_info.get('name', 'Guide')
        guide_subject = f"[FYI] AP Coaching Message to {student_name}"
        guide_body = f"Hi {guide_name},\n\nFor your awareness, I sent the following message to {student_name}:\n\n---\n\n{message_text}\n\n---\n\nBest,\nCoach Adam"
        success, msg = send_email(guide_email, guide_subject, guide_body)
        results['email_guide'] = {'success': success, 'message': msg}
    else:
        results['email_guide'] = {'success': False, 'message': 'No guide assigned'}

    return results

# =============================================================================
# COURSE MAPPINGS
# =============================================================================

# Map between different course name formats
COURSE_NORMALIZE = {
    'AP Human Geography - PP100': 'APHG',
    'AP Human Geography': 'APHG',
    'AP Psychology': 'APPSY',
    'AP United States Government - PP 100': 'APGOV',
    'AP US Government - PP 100': 'APGOV',
    'AP United States History - PP100': 'APUSH',
    'AP US History - PP100': 'APUSH',
    'AP World History: Modern - PP100': 'APWH',
    'AP World History': 'APWH',
    'APHG': 'APHG',
    'APGOV': 'APGOV',
    'APUSH': 'APUSH',
    'APWH': 'APWH',
}

# Timeback course IDs to normalized names
TIMEBACK_COURSE_MAP = {
    'AP Human Geography - PP100': 'APHG',
    'AP United States Government - PP 100': 'APGOV',
    'AP United States History - PP100': 'APUSH',
    'AP World History: Modern - PP100': 'APWH',
}

# Total XP per course (from lesson details)
COURSE_TOTAL_XP = {
    'APHG': 3445,
    'APGOV': 4107,
    'APUSH': 6134,
    'APWH': 5338,
}

# Total skills per course (from Austin Way)
COURSE_TOTAL_SKILLS = {
    'APHG': 413,
    'APGOV': 396,
    'APUSH': 539,
    'APWH': 544,
}

# XP per skill ratio
XP_PER_SKILL = {
    course: COURSE_TOTAL_XP.get(course, 0) / COURSE_TOTAL_SKILLS.get(course, 1)
    for course in ['APHG', 'APGOV', 'APUSH', 'APWH']
}

# Practice test deadline
PT_DEADLINE = datetime(2026, 4, 16).date()

# Maximum reasonable XP/day (above this is "impossible")
MAX_REASONABLE_XP_PER_DAY = 150

# Minimum recommended XP/day (floor for recommendations)
MIN_RECOMMENDED_XP_PER_DAY = 30

# FRQ weakness threshold (if MCQ - FRQ >= this, recommend FRQ practice)
FRQ_WEAKNESS_THRESHOLD = 15

# Non-CED units (intro + exam prep) - marked with asterisk in display
# Unit 0 = intro for all courses
NON_CED_UNITS = {
    'APHG': ['0'],           # Only intro (Units 1-7 are CED)
    'APGOV': ['0', '6'],     # Intro + Unit 6 Exam Prep
    'APUSH': ['0'],          # Only intro (Units 1-9 are CED)
    'APWH': ['0', '10'],     # Intro + Unit 10 Test Prep
}

# Name aliases (registration name -> tracker name)
# Used when formal names differ from nicknames
NAME_ALIASES = {
    'august castillo': 'gus castillo',
    'benjamin valles': 'benny valles',
    'greyson walker': 'grey walker',
    'alexander mathew': 'alex mathew',
    'madelena price': 'maddie price',
    'patricia margain': 'paty margain-junco',
    'walter saunders': 'cruce saunders iv',
    'sara way': 'sara beth way',
    'mollie anne mcdougald': 'mollie mcdougald',
    'juliana orloff': 'ju orloff',
    'said tarawneh': 'saeed tarawneh',
}


def count_school_days(start_date, end_date):
    """Count weekdays (Mon-Fri) between two dates, exclusive of start, inclusive of end."""
    count = 0
    current = start_date + timedelta(days=1)
    while current <= end_date:
        if current.weekday() < 5:  # Mon=0 through Fri=4
            count += 1
        current += timedelta(days=1)
    return max(count, 1)  # Minimum 1 to avoid division by zero

# Mini-courses available for hole-filling
MINI_COURSES = {
    'APHG': [
        {'id': 's4-r2-mc1-5ff6309d', 'name': 'DTM Application', 'units': ['u2']},
        {'id': 's4-r2-mc2-bf633a64', 'name': 'Boundary Types', 'units': ['u4']},
        {'id': 's4-r2-mc3-a673e09c', 'name': 'Centripetal/Centrifugal', 'units': ['u4']},
    ],
    'APWH': [
        {'id': 's4-r1r3-mc1-a120a364', 'name': 'China (Han to Qing)', 'units': ['u1', 'u3']},
        {'id': 's4-r1r3-mc2-83cb26f8', 'name': 'Russia (Kievan Rus to Soviet)', 'units': ['u3', 'u5', 'u7']},
        {'id': 's4-r1r3-mc3-c1b3d384', 'name': 'Ottoman Empire', 'units': ['u3']},
    ],
}

# =============================================================================
# DATA LOADING
# =============================================================================

def load_all_data():
    """Load and merge all data sources."""

    # 1. Tracker data (Phase 3 Tracker Excel)
    tracker_file = BASE_DIR / 'Phase 3 Tracker - AP Progress AY 25-26.xlsx'
    if tracker_file.exists():
        tracker_students = pd.read_excel(tracker_file, sheet_name='Students')
        tracker_practice = pd.read_excel(tracker_file, sheet_name='practice_test_data')
    else:
        tracker_students = pd.DataFrame()
        tracker_practice = pd.DataFrame()

    # Filter to SocSci courses
    socsci_courses = ['Geography', 'World History', 'United States History', 'Government']
    if len(tracker_students) > 0:
        tracker_students = tracker_students[
            tracker_students['Course'].str.contains('|'.join(socsci_courses), case=False, na=False)
        ].copy()

    # Filter to only registered AP test takers (by student AND course)
    registration_file = BASE_DIR / 'AP_2026_student_analysis_March_24th.xlsx'
    if registration_file.exists() and len(tracker_students) > 0:
        registrations = pd.read_excel(registration_file)
        # Get SocSci registrations
        socsci_reg = registrations[
            registrations['course_enrolled_in'].str.contains('|'.join(socsci_courses), case=False, na=False)
        ]
        # Build set of registered (name, course) pairs
        registered_pairs = set()
        for _, row in socsci_reg.iterrows():
            name = f"{row['student_first_name']} {row['student_last_name']}".strip().lower()
            # Normalize registration course to match tracker format
            reg_course = row['course_enrolled_in']
            if 'Geography' in reg_course:
                norm_course = 'Geography'
            elif 'World History' in reg_course:
                norm_course = 'World History'
            elif 'United States History' in reg_course or 'US History' in reg_course:
                norm_course = 'United States History'
            elif 'Government' in reg_course:
                norm_course = 'Government'
            else:
                norm_course = reg_course
            registered_pairs.add((name, norm_course))
            # Also add any known aliases (e.g., August -> Gus)
            if name in NAME_ALIASES:
                registered_pairs.add((NAME_ALIASES[name], norm_course))

        # Filter tracker to only registered (student, course) pairs
        def is_registered(row):
            student = row['Student'].lower().strip()
            course = row['Course']
            for kw in ['Geography', 'World History', 'United States History', 'Government']:
                if kw in course:
                    return (student, kw) in registered_pairs
            return False

        tracker_students = tracker_students[tracker_students.apply(is_registered, axis=1)].copy()

    # Load student dimension to get alpha_id mapping
    students_file = DATA_DIR / 'ap_social_studies_students.csv'
    if students_file.exists():
        students_dim = pd.read_csv(students_file)
        # Create name -> id mapping
        name_to_id = dict(zip(students_dim['student'], students_dim['student_id']))
        # Add alpha_id to tracker
        if len(tracker_students) > 0:
            tracker_students['student_alpha_id'] = tracker_students['Student'].map(name_to_id)

    # 2. Austin Way data
    aw_mastery_file = BASE_DIR / 'austin_way_mastery.csv'
    aw_daily_file = BASE_DIR / 'austin_way_daily.csv'

    if aw_mastery_file.exists():
        aw_mastery_raw = pd.read_csv(aw_mastery_file)
        # Get per-student-course summary
        aw_summary = aw_mastery_raw.groupby(['student_name', 'student_email', 'course']).agg({
            'course_mastery_pct': 'first',
            'mastered': 'sum',
            'total': 'sum'
        }).reset_index()
    else:
        aw_mastery_raw = pd.DataFrame()
        aw_summary = pd.DataFrame()

    if aw_daily_file.exists():
        aw_daily = pd.read_csv(aw_daily_file)
        # Get recent activity (last 14 days)
        aw_activity = aw_daily.groupby(['Student', 'Email']).agg({
            'Completed': 'sum',
            'Planned': 'sum',
            'Date': 'max'
        }).reset_index()
        aw_activity['completion_rate'] = (aw_activity['Completed'] / aw_activity['Planned'] * 100).fillna(0).round(0)
    else:
        aw_activity = pd.DataFrame()

    # 3. Practice test data - get most recent per student
    if len(tracker_practice) > 0:
        practice_socsci = tracker_practice[
            tracker_practice['course'].str.contains('|'.join(socsci_courses), case=False, na=False)
        ].copy()
        practice_socsci['test_rank'] = practice_socsci['test'].map({'Final': 1, 'Mid-term': 2})
        practice_recent = practice_socsci.sort_values(['student', 'test_rank']).drop_duplicates('student', keep='first')
    else:
        practice_recent = pd.DataFrame()

    # 4. Timeback learning data (for XP calculations)
    timeback_file = DATA_DIR / 'ap_social_studies_learning_data.csv'
    if timeback_file.exists():
        timeback_data = pd.read_csv(timeback_file)
        timeback_data['completed'] = timeback_data['completed_at'].notna()
    else:
        timeback_data = pd.DataFrame()

    # 5. Lesson details (for XP values)
    lesson_details_file = DATA_DIR / 'ap_social_studies_lesson_details_combined.csv'
    if lesson_details_file.exists():
        lesson_details = pd.read_csv(lesson_details_file)
    else:
        lesson_details = pd.DataFrame()

    # Load raw Austin Way daily for XP calculations
    if aw_daily_file.exists():
        aw_daily_raw = pd.read_csv(aw_daily_file)
    else:
        aw_daily_raw = pd.DataFrame()

    return {
        'tracker': tracker_students,
        'practice': practice_recent,
        'aw_mastery': aw_summary,
        'aw_mastery_raw': aw_mastery_raw,
        'aw_activity': aw_activity,
        'aw_daily_raw': aw_daily_raw,
        'timeback': timeback_data,
        'lesson_details': lesson_details
    }


def calculate_student_xp(timeback_data, lesson_details, aw_daily_raw, student_name, student_id, course):
    """Calculate unified XP earned and daily rate from both Timeback and Austin Way."""
    import re
    from collections import defaultdict

    today = datetime.now().date()
    cutoff = today - timedelta(days=14)

    # Initialize daily XP dict for last 14 days
    daily_xp = defaultdict(float)

    # === PART 1: Timeback XP ===
    tb_total_xp = 0
    if len(timeback_data) > 0 and len(lesson_details) > 0 and student_id:
        # Normalize course name for Timeback
        tb_course = None
        for tb_name, norm in TIMEBACK_COURSE_MAP.items():
            if norm == course:
                tb_course = tb_name
                break

        if tb_course:
            # Filter to this student and course
            student_data = timeback_data[
                (timeback_data['student_alpha_id'] == student_id) &
                (timeback_data['course_on_timeback'] == tb_course) &
                (timeback_data['completed'] == True)
            ].copy()

            if len(student_data) > 0:
                def resource_key(tb_id):
                    m = re.search(r'(r\d+)', str(tb_id))
                    return m.group(1) if m else str(tb_id)

                student_data['rkey'] = student_data['item_tb_id'].apply(resource_key)
                lesson_details_course = lesson_details[lesson_details['course_on_timeback'] == tb_course].copy()
                lesson_details_course['rkey'] = lesson_details_course['item_tb_id'].apply(resource_key)

                merged = student_data.merge(
                    lesson_details_course[['rkey', 'item_xp']],
                    on='rkey',
                    how='left'
                )
                merged['item_xp'] = merged['item_xp'].fillna(0)

                # Total XP from Timeback
                tb_total_xp = merged['item_xp'].sum()

                # Daily XP from Timeback (last 14 days)
                merged['completed_date'] = pd.to_datetime(merged['completed_at']).dt.date
                recent = merged[merged['completed_date'] >= cutoff]
                for date, xp in recent.groupby('completed_date')['item_xp'].sum().items():
                    daily_xp[date] += xp

    # === PART 2: Austin Way XP (converted from completed tasks) ===
    aw_total_xp = 0
    xp_per_skill = XP_PER_SKILL.get(course, 10)

    if len(aw_daily_raw) > 0:
        # Match by student name (first name match, case insensitive)
        first_name = student_name.lower().split()[0]
        aw_student = aw_daily_raw[
            (aw_daily_raw['Student'].str.lower().str.split().str[0] == first_name) &
            (aw_daily_raw['Course'] == course)
        ].copy()

        if len(aw_student) > 0:
            aw_student['Date'] = pd.to_datetime(aw_student['Date']).dt.date
            aw_student['xp_equiv'] = aw_student['Completed'] * xp_per_skill

            # Total AW XP (all time in the data)
            aw_total_xp = aw_student['xp_equiv'].sum()

            # Daily AW XP (last 14 days)
            recent_aw = aw_student[aw_student['Date'] >= cutoff]
            for date, xp in recent_aw.groupby('Date')['xp_equiv'].sum().items():
                daily_xp[date] += xp

    # === COMBINE ===
    total_xp = tb_total_xp + aw_total_xp

    # Daily rate (average over school days in last 14 calendar days)
    school_days_in_window = count_school_days(cutoff, today)
    daily_rate = sum(daily_xp.values()) / school_days_in_window

    # Convert dates to strings for JSON
    daily_xp_str = {str(k): float(v) for k, v in daily_xp.items()}

    return {
        'total_xp': int(total_xp),
        'tb_xp': int(tb_total_xp),
        'aw_xp': int(aw_total_xp),
        'daily_rate': round(daily_rate, 1),
        'daily_xp': daily_xp_str
    }


def calculate_unit_combined_progress(student_name, student_id, course, timeback_data, aw_mastery_raw):
    """
    Calculate unit-by-unit combined progress.
    For each unit, take max(Timeback completion %, Austin Way mastery %).
    Return average across all units.
    """
    import re

    # Get Timeback course name
    tb_course = None
    for tb_name, norm in TIMEBACK_COURSE_MAP.items():
        if norm == course:
            tb_course = tb_name
            break

    # === Timeback unit completion ===
    tb_unit_pct = {}
    if tb_course and len(timeback_data) > 0 and student_id:
        student_tb = timeback_data[
            (timeback_data['student_alpha_id'] == student_id) &
            (timeback_data['course_on_timeback'] == tb_course)
        ]
        if len(student_tb) > 0:
            for unit_title, group in student_tb.groupby('unit_title'):
                completed = group['completed_at'].notna().sum()
                total = len(group)
                pct = (completed / total * 100) if total > 0 else 0
                # Extract unit number (e.g., "Unit 1:" -> "1")
                match = re.search(r'Unit\s*(\d+)', str(unit_title))
                if match:
                    unit_num = match.group(1)
                    tb_unit_pct[unit_num] = pct

    # === Austin Way unit mastery ===
    aw_unit_pct = {}
    if len(aw_mastery_raw) > 0:
        name_normalized = student_name.lower().strip()
        name_parts = name_normalized.split()
        # Also try without hyphens for matching
        name_no_hyphen = name_normalized.replace('-', '')

        # Try exact match first
        student_aw = aw_mastery_raw[
            (aw_mastery_raw['student_name'].str.lower().str.strip() == name_normalized) &
            (aw_mastery_raw['course'] == course)
        ]

        # Try without hyphen (Margain-Junco → margainjunco)
        if len(student_aw) == 0:
            student_aw = aw_mastery_raw[
                (aw_mastery_raw['student_name'].str.lower().str.strip().str.replace('-', '', regex=False) == name_no_hyphen) &
                (aw_mastery_raw['course'] == course)
            ]

        # If no exact match, try fuzzy matching
        if len(student_aw) == 0 and len(name_parts) >= 2:
            first_name = name_parts[0]
            last_name = name_parts[-1].replace('-', '')  # Remove hyphen from last name
            course_data = aw_mastery_raw[aw_mastery_raw['course'] == course]

            for aw_name in course_data['student_name'].unique():
                aw_lower = aw_name.lower().strip()
                aw_parts = aw_lower.replace('-', '').split()

                if len(aw_parts) >= 2:
                    aw_first = aw_parts[0]
                    aw_last = aw_parts[-1]
                    # Last name must match (ignoring hyphens), first name must share 3+ char prefix
                    if aw_last == last_name and len(first_name) >= 3 and len(aw_first) >= 3:
                        if aw_first.startswith(first_name[:3]) or first_name.startswith(aw_first[:3]):
                            student_aw = aw_mastery_raw[
                                (aw_mastery_raw['student_name'].str.lower().str.strip() == aw_lower) &
                                (aw_mastery_raw['course'] == course)
                            ]
                            break
                elif len(aw_parts) == 1:
                    # Single name in AW (e.g., "Jeremy") - match if first name matches
                    aw_single = aw_parts[0]
                    if aw_single == first_name or (len(first_name) >= 3 and len(aw_single) >= 3 and
                        (aw_single.startswith(first_name[:3]) or first_name.startswith(aw_single[:3]))):
                        student_aw = aw_mastery_raw[
                            (aw_mastery_raw['student_name'].str.lower().str.strip() == aw_lower) &
                            (aw_mastery_raw['course'] == course)
                        ]
                        break
        for _, row in student_aw.iterrows():
            unit_id = str(row.get('unit_id', ''))
            mastery = row.get('unit_mastery_pct', 0)
            if unit_id.startswith('u') and pd.notna(mastery):
                unit_num = unit_id[1:]  # "u1" -> "1"
                aw_unit_pct[unit_num] = float(mastery)

    # === Combine unit-by-unit (take max) ===
    all_units = set(tb_unit_pct.keys()) | set(aw_unit_pct.keys())
    # Filter to actual units (0+), using CED numbering from unit titles
    content_units = [u for u in all_units if u.isdigit() and int(u) >= 0]

    if not content_units:
        return {'combined_progress': 0, 'unit_details': []}

    unit_details = []
    total_pct = 0
    ced_unit_count = 0
    non_ced_for_course = NON_CED_UNITS.get(course, ['0'])
    for unit_num in sorted(content_units, key=int):
        tb_pct = tb_unit_pct.get(unit_num, 0)
        aw_pct = aw_unit_pct.get(unit_num, 0)
        combined = max(tb_pct, aw_pct)
        is_non_ced = unit_num in non_ced_for_course
        # Only count CED units toward progress (exclude U0 and other non-CED)
        if not is_non_ced:
            total_pct += combined
            ced_unit_count += 1
        unit_details.append({
            'unit': unit_num,
            'timeback': round(tb_pct, 1),
            'austin_way': round(aw_pct, 1),
            'combined': round(combined, 1),
            'non_ced': is_non_ced
        })

    combined_progress = total_pct / ced_unit_count if ced_unit_count > 0 else 0

    return {
        'combined_progress': round(combined_progress, 1),
        'unit_details': unit_details
    }


def calculate_test_performance(student_id, course, timeback_data):
    """
    Calculate MCQ vs FRQ accuracy from Timeback test data.
    Returns dict with mcq_accuracy, frq_accuracy, and whether FRQ is a weakness.
    """
    # Get Timeback course name
    tb_course = None
    for tb_name, norm in TIMEBACK_COURSE_MAP.items():
        if norm == course:
            tb_course = tb_name
            break

    result = {
        'mcq_accuracy': None,
        'frq_accuracy': None,
        'mcq_count': 0,
        'frq_count': 0,
        'frq_weak': False
    }

    if not tb_course or len(timeback_data) == 0 or not student_id:
        return result

    # Filter to this student's completed tests
    student_tests = timeback_data[
        (timeback_data['student_alpha_id'] == student_id) &
        (timeback_data['course_on_timeback'] == tb_course) &
        (timeback_data['completed_at'].notna()) &
        (timeback_data['accuracy'].notna())
    ]

    if len(student_tests) == 0:
        return result

    # Split by test type
    mcq_tests = student_tests[student_tests['test_type'].str.contains('mcq', case=False, na=False)]
    frq_tests = student_tests[student_tests['test_type'].str.contains('frq', case=False, na=False)]

    if len(mcq_tests) > 0:
        result['mcq_accuracy'] = round(mcq_tests['accuracy'].astype(float).mean(), 1)
        result['mcq_count'] = len(mcq_tests)

    if len(frq_tests) > 0:
        result['frq_accuracy'] = round(frq_tests['accuracy'].astype(float).mean(), 1)
        result['frq_count'] = len(frq_tests)

    # Determine if FRQ is a weakness (MCQ - FRQ >= threshold)
    if result['mcq_accuracy'] is not None and result['frq_accuracy'] is not None:
        if result['mcq_accuracy'] - result['frq_accuracy'] >= FRQ_WEAKNESS_THRESHOLD:
            result['frq_weak'] = True

    return result


def get_frq_practice_detail(frq_accuracy, mcq_accuracy, course):
    """
    Generate specific FRQ practice recommendations based on student level.

    Time budget: Max 75 minutes FRQ practice daily
    Higher performers (70%+) get lighter load (30-45 min) to avoid burnout
    FRQ types and times:
    - SAQ (Short Answer): 10-15 min each
    - LEQ (Long Essay): 35-45 min
    - DBQ (Document Based): 45-60 min
    """
    frq = frq_accuracy or 0

    # Course-specific FRQ types
    if course in ['APUSH', 'APWH']:
        # These have SAQ, LEQ, and DBQ
        if frq < 40:
            # Foundation building - focus on SAQs
            return "Start with SAQs: do 4-5 SAQs daily (60 min), focus on using specific evidence"
        elif frq < 55:
            # Building up - mix SAQs with one essay
            return "Do 2 SAQs + 1 LEQ daily (75 min). Practice thesis statements and evidence organization"
        elif frq < 70:
            # Getting stronger - essay practice
            return "Do 1 DBQ OR 1 LEQ + 2 SAQs daily (75 min). Work on document analysis and argument structure"
        else:
            # Polish mode - lighter load, quality over quantity
            return "Do 1 LEQ or 1 DBQ daily (45 min). Focus on refining thesis and evidence quality"

    elif course == 'APGOV':
        # APGOV has 4 FRQs: Concept Application, Quantitative Analysis, SCOTUS Comparison, Argument Essay
        if frq < 40:
            return "Do 3 Concept Application FRQs daily (60 min). Build foundational political analysis"
        elif frq < 55:
            return "Do 2 Concept Apps + 1 Quantitative FRQ daily (75 min). Practice data interpretation"
        elif frq < 70:
            return "Do 3 mixed FRQ types daily (75 min). Focus on SCOTUS comparisons"
        else:
            return "Do 2 FRQs daily (30 min). Polish Argument Essay thesis and maintain skills"

    elif course == 'APHG':
        # APHG has 3 FRQs, no DBQ/LEQ
        if frq < 40:
            return "Do 3 FRQs daily (60 min). Focus on defining geographic concepts with examples"
        elif frq < 55:
            return "Do 4 FRQs daily (75 min). Practice connecting concepts to real-world cases"
        elif frq < 70:
            return "Do 4-5 FRQs daily (75 min). Work on multi-part question organization"
        else:
            return "Do 2-3 FRQs daily (30-45 min). Maintain skills with quality practice"

    # Generic fallback
    if frq < 40:
        return "Do 3-4 short FRQs daily (60 min). Build foundation with practice and review"
    elif frq < 55:
        return "Do 4-5 FRQs daily (75 min). Focus on complete answers with evidence"
    elif frq < 70:
        return "Do 4-5 FRQs daily (75 min). Practice essay structure and time management"
    else:
        return "Do 2-3 FRQs daily (30-45 min). Maintain skills with quality over quantity"


def calculate_recommendation(student_name, course, xp_to_90, daily_xp_rate, late_for_pt, aw_mastery_raw, test_perf=None, unit_details=None, pt_score=None):
    """
    Calculate recommendation for students:

    For students with PT score of 5:
    - Stay: Keep doing what they're doing
    - FRQ: If FRQ accuracy is weak
    - Speed: If they need to pick up pace

    For LATE students:
    - Speed: Just need to work harder (required <= 2x current, and achievable)
    - Holes: Has specific weak units (before frontier), recommend mini-courses
    - Impossible: Can't make it even at max effort

    For ON-TRACK students:
    - Hole-Fill: Has holes (weak units BEFORE their frontier)
    - FRQ: FRQ accuracy significantly lower than MCQ
    - Stay: Balanced, keep doing what they're doing

    A "hole" is a unit the student has passed through but didn't learn.
    Units they haven't reached yet are just incomplete, not holes.
    """
    # Initialize test_perf if not provided
    if test_perf is None:
        test_perf = {'frq_weak': False, 'mcq_accuracy': None, 'frq_accuracy': None}
    if unit_details is None:
        unit_details = []

    # === STUDENTS WITH PT SCORE OF 5 ===
    # They're already excelling - only recommend Stay, FRQ practice, or Speed
    if pt_score == 5:
        has_frq_weakness = test_perf.get('frq_weak', False)
        mcq = test_perf.get('mcq_accuracy', 0) or 0
        frq = test_perf.get('frq_accuracy', 0) or 0

        # Check if they need to speed up (late for PT deadline)
        if late_for_pt:
            today = datetime.now().date()
            days_remaining = count_school_days(today, PT_DEADLINE)
            required_xp_per_day = max(xp_to_90 / days_remaining, MIN_RECOMMENDED_XP_PER_DAY) if days_remaining > 0 else 999
            return {
                'rec': 'Speed',
                'detail': f'PT score 5 - maintain pace ({int(required_xp_per_day)} XP/day needed)',
                'required_xp_day': int(required_xp_per_day),
                'days_remaining': days_remaining,
                'weak_units': [],
                'courses': [],
                'frontier': 0,
                'incomplete': []
            }

        # Check for FRQ weakness
        if has_frq_weakness:
            frq_detail = get_frq_practice_detail(frq, mcq, course)
            return {
                'rec': 'FRQ',
                'detail': f'PT score 5 - FRQ ({frq:.0f}%) vs MCQ ({mcq:.0f}%). {frq_detail}',
                'weak_units': [],
                'courses': [],
                'frontier': 0,
                'incomplete': []
            }

        # Otherwise, stay the course
        return {
            'rec': 'Stay',
            'detail': 'PT score 5 - stay the course, you\'re doing great!',
            'weak_units': [],
            'courses': [],
            'frontier': 0,
            'incomplete': []
        }

    # Build unit name lookup from Austin Way data
    unit_names = {}
    if len(aw_mastery_raw) > 0:
        name_normalized = student_name.lower().strip()
        student_units = aw_mastery_raw[
            (aw_mastery_raw['student_name'].str.lower().str.strip() == name_normalized) &
            (aw_mastery_raw['course'] == course)
        ]
        for _, row in student_units.iterrows():
            unit_id = str(row.get('unit_id', ''))
            unit_names[unit_id] = row.get('unit_name', f'Unit {unit_id}')

    # Find the "frontier" - highest CED unit where student has done meaningful work (>20% combined)
    # Units BEFORE frontier with <60% are "holes"
    # Units AT or AFTER frontier with <60% are just "incomplete"
    # Skip non-CED units (U0, etc.) - they don't affect frontier
    frontier = 0
    for ud in unit_details:
        unit_num = ud.get('unit', '')
        combined = ud.get('combined', 0)
        non_ced = ud.get('non_ced', False)
        if non_ced:
            continue
        try:
            unit_int = int(unit_num)
            if combined > 20 and unit_int > frontier:
                frontier = unit_int
        except (ValueError, TypeError):
            pass

    # Identify holes (weak units BEFORE frontier) vs incomplete (at/after frontier)
    # SKIP non-CED units (U0, etc.) - they don't count for completion or holes
    holes = []
    incomplete = []
    for ud in unit_details:
        unit_num = ud.get('unit', '')
        combined = ud.get('combined', 0)
        non_ced = ud.get('non_ced', False)

        # Skip non-CED units entirely - they don't count for holes or completion
        if non_ced:
            continue

        try:
            unit_int = int(unit_num)
        except (ValueError, TypeError):
            continue

        if combined < 60:
            unit_id = f'u{unit_num}'
            unit_info = {
                'unit_id': unit_id,
                'unit_name': unit_names.get(unit_id, f'Unit {unit_num}'),
                'mastery': int(combined),
                'non_ced': False  # By definition, we've filtered out non-CED
            }
            if unit_int < frontier:
                holes.append(unit_info)
            else:
                incomplete.append(unit_info)

    # For backward compatibility, weak_units = holes only (not incomplete)
    weak_units = holes

    # Find matching mini-courses for weak units
    recommended_courses = []
    if course in MINI_COURSES:
        weak_unit_ids = {u['unit_id'] for u in weak_units}
        for mc in MINI_COURSES[course]:
            if any(u in weak_unit_ids for u in mc['units']):
                recommended_courses.append(mc)

    # === ON-TRACK STUDENTS ===
    if not late_for_pt:
        has_holes = len(holes) > 0
        has_frq_weakness = test_perf.get('frq_weak', False)
        mcq = test_perf.get('mcq_accuracy', 0) or 0
        frq = test_perf.get('frq_accuracy', 0) or 0

        # FIRST: Check if course is complete - they need PT before anything else
        # Last content units: APHG=7, APGOV=5, APUSH=9, APWH=9
        last_content_unit = {'APHG': 7, 'APGOV': 5, 'APUSH': 9, 'APWH': 9}.get(course, 9)
        course_complete = (
            len(incomplete) == 0 or
            xp_to_90 <= 0 or
            frontier >= last_content_unit
        )
        if course_complete:
            return {
                'rec': 'PT',
                'detail': 'Course complete — take practice test for new baseline',
                'weak_units': [],
                'courses': [],
                'frontier': frontier,
                'incomplete': incomplete
            }

        # NOT complete yet - check for holes and FRQ issues
        # Both holes AND FRQ weakness
        if has_holes and has_frq_weakness:
            hole_units = ', '.join([h['unit_id'].replace('u', '') for h in holes])
            frq_detail = get_frq_practice_detail(frq, mcq, course)
            return {
                'rec': 'Hole+FRQ',
                'detail': f'Holes in unit(s) {hole_units} — fix first, then FRQ: {frq_detail}',
                'weak_units': holes,
                'courses': recommended_courses,
                'frontier': frontier,
                'incomplete': incomplete
            }
        # Just holes
        if has_holes:
            hole_units = ', '.join([h['unit_id'].replace('u', '') for h in holes])
            return {
                'rec': 'Hole-Fill',
                'detail': f'Hole in unit(s) {hole_units} — revisit before moving on (frontier: unit {frontier})',
                'weak_units': holes,
                'courses': recommended_courses,
                'frontier': frontier,
                'incomplete': incomplete
            }
        # Just FRQ weakness (but not complete)
        if has_frq_weakness:
            frq_detail = get_frq_practice_detail(frq, mcq, course)
            return {
                'rec': 'FRQ',
                'detail': f'FRQ ({frq:.0f}%) vs MCQ ({mcq:.0f}%). {frq_detail}',
                'weak_units': [],
                'courses': [],
                'frontier': frontier,
                'incomplete': incomplete
            }
        # Otherwise, stay the course
        return {
            'rec': 'Stay',
            'detail': f'On track — continue through remaining units (frontier: unit {frontier})',
            'weak_units': [],
            'courses': [],
            'frontier': frontier,
            'incomplete': incomplete
        }

    # === LATE STUDENTS ===
    # Calculate school days remaining and required XP/day
    today = datetime.now().date()
    days_remaining = count_school_days(today, PT_DEADLINE)
    if (PT_DEADLINE - today).days <= 0:
        return {'rec': 'Impossible', 'detail': 'Deadline has passed', 'courses': [], 'weak_units': holes, 'frontier': frontier, 'incomplete': incomplete}

    required_xp_per_day = max(xp_to_90 / days_remaining, MIN_RECOMMENDED_XP_PER_DAY)

    # Determine recommendation
    if required_xp_per_day > MAX_REASONABLE_XP_PER_DAY:
        return {
            'rec': 'Impossible',
            'detail': f'Need {int(required_xp_per_day)} XP/day but max reasonable is {MAX_REASONABLE_XP_PER_DAY}',
            'required_xp_day': int(required_xp_per_day),
            'days_remaining': days_remaining,
            'weak_units': holes,
            'courses': recommended_courses,
            'frontier': frontier,
            'incomplete': incomplete
        }

    # If they have actual holes (not just incomplete) and matching courses, recommend hole-filling
    if holes and recommended_courses and daily_xp_rate > 0 and required_xp_per_day <= 1.5 * daily_xp_rate:
        hole_units = ', '.join([h['unit_id'].replace('u', '') for h in holes])
        return {
            'rec': 'Holes',
            'detail': f'Fill holes in unit(s) {hole_units} with targeted courses',
            'required_xp_day': int(required_xp_per_day),
            'days_remaining': days_remaining,
            'weak_units': holes,
            'courses': recommended_courses,
            'frontier': frontier,
            'incomplete': incomplete
        }

    # If they just need to speed up (achievable increase)
    if daily_xp_rate > 0 and required_xp_per_day <= 2 * daily_xp_rate:
        multiplier = required_xp_per_day / daily_xp_rate if daily_xp_rate > 0 else 999
        return {
            'rec': 'Speed',
            'detail': f'Need {multiplier:.1f}x current pace ({int(required_xp_per_day)} vs {int(daily_xp_rate)} XP/day)',
            'required_xp_day': int(required_xp_per_day),
            'days_remaining': days_remaining,
            'weak_units': holes,
            'courses': recommended_courses,
            'frontier': frontier,
            'incomplete': incomplete
        }

    # If they have actual holes and courses available, still recommend holes
    if holes and recommended_courses:
        hole_units = ', '.join([h['unit_id'].replace('u', '') for h in holes])
        return {
            'rec': 'Holes',
            'detail': f'Focus on holes in unit(s) {hole_units}',
            'required_xp_day': int(required_xp_per_day),
            'days_remaining': days_remaining,
            'weak_units': holes,
            'courses': recommended_courses,
            'frontier': frontier,
            'incomplete': incomplete
        }

    # Otherwise it's a speed issue (but harder)
    return {
        'rec': 'Speed',
        'detail': f'Need to increase to {int(required_xp_per_day)} XP/day',
        'required_xp_day': int(required_xp_per_day),
        'days_remaining': days_remaining,
        'weak_units': holes,
        'courses': [],
        'frontier': frontier,
        'incomplete': incomplete
    }


def build_unified_table(data):
    """Build the unified student table."""
    tracker = data['tracker']
    practice = data['practice']
    aw_mastery = data['aw_mastery']
    aw_mastery_raw = data['aw_mastery_raw']
    aw_activity = data['aw_activity']
    aw_daily_raw = data['aw_daily_raw']
    timeback = data['timeback']
    lesson_details = data['lesson_details']

    if len(tracker) == 0:
        return []

    rows = []

    for _, t in tracker.iterrows():
        student_name = t['Student']
        student_id = t.get('student_alpha_id', '')
        email = student_name.lower().replace(' ', '.') + '@alpha.school'  # Approximate
        course_full = t['Course']

        # Simplify course name
        if 'Geography' in course_full:
            course = 'APHG'
        elif 'World History' in course_full:
            course = 'APWH'
        elif 'United States History' in course_full:
            course = 'APUSH'
        elif 'Government' in course_full:
            course = 'APGOV'
        else:
            course = course_full

        # Tracker data
        progress = t.get('Progress', 0)
        mcq = t.get('MCQ', None)
        frq = t.get('FRQ', None)

        # Austin Way mastery for this course
        aw_course_match = aw_mastery[
            (aw_mastery['student_name'].str.lower().str.contains(student_name.lower().split()[0], na=False)) &
            (aw_mastery['course'] == course)
        ] if len(aw_mastery) > 0 else pd.DataFrame()

        aw_mastery_pct = aw_course_match['course_mastery_pct'].iloc[0] if len(aw_course_match) > 0 else None

        # Austin Way activity
        aw_act_match = aw_activity[
            aw_activity['Student'].str.lower().str.contains(student_name.lower().split()[0], na=False)
        ] if len(aw_activity) > 0 else pd.DataFrame()

        aw_completed = int(aw_act_match['Completed'].iloc[0]) if len(aw_act_match) > 0 else None
        aw_last_active = aw_act_match['Date'].iloc[0] if len(aw_act_match) > 0 else None
        aw_rate = int(aw_act_match['completion_rate'].iloc[0]) if len(aw_act_match) > 0 else None

        # Practice test - match on full name AND course
        pt_match = pd.DataFrame()
        if len(practice) > 0:
            # Simplify course name for matching
            if 'Geography' in course_full:
                course_match = 'Geography'
            elif 'World History' in course_full:
                course_match = 'World History'
            elif 'United States History' in course_full:
                course_match = 'United States History'
            elif 'Government' in course_full:
                course_match = 'Government'
            else:
                course_match = course_full.split(' - ')[0]

            # Try exact match first
            name_lower = student_name.lower().strip()
            pt_match = practice[
                (practice['student'].str.lower().str.strip() == name_lower) &
                (practice['course'].str.contains(course_match, case=False, na=False))
            ]
            # If no exact match, try matching first AND last name (both required)
            if len(pt_match) == 0:
                name_parts = name_lower.split()
                if len(name_parts) >= 2:
                    first, last = name_parts[0], name_parts[-1]
                    # Both first and last must match as whole words (not substrings)
                    pt_match = practice[
                        (practice['course'].str.contains(course_match, case=False, na=False))
                    ].copy()
                    if len(pt_match) > 0:
                        # Check each candidate
                        matches = []
                        for idx, row in pt_match.iterrows():
                            pt_name = row['student'].lower().strip()
                            pt_parts = pt_name.replace('-', ' ').split()
                            # First name must match start, last name must match end
                            if pt_parts and pt_parts[0] == first and pt_parts[-1] == last:
                                matches.append(idx)
                        pt_match = pt_match.loc[matches] if matches else pd.DataFrame()

        pt_score = int(pt_match['ap_score (from albert calculator)'].iloc[0]) if len(pt_match) > 0 and pd.notna(pt_match['ap_score (from albert calculator)'].iloc[0]) else None
        pt_mcq = int(pt_match['final_mcq_accuracy'].iloc[0] * 100) if len(pt_match) > 0 and pd.notna(pt_match['final_mcq_accuracy'].iloc[0]) else None
        pt_frq = int(pt_match['final_frq_accuracy'].iloc[0] * 100) if len(pt_match) > 0 and pd.notna(pt_match['final_frq_accuracy'].iloc[0]) else None

        # Calculate XP data
        xp_data = calculate_student_xp(timeback, lesson_details, aw_daily_raw, student_name, student_id, course)
        current_xp = xp_data['total_xp']
        daily_xp_rate = xp_data['daily_rate']

        # Calculate COMBINED progress unit-by-unit (take max per unit, then average)
        unit_progress_data = calculate_unit_combined_progress(
            student_name, student_id, course, timeback, aw_mastery_raw
        )
        unit_based_combined = unit_progress_data['combined_progress']
        unit_details = unit_progress_data['unit_details']

        combined_progress = unit_based_combined

        # Calculate XP to 90% using COMBINED progress
        # Note: PT 4+ students still see actual progress (may have holes to address)
        current_mastery = combined_progress / 100
        target_mastery = 0.90
        total_skills = COURSE_TOTAL_SKILLS.get(course, 0)
        xp_per_skill = XP_PER_SKILL.get(course, 10)

        # Skills remaining = (90% - current%) * total_skills
        # XP remaining = skills_remaining * xp_per_skill
        if current_mastery < target_mastery and total_skills > 0:
            skills_remaining = (target_mastery - current_mastery) * total_skills
            xp_to_90 = int(skills_remaining * xp_per_skill)
        else:
            xp_to_90 = 0

        # Projected date to 90%
        # April 16, 2026 is the practice test deadline
        pt_deadline = datetime(2026, 4, 16).date()
        late_for_pt = False
        projected_date = None

        if daily_xp_rate > 0 and xp_to_90 > 0:
            days_to_90 = xp_to_90 / daily_xp_rate
            projected_date = (datetime.now() + timedelta(days=days_to_90)).date()
            # Cap at reasonable future (2 years)
            if days_to_90 > 730:
                projected_90_str = '>2y'
                late_for_pt = True
            else:
                projected_90_str = projected_date.strftime('%b %d')
                late_for_pt = projected_date > pt_deadline
        elif xp_to_90 == 0:
            projected_90_str = 'Done'
            late_for_pt = False
        else:
            projected_90_str = 'Never'
            late_for_pt = True

        # Calculate combined/estimated readiness
        scores = []
        if mcq is not None and pd.notna(mcq):
            scores.append(mcq)
        if frq is not None and pd.notna(frq):
            scores.append(frq)
        if aw_mastery_pct is not None:
            scores.append(aw_mastery_pct)

        combined_est = round(sum(scores) / len(scores), 0) if scores else None

        # Calculate test performance (MCQ vs FRQ from Timeback)
        test_perf = calculate_test_performance(student_id, course, timeback)

        # Calculate recommendation first (needed for risk assessment)
        rec_data = calculate_recommendation(
            student_name, course, xp_to_90, daily_xp_rate, late_for_pt, aw_mastery_raw, test_perf, unit_details, pt_score
        )

        # Risk assessment - factors in both knowledge AND deadline
        risk = 'Unknown'
        if pt_score is not None:
            # Have PT score - use it as primary indicator
            if pt_score <= 2:
                risk = 'Critical'
            elif pt_score == 3:
                risk = 'At Risk'
            elif pt_score == 4:
                risk = 'On Track'
            else:
                risk = 'Strong'
        elif combined_est is not None:
            if combined_est < 50:
                risk = 'Critical'
            elif combined_est < 65:
                risk = 'At Risk'
            elif combined_est < 80:
                risk = 'On Track'
            else:
                risk = 'Strong'

        # Override risk if deadline makes it impossible/unlikely
        # BUT don't override if they have a PT score of 4+ (they've proven readiness)
        if pt_score is None or pt_score < 4:
            if rec_data['rec'] == 'Impossible':
                if risk in ('On Track', 'Strong', 'Unknown'):
                    risk = 'Critical'
            elif rec_data['rec'] in ('Speed', 'Holes'):
                if risk in ('On Track', 'Strong', 'Unknown'):
                    risk = 'At Risk'

        # Calculate TB-only progress from unit data (consistent with combined calculation)
        ced_units = [ud for ud in unit_details if not ud.get('non_ced', False)]
        tb_from_units = sum(ud['timeback'] for ud in ced_units) / len(ced_units) if ced_units else 0
        aw_from_units = sum(ud['austin_way'] for ud in ced_units) / len(ced_units) if ced_units else 0

        rows.append({
            'student': student_name,
            'student_id': student_id,
            'course': course,
            'timeback_progress': round(tb_from_units, 1),
            'aw_mastery': round(aw_from_units, 1) if aw_from_units > 0 else (int(aw_mastery_pct) if aw_mastery_pct is not None else None),
            'combined_progress': round(combined_progress, 1),
            'unit_details': unit_details,
            'mcq': round(mcq, 1) if pd.notna(mcq) else None,
            'frq': round(frq, 1) if pd.notna(frq) else None,
            'aw_completed_14d': aw_completed,
            'aw_rate': aw_rate,
            'aw_last_active': aw_last_active,
            'pt_score': pt_score,
            'pt_mcq': pt_mcq,
            'pt_frq': pt_frq,
            'combined_est': int(combined_est) if combined_est is not None else None,
            'risk': risk,
            'current_xp': current_xp,
            'tb_xp': xp_data['tb_xp'],
            'aw_xp': xp_data['aw_xp'],
            'daily_xp': daily_xp_rate,
            'xp_to_90': xp_to_90,
            'projected_90': projected_90_str,
            'late_for_pt': late_for_pt,
            'recommendation': rec_data['rec'],
            'rec_detail': rec_data.get('detail', ''),
            'rec_required_xp': rec_data.get('required_xp_day', 0),
            'rec_days': rec_data.get('days_remaining', 0),
            'weak_units': rec_data.get('weak_units', []),
            'rec_courses': rec_data.get('courses', []),
            'frontier': rec_data.get('frontier', 0),
            'incomplete': rec_data.get('incomplete', []),
            'tb_mcq_accuracy': test_perf.get('mcq_accuracy'),
            'tb_frq_accuracy': test_perf.get('frq_accuracy'),
            'frq_weak': test_perf.get('frq_weak', False),
        })

        # Add locked recommendation and progress delta
        locked_data = get_locked_data(student_name, course)
        if locked_data:
            rows[-1]['locked_recommendation'] = locked_data.get('rec')
            rows[-1]['locked_rec_detail'] = locked_data.get('detail')
            locked_progress = locked_data.get('combined_progress', 0)
            rows[-1]['progress_vs_last_week'] = round(combined_progress - locked_progress, 1)
        else:
            rows[-1]['locked_recommendation'] = None
            rows[-1]['locked_rec_detail'] = None
            rows[-1]['progress_vs_last_week'] = None

    return rows


def get_student_timeseries(data, student_name, course):
    """Get daily unified XP time series for a student (Timeback + Austin Way)."""
    import re
    from collections import defaultdict

    timeback = data['timeback']
    lesson_details = data['lesson_details']
    tracker = data['tracker']
    aw_daily_raw = data['aw_daily_raw']

    today = datetime.now().date()
    all_days = pd.date_range(end=today, periods=30).date

    # Initialize daily data
    daily_data = {d: {'tb_xp': 0, 'tb_items': 0, 'aw_xp': 0, 'aw_items': 0} for d in all_days}

    # Find student ID
    student_match = tracker[tracker['Student'] == student_name]
    student_id = student_match['student_alpha_id'].iloc[0] if len(student_match) > 0 else None

    # === TIMEBACK XP ===
    tb_course = None
    for tb_name, norm in TIMEBACK_COURSE_MAP.items():
        if norm == course:
            tb_course = tb_name
            break

    if tb_course and len(timeback) > 0 and student_id:
        def resource_key(tb_id):
            m = re.search(r'(r\d+)', str(tb_id))
            return m.group(1) if m else str(tb_id)

        student_data = timeback[
            (timeback['student_alpha_id'] == student_id) &
            (timeback['course_on_timeback'] == tb_course) &
            (timeback['completed_at'].notna())
        ].copy()

        if len(student_data) > 0:
            student_data['rkey'] = student_data['item_tb_id'].apply(resource_key)
            lesson_details_course = lesson_details[lesson_details['course_on_timeback'] == tb_course].copy()
            lesson_details_course['rkey'] = lesson_details_course['item_tb_id'].apply(resource_key)

            merged = student_data.merge(
                lesson_details_course[['rkey', 'item_xp']],
                on='rkey',
                how='left'
            )
            merged['item_xp'] = merged['item_xp'].fillna(0)
            merged['completed_date'] = pd.to_datetime(merged['completed_at']).dt.date

            for date, group in merged.groupby('completed_date'):
                if date in daily_data:
                    daily_data[date]['tb_xp'] = group['item_xp'].sum()
                    daily_data[date]['tb_items'] = len(group)

    # === AUSTIN WAY XP ===
    xp_per_skill = XP_PER_SKILL.get(course, 10)

    if len(aw_daily_raw) > 0:
        first_name = student_name.lower().split()[0]
        aw_student = aw_daily_raw[
            (aw_daily_raw['Student'].str.lower().str.split().str[0] == first_name) &
            (aw_daily_raw['Course'] == course)
        ].copy()

        if len(aw_student) > 0:
            aw_student['Date'] = pd.to_datetime(aw_student['Date']).dt.date
            for _, row in aw_student.iterrows():
                date = row['Date']
                if date in daily_data:
                    daily_data[date]['aw_xp'] = row['Completed'] * xp_per_skill
                    daily_data[date]['aw_items'] = row['Completed']

    # Build result
    result = []
    for date in all_days:
        d = daily_data[date]
        result.append({
            'date': str(date),
            'xp': int(d['tb_xp'] + d['aw_xp']),
            'tb_xp': int(d['tb_xp']),
            'aw_xp': int(d['aw_xp']),
            'items': int(d['tb_items'] + d['aw_items'])
        })

    return result


# =============================================================================
# DATA REFRESH
# =============================================================================

try:
    import browser_cookie3
    BROWSER_COOKIES_AVAILABLE = True
except ImportError:
    BROWSER_COOKIES_AVAILABLE = False

# Timeback API URLs
TIMEBACK_TOKEN_URL = "https://prod-beyond-timeback-api-2-idp.auth.us-east-1.amazoncognito.com/oauth2/token"
TIMEBACK_API_BASE = "https://api.alpha-1edtech.ai"

# Austin Way API
AUSTIN_WAY_API_BASE = "https://api.aphistoryforge.com/api/guide"
AUSTIN_WAY_AUTH_FILE = BASE_DIR / "austin_way_auth.txt"
AUSTIN_WAY_OUTPUT_FILE = BASE_DIR / "austin_way_mastery.csv"

# Timeback output
TIMEBACK_LEARNING_DATA_FILE = DATA_DIR / "ap_social_studies_learning_data.csv"


class TimebackAuth:
    """OAuth2 client-credentials auth with automatic token refresh."""

    def __init__(self, client_id, client_secret):
        self.client_id = client_id
        self.client_secret = client_secret
        self._token = None
        self._expires_at = None

    def get_headers(self):
        if not self._token or datetime.now() >= self._expires_at:
            self._refresh()
        return {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
        }

    def _refresh(self):
        resp = requests.post(
            TIMEBACK_TOKEN_URL,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data={
                "grant_type": "client_credentials",
                "client_id": self.client_id,
                "client_secret": self.client_secret,
            },
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        self._token = data["access_token"]
        self._expires_at = datetime.now() + timedelta(seconds=data["expires_in"] - 300)


def make_retry_session():
    """Create a requests session with retry logic."""
    session = requests.Session()
    retry = Retry(total=3, backoff_factor=1, status_forcelist=[429, 500, 502, 503, 504])
    session.mount("https://", HTTPAdapter(max_retries=retry))
    return session


def harvest_austin_way_cookie():
    """Harvest aph_auth cookie from browser and save to auth file."""
    if not BROWSER_COOKIES_AVAILABLE:
        return {"success": False, "message": "browser-cookie3 not installed"}

    # Try different browsers
    browsers = [
        ('Chrome', browser_cookie3.chrome),
        ('Firefox', browser_cookie3.firefox),
        ('Edge', browser_cookie3.edge),
    ]

    for browser_name, browser_fn in browsers:
        try:
            cj = browser_fn(domain_name='.aphistoryforge.com')
            for cookie in cj:
                if cookie.name == 'aph_auth':
                    # Save to file
                    AUSTIN_WAY_AUTH_FILE.write_text(cookie.value)
                    return {
                        "success": True,
                        "message": f"Harvested aph_auth cookie from {browser_name}",
                        "browser": browser_name
                    }
        except Exception as e:
            continue

    return {"success": False, "message": "No aph_auth cookie found in any browser. Please sign in at aphistoryforge.com first."}


def refresh_austin_way():
    """Refresh Austin Way mastery data by calling the existing scraper."""
    results = {"success": False, "message": "", "rows": 0}

    try:
        import subprocess
        result = subprocess.run(
            ['python', str(BASE_DIR / 'austin_way_scraper.py')],
            capture_output=True,
            text=True,
            timeout=120
        )

        if result.returncode == 0:
            # Parse output for row count
            output = result.stdout
            if 'Wrote' in output:
                import re
                match = re.search(r'Wrote (\d+) rows', output)
                if match:
                    results["rows"] = int(match.group(1))
            results["success"] = True
            results["message"] = f"Scraper completed. {results['rows']} rows written."
        else:
            error = result.stderr or result.stdout
            if 'expired' in error.lower() or '401' in error or 'Unauthorized' in error:
                results["message"] = "Auth cookie expired. Please sign in and paste new cookie."
            else:
                results["message"] = f"Scraper failed: {error[:200]}"

    except subprocess.TimeoutExpired:
        results["message"] = "Scraper timed out after 120 seconds"
    except Exception as e:
        results["message"] = f"Error running scraper: {e}"

    return results


def refresh_timeback():
    """Refresh Timeback learning data by calling the safe refresh script."""
    results = {"success": False, "message": "", "rows": 0}

    try:
        import subprocess
        result = subprocess.run(
            ['python', str(BASE_DIR / 'refresh_timeback_safe.py')],
            capture_output=True,
            text=True,
            timeout=300  # 5 minutes for all API calls
        )

        output = result.stdout + result.stderr

        if result.returncode == 0 and 'Saved to:' in output:
            # Parse completion count
            import re
            match = re.search(r'Rows with completion: (\d+)', output)
            if match:
                results["rows"] = int(match.group(1))

            # Check for new completions
            new_match = re.search(r'\+(\d+) new completions', output)
            if new_match:
                results["message"] = f"Refreshed with {new_match.group(1)} new completions"
            else:
                results["message"] = f"Refreshed ({results['rows']} completed items)"

            # Auto-copy the new file over the old one
            new_file = DATA_DIR / 'ap_social_studies_learning_data_NEW.csv'
            old_file = DATA_DIR / 'ap_social_studies_learning_data.csv'
            if new_file.exists():
                import shutil
                shutil.copy(new_file, old_file)

            results["success"] = True
        else:
            # Extract error message
            if 'ERROR' in output:
                error_line = [l for l in output.split('\n') if 'ERROR' in l]
                results["message"] = error_line[0] if error_line else "Unknown error"
            else:
                results["message"] = f"Script failed: {output[-500:]}"

    except subprocess.TimeoutExpired:
        results["message"] = "Refresh timed out after 5 minutes"
    except Exception as e:
        results["message"] = f"Error: {e}"

    return results


def _refresh_timeback_DISABLED():
    """DISABLED - was corrupting data. Refresh Timeback learning data from PowerPath API."""
    results = {"success": False, "message": "", "rows": 0}

    # Check for credentials
    client_id = os.environ.get("TIMEBACK_CLIENT_ID")
    client_secret = os.environ.get("TIMEBACK_CLIENT_SECRET")
    if not client_id or not client_secret:
        results["message"] = "TIMEBACK_CLIENT_ID and TIMEBACK_CLIENT_SECRET not set in .env"
        return results

    # Load enrollments
    enrollments_file = DATA_DIR / "ap_social_studies_enrollments.csv"
    if not enrollments_file.exists():
        results["message"] = f"Enrollments file not found: {enrollments_file}"
        return results

    enrollments = pd.read_csv(enrollments_file)

    # Load lesson details scaffold
    lesson_details_file = DATA_DIR / "ap_social_studies_lesson_details_combined.csv"
    if not lesson_details_file.exists():
        results["message"] = f"Lesson details file not found: {lesson_details_file}"
        return results

    lesson_details = pd.read_csv(lesson_details_file)

    try:
        auth = TimebackAuth(client_id, client_secret)
        session = make_retry_session()

        # Build resource key extraction function
        def resource_key(tb_id):
            m = re.search(r'(r\d+)', str(tb_id))
            return m.group(1) if m else str(tb_id)

        # Fetch progress for each enrollment
        all_progress = []
        for _, enrollment in enrollments.iterrows():
            student_tb_id = enrollment['student_timeback_id']
            course_tb_id = enrollment['course_timeback_id']
            student_alpha_id = enrollment['student_alpha_id']

            try:
                url = f"{TIMEBACK_API_BASE}/powerpath/lessonPlans/getCourseProgress/{course_tb_id}/student/{student_tb_id}"
                resp = session.get(url, headers=auth.get_headers(), timeout=30)

                if resp.status_code == 200:
                    items = resp.json()
                    for item in items:
                        item_tb_id = item.get('courseComponentResourceSourcedId', '')
                        results_list = item.get('results', [])

                        if results_list:
                            # Get latest result
                            latest = max(results_list, key=lambda x: x.get('scoreDate', ''))
                            completed_at = latest.get('scoreDate')
                            text_score = latest.get('textScore')
                            score = latest.get('score')

                            # Only use score as accuracy for graded items (textScore is null)
                            accuracy = score if text_score is None else None

                            all_progress.append({
                                'student_timeback_id': student_tb_id,
                                'student_alpha_id': student_alpha_id,
                                'course_timeback_id': course_tb_id,
                                'item_tb_id': item_tb_id,
                                'rkey': resource_key(item_tb_id),
                                'completed_at': completed_at,
                                'accuracy': accuracy,
                            })

                time.sleep(0.1)  # Rate limiting
            except Exception as e:
                print(f"Error fetching progress for {student_alpha_id}/{course_tb_id}: {e}")
                continue

        # Build scaffold with resource keys
        lesson_details['rkey'] = lesson_details['item_tb_id'].apply(resource_key)

        # Drop course_on_timeback from enrollments to avoid _x/_y suffix after merge
        enrollments_clean = enrollments.drop(columns=['course_on_timeback'], errors='ignore')

        # Cross enrollments with lesson details to get full scaffold
        scaffold = enrollments_clean.merge(
            lesson_details,
            on='course_timeback_id',
            how='inner'
        )

        if all_progress:
            progress_df = pd.DataFrame(all_progress)

            # Merge scaffold with progress
            learning_data = scaffold.merge(
                progress_df[['student_timeback_id', 'course_timeback_id', 'rkey', 'completed_at', 'accuracy']],
                on=['student_timeback_id', 'course_timeback_id', 'rkey'],
                how='left'
            )
        else:
            learning_data = scaffold.copy()
            learning_data['completed_at'] = None
            learning_data['accuracy'] = None

        # Save to CSV
        learning_data.to_csv(TIMEBACK_LEARNING_DATA_FILE, index=False)

        results["success"] = True
        results["rows"] = len(learning_data)
        results["message"] = f"Wrote {len(learning_data)} rows ({len(all_progress)} completed items)"

    except Exception as e:
        results["message"] = f"Error: {e}"

    return results


# =============================================================================
# FLASK ROUTES
# =============================================================================

DASHBOARD_HTML = '''
<!DOCTYPE html>
<html>
<head>
    <title>AP Social Studies Dashboard</title>
    <style>
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
            background: #1a1a2e;
            color: #eee;
            padding: 20px;
        }
        h1 { margin-bottom: 10px; color: #fff; }
        .subtitle { color: #888; margin-bottom: 20px; }

        .nav { margin-bottom: 20px; }
        .nav a {
            color: #4da6ff;
            margin-right: 20px;
            text-decoration: none;
        }
        .nav a:hover { text-decoration: underline; }

        .filters {
            display: flex;
            gap: 15px;
            margin-bottom: 20px;
            flex-wrap: wrap;
        }
        .filters select, .filters input {
            padding: 8px 12px;
            border: 1px solid #333;
            border-radius: 4px;
            background: #252540;
            color: #fff;
            font-size: 14px;
        }

        table {
            width: 100%;
            border-collapse: collapse;
            font-size: 12px;
        }
        th, td {
            padding: 8px 6px;
            text-align: left;
            border-bottom: 1px solid #333;
        }
        th {
            background: #252540;
            cursor: pointer;
            user-select: none;
            position: sticky;
            top: 0;
            white-space: nowrap;
        }
        th:hover { background: #353560; }
        th.sorted-asc::after { content: ' ▲'; }
        th.sorted-desc::after { content: ' ▼'; }

        tr:hover { background: #252540; cursor: pointer; }

        .course-tag {
            padding: 2px 6px;
            border-radius: 4px;
            font-size: 10px;
            font-weight: bold;
        }
        .course-APHG { background: #2d5a27; }
        .course-APWH { background: #5a2727; }
        .course-APUSH { background: #27415a; }
        .course-APGOV { background: #5a4a27; }

        .risk-Critical { color: #ff4444; font-weight: bold; }
        .risk-At-Risk { color: #ffaa00; }
        .risk-On-Track { color: #88cc88; }
        .risk-Strong { color: #44ff44; }
        .risk-Unknown { color: #888; }

        .metric { font-family: monospace; }
        .metric-good { color: #88cc88; }
        .metric-ok { color: #cccc88; }
        .metric-bad { color: #cc8888; }
        .metric-null { color: #666; }

        .activity-hot { color: #44ff44; }
        .activity-warm { color: #ffaa00; }
        .activity-cold { color: #ff4444; }

        .projection-good { color: #44ff44; }
        .projection-ok { color: #ffaa00; }
        .projection-bad { color: #ff4444; }
        .projection-done { color: #88cc88; }

        .rec-stay { color: #44ff44; }
        .rec-pt { color: #44dddd; }
        .rec-hole-fill { color: #ff8844; }
        .rec-frq { color: #aa88ff; }
        .rec-hole-frq { color: #ff88ff; font-weight: bold; }
        .rec-speed { color: #ffaa00; font-weight: bold; }
        .rec-holes { color: #ff8844; font-weight: bold; }
        .rec-impossible { color: #ff4444; font-weight: bold; }

        .summary-cards {
            display: flex;
            gap: 15px;
            margin-bottom: 20px;
            flex-wrap: wrap;
        }
        .card {
            background: #252540;
            padding: 12px 16px;
            border-radius: 8px;
            min-width: 120px;
        }
        .card-value { font-size: 24px; font-weight: bold; }
        .card-label { color: #888; font-size: 11px; }

        .pt-score {
            display: inline-block;
            width: 22px;
            height: 22px;
            line-height: 22px;
            text-align: center;
            border-radius: 50%;
            font-weight: bold;
            font-size: 11px;
        }
        .pt-1, .pt-2 { background: #ff4444; color: #fff; }
        .pt-3 { background: #ffaa00; color: #000; }
        .pt-4 { background: #88cc88; color: #000; }
        .pt-5 { background: #44ff44; color: #000; }

        .xp-bar {
            width: 60px;
            height: 8px;
            background: #333;
            border-radius: 4px;
            overflow: hidden;
            display: inline-block;
            vertical-align: middle;
            margin-left: 5px;
        }
        .xp-bar-fill {
            height: 100%;
            background: linear-gradient(90deg, #4da6ff, #44ff44);
        }
    </style>
</head>
<body>
    <h1>AP Social Studies Dashboard</h1>
    <p class="subtitle">APHG / AP World / APUSH / AP Gov — {{ students|length }} students — Exam: May 2026</p>

    <nav class="nav">
        <a href="/">Dashboard</a>
        <a href="/coaching">Coaching</a>
        <a href="/comms">Communications</a>
        <a href="/refresh">Refresh Data</a>
        <a href="/settings">Settings</a>
    </nav>

    <div class="summary-cards">
        <div class="card">
            <div class="card-value" style="color: #ff4444;">{{ summary.critical }}</div>
            <div class="card-label">Critical</div>
        </div>
        <div class="card">
            <div class="card-value" style="color: #ffaa00;">{{ summary.at_risk }}</div>
            <div class="card-label">At Risk</div>
        </div>
        <div class="card">
            <div class="card-value" style="color: #88cc88;">{{ summary.on_track }}</div>
            <div class="card-label">On Track</div>
        </div>
        <div class="card">
            <div class="card-value" style="color: #666;">{{ summary.no_pt }}</div>
            <div class="card-label">No PT Score</div>
        </div>
        <div class="card">
            <div class="card-value" style="color: #ff4444;">{{ summary.late_for_pt }}</div>
            <div class="card-label">Late for Apr 16 PT</div>
        </div>
    </div>

    <div class="filters">
        <select id="filter-course">
            <option value="">All Courses</option>
            <option value="APHG">APHG</option>
            <option value="APWH">AP World</option>
            <option value="APUSH">APUSH</option>
            <option value="APGOV">AP Gov</option>
        </select>
        <select id="filter-risk">
            <option value="">All Risk Levels</option>
            <option value="Critical">Critical</option>
            <option value="At Risk">At Risk</option>
            <option value="On Track">On Track</option>
            <option value="Strong">Strong</option>
        </select>
        <select id="filter-rec">
            <option value="">All Recommendations</option>
            <option value="PT">PT</option>
            <option value="Stay">Stay</option>
            <option value="FRQ">FRQ</option>
            <option value="Hole-Fill">Hole-Fill</option>
            <option value="Hole+FRQ">Hole+FRQ</option>
            <option value="Speed">Speed</option>
            <option value="Holes">Holes</option>
            <option value="Impossible">Impossible</option>
        </select>
        <select id="filter-late">
            <option value="">All Students</option>
            <option value="true">Late for Apr 16</option>
            <option value="false">On Track for Apr 16</option>
        </select>
        <input type="text" id="filter-search" placeholder="Search student...">
        <button onclick="exportCSV()" style="padding: 8px 16px; background: #4da6ff; color: #fff; border: none; border-radius: 4px; cursor: pointer;">Export CSV</button>
    </div>

    <div id="lock-status" style="display: flex; align-items: center; gap: 15px; margin-bottom: 15px; padding: 10px 15px; background: #1a1a2e; border-radius: 6px;">
        <span id="lock-indicator" style="font-size: 14px;"></span>
        <button id="lock-btn" onclick="lockRecommendations()" style="padding: 6px 12px; background: #4da6ff; color: #fff; border: none; border-radius: 4px; cursor: pointer; display: none;">Lock Recommendations</button>
        <button id="unlock-btn" onclick="unlockRecommendations()" style="padding: 6px 12px; background: #ff8844; color: #fff; border: none; border-radius: 4px; cursor: pointer; display: none;">Unlock</button>
        <span id="lock-warning" style="color: #ff8844; font-size: 12px; display: none;"></span>
    </div>

    <table id="student-table">
        <thead>
            <tr>
                <th data-sort="student">Student</th>
                <th data-sort="course">Course</th>
                <th data-sort="risk">Risk</th>
                <th data-sort="pt_score">PT</th>
                <th data-sort="timeback_progress">Timeback</th>
                <th data-sort="aw_mastery">Austin Way</th>
                <th data-sort="combined_progress">Combined</th>
                <th data-sort="progress_vs_last_week">vs Last Wk</th>
                <th data-sort="xp_to_90">XP to 90%</th>
                <th data-sort="projected_90">Proj 90%</th>
                <th data-sort="locked_recommendation" class="locked-col">Rec (Locked)</th>
                <th data-sort="recommendation">Rec (Live)</th>
                <th data-sort="daily_xp">XP/SchoolDay</th>
            </tr>
        </thead>
        <tbody>
            {% for s in students %}
            <tr data-course="{{ s.course }}" data-risk="{{ s.risk }}" data-rec="{{ s.recommendation }}" data-late="{{ 'true' if s.late_for_pt else 'false' }}" data-student="{{ s.student }}" onclick="window.location='/student/{{ s.student|urlencode }}/{{ s.course }}'">
                <td>{{ s.student }}</td>
                <td><span class="course-tag course-{{ s.course }}">{{ s.course }}</span></td>
                <td class="risk-{{ s.risk|replace(' ', '-') }}">{{ s.risk }}</td>
                <td>
                    {% if s.pt_score %}
                    <span class="pt-score pt-{{ s.pt_score }}">{{ s.pt_score }}</span>
                    {% else %}
                    <span class="metric-null">—</span>
                    {% endif %}
                </td>
                <td class="metric {% if s.timeback_progress and s.timeback_progress >= 90 %}metric-good{% elif s.timeback_progress and s.timeback_progress >= 70 %}metric-ok{% elif s.timeback_progress %}metric-bad{% else %}metric-null{% endif %}">
                    {{ s.timeback_progress|default('—', true) }}{% if s.timeback_progress %}%{% endif %}
                </td>
                <td class="metric {% if s.aw_mastery and s.aw_mastery >= 70 %}metric-good{% elif s.aw_mastery and s.aw_mastery >= 50 %}metric-ok{% elif s.aw_mastery %}metric-bad{% else %}metric-null{% endif %}">
                    {{ s.aw_mastery|default('—', true) }}{% if s.aw_mastery %}%{% endif %}
                </td>
                <td class="metric {% if s.combined_progress and s.combined_progress >= 90 %}metric-good{% elif s.combined_progress and s.combined_progress >= 70 %}metric-ok{% elif s.combined_progress %}metric-bad{% else %}metric-null{% endif %}">
                    {{ s.combined_progress|default('—', true) }}{% if s.combined_progress %}%{% endif %}
                </td>
                <td class="{% if s.progress_vs_last_week and s.progress_vs_last_week > 0 %}metric-good{% elif s.progress_vs_last_week == 0 %}metric-null{% elif s.progress_vs_last_week %}metric-bad{% else %}metric-null{% endif %}">
                    {% if s.progress_vs_last_week is not none %}
                    {{ '+' if s.progress_vs_last_week > 0 else '' }}{{ s.progress_vs_last_week }}%
                    {% else %}
                    —
                    {% endif %}
                </td>
                <td class="metric">
                    {% if s.xp_to_90 == 0 %}
                    <span class="metric-good">Done</span>
                    {% elif s.xp_to_90 %}
                    {{ s.xp_to_90 }}
                    {% else %}
                    <span class="metric-null">—</span>
                    {% endif %}
                </td>
                <td class="{% if s.projected_90 == 'Done' %}projection-done{% elif s.late_for_pt %}projection-bad{% else %}projection-ok{% endif %}">
                    {{ s.projected_90|default('—', true) }}
                </td>
                <td class="locked-col rec-{{ s.locked_recommendation|lower|default('none', true) }}">
                    {% if s.locked_recommendation %}
                    {{ s.locked_recommendation }}
                    {% else %}
                    —
                    {% endif %}
                </td>
                <td class="rec-{{ s.recommendation|lower }}">
                    {{ s.recommendation }}{% if s.locked_recommendation and s.locked_recommendation != s.recommendation %} <span style="color: #ff8844;" title="Changed from {{ s.locked_recommendation }}">⚠</span>{% endif %}
                </td>
                <td class="{% if s.daily_xp >= 50 %}activity-hot{% elif s.daily_xp >= 20 %}activity-warm{% elif s.daily_xp > 0 %}activity-cold{% else %}metric-null{% endif %}">
                    {{ s.daily_xp if s.daily_xp else '—' }}
                </td>
            </tr>
            {% endfor %}
        </tbody>
    </table>

    <script>
        // Sorting
        let currentSort = { column: null, desc: false };

        document.querySelectorAll('th[data-sort]').forEach(th => {
            th.addEventListener('click', (e) => {
                e.stopPropagation();
                const column = th.dataset.sort;
                const desc = currentSort.column === column ? !currentSort.desc : false;
                currentSort = { column, desc };

                // Update header classes
                document.querySelectorAll('th').forEach(h => h.classList.remove('sorted-asc', 'sorted-desc'));
                th.classList.add(desc ? 'sorted-desc' : 'sorted-asc');

                // Sort rows
                const tbody = document.querySelector('#student-table tbody');
                const rows = Array.from(tbody.querySelectorAll('tr'));

                rows.sort((a, b) => {
                    const aVal = a.children[getColumnIndex(column)].textContent.trim();
                    const bVal = b.children[getColumnIndex(column)].textContent.trim();

                    // Handle special values
                    if (aVal === 'Never' || aVal === '>2y') return desc ? -1 : 1;
                    if (bVal === 'Never' || bVal === '>2y') return desc ? 1 : -1;
                    if (aVal === 'Done') return desc ? 1 : -1;
                    if (bVal === 'Done') return desc ? -1 : 1;

                    // Handle numeric vs string
                    const aNum = parseFloat(aVal.replace('%', ''));
                    const bNum = parseFloat(bVal.replace('%', ''));

                    let cmp;
                    if (!isNaN(aNum) && !isNaN(bNum)) {
                        cmp = aNum - bNum;
                    } else if (aVal === '—' && bVal !== '—') {
                        cmp = 1;
                    } else if (aVal !== '—' && bVal === '—') {
                        cmp = -1;
                    } else {
                        cmp = aVal.localeCompare(bVal);
                    }

                    return desc ? -cmp : cmp;
                });

                rows.forEach(row => tbody.appendChild(row));
            });
        });

        function getColumnIndex(column) {
            const columns = ['student', 'course', 'risk', 'pt_score', 'timeback_progress', 'aw_mastery', 'combined_progress', 'xp_to_90', 'projected_90', 'recommendation', 'daily_xp'];
            return columns.indexOf(column);
        }

        // Filtering
        function applyFilters() {
            const courseFilter = document.getElementById('filter-course').value;
            const riskFilter = document.getElementById('filter-risk').value;
            const recFilter = document.getElementById('filter-rec').value;
            const lateFilter = document.getElementById('filter-late').value;
            const searchFilter = document.getElementById('filter-search').value.toLowerCase();

            document.querySelectorAll('#student-table tbody tr').forEach(row => {
                const course = row.dataset.course;
                const risk = row.dataset.risk;
                const rec = row.dataset.rec;
                const late = row.dataset.late;
                const name = row.children[0].textContent.toLowerCase();

                const matchCourse = !courseFilter || course === courseFilter;
                const matchRisk = !riskFilter || risk === riskFilter;
                const matchRec = !recFilter || rec === recFilter;
                const matchLate = !lateFilter || late === lateFilter;
                const matchSearch = !searchFilter || name.includes(searchFilter);

                row.style.display = (matchCourse && matchRisk && matchRec && matchLate && matchSearch) ? '' : 'none';
            });
        }

        document.getElementById('filter-course').addEventListener('change', applyFilters);
        document.getElementById('filter-risk').addEventListener('change', applyFilters);
        document.getElementById('filter-rec').addEventListener('change', applyFilters);
        document.getElementById('filter-late').addEventListener('change', applyFilters);
        document.getElementById('filter-search').addEventListener('input', applyFilters);

        // Export visible rows to CSV
        function exportCSV() {
            const table = document.getElementById('student-table');
            const headers = [];
            const headerCells = table.querySelectorAll('thead th');
            headerCells.forEach(th => headers.push(th.textContent.trim()));

            const rows = [];
            rows.push(headers.join(','));

            table.querySelectorAll('tbody tr').forEach(row => {
                if (row.style.display === 'none') return; // Skip filtered out rows

                const cells = [];
                row.querySelectorAll('td').forEach(td => {
                    let text = td.textContent.trim().replace(/,/g, ';').replace(/\\n/g, ' ');
                    // Wrap in quotes if contains special chars
                    if (text.includes(';') || text.includes('"')) {
                        text = '"' + text.replace(/"/g, '""') + '"';
                    }
                    cells.push(text);
                });
                rows.push(cells.join(','));
            });

            const csv = rows.join('\\n');
            const blob = new Blob([csv], {type: 'text/csv;charset=utf-8;'});
            const link = document.createElement('a');
            const url = URL.createObjectURL(blob);
            link.setAttribute('href', url);
            link.setAttribute('download', 'ap_dashboard_export_' + new Date().toISOString().split('T')[0] + '.csv');
            link.style.visibility = 'hidden';
            document.body.appendChild(link);
            link.click();
            document.body.removeChild(link);
        }

        // Lock status management
        let lockState = { locked: false, lock_date: null, reason: '' };

        async function updateLockStatus() {
            try {
                const resp = await fetch('/api/lock-status');
                lockState = await resp.json();
                renderLockStatus();
            } catch (e) {
                console.error('Failed to get lock status:', e);
            }
        }

        function renderLockStatus() {
            const indicator = document.getElementById('lock-indicator');
            const lockBtn = document.getElementById('lock-btn');
            const unlockBtn = document.getElementById('unlock-btn');
            const warning = document.getElementById('lock-warning');
            const lockedCols = document.querySelectorAll('.locked-col');

            if (lockState.locked) {
                const lockDate = lockState.lock_date ? new Date(lockState.lock_date).toLocaleDateString('en-US', { weekday: 'short', month: 'short', day: 'numeric' }) : '';
                indicator.innerHTML = `<span style="color: #4da6ff;">🔒 Locked (${lockDate})</span>`;
                lockBtn.style.display = 'none';
                unlockBtn.style.display = 'inline-block';
                warning.style.display = 'none';
                lockedCols.forEach(col => col.style.display = '');
            } else {
                indicator.innerHTML = '<span style="color: #888;">🔓 Unlocked</span>';
                lockBtn.style.display = 'inline-block';
                unlockBtn.style.display = 'none';

                if (lockState.reason === 'weekend') {
                    warning.textContent = 'Weekend mode - recommendations can change freely';
                    warning.style.display = 'inline';
                } else if (lockState.reason === 'stale_lock') {
                    warning.textContent = 'Previous week\\'s lock expired - refresh data and re-lock';
                    warning.style.display = 'inline';
                } else {
                    warning.style.display = 'none';
                }

                // Hide locked column when unlocked (optional - could show for comparison)
                // lockedCols.forEach(col => col.style.display = 'none');
            }
        }

        async function lockRecommendations() {
            if (!confirm('Lock recommendations for this week? They will not change until next Monday.')) {
                return;
            }
            try {
                const resp = await fetch('/api/lock', { method: 'POST' });
                const data = await resp.json();
                if (data.success) {
                    alert(`Locked ${data.student_count} student recommendations.`);
                    location.reload();
                }
            } catch (e) {
                alert('Failed to lock: ' + e.message);
            }
        }

        async function unlockRecommendations() {
            if (!confirm('Unlock recommendations? Live values will be shown.')) {
                return;
            }
            try {
                const resp = await fetch('/api/unlock', { method: 'POST' });
                const data = await resp.json();
                if (data.success) {
                    location.reload();
                }
            } catch (e) {
                alert('Failed to unlock: ' + e.message);
            }
        }

        // Initialize lock status on page load
        updateLockStatus();
    </script>
</body>
</html>
'''

STUDENT_HTML = '''
<!DOCTYPE html>
<html>
<head>
    <title>{{ student.student }} - {{ student.course }}</title>
    <style>
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
            background: #1a1a2e;
            color: #eee;
            padding: 20px;
        }
        h1 { margin-bottom: 5px; color: #fff; }
        .subtitle { color: #888; margin-bottom: 20px; }

        .nav { margin-bottom: 20px; }
        .nav a { color: #4da6ff; margin-right: 20px; text-decoration: none; }
        .nav a:hover { text-decoration: underline; }

        .stats-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
            gap: 15px;
            margin-bottom: 30px;
        }
        .stat-card {
            background: #252540;
            padding: 15px;
            border-radius: 8px;
        }
        .stat-value { font-size: 28px; font-weight: bold; }
        .stat-label { color: #888; font-size: 12px; margin-top: 5px; }

        .chart-container {
            background: #252540;
            padding: 20px;
            border-radius: 8px;
            margin-bottom: 20px;
        }
        .chart-title { font-size: 16px; margin-bottom: 15px; }

        .bar-chart {
            display: flex;
            align-items: flex-end;
            height: 200px;
            gap: 3px;
            padding-bottom: 30px;
            position: relative;
        }
        .bar {
            flex: 1;
            background: linear-gradient(to top, #4da6ff, #44ff44);
            border-radius: 2px 2px 0 0;
            min-width: 8px;
            position: relative;
        }
        .bar:hover {
            opacity: 0.8;
        }
        .bar-label {
            position: absolute;
            bottom: -25px;
            left: 50%;
            transform: translateX(-50%);
            font-size: 9px;
            color: #888;
            white-space: nowrap;
        }
        .bar:nth-child(7n)::after {
            content: attr(data-date);
            position: absolute;
            bottom: -25px;
            left: 50%;
            transform: translateX(-50%);
            font-size: 9px;
            color: #888;
        }

        .y-axis {
            position: absolute;
            left: -40px;
            top: 0;
            bottom: 30px;
            display: flex;
            flex-direction: column;
            justify-content: space-between;
            font-size: 10px;
            color: #666;
        }

        .metric-good { color: #88cc88; }
        .metric-ok { color: #cccc88; }
        .metric-bad { color: #cc8888; }

        .risk-Critical { color: #ff4444; }
        .risk-At-Risk { color: #ffaa00; }
        .risk-On-Track { color: #88cc88; }
        .risk-Strong { color: #44ff44; }

        .course-tag {
            padding: 4px 10px;
            border-radius: 4px;
            font-size: 12px;
            font-weight: bold;
            display: inline-block;
            margin-left: 10px;
        }
        .course-APHG { background: #2d5a27; }
        .course-APWH { background: #5a2727; }
        .course-APUSH { background: #27415a; }
        .course-APGOV { background: #5a4a27; }
    </style>
</head>
<body>
    <nav class="nav">
        <a href="/">Dashboard</a>
        <a href="/coaching">Coaching</a>
        <a href="/comms">Communications</a>
        <a href="/settings">Settings</a>
    </nav>

    <h1>{{ student.student }} <span class="course-tag course-{{ student.course }}">{{ student.course }}</span></h1>
    <p class="subtitle">
        Risk: <span class="risk-{{ student.risk|replace(' ', '-') }}">{{ student.risk }}</span>
        {% if student.pt_score %} | Practice Test: {{ student.pt_score }}{% endif %}
    </p>

    <div class="stats-grid">
        <div class="stat-card">
            <div class="stat-value {% if student.aw_mastery and student.aw_mastery >= 70 %}metric-good{% elif student.aw_mastery and student.aw_mastery >= 50 %}metric-ok{% else %}metric-bad{% endif %}">
                {{ student.aw_mastery|default('—') }}{% if student.aw_mastery %}%{% endif %}
            </div>
            <div class="stat-label">Current Mastery</div>
        </div>
        <div class="stat-card">
            <div class="stat-value">{{ student.daily_xp|default('—') }}</div>
            <div class="stat-label">XP per School Day (14d avg)</div>
        </div>
        <div class="stat-card">
            <div class="stat-value">{{ student.xp_to_90|default('—') }}</div>
            <div class="stat-label">XP to 90%</div>
        </div>
        <div class="stat-card">
            <div class="stat-value {% if student.projected_90 == 'Done' %}metric-good{% elif student.projected_90 == 'Never' %}metric-bad{% endif %}">
                {{ student.projected_90|default('—') }}
            </div>
            <div class="stat-label">Projected 90% Date</div>
        </div>
        <div class="stat-card">
            <div class="stat-value {% if student.progress and student.progress >= 90 %}metric-good{% elif student.progress and student.progress >= 70 %}metric-ok{% else %}metric-bad{% endif %}">
                {{ student.progress|default('—') }}{% if student.progress %}%{% endif %}
            </div>
            <div class="stat-label">Course Progress</div>
        </div>
        <div class="stat-card">
            <div class="stat-value {% if student.tb_mcq_accuracy and student.tb_mcq_accuracy >= 75 %}metric-good{% elif student.tb_mcq_accuracy and student.tb_mcq_accuracy >= 60 %}metric-ok{% elif student.tb_mcq_accuracy %}metric-bad{% endif %}">
                {{ student.tb_mcq_accuracy|default('—') }}{% if student.tb_mcq_accuracy %}%{% endif %}
            </div>
            <div class="stat-label">MCQ Accuracy (Timeback)</div>
        </div>
        <div class="stat-card">
            <div class="stat-value {% if student.tb_frq_accuracy and student.tb_frq_accuracy >= 70 %}metric-good{% elif student.tb_frq_accuracy and student.tb_frq_accuracy >= 50 %}metric-ok{% elif student.tb_frq_accuracy %}metric-bad{% endif %}{% if student.frq_weak %} metric-bad{% endif %}">
                {{ student.tb_frq_accuracy|default('—') }}{% if student.tb_frq_accuracy %}%{% endif %}
                {% if student.frq_weak %}<span style="color: #ff4444; font-size: 12px;">⚠️</span>{% endif %}
            </div>
            <div class="stat-label">FRQ Accuracy (Timeback)</div>
        </div>
        <div class="stat-card">
            <div class="stat-value">{{ student.current_xp|default(0) }}</div>
            <div class="stat-label">Total XP Earned</div>
        </div>
    </div>

    <div class="chart-container">
        <div class="chart-title">Daily XP (Last 30 Days)</div>
        <div class="bar-chart" style="margin-left: 45px;">
            {% set max_xp = timeseries|map(attribute='xp')|max if timeseries else 1 %}
            {% for day in timeseries %}
            <div class="bar"
                 style="height: {{ (day.xp / max_xp * 100) if max_xp > 0 else 0 }}%;"
                 data-date="{{ day.date[5:] }}"
                 title="{{ day.date }}: {{ day.xp }} XP ({{ day.items }} items)">
            </div>
            {% endfor %}
        </div>
    </div>

    <div class="chart-container">
        <div class="chart-title">Projection Analysis</div>
        <p style="color: #888; margin-bottom: 10px;">
            At current rate of <strong>{{ student.daily_xp }}</strong> XP/day:
        </p>
        <ul style="color: #ccc; margin-left: 20px;">
            {% if student.xp_to_90 == 0 %}
            <li style="color: #44ff44;">Already at or above 90% mastery!</li>
            {% elif student.daily_xp and student.daily_xp > 0 %}
            <li>Need <strong>{{ student.xp_to_90 }}</strong> more XP to reach 90%</li>
            <li>At current pace: <strong>{{ (student.xp_to_90 / student.daily_xp)|round|int }}</strong> school days remaining</li>
            <li>Projected date: <strong>{{ student.projected_90 }}</strong></li>
            {% if student.projected_90 == 'Never' or student.projected_90 == '>2y' %}
            <li style="color: #ff4444;">Needs to increase daily XP significantly</li>
            {% endif %}
            {% else %}
            <li style="color: #ff4444;">No recent activity — cannot project</li>
            {% endif %}
        </ul>
    </div>

    <div class="chart-container">
        <div class="chart-title">Unit Progress (Frontier: Unit {{ student.frontier }})</div>
        <p style="color: #888; margin-bottom: 15px; font-size: 12px;">
            <span style="color: #44ff44;">■</span> Mastered (≥60%)
            <span style="color: #ff8844; margin-left: 15px;">■</span> Hole (before frontier, needs review)
            <span style="color: #666; margin-left: 15px;">■</span> Incomplete (at/after frontier)
            <span style="margin-left: 15px;">*</span> = non-CED (intro/exam prep)
        </p>
        <div style="display: flex; gap: 8px; flex-wrap: wrap;">
            {% for ud in student.unit_details %}
            {% set is_hole = ud.combined < 60 and ud.unit|int < student.frontier %}
            {% set is_incomplete = ud.combined < 60 and ud.unit|int >= student.frontier %}
            {% set is_mastered = ud.combined >= 60 %}
            <div style="
                width: 70px;
                padding: 8px;
                border-radius: 6px;
                text-align: center;
                background: {% if is_hole %}#3d2a1a{% elif is_incomplete %}#252540{% else %}#1a3d1a{% endif %};
                border: 2px solid {% if is_hole %}#ff8844{% elif is_incomplete %}#444{% else %}#44ff44{% endif %};
            ">
                <div style="font-weight: bold; font-size: 14px; color: {% if is_hole %}#ff8844{% elif is_incomplete %}#888{% else %}#44ff44{% endif %};">
                    U{{ ud.unit }}{% if ud.non_ced %}*{% endif %}
                </div>
                <div style="font-size: 18px; font-weight: bold; color: {% if is_hole %}#ff8844{% elif is_incomplete %}#666{% else %}#44ff44{% endif %};">
                    {{ ud.combined|int }}%
                </div>
                <div style="font-size: 9px; color: #888; margin-top: 2px;">
                    {% if is_hole %}HOLE{% elif is_incomplete %}TODO{% else %}OK{% endif %}
                </div>
            </div>
            {% endfor %}
        </div>
        {% if student.weak_units %}
        <p style="color: #ff8844; margin-top: 15px; font-size: 13px;">
            <strong>Holes detected:</strong> Units passed but not learned — revisit before continuing.
        </p>
        {% elif student.incomplete %}
        <p style="color: #888; margin-top: 15px; font-size: 13px;">
            No holes — just continue through remaining units.
        </p>
        {% endif %}
    </div>

    {# Recommendation box - show for late students OR on-track students with action items #}
    {% if student.late_for_pt or student.recommendation in ['Hole-Fill', 'FRQ', 'Hole+FRQ'] %}
    <div class="chart-container" style="border-left: 4px solid {% if student.recommendation == 'Impossible' %}#ff4444{% elif student.recommendation == 'Hole+FRQ' %}#ff88ff{% elif student.recommendation in ['Holes', 'Hole-Fill'] %}#ff8844{% elif student.recommendation == 'FRQ' %}#aa88ff{% elif student.recommendation == 'PT' %}#44dddd{% elif student.recommendation == 'Stay' %}#44ff44{% else %}#ffaa00{% endif %};">
        <div class="chart-title" style="color: {% if student.recommendation == 'Impossible' %}#ff4444{% elif student.recommendation == 'Hole+FRQ' %}#ff88ff{% elif student.recommendation in ['Holes', 'Hole-Fill'] %}#ff8844{% elif student.recommendation == 'FRQ' %}#aa88ff{% elif student.recommendation == 'PT' %}#44dddd{% elif student.recommendation == 'Stay' %}#44ff44{% else %}#ffaa00{% endif %};">
            Recommendation: {{ student.recommendation }}
        </div>
        <p style="color: #ccc; margin: 10px 0;">{{ student.rec_detail }}</p>

        {% if student.rec_required_xp %}
        <p style="color: #888; margin: 10px 0;">
            Need <strong>{{ student.rec_required_xp }}</strong> XP/day to make Apr 16 deadline
            ({{ student.rec_days }} school days remaining)
        </p>
        {% endif %}

        {% if student.weak_units %}
        <div style="margin-top: 15px;">
            <p style="color: #ff8844; font-weight: bold;">Holes (units passed but not learned):</p>
            <ul style="color: #ccc; margin-left: 20px; margin-top: 5px;">
                {% for unit in student.weak_units %}
                <li>{{ unit.unit_name }}{% if unit.non_ced %}*{% endif %} — <span style="color: #ff4444;">{{ unit.mastery }}%</span></li>
                {% endfor %}
            </ul>
        </div>
        {% endif %}

        {% if student.rec_courses %}
        <div style="margin-top: 15px; padding: 15px; background: #1a1a2e; border-radius: 8px;">
            <p style="color: #44ff44; font-weight: bold;">Recommended Mini-Courses:</p>
            <ul style="color: #ccc; margin-left: 20px; margin-top: 5px;">
                {% for course in student.rec_courses %}
                <li><strong>{{ course.name }}</strong> <span style="color: #888;">({{ course.id }})</span></li>
                {% endfor %}
            </ul>
        </div>
        {% endif %}

        {% if student.recommendation in ['FRQ', 'Hole+FRQ'] %}
        <div style="margin-top: 15px; padding: 15px; background: #1a1a2e; border-radius: 8px;">
            <p style="color: #aa88ff; font-weight: bold;">FRQ Practice Recommended</p>
            <p style="color: #ccc; margin-top: 5px;">
                MCQ: <strong>{{ student.tb_mcq_accuracy|default('—') }}%</strong> |
                FRQ: <strong>{{ student.tb_frq_accuracy|default('—') }}%</strong>
            </p>
            <p style="color: #888; margin-top: 5px; font-size: 12px;">
                Focus on essay structure, argument development, and evidence use.
            </p>
        </div>
        {% endif %}
    </div>
    {% elif student.recommendation == 'Stay' %}
    <div class="chart-container" style="border-left: 4px solid #44ff44;">
        <div class="chart-title" style="color: #44ff44;">
            Recommendation: Stay the Course
        </div>
        <p style="color: #ccc; margin: 10px 0;">{{ student.rec_detail }}</p>
        <p style="color: #888; margin-top: 10px; font-size: 12px;">
            No weak units detected. MCQ and FRQ performance balanced. Keep up the current approach!
        </p>
    </div>
    {% endif %}
</body>
</html>
'''

COACHING_HTML = '''
<!DOCTYPE html>
<html>
<head>
    <title>Coaching Calls</title>
    <style>
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
            background: #1a1a2e;
            color: #eee;
            padding: 20px;
        }
        h1 { margin-bottom: 10px; color: #fff; }
        .subtitle { color: #888; margin-bottom: 20px; }
        .nav { margin-bottom: 20px; }
        .nav a { color: #4da6ff; margin-right: 20px; text-decoration: none; }
        .nav a:hover { text-decoration: underline; }
        p { margin: 10px 0; color: #888; }
        a { color: #4da6ff; }
    </style>
</head>
<body>
    <h1>Coaching Calls</h1>
    <p class="subtitle">Schedule and call management</p>

    <nav class="nav">
        <a href="/">Dashboard</a>
        <a href="/coaching">Coaching</a>
        <a href="/comms">Communications</a>
        <a href="/settings">Settings</a>
    </nav>

    <p>Coaching call functionality moved here. <a href="http://localhost:5000">Open original dashboard</a> for full features.</p>
</body>
</html>
'''


@app.route('/')
def dashboard():
    data = load_all_data()
    students = build_unified_table(data)

    # Sort by risk then name
    risk_order = {'Critical': 0, 'At Risk': 1, 'On Track': 2, 'Strong': 3, 'Unknown': 4}
    students.sort(key=lambda x: (risk_order.get(x['risk'], 5), x['student']))

    # Summary stats
    summary = {
        'critical': len([s for s in students if s['risk'] == 'Critical']),
        'at_risk': len([s for s in students if s['risk'] == 'At Risk']),
        'on_track': len([s for s in students if s['risk'] in ('On Track', 'Strong')]),
        'no_pt': len([s for s in students if s['pt_score'] is None]),
        'late_for_pt': len([s for s in students if s['late_for_pt']])
    }

    return render_template_string(DASHBOARD_HTML, students=students, summary=summary)


@app.route('/student/<student_name>/<course>')
def student_detail(student_name, course):
    data = load_all_data()
    students = build_unified_table(data)

    # Find the student
    student = None
    for s in students:
        if s['student'] == student_name and s['course'] == course:
            student = s
            break

    if not student:
        return redirect('/')

    # Get time series
    timeseries = get_student_timeseries(data, student_name, course)

    return render_template_string(STUDENT_HTML, student=student, timeseries=timeseries)


@app.route('/coaching')
def coaching():
    return render_template_string(COACHING_HTML)


@app.route('/api/students')
def api_students():
    data = load_all_data()
    students = build_unified_table(data)
    return jsonify(students)


@app.route('/api/student/<student_name>/<course>/timeseries')
def api_student_timeseries(student_name, course):
    data = load_all_data()
    timeseries = get_student_timeseries(data, student_name, course)
    return jsonify(timeseries)


@app.route('/api/lock-status')
def api_lock_status():
    """Get current lock state."""
    state = get_lock_state()
    return jsonify(state)


@app.route('/api/lock', methods=['POST'])
def api_lock():
    """Lock current recommendations."""
    data = load_all_data()
    students = build_unified_table(data)
    lock_data = save_recommendation_lock(students)
    return jsonify({
        'success': True,
        'locked_at': lock_data['locked_at'],
        'student_count': len(lock_data['students'])
    })


@app.route('/api/unlock', methods=['POST'])
def api_unlock():
    """Unlock recommendations (delete lock file)."""
    delete_recommendation_lock()
    return jsonify({'success': True})


REFRESH_HTML = '''
<!DOCTYPE html>
<html>
<head>
    <title>Data Refresh - AP Social Studies</title>
    <style>
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
            background: #1a1a2e;
            color: #eee;
            padding: 20px;
        }
        h1 { margin-bottom: 10px; color: #fff; }
        .subtitle { color: #888; margin-bottom: 20px; }
        .nav { margin-bottom: 20px; }
        .nav a { color: #4da6ff; margin-right: 20px; text-decoration: none; }
        .nav a:hover { text-decoration: underline; }
        .card {
            background: #252540;
            padding: 20px;
            border-radius: 8px;
            margin-bottom: 15px;
        }
        .card h2 { margin-bottom: 10px; font-size: 18px; }
        .success { color: #44ff44; }
        .error { color: #ff4444; }
        .pending { color: #ffaa00; }
        .btn {
            display: inline-block;
            padding: 12px 24px;
            background: #4da6ff;
            color: #fff;
            text-decoration: none;
            border-radius: 6px;
            margin-right: 10px;
            margin-top: 10px;
            border: none;
            cursor: pointer;
            font-size: 14px;
        }
        .btn:hover { background: #3d8cd9; }
        .btn-secondary { background: #6b7280; }
        .btn-secondary:hover { background: #555; }
        .stats { color: #888; font-size: 14px; margin-top: 10px; }
        .note { color: #888; font-size: 12px; margin-top: 15px; padding: 10px; background: #1a1a2e; border-radius: 4px; }
    </style>
</head>
<body>
    <nav class="nav">
        <a href="/">Dashboard</a>
        <a href="/coaching">Coaching</a>
        <a href="/comms">Communications</a>
        <a href="/settings">Settings</a>
    </nav>

    <h1>Data Refresh</h1>
    <p class="subtitle">Pull latest data from Timeback and Austin Way</p>

    {% if results %}
    <div class="card">
        <h2>Refresh Results</h2>

        <h3 style="margin-top: 15px;">Austin Way</h3>
        {% if results.austin_way.success %}
        <p class="success">Success: {{ results.austin_way.message }}</p>
        {% else %}
        <p class="error">Failed: {{ results.austin_way.message }}</p>
        {% endif %}

        <h3 style="margin-top: 15px;">Timeback</h3>
        {% if results.timeback.success %}
        <p class="success">Success: {{ results.timeback.message }}</p>
        {% else %}
        <p class="error">Failed: {{ results.timeback.message }}</p>
        {% endif %}

        <p class="stats">Refreshed at {{ results.timestamp }}</p>

        <a href="/" class="btn" style="margin-top: 20px;">View Updated Dashboard</a>
    </div>
    {% else %}
    <div class="card">
        <h2>Refresh Options</h2>
        <p style="margin-bottom: 15px;">Click below to pull fresh data from external systems.</p>

        <a href="/refresh/all" class="btn">Refresh All Data</a>
        <a href="/refresh/austin-way" class="btn btn-secondary">Austin Way Only</a>
        <a href="/refresh/timeback" class="btn btn-secondary">Timeback Only</a>

        <div class="note">
            <strong>Requirements:</strong><br>
            - Austin Way: Valid auth cookie (auto-harvested from browser or in <code>austin_way_auth.txt</code>)<br>
            - Timeback: <code>TIMEBACK_CLIENT_ID</code> and <code>TIMEBACK_CLIENT_SECRET</code> in <code>.env</code>
        </div>

        <h3 style="margin-top: 20px;">Austin Way Auth</h3>
        <p style="margin: 10px 0; color: #888;">If Austin Way auth fails, sign into <a href="https://www.aphistoryforge.com/guide/" target="_blank" style="color: #4da6ff;">aphistoryforge.com</a>, then paste the <code>aph_auth</code> cookie below.</p>
        <form action="/refresh/save-cookie" method="POST" style="margin-top: 10px;">
            <input type="text" name="cookie" placeholder="Paste aph_auth cookie value here" style="width: 100%; padding: 10px; border: 1px solid #444; border-radius: 4px; background: #1a1a2e; color: #fff; font-family: monospace; margin-bottom: 10px;">
            <button type="submit" class="btn btn-secondary">Save Cookie</button>
        </form>
        <p style="margin-top: 10px; color: #666; font-size: 11px;">DevTools → Application → Cookies → api.aphistoryforge.com → aph_auth</p>
    </div>
    {% endif %}
</body>
</html>
'''


def backup_data_files():
    """Backup CSV files before refresh."""
    import shutil
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_dir = DATA_DIR / 'backups'
    backup_dir.mkdir(exist_ok=True)

    backed_up = []

    # Backup Austin Way
    aw_file = AUSTIN_WAY_OUTPUT_FILE
    if aw_file.exists():
        backup_path = backup_dir / f'austin_way_mastery_{timestamp}.csv'
        shutil.copy(aw_file, backup_path)
        backed_up.append('austin_way_mastery.csv')

    # Backup Timeback
    tb_file = DATA_DIR / 'ap_social_studies_learning_data.csv'
    if tb_file.exists():
        backup_path = backup_dir / f'ap_social_studies_learning_data_{timestamp}.csv'
        shutil.copy(tb_file, backup_path)
        backed_up.append('ap_social_studies_learning_data.csv')

    return backed_up


@app.route('/refresh')
def refresh():
    """One-click refresh: backup, refresh all, redirect to dashboard on success."""
    # Backup first
    backup_data_files()

    # Refresh both sources
    aw_result = refresh_austin_way()
    tb_result = refresh_timeback()

    # If both succeeded, redirect to dashboard
    if aw_result["success"] and tb_result["success"]:
        return redirect('/')

    # If there's an error, show the error page
    results = {
        "austin_way": aw_result,
        "timeback": tb_result,
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    return render_template_string(REFRESH_ERROR_HTML, results=results)


REFRESH_ERROR_HTML = '''
<!DOCTYPE html>
<html>
<head>
    <title>Refresh Error - AP Social Studies</title>
    <style>
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
            background: #1a1a2e;
            color: #eee;
            padding: 20px;
        }
        h1 { margin-bottom: 10px; color: #fff; }
        .card {
            background: #252540;
            padding: 20px;
            border-radius: 8px;
            margin-bottom: 15px;
            max-width: 700px;
        }
        .success { color: #44ff44; }
        .error { color: #ff4444; }
        .btn {
            display: inline-block;
            padding: 12px 24px;
            background: #4da6ff;
            color: #fff;
            text-decoration: none;
            border-radius: 6px;
            margin-right: 10px;
            margin-top: 10px;
            border: none;
            cursor: pointer;
            font-size: 14px;
        }
        .btn:hover { background: #3d8cd9; }
        .btn-secondary { background: #6b7280; }
        input[type="text"] {
            width: 100%;
            padding: 10px;
            border: 1px solid #444;
            border-radius: 4px;
            background: #1a1a2e;
            color: #fff;
            font-family: monospace;
            margin: 10px 0;
        }
        .note { color: #888; font-size: 12px; margin-top: 10px; }
    </style>
</head>
<body>
    <h1>Refresh Error</h1>

    <div class="card">
        <h3>Austin Way</h3>
        {% if results.austin_way.success %}
        <p class="success">OK: {{ results.austin_way.message }}</p>
        {% else %}
        <p class="error">Failed: {{ results.austin_way.message }}</p>
        {% if 'cookie' in results.austin_way.message|lower or 'auth' in results.austin_way.message|lower or '401' in results.austin_way.message %}
        <p style="margin-top: 15px; color: #ccc;">Sign into <a href="https://www.aphistoryforge.com/guide/" target="_blank" style="color: #4da6ff;">aphistoryforge.com</a>, then paste the <code>aph_auth</code> cookie:</p>
        <form action="/refresh/save-cookie" method="POST">
            <input type="text" name="cookie" placeholder="Paste aph_auth cookie value here">
            <button type="submit" class="btn">Save Cookie & Retry</button>
        </form>
        <p class="note">DevTools → Application → Cookies → api.aphistoryforge.com → aph_auth</p>
        {% endif %}
        {% endif %}
    </div>

    <div class="card">
        <h3>Timeback</h3>
        {% if results.timeback.success %}
        <p class="success">OK: {{ results.timeback.message }}</p>
        {% else %}
        <p class="error">Failed: {{ results.timeback.message }}</p>
        {% endif %}
    </div>

    <a href="/" class="btn btn-secondary">Back to Dashboard</a>
    <a href="/refresh" class="btn">Retry Refresh</a>
</body>
</html>
'''


@app.route('/refresh/austin-way')
def refresh_austin_way_route():
    """Refresh Austin Way data only, then redirect or show error."""
    backup_data_files()
    result = refresh_austin_way()
    if result["success"]:
        return redirect('/')
    results = {
        "austin_way": result,
        "timeback": {"success": True, "message": "Not requested", "rows": 0},
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    return render_template_string(REFRESH_ERROR_HTML, results=results)


@app.route('/refresh/timeback')
def refresh_timeback_route():
    """Refresh Timeback data only, then redirect or show error."""
    backup_data_files()
    result = refresh_timeback()
    if result["success"]:
        return redirect('/')
    results = {
        "austin_way": {"success": True, "message": "Not requested", "rows": 0},
        "timeback": result,
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    return render_template_string(REFRESH_ERROR_HTML, results=results)


@app.route('/refresh/save-cookie', methods=['POST'])
def save_cookie_route():
    """Save Austin Way cookie from form input, then retry full refresh."""
    cookie = request.form.get('cookie', '').strip()
    if cookie:
        # Strip 'aph_auth=' prefix if present
        if cookie.startswith('aph_auth='):
            cookie = cookie[9:]
        AUSTIN_WAY_AUTH_FILE.write_text(cookie)
    return redirect('/refresh')


@app.route('/refresh/harvest-cookie')
def harvest_cookie_route():
    """Harvest Austin Way cookie from browser."""
    result = harvest_austin_way_cookie()

    harvest_html = '''
    <!DOCTYPE html>
    <html>
    <head>
        <title>Cookie Harvest - AP Social Studies</title>
        <style>
            * { box-sizing: border-box; margin: 0; padding: 0; }
            body {
                font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
                background: #1a1a2e;
                color: #eee;
                padding: 20px;
            }
            .nav { margin-bottom: 20px; }
            .nav a { color: #4da6ff; text-decoration: none; }
            .card {
                background: #252540;
                padding: 20px;
                border-radius: 8px;
                max-width: 600px;
            }
            .success { color: #44ff44; }
            .error { color: #ff4444; }
            .btn {
                display: inline-block;
                padding: 12px 24px;
                background: #4da6ff;
                color: #fff;
                text-decoration: none;
                border-radius: 6px;
                margin-top: 15px;
            }
        </style>
    </head>
    <body>
        <nav class="nav"><a href="/refresh">← Back to Refresh</a></nav>
        <div class="card">
            <h2>Cookie Harvest</h2>
            {% if result.success %}
            <p class="success" style="margin: 15px 0;">{{ result.message }}</p>
            <a href="/refresh/austin-way" class="btn">Now Refresh Austin Way Data</a>
            {% else %}
            <p class="error" style="margin: 15px 0;">{{ result.message }}</p>
            <p style="margin-top: 15px; color: #888;">
                1. Open <a href="https://www.aphistoryforge.com/guide/" target="_blank" style="color: #4da6ff;">aphistoryforge.com/guide</a><br>
                2. Sign in with your account<br>
                3. Come back here and click the button again
            </p>
            <a href="/refresh/harvest-cookie" class="btn">Try Again</a>
            {% endif %}
        </div>
    </body>
    </html>
    '''
    return render_template_string(harvest_html, result=result)


# =============================================================================
# COMMUNICATIONS PAGE
# =============================================================================

COMMS_HTML = '''
<!DOCTYPE html>
<html>
<head>
    <title>Communications - AP Dashboard</title>
    <style>
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
            background: #1a1a2e;
            color: #eee;
            padding: 20px;
        }
        h1 { margin-bottom: 10px; color: #fff; }
        .subtitle { color: #888; margin-bottom: 20px; }
        .nav { margin-bottom: 20px; }
        .nav a { color: #4da6ff; margin-right: 20px; text-decoration: none; }
        .nav a:hover { text-decoration: underline; }

        .rec-section {
            background: #252540;
            border-radius: 8px;
            margin-bottom: 15px;
            overflow: hidden;
        }
        .rec-header {
            padding: 15px 20px;
            background: #353560;
            display: flex;
            justify-content: space-between;
            align-items: center;
            cursor: pointer;
        }
        .rec-header:hover { background: #404070; }
        .rec-title {
            font-weight: bold;
            font-size: 16px;
        }
        .rec-count {
            background: #4da6ff;
            color: #fff;
            padding: 2px 10px;
            border-radius: 12px;
            font-size: 12px;
            margin-left: 10px;
        }
        .rec-actions { display: flex; gap: 10px; }
        .rec-body { padding: 0; display: none; }
        .rec-body.open { display: block; }

        .student-row {
            display: flex;
            align-items: center;
            padding: 12px 20px;
            border-top: 1px solid #333;
        }
        .student-row:hover { background: #303050; }
        .student-info { flex: 1; }
        .student-name { font-weight: bold; }
        .student-meta { font-size: 12px; color: #888; margin-top: 2px; }
        .student-course {
            display: inline-block;
            padding: 2px 6px;
            border-radius: 4px;
            font-size: 10px;
            margin-left: 8px;
        }
        .course-APHG { background: #2d5a27; }
        .course-APWH { background: #5a2727; }
        .course-APUSH { background: #27415a; }
        .course-APGOV { background: #5a4a27; }

        .new-badge {
            background: #ff8844;
            color: #fff;
            padding: 2px 6px;
            border-radius: 4px;
            font-size: 10px;
            margin-left: 8px;
        }

        .btn {
            padding: 8px 16px;
            border: none;
            border-radius: 4px;
            cursor: pointer;
            font-size: 13px;
            text-decoration: none;
        }
        .btn-primary { background: #4da6ff; color: #fff; }
        .btn-primary:hover { background: #3d8cd9; }
        .btn-secondary { background: #6b7280; color: #fff; }
        .btn-secondary:hover { background: #5b6270; }
        .btn-sm { padding: 4px 12px; font-size: 12px; }

        .modal {
            display: none;
            position: fixed;
            top: 0; left: 0;
            width: 100%; height: 100%;
            background: rgba(0,0,0,0.8);
            z-index: 1000;
            justify-content: center;
            align-items: center;
        }
        .modal.open { display: flex; }
        .modal-content {
            background: #252540;
            border-radius: 8px;
            width: 90%;
            max-width: 700px;
            max-height: 90vh;
            overflow-y: auto;
        }
        .modal-header {
            padding: 15px 20px;
            background: #353560;
            display: flex;
            justify-content: space-between;
            align-items: center;
        }
        .modal-body { padding: 20px; }
        .modal-footer {
            padding: 15px 20px;
            background: #1a1a2e;
            display: flex;
            justify-content: flex-end;
            gap: 10px;
        }

        .close-btn {
            background: none;
            border: none;
            color: #888;
            font-size: 24px;
            cursor: pointer;
        }
        .close-btn:hover { color: #fff; }

        textarea {
            width: 100%;
            min-height: 200px;
            padding: 12px;
            border: 1px solid #444;
            border-radius: 4px;
            background: #1a1a2e;
            color: #fff;
            font-family: inherit;
            font-size: 14px;
            resize: vertical;
        }

        .student-data {
            background: #1a1a2e;
            padding: 12px;
            border-radius: 4px;
            margin-bottom: 15px;
            font-size: 12px;
        }
        .student-data dt { color: #888; display: inline; }
        .student-data dd { display: inline; margin-right: 15px; }

        .send-results {
            margin-top: 15px;
            padding: 12px;
            background: #1a1a2e;
            border-radius: 4px;
        }
        .result-success { color: #44ff44; }
        .result-fail { color: #ff4444; }

        .loading {
            display: none;
            text-align: center;
            padding: 20px;
            color: #888;
        }

        .config-warning {
            background: #5a4a27;
            padding: 15px 20px;
            border-radius: 8px;
            margin-bottom: 20px;
        }
        .config-warning a { color: #4da6ff; }
    </style>
</head>
<body>
    <h1>Communications Center</h1>
    <p class="subtitle">Send recommendation-based messages to students</p>

    <nav class="nav">
        <a href="/">Dashboard</a>
        <a href="/coaching">Coaching</a>
        <a href="/comms">Communications</a>
        <a href="/refresh">Refresh Data</a>
        <a href="/settings">Settings</a>
    </nav>

    {% if not openai_configured %}
    <div class="config-warning">
        <strong>OpenAI not configured.</strong> Add your API key in <a href="/settings">Settings</a> to enable AI-generated messages.
    </div>
    {% endif %}

    {% for rec_type, students in by_recommendation.items() %}
    <div class="rec-section">
        <div class="rec-header" onclick="toggleSection(this)">
            <div>
                <span class="rec-title">{{ rec_type }}</span>
                <span class="rec-count">{{ students|length }}</span>
            </div>
            <div class="rec-actions">
                {% if students %}
                <button class="btn btn-secondary btn-sm" onclick="event.stopPropagation(); sendBulk('{{ rec_type }}', 'new')">Send New Only</button>
                <button class="btn btn-primary btn-sm" onclick="event.stopPropagation(); sendBulk('{{ rec_type }}', 'all')">Send All</button>
                {% endif %}
            </div>
        </div>
        <div class="rec-body">
            {% for s in students %}
            <div class="student-row" data-student="{{ s.student }}" data-course="{{ s.course }}" data-rec="{{ rec_type }}" data-new="{{ 'true' if s.is_new else 'false' }}">
                <div class="student-info">
                    <span class="student-name">{{ s.student }}</span>
                    <span class="student-course course-{{ s.course }}">{{ s.course }}</span>
                    {% if s.is_new %}<span class="new-badge">NEW</span>{% endif %}
                    <div class="student-meta">
                        XP/day: {{ s.daily_xp|round(1) if s.daily_xp else 'N/A' }} |
                        Progress: {{ s.combined_progress|round(0)|int if s.combined_progress else 'N/A' }}% |
                        {% if s.last_sent %}Last sent: {{ s.last_sent[:10] }}{% else %}Never sent{% endif %}
                    </div>
                </div>
                <button class="btn btn-primary btn-sm" onclick="openPreview('{{ s.student }}', '{{ s.course }}')">Preview & Send</button>
            </div>
            {% else %}
            <div class="student-row" style="color: #666;">No students with this recommendation</div>
            {% endfor %}
        </div>
    </div>
    {% endfor %}

    <!-- Bulk Send Modal -->
    <div class="modal" id="bulk-modal">
        <div class="modal-content">
            <div class="modal-header">
                <span id="bulk-modal-title">Bulk Send</span>
                <button class="close-btn" onclick="closeBulkModal()">&times;</button>
            </div>
            <div class="modal-body">
                <!-- Phase 1: Context input -->
                <div id="bulk-context-section">
                    <p style="margin-bottom: 15px; color: #ccc;">
                        Sending <strong id="bulk-rec-type"></strong> message to <strong id="bulk-count"></strong> student(s):
                    </p>
                    <div id="bulk-student-list" style="background: #1a1a2e; padding: 10px; border-radius: 4px; margin-bottom: 15px; max-height: 100px; overflow-y: auto; font-size: 12px; color: #888;"></div>

                    <label style="display: block; margin-bottom: 8px; color: #aaa;">
                        Additional context for AI (applies to ALL messages):
                    </label>
                    <textarea id="bulk-context-text" placeholder="e.g., Your practice test will be this Friday. Mini-courses will be assigned tomorrow." style="min-height: 80px; margin-bottom: 15px;"></textarea>
                    <button class="btn btn-primary" onclick="startBulkGeneration()">Generate All Messages</button>
                </div>

                <!-- Phase 2: Generation progress -->
                <div id="bulk-progress-section" style="display: none;">
                    <p style="margin-bottom: 10px; color: #ccc;">Generating messages...</p>
                    <div id="bulk-progress-bar" style="background: #333; border-radius: 4px; height: 20px; overflow: hidden; margin-bottom: 15px;">
                        <div id="bulk-progress-fill" style="background: #4da6ff; height: 100%; width: 0%; transition: width 0.3s;"></div>
                    </div>
                    <div id="bulk-progress-text" style="color: #888; font-size: 12px;">0 / 0</div>
                </div>

                <!-- Phase 3: Review before send -->
                <div id="bulk-review-section" style="display: none;">
                    <p style="margin-bottom: 15px; color: #ccc;">Messages generated. Review and send:</p>
                    <div id="bulk-messages-list" style="max-height: 500px; overflow-y: auto;"></div>
                </div>

                <!-- Phase 4: Send progress -->
                <div id="bulk-send-section" style="display: none;">
                    <p style="margin-bottom: 10px; color: #ccc;">Sending messages...</p>
                    <div id="bulk-send-bar" style="background: #333; border-radius: 4px; height: 20px; overflow: hidden; margin-bottom: 15px;">
                        <div id="bulk-send-fill" style="background: #44ff44; height: 100%; width: 0%; transition: width 0.3s;"></div>
                    </div>
                    <div id="bulk-send-text" style="color: #888; font-size: 12px;">0 / 0</div>
                    <div id="bulk-send-results" style="margin-top: 15px; max-height: 200px; overflow-y: auto; font-size: 12px;"></div>
                </div>
            </div>
            <div class="modal-footer">
                <button class="btn btn-secondary" onclick="closeBulkModal()">Close</button>
                <button class="btn btn-primary" id="bulk-send-btn" onclick="executeBulkSend(false)" style="display: none;">Send All Messages</button>
                <button class="btn btn-primary" id="bulk-retry-btn" onclick="executeBulkSend(true)" style="display: none; background: #ff8844;">Retry Failed</button>
            </div>
        </div>
    </div>

    <!-- Preview Modal -->
        <div class="modal-content">
            <div class="modal-header">
                <span id="modal-title">Message Preview</span>
                <button class="close-btn" onclick="closeModal()">&times;</button>
            </div>
            <div class="modal-body">
                <div class="student-data" id="student-data"></div>

                <!-- Context input (shown first) -->
                <div id="context-section">
                    <label style="display: block; margin-bottom: 8px; color: #aaa;">
                        Additional context for AI (optional):
                    </label>
                    <textarea id="context-text" placeholder="e.g., Your practice test will be Friday. These mini-courses will be assigned tomorrow. Great job on last week's call!" style="min-height: 80px; margin-bottom: 15px;"></textarea>
                    <button class="btn btn-primary" onclick="generateMessage()" id="generate-btn">Generate Message</button>
                </div>

                <!-- Generated message (shown after generation) -->
                <div id="message-section" style="display: none;">
                    <label style="display: block; margin-bottom: 8px; color: #aaa;">
                        Message (edit as needed):
                    </label>
                    <div class="loading" id="loading">Generating message with AI...</div>
                    <textarea id="message-text" placeholder="Message will appear here..."></textarea>
                    <div class="send-results" id="send-results" style="display: none;"></div>
                </div>
            </div>
            <div class="modal-footer">
                <button class="btn btn-secondary" id="back-btn" onclick="showContextSection()" style="display: none;">Back</button>
                <button class="btn btn-secondary" onclick="closeModal()">Cancel</button>
                <button class="btn btn-primary" id="send-btn" onclick="sendMessage()" style="display: none;">Send to All 4 Channels</button>
            </div>
        </div>
    </div>

    <script>
        let currentStudent = null;
        let currentCourse = null;

        function toggleSection(header) {
            const body = header.nextElementSibling;
            body.classList.toggle('open');
        }

        function openPreview(student, course) {
            currentStudent = student;
            currentCourse = course;
            document.getElementById('modal-title').textContent = `Message: ${student}`;
            document.getElementById('context-text').value = '';
            document.getElementById('message-text').value = '';
            document.getElementById('send-results').style.display = 'none';
            document.getElementById('send-btn').disabled = false;
            document.getElementById('send-btn').textContent = 'Send to All 4 Channels';

            // Show context section, hide message section
            document.getElementById('context-section').style.display = 'block';
            document.getElementById('message-section').style.display = 'none';
            document.getElementById('send-btn').style.display = 'none';
            document.getElementById('back-btn').style.display = 'none';

            document.getElementById('preview-modal').classList.add('open');

            // Load student data immediately
            loadStudentData();
        }

        function closeModal() {
            document.getElementById('preview-modal').classList.remove('open');
            currentStudent = null;
            currentCourse = null;
        }

        function showContextSection() {
            document.getElementById('context-section').style.display = 'block';
            document.getElementById('message-section').style.display = 'none';
            document.getElementById('send-btn').style.display = 'none';
            document.getElementById('back-btn').style.display = 'none';
        }

        async function loadStudentData() {
            try {
                const resp = await fetch('/api/comms/preview', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({student_name: currentStudent, course: currentCourse, context: '', skip_generation: true})
                });
                const data = await resp.json();
                if (data.student) {
                    const s = data.student;
                    document.getElementById('student-data').innerHTML = `
                        <dl>
                            <dt>Course:</dt><dd>${s.course}</dd>
                            <dt>Rec:</dt><dd>${s.recommendation}</dd>
                            <dt>Progress:</dt><dd>${s.combined_progress ? s.combined_progress.toFixed(0) : 'N/A'}%</dd>
                            <dt>XP/day:</dt><dd>${s.daily_xp ? s.daily_xp.toFixed(1) : 'N/A'}</dd>
                            <dt>Projected:</dt><dd>${s.projected_90 || 'N/A'}</dd>
                        </dl>
                    `;
                }
            } catch (e) {
                console.error('Failed to load student data:', e);
            }
        }

        async function generateMessage() {
            const context = document.getElementById('context-text').value.trim();

            // Switch to message section
            document.getElementById('context-section').style.display = 'none';
            document.getElementById('message-section').style.display = 'block';
            document.getElementById('loading').style.display = 'block';
            document.getElementById('message-text').value = '';
            document.getElementById('send-btn').style.display = 'inline-block';
            document.getElementById('back-btn').style.display = 'inline-block';

            try {
                const resp = await fetch('/api/comms/preview', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({student_name: currentStudent, course: currentCourse, context: context})
                });
                const data = await resp.json();

                if (data.error) {
                    document.getElementById('message-text').value = `Error: ${data.error}\n\nWrite your message manually below:`;
                } else {
                    document.getElementById('message-text').value = data.message;
                }
            } catch (e) {
                document.getElementById('message-text').value = `Error: ${e.message}\n\nWrite your message manually below:`;
            }

            document.getElementById('loading').style.display = 'none';
        }

        function regenerateMessage() {
            showContextSection();
        }

        async function sendMessage() {
            const message = document.getElementById('message-text').value.trim();
            if (!message) {
                alert('Please enter a message');
                return;
            }

            document.getElementById('send-btn').disabled = true;
            document.getElementById('send-btn').textContent = 'Sending...';

            try {
                const resp = await fetch('/api/comms/send', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({
                        student_name: currentStudent,
                        course: currentCourse,
                        message: message
                    })
                });
                const data = await resp.json();

                // Show results
                let html = '<strong>Send Results:</strong><br>';
                for (const [channel, result] of Object.entries(data)) {
                    const cls = result.success ? 'result-success' : 'result-fail';
                    const icon = result.success ? '&#10004;' : '&#10008;';
                    html += `<span class="${cls}">${icon} ${channel}: ${result.message}</span><br>`;
                }
                document.getElementById('send-results').innerHTML = html;
                document.getElementById('send-results').style.display = 'block';
                document.getElementById('send-btn').textContent = 'Sent!';

                // Update the row to remove NEW badge
                const row = document.querySelector(`[data-student="${currentStudent}"][data-course="${currentCourse}"]`);
                if (row) {
                    const badge = row.querySelector('.new-badge');
                    if (badge) badge.remove();
                    row.dataset.new = 'false';
                }
            } catch (e) {
                alert(`Send failed: ${e.message}`);
                document.getElementById('send-btn').disabled = false;
                document.getElementById('send-btn').textContent = 'Send to All 4 Channels';
            }
        }

        let bulkStudents = [];
        let bulkMessages = {};
        let bulkRecType = '';

        function sendBulk(recType, mode) {
            const section = event.target.closest('.rec-section');
            const rows = section.querySelectorAll('.student-row[data-student]');
            bulkStudents = [];
            bulkMessages = {};
            bulkRecType = recType;

            rows.forEach(row => {
                if (mode === 'all' || (mode === 'new' && row.dataset.new === 'true')) {
                    bulkStudents.push({
                        name: row.dataset.student,
                        course: row.dataset.course
                    });
                }
            });

            if (bulkStudents.length === 0) {
                alert(mode === 'new' ? 'No new students to send to' : 'No students to send to');
                return;
            }

            // Show bulk modal
            document.getElementById('bulk-modal-title').textContent = `Bulk Send: ${recType}`;
            document.getElementById('bulk-rec-type').textContent = recType;
            document.getElementById('bulk-count').textContent = bulkStudents.length;
            document.getElementById('bulk-student-list').innerHTML = bulkStudents.map(s => s.name).join(', ');
            document.getElementById('bulk-context-text').value = '';

            // Reset to phase 1
            document.getElementById('bulk-context-section').style.display = 'block';
            document.getElementById('bulk-progress-section').style.display = 'none';
            document.getElementById('bulk-review-section').style.display = 'none';
            document.getElementById('bulk-send-section').style.display = 'none';
            document.getElementById('bulk-send-btn').style.display = 'none';

            document.getElementById('bulk-modal').classList.add('open');
        }

        function closeBulkModal() {
            document.getElementById('bulk-modal').classList.remove('open');
            bulkStudents = [];
            bulkMessages = {};
        }

        async function startBulkGeneration() {
            const context = document.getElementById('bulk-context-text').value.trim();

            // Switch to progress phase
            document.getElementById('bulk-context-section').style.display = 'none';
            document.getElementById('bulk-progress-section').style.display = 'block';

            const total = bulkStudents.length;
            let completed = 0;

            for (const student of bulkStudents) {
                try {
                    const resp = await fetch('/api/comms/preview', {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({
                            student_name: student.name,
                            course: student.course,
                            context: context
                        })
                    });
                    const data = await resp.json();
                    bulkMessages[student.name] = {
                        course: student.course,
                        message: data.message || `Error: ${data.error}`,
                        error: !!data.error
                    };
                } catch (e) {
                    bulkMessages[student.name] = {
                        course: student.course,
                        message: `Error: ${e.message}`,
                        error: true
                    };
                }

                completed++;
                const pct = (completed / total) * 100;
                document.getElementById('bulk-progress-fill').style.width = pct + '%';
                document.getElementById('bulk-progress-text').textContent = `${completed} / ${total}`;
            }

            // Show review phase
            showBulkReview();
        }

        function showBulkReview() {
            document.getElementById('bulk-progress-section').style.display = 'none';
            document.getElementById('bulk-review-section').style.display = 'block';
            document.getElementById('bulk-send-btn').style.display = 'inline-block';

            let html = '';
            let idx = 0;
            for (const student of bulkStudents) {
                const msg = bulkMessages[student.name];
                const color = msg.error ? '#ff4444' : '#ccc';
                const fullMsg = msg.message || '';
                const needsExpand = fullMsg.length > 200;
                const preview = needsExpand ? fullMsg.substring(0, 200) + '...' : fullMsg;
                html += `
                    <div style="background: #1a1a2e; padding: 10px; border-radius: 4px; margin-bottom: 8px;">
                        <strong style="color: #4da6ff;">${student.name}</strong>
                        <span style="color: #666; font-size: 11px;">(${msg.course})</span>
                        <div id="msg-preview-${idx}" style="color: ${color}; font-size: 12px; margin-top: 5px; white-space: pre-wrap;">${preview}</div>
                        <div id="msg-full-${idx}" style="color: ${color}; font-size: 12px; margin-top: 5px; white-space: pre-wrap; display: none;">${fullMsg}</div>
                        ${needsExpand ? `<button onclick="toggleBulkMsg(${idx})" id="msg-toggle-${idx}" style="background: none; border: none; color: #4da6ff; cursor: pointer; font-size: 11px; padding: 0; margin-top: 5px;">Show full message</button>` : ''}
                    </div>
                `;
                idx++;
            }
            document.getElementById('bulk-messages-list').innerHTML = html;
        }

        function toggleBulkMsg(idx) {
            const preview = document.getElementById('msg-preview-' + idx);
            const full = document.getElementById('msg-full-' + idx);
            const btn = document.getElementById('msg-toggle-' + idx);
            if (full.style.display === 'none') {
                preview.style.display = 'none';
                full.style.display = 'block';
                btn.textContent = 'Show less';
            } else {
                preview.style.display = 'block';
                full.style.display = 'none';
                btn.textContent = 'Show full message';
            }
        }

        let failedStudents = [];

        async function executeBulkSend(retryOnly = false) {
            document.getElementById('bulk-review-section').style.display = 'none';
            document.getElementById('bulk-send-btn').style.display = 'none';
            document.getElementById('bulk-retry-btn').style.display = 'none';
            document.getElementById('bulk-send-section').style.display = 'block';

            const studentsToSend = retryOnly ? failedStudents : bulkStudents;
            failedStudents = [];  // Reset for this run

            const total = studentsToSend.length;
            let completed = 0;
            let successCount = 0;
            let resultsHtml = '';

            for (const student of studentsToSend) {
                const msg = bulkMessages[student.name];
                if (msg.error) {
                    resultsHtml += `<div style="color: #ff8844;">${student.name}: Skipped (generation error)</div>`;
                    failedStudents.push(student);
                    completed++;
                    continue;
                }

                try {
                    const resp = await fetch('/api/comms/send', {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({
                            student_name: student.name,
                            course: msg.course,
                            message: msg.message
                        })
                    });
                    const data = await resp.json();

                    const slackOk = data.slack_student?.success;
                    const emailOk = data.email_student?.success;
                    const anyOk = slackOk || emailOk;
                    const icon = anyOk ? '&#10004;' : '&#10008;';
                    const color = anyOk ? '#44ff44' : '#ff4444';
                    resultsHtml += `<div style="color: ${color};">${icon} ${student.name}: Slack ${slackOk ? 'OK' : 'fail'}, Email ${emailOk ? 'OK' : 'fail'}</div>`;

                    if (anyOk) {
                        successCount++;
                        // Update row badge
                        const row = document.querySelector(`[data-student="${student.name}"][data-course="${msg.course}"]`);
                        if (row) {
                            const badge = row.querySelector('.new-badge');
                            if (badge) badge.remove();
                            row.dataset.new = 'false';
                        }
                    } else {
                        failedStudents.push(student);
                    }
                } catch (e) {
                    resultsHtml += `<div style="color: #ff4444;">&#10008; ${student.name}: Error - ${e.message}</div>`;
                    failedStudents.push(student);
                }

                completed++;
                const pct = (completed / total) * 100;
                document.getElementById('bulk-send-fill').style.width = pct + '%';
                document.getElementById('bulk-send-text').textContent = `${completed} / ${total}`;
                document.getElementById('bulk-send-results').innerHTML = resultsHtml;

                // Small delay between sends to avoid rate limits
                await new Promise(r => setTimeout(r, 500));
            }

            const statusText = retryOnly ? 'Retry complete!' : 'Done!';
            document.getElementById('bulk-send-text').textContent = `${statusText} ${successCount} succeeded, ${failedStudents.length} failed`;

            // Show retry button if there were failures
            if (failedStudents.length > 0) {
                document.getElementById('bulk-retry-btn').style.display = 'inline-block';
                document.getElementById('bulk-retry-btn').textContent = `Retry ${failedStudents.length} Failed`;
            }
        }
    </script>
</body>
</html>
'''

@app.route('/comms')
def comms_page():
    """Communications page - send recommendation-based messages."""
    data = load_all_data()
    students = build_unified_table(data)
    by_rec = get_students_by_recommendation(students)

    # Check if OpenAI is configured
    config = load_config()
    openai_configured = bool(config.get('openai_api_key') or os.environ.get('OPENAI_API_KEY'))

    return render_template_string(COMMS_HTML, by_recommendation=by_rec, openai_configured=openai_configured)


@app.route('/api/comms/preview', methods=['POST'])
def api_comms_preview():
    """Generate message preview for a student."""
    req = request.json
    student_name = req.get('student_name')
    course = req.get('course')
    context = req.get('context', '')
    skip_generation = req.get('skip_generation', False)

    # Load student data
    data = load_all_data()
    students = build_unified_table(data)
    student = next((s for s in students if s['student'] == student_name and s['course'] == course), None)

    if not student:
        return jsonify({'error': 'Student not found'}), 404

    # If just loading student data (no message generation yet)
    if skip_generation:
        return jsonify({'student': student})

    message, error = generate_recommendation_message(student, context)
    if error:
        return jsonify({'error': error, 'student': student})

    return jsonify({'message': message, 'student': student})


@app.route('/api/comms/send', methods=['POST'])
def api_comms_send():
    """Send message to student (all 4 channels)."""
    req = request.json
    student_name = req.get('student_name')
    course = req.get('course')
    message_text = req.get('message')

    if not message_text:
        return jsonify({'error': 'No message provided'}), 400

    # Load student data
    data = load_all_data()
    students = build_unified_table(data)
    student = next((s for s in students if s['student'] == student_name and s['course'] == course), None)

    if not student:
        return jsonify({'error': 'Student not found'}), 404

    # Send to all channels
    results = send_recommendation_message(student, message_text)

    # Record in history
    record_comms_send(student_name, student.get('recommendation', 'Unknown'))

    return jsonify(results)


# =============================================================================
# SETTINGS PAGE
# =============================================================================

SETTINGS_HTML = '''
<!DOCTYPE html>
<html>
<head>
    <title>Settings - AP Dashboard</title>
    <style>
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
            background: #1a1a2e;
            color: #eee;
            padding: 20px;
        }
        h1 { margin-bottom: 10px; color: #fff; }
        .subtitle { color: #888; margin-bottom: 20px; }
        .nav { margin-bottom: 20px; }
        .nav a { color: #4da6ff; margin-right: 20px; text-decoration: none; }

        .card {
            background: #252540;
            padding: 20px;
            border-radius: 8px;
            margin-bottom: 20px;
            max-width: 600px;
        }
        .card h2 { margin-bottom: 15px; font-size: 18px; }

        label { display: block; margin-bottom: 5px; color: #aaa; }
        input[type="text"], input[type="password"] {
            width: 100%;
            padding: 10px;
            border: 1px solid #444;
            border-radius: 4px;
            background: #1a1a2e;
            color: #fff;
            margin-bottom: 15px;
        }
        .btn {
            padding: 10px 20px;
            border: none;
            border-radius: 4px;
            cursor: pointer;
            font-size: 14px;
            margin-right: 10px;
        }
        .btn-primary { background: #4da6ff; color: #fff; }
        .btn-secondary { background: #6b7280; color: #fff; }
        .status { margin-top: 10px; font-size: 12px; }
        .status-ok { color: #44ff44; }
        .status-err { color: #ff4444; }
    </style>
</head>
<body>
    <h1>Settings</h1>
    <p class="subtitle">Configure integrations</p>

    <nav class="nav">
        <a href="/">Dashboard</a>
        <a href="/coaching">Coaching</a>
        <a href="/comms">Communications</a>
        <a href="/refresh">Refresh Data</a>
        <a href="/settings">Settings</a>
    </nav>

    <div class="card">
        <h2>Slack Integration</h2>
        <form action="/settings/slack" method="POST">
            <label>Bot Token (xoxb-...)</label>
            <input type="password" name="slack_token" value="{{ slack_token_masked }}" placeholder="xoxb-your-token">
            <button type="submit" class="btn btn-primary">Save</button>
            <a href="/settings/slack/test" class="btn btn-secondary">Test</a>
        </form>
        <div class="status {{ 'status-ok' if slack_ok else 'status-err' }}">
            {{ 'Connected' if slack_ok else 'Not configured' }}
        </div>
    </div>

    <div class="card">
        <h2>Email (SMTP)</h2>
        <form action="/settings/email" method="POST">
            <label>SMTP Server</label>
            <input type="text" name="smtp_server" value="{{ email_config.smtp_server }}">
            <label>SMTP Port</label>
            <input type="text" name="smtp_port" value="{{ email_config.smtp_port }}">
            <label>Username</label>
            <input type="text" name="smtp_username" value="{{ email_config.smtp_username }}">
            <label>Password</label>
            <input type="password" name="smtp_password" value="{{ smtp_password_masked }}" placeholder="Enter password">
            <label>From Email</label>
            <input type="text" name="from_email" value="{{ email_config.from_email }}">
            <label>From Name</label>
            <input type="text" name="from_name" value="{{ email_config.from_name }}">
            <button type="submit" class="btn btn-primary">Save</button>
            <a href="/settings/email/test" class="btn btn-secondary">Test</a>
        </form>
        <div class="status {{ 'status-ok' if email_ok else 'status-err' }}">
            {{ 'Configured' if email_ok else 'Not configured' }}
        </div>
    </div>

    <div class="card">
        <h2>OpenAI API</h2>
        <form action="/settings/openai" method="POST">
            <label>API Key (sk-...)</label>
            <input type="password" name="openai_api_key" value="{{ openai_key_masked }}" placeholder="sk-your-api-key">
            <button type="submit" class="btn btn-primary">Save</button>
        </form>
        <div class="status {{ 'status-ok' if openai_ok else 'status-err' }}">
            {{ 'Configured' if openai_ok else 'Not configured' }}
        </div>
    </div>
</body>
</html>
'''

@app.route('/settings')
def settings_page():
    """Settings page."""
    config = load_config()

    slack_token = config.get('slack_token', '')
    slack_token_masked = slack_token[:10] + '...' if len(slack_token) > 10 else ''
    slack_ok = bool(slack_token)

    email_config = get_email_config()
    smtp_password_masked = '********' if email_config.get('smtp_password') else ''
    email_ok = is_email_configured()

    openai_key = config.get('openai_api_key') or os.environ.get('OPENAI_API_KEY', '')
    openai_key_masked = openai_key[:10] + '...' if len(openai_key) > 10 else ''
    openai_ok = bool(openai_key)

    return render_template_string(SETTINGS_HTML,
        slack_token_masked=slack_token_masked,
        slack_ok=slack_ok,
        email_config=email_config,
        smtp_password_masked=smtp_password_masked,
        email_ok=email_ok,
        openai_key_masked=openai_key_masked,
        openai_ok=openai_ok
    )


@app.route('/settings/slack', methods=['POST'])
def save_slack_settings():
    """Save Slack settings."""
    config = load_config()
    token = request.form.get('slack_token', '').strip()
    # Don't overwrite with masked token
    if token and not token.endswith('...'):
        config['slack_token'] = token
        save_config(config)
        init_slack()
    return redirect('/settings')


@app.route('/settings/slack/test')
def test_slack():
    """Test Slack connection."""
    init_slack()
    if slack_client:
        try:
            slack_client.auth_test()
            return "Slack connection OK!"
        except Exception as e:
            return f"Slack error: {e}"
    return "Slack not configured"


@app.route('/settings/email', methods=['POST'])
def save_email_settings():
    """Save email settings."""
    config = load_config()
    config['smtp_server'] = request.form.get('smtp_server', '').strip()
    config['smtp_port'] = int(request.form.get('smtp_port', 587))
    config['smtp_username'] = request.form.get('smtp_username', '').strip()
    password = request.form.get('smtp_password', '').strip()
    if password and password != '********':
        config['smtp_password'] = password
    config['from_email'] = request.form.get('from_email', '').strip()
    config['from_name'] = request.form.get('from_name', '').strip()
    save_config(config)
    return redirect('/settings')


@app.route('/settings/email/test')
def test_email():
    """Test email sending."""
    config = load_config()
    to_email = config.get('from_email', '')
    if not to_email:
        return "No from_email configured"
    success, msg = send_email(to_email, "Test Email", "This is a test email from AP Dashboard.")
    return f"Email {'sent!' if success else 'failed: ' + msg}"


@app.route('/settings/openai', methods=['POST'])
def save_openai_settings():
    """Save OpenAI settings."""
    config = load_config()
    key = request.form.get('openai_api_key', '').strip()
    if key and not key.endswith('...'):
        config['openai_api_key'] = key
        save_config(config)
    return redirect('/settings')


# =============================================================================
# MAIN
# =============================================================================

if __name__ == '__main__':
    # Initialize Slack on startup
    init_slack()

    print("Unified AP Dashboard")
    print("=" * 50)
    print("Open: http://localhost:5000")
    print()
    print("Pages:")
    print("  /         - AP Social Studies Dashboard")
    print("  /coaching - Coaching Schedule")
    print("  /comms    - Communications Center")
    print("  /settings - Settings")
    print()
    app.run(debug=True, port=5000)
