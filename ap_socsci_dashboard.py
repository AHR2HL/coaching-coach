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
SELF_ASSESSMENT_DIR = BASE_DIR / 'self_assessment'
COACHING_NOTES_FILE = BASE_DIR / 'coaching_notes.json'
STUDENT_LOGS_FILE = BASE_DIR / 'student_logs.json'

# Survey course name mapping
SURVEY_COURSE_MAP = {
    'AP Human Geography': 'APHG',
    'AP US Government & Politics': 'APGOV',
    'AP US History': 'APUSH',
    'AP World History: Modern': 'APWH',
}

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
# SELF-ASSESSMENT SURVEY DATA
# =============================================================================

def load_self_assessment_data():
    """Load all self-assessment survey CSVs and return parsed data."""
    surveys = {}

    if not SELF_ASSESSMENT_DIR.exists():
        return surveys

    for csv_file in SELF_ASSESSMENT_DIR.glob('*.csv'):
        try:
            df = pd.read_csv(csv_file)
            if len(df) == 0:
                continue

            # Determine course from filename or Course column
            course_col = df.get('Course')
            if course_col is not None and len(course_col) > 0:
                survey_course = course_col.iloc[0]
            else:
                # Infer from filename
                fname = csv_file.stem
                if 'Human Geography' in fname:
                    survey_course = 'AP Human Geography'
                elif 'Government' in fname:
                    survey_course = 'AP US Government & Politics'
                elif 'US History' in fname:
                    survey_course = 'AP US History'
                elif 'World History' in fname:
                    survey_course = 'AP World History: Modern'
                else:
                    continue

            norm_course = SURVEY_COURSE_MAP.get(survey_course, survey_course)

            for _, row in df.iterrows():
                # Get student name (normalize)
                name_raw = row.get('Your Name', '')
                if pd.isna(name_raw) or not name_raw:
                    continue
                name = str(name_raw).strip().title()

                # Parse confidence ratings (1-4 scale)
                unit_scores = []
                skill_scores = []
                frq_scores = []

                for col in df.columns:
                    val = row.get(col)
                    if pd.isna(val) or not isinstance(val, str):
                        continue

                    # Extract numeric confidence (1-4)
                    if val.startswith(('1 -', '2 -', '3 -', '4 -')):
                        score = int(val[0])
                        col_lower = col.lower()

                        # Categorize the rating
                        if 'unit' in col_lower or 'period' in col_lower:
                            unit_scores.append(score)
                        elif any(x in col_lower for x in ['thesis', 'evidence', 'answering all', 'organizing', 'finishing']):
                            frq_scores.append(score)
                        else:
                            skill_scores.append(score)

                # Calculate aggregate confidence (0-100 scale)
                all_scores = unit_scores + skill_scores + frq_scores
                if all_scores:
                    # Convert 1-4 to 0-100: (avg - 1) / 3 * 100
                    avg_score = sum(all_scores) / len(all_scores)
                    confidence_pct = round((avg_score - 1) / 3 * 100)
                else:
                    confidence_pct = None

                # Get predicted and target scores
                predicted_raw = row.get('If you took the full AP exam TODAY, what score do you think you\'d get?')
                target_raw = row.get('What score are you aiming for?')

                predicted = None
                if pd.notna(predicted_raw):
                    try:
                        predicted = int(str(predicted_raw).strip()[0])
                    except (ValueError, IndexError):
                        pass

                target = None
                if pd.notna(target_raw):
                    target_str = str(target_raw).strip()
                    if target_str.startswith('5'):
                        target = 5
                    elif target_str.startswith('4'):
                        target = 4
                    elif target_str.startswith('3'):
                        target = 3
                    elif target_str.startswith('2'):
                        target = 2
                    elif target_str.startswith('1'):
                        target = 1

                # Get open-ended concerns
                worry = row.get("What's the ONE thing you're most worried about for this exam?", '')
                help_needed = row.get("Is there a specific topic, skill, or question type you'd like more help with?", '')
                other = row.get("Anything else we should know about how you're feeling about the exam?", '')

                # Find weak areas (score 1 or 2)
                weak_units = []
                weak_skills = []

                for col in df.columns:
                    val = row.get(col)
                    if pd.isna(val) or not isinstance(val, str):
                        continue
                    if val.startswith(('1 -', '2 -')):
                        score = int(val[0])
                        col_clean = col.replace('Unit ', 'U').replace('Period ', 'P')
                        if 'unit' in col.lower() or 'period' in col.lower():
                            weak_units.append({'name': col_clean, 'score': score})
                        else:
                            weak_skills.append({'name': col, 'score': score})

                # Generate recommendations based on survey
                recommendations = []

                # FRQ-specific weak areas
                frq_weak_skills = [s for s in weak_skills if any(x in s['name'].lower() for x in
                    ['thesis', 'evidence', 'answering all', 'organizing', 'frq', 'leq', 'dbq', 'saq'])]
                if frq_weak_skills:
                    recommendations.append({
                        'type': 'FRQ',
                        'priority': 'high' if any(s['score'] == 1 for s in frq_weak_skills) else 'medium',
                        'detail': f"Weak FRQ skills: {', '.join(s['name'] for s in frq_weak_skills[:3])}"
                    })

                # Content gaps
                if weak_units:
                    recommendations.append({
                        'type': 'Content',
                        'priority': 'high' if any(u['score'] == 1 for u in weak_units) else 'medium',
                        'detail': f"Review needed: {', '.join(u['name'] for u in weak_units[:4])}"
                    })

                # Stress/anxiety
                stress_cols = [c for c in df.columns if 'stress' in c.lower() or 'anxiety' in c.lower() or 'pressure' in c.lower()]
                for col in stress_cols:
                    val = row.get(col)
                    if pd.notna(val) and isinstance(val, str) and val.startswith('1 -'):
                        recommendations.append({
                            'type': 'Support',
                            'priority': 'high',
                            'detail': 'Struggling with exam anxiety/stress - consider mindset coaching'
                        })
                        break

                # Time management
                time_cols = [c for c in df.columns if 'pace' in c.lower() or 'time' in c.lower()]
                time_issues = []
                for col in time_cols:
                    val = row.get(col)
                    if pd.notna(val) and isinstance(val, str) and val.startswith(('1 -', '2 -')):
                        time_issues.append(col)
                if time_issues:
                    recommendations.append({
                        'type': 'Pacing',
                        'priority': 'medium',
                        'detail': 'Practice timed conditions'
                    })

                # Store survey data
                key = (name, norm_course)
                surveys[key] = {
                    'name': name,
                    'email': row.get('Your Email', ''),
                    'course': norm_course,
                    'confidence': confidence_pct,
                    'predicted_score': predicted,
                    'target_score': target,
                    'unit_avg': round(sum(unit_scores) / len(unit_scores), 1) if unit_scores else None,
                    'skill_avg': round(sum(skill_scores) / len(skill_scores), 1) if skill_scores else None,
                    'frq_avg': round(sum(frq_scores) / len(frq_scores), 1) if frq_scores else None,
                    'weak_units': weak_units,
                    'weak_skills': weak_skills,
                    'worry': str(worry) if pd.notna(worry) and worry else None,
                    'help_needed': str(help_needed) if pd.notna(help_needed) and help_needed else None,
                    'other': str(other) if pd.notna(other) and other else None,
                    'recommendations': recommendations,
                    'timestamp': row.get('Timestamp', ''),
                }

        except Exception as e:
            print(f"Error loading survey {csv_file}: {e}")
            continue

    return surveys


def get_student_survey(surveys, student_name, course):
    """Get survey data for a specific student and course."""
    # Try exact match first
    key = (student_name, course)
    if key in surveys:
        return surveys[key]

    # Try matching by first name
    first_name = student_name.split()[0].lower()
    for (name, surv_course), data in surveys.items():
        if surv_course == course and name.lower().split()[0] == first_name:
            return data

    # Try matching by last name (handles Saeed/Said type variations)
    if len(student_name.split()) >= 2:
        last_name = student_name.split()[-1].lower()
        for (name, surv_course), data in surveys.items():
            if surv_course == course and len(name.split()) >= 2:
                if name.lower().split()[-1] == last_name:
                    return data

    return None


def get_students_without_survey(students, surveys):
    """Get list of students who haven't completed the survey, grouped by course."""
    missing = {}

    for s in students:
        name = s.get('student', '')
        course = s.get('course', '')
        if not name or not course:
            continue

        survey = get_student_survey(surveys, name, course)
        if survey is None:
            if course not in missing:
                missing[course] = []
            missing[course].append({
                'student': name,
                'course': course,
                'email': s.get('email', ''),
                'risk': s.get('risk', 'Unknown'),
                'combined_progress': s.get('combined_progress')
            })

    return missing


def generate_coaching_insights(student):
    """
    Generate unified coaching insights by triangulating:
    - Survey data (self-assessment)
    - Quantitative data (Timeback, Austin Way, PT)
    - Coaching notes (qualitative observations from calls)
    """
    insights = {
        'blind_spots': [],      # Confident but data shows weak (dangerous)
        'false_worries': [],    # Worried but data shows strong (reassure)
        'confirmed_gaps': [],   # Both agree it's weak (priority)
        'confirmed_strengths': [],  # Both agree it's strong
        'data_only_gaps': [],   # Data shows weak, no survey data
        'priority_actions': [], # Top recommended actions
        'from_coaching': [],    # Insights from coaching notes
    }

    survey = student.get('survey')
    unit_details = student.get('unit_details', [])
    coaching_patterns = student.get('coaching_patterns')
    frq_accuracy = student.get('tb_frq_accuracy')
    mcq_accuracy = student.get('tb_mcq_accuracy')
    weak_units_data = student.get('weak_units', [])  # From quantitative analysis

    # Build unit mastery lookup from quantitative data
    unit_mastery = {}
    for ud in unit_details:
        if not ud.get('non_ced', False):
            unit_num = ud.get('unit', '')
            unit_mastery[str(unit_num)] = ud.get('combined', 0)

    if survey:
        survey_weak_units = survey.get('weak_units', [])
        survey_frq_avg = survey.get('frq_avg')

        # === UNIT-LEVEL ANALYSIS ===
        # Map survey unit names to unit numbers
        for sw in survey_weak_units:
            unit_name = sw.get('name', '')
            confidence = sw.get('score', 2)  # 1-4 scale

            # Extract unit number from name (e.g., "U7" -> "7", "P3" -> "3")
            unit_num = None
            if unit_name.startswith('U') or unit_name.startswith('P'):
                parts = unit_name.split(':')[0].replace('U', '').replace('P', '').strip()
                if parts.isdigit():
                    unit_num = parts

            if unit_num and unit_num in unit_mastery:
                mastery = unit_mastery[unit_num]
                is_confident = confidence >= 3  # 3-4 = confident
                is_mastered = mastery >= 60     # 60%+ = mastered

                if is_confident and not is_mastered:
                    insights['blind_spots'].append({
                        'area': f'Unit {unit_num}',
                        'detail': unit_name,
                        'self_rating': f'{confidence}/4 confident',
                        'actual': f'{mastery:.0f}% mastery',
                        'action': 'Review needed - confidence exceeds actual performance'
                    })
                elif not is_confident and is_mastered:
                    insights['false_worries'].append({
                        'area': f'Unit {unit_num}',
                        'detail': unit_name,
                        'self_rating': f'{confidence}/4 confident',
                        'actual': f'{mastery:.0f}% mastery',
                        'action': 'Reassure - performing better than they think'
                    })
                elif not is_confident and not is_mastered:
                    insights['confirmed_gaps'].append({
                        'area': f'Unit {unit_num}',
                        'detail': unit_name,
                        'self_rating': f'{confidence}/4 confident',
                        'actual': f'{mastery:.0f}% mastery',
                        'action': 'Priority review - both perception and data show gap'
                    })

        # Check for blind spots in units student didn't mark as weak
        marked_weak_units = set()
        for sw in survey_weak_units:
            unit_name = sw.get('name', '')
            if unit_name.startswith('U') or unit_name.startswith('P'):
                parts = unit_name.split(':')[0].replace('U', '').replace('P', '').strip()
                if parts.isdigit():
                    marked_weak_units.add(parts)

        for unit_num, mastery in unit_mastery.items():
            if unit_num not in marked_weak_units and mastery < 60:
                insights['blind_spots'].append({
                    'area': f'Unit {unit_num}',
                    'detail': f'Unit {unit_num}',
                    'self_rating': 'Not flagged as weak',
                    'actual': f'{mastery:.0f}% mastery',
                    'action': 'Blind spot - student may not realize this needs work'
                })

        # === FRQ ANALYSIS ===
        if survey_frq_avg is not None and frq_accuracy is not None:
            frq_confident = survey_frq_avg >= 3
            frq_strong = frq_accuracy >= 70

            if frq_confident and not frq_strong:
                insights['blind_spots'].append({
                    'area': 'FRQ Skills',
                    'detail': 'Free Response Questions',
                    'self_rating': f'{survey_frq_avg}/4 confident',
                    'actual': f'{frq_accuracy:.0f}% accuracy',
                    'action': 'FRQ practice needed - overconfident'
                })
            elif not frq_confident and frq_strong:
                insights['false_worries'].append({
                    'area': 'FRQ Skills',
                    'detail': 'Free Response Questions',
                    'self_rating': f'{survey_frq_avg}/4 confident',
                    'actual': f'{frq_accuracy:.0f}% accuracy',
                    'action': 'Reassure - FRQ performance is actually strong'
                })
            elif not frq_confident and not frq_strong:
                insights['confirmed_gaps'].append({
                    'area': 'FRQ Skills',
                    'detail': 'Free Response Questions',
                    'self_rating': f'{survey_frq_avg}/4 confident',
                    'actual': f'{frq_accuracy:.0f}% accuracy',
                    'action': 'Priority - both feel weak and data confirms'
                })

        # === PREDICTED vs ACTUAL ANALYSIS ===
        predicted = survey.get('predicted_score')
        pt_score = student.get('pt_score')
        if predicted and pt_score:
            if predicted > pt_score + 1:
                insights['blind_spots'].append({
                    'area': 'Overall Readiness',
                    'detail': 'Predicted vs Practice Test',
                    'self_rating': f'Predicts {predicted}',
                    'actual': f'PT score: {pt_score}',
                    'action': 'Reality check needed - significant gap between expectation and performance'
                })
            elif predicted < pt_score:
                insights['false_worries'].append({
                    'area': 'Overall Readiness',
                    'detail': 'Predicted vs Practice Test',
                    'self_rating': f'Predicts {predicted}',
                    'actual': f'PT score: {pt_score}',
                    'action': 'Confidence boost - performing better than they expect'
                })

        # === STRESS/ANXIETY from survey ===
        for rec in survey.get('recommendations', []):
            if rec.get('type') == 'Support':
                insights['priority_actions'].append({
                    'type': 'Mindset',
                    'action': rec.get('detail', 'Address exam anxiety'),
                    'source': 'Self-reported'
                })

    else:
        # No survey - use data-only gaps
        for wu in weak_units_data:
            insights['data_only_gaps'].append({
                'area': wu.get('unit_name', 'Unknown'),
                'actual': f"{wu.get('mastery', 0)}% mastery",
                'action': 'Review needed (no self-assessment available)'
            })

        if frq_accuracy is not None and frq_accuracy < 70:
            insights['data_only_gaps'].append({
                'area': 'FRQ Skills',
                'actual': f'{frq_accuracy:.0f}% accuracy',
                'action': 'FRQ practice recommended'
            })

    # === INTEGRATE COACHING NOTES ===
    if coaching_patterns:
        # Add recurring concerns from coaching calls
        for concern in coaching_patterns.get('recurring_concerns', [])[:2]:
            insights['from_coaching'].append({
                'type': 'Recurring Concern',
                'detail': concern,
                'sessions': coaching_patterns.get('total_sessions', 0)
            })

        # Add pending action items
        for action in coaching_patterns.get('pending_actions', [])[:2]:
            insights['from_coaching'].append({
                'type': 'Action Item',
                'detail': action,
                'sessions': coaching_patterns.get('total_sessions', 0)
            })

        # Add recent strengths observed
        for strength in coaching_patterns.get('recent_strengths', [])[:2]:
            insights['from_coaching'].append({
                'type': 'Strength Observed',
                'detail': strength,
                'sessions': coaching_patterns.get('total_sessions', 0)
            })

        # Add trajectory info
        insights['coaching_trajectory'] = coaching_patterns.get('trajectory', 'unknown')
        insights['coaching_sessions'] = coaching_patterns.get('total_sessions', 0)
        insights['latest_sentiment'] = coaching_patterns.get('latest_sentiment', 'unknown')

    # === GENERATE PRIORITY ACTIONS ===
    # Coaching concerns are highest priority (human-observed)
    if coaching_patterns:
        for concern in coaching_patterns.get('recurring_concerns', [])[:1]:
            insights['priority_actions'].append({
                'type': 'Coach Observed',
                'action': concern,
                'source': f"Recurring across {coaching_patterns.get('total_sessions', 0)} sessions"
            })

    # Blind spots are next priority (dangerous)
    for bs in insights['blind_spots'][:2]:
        insights['priority_actions'].append({
            'type': 'Blind Spot',
            'action': f"{bs['area']}: {bs['action']}",
            'source': 'Data vs Self-Assessment mismatch'
        })

    # Confirmed gaps are next priority
    for cg in insights['confirmed_gaps'][:2]:
        insights['priority_actions'].append({
            'type': 'Confirmed Gap',
            'action': f"{cg['area']}: {cg['action']}",
            'source': 'Both data and student agree'
        })

    # False worries - mention for reassurance
    if insights['false_worries']:
        insights['priority_actions'].append({
            'type': 'Reassurance',
            'action': f"Student underestimates themselves in: {', '.join(fw['area'] for fw in insights['false_worries'][:3])}",
            'source': 'Performing better than they think'
        })

    return insights


# =============================================================================
# COACHING NOTES (Qualitative Data from Calls)
# =============================================================================

def load_coaching_notes():
    """Load all coaching notes."""
    if COACHING_NOTES_FILE.exists():
        try:
            with open(COACHING_NOTES_FILE, 'r') as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            return {}
    return {}


def save_coaching_notes(notes):
    """Save coaching notes."""
    with open(COACHING_NOTES_FILE, 'w') as f:
        json.dump(notes, f, indent=2)


def extract_insights_from_notes(raw_notes, student_name, course):
    """
    Use OpenAI to extract structured insights from free-form coaching notes.
    Returns extracted themes, action items, concerns, strengths, and sentiment.
    """
    if not OPENAI_AVAILABLE:
        return {
            'themes': [],
            'action_items': [],
            'concerns': [],
            'strengths': [],
            'sentiment': 'unknown',
            'summary': raw_notes[:200] + '...' if len(raw_notes) > 200 else raw_notes,
            'processed': False
        }

    config = load_config()
    api_key = config.get('openai_api_key') or os.environ.get('OPENAI_API_KEY', '')

    if not api_key:
        return {
            'themes': [],
            'action_items': [],
            'concerns': [],
            'strengths': [],
            'sentiment': 'unknown',
            'summary': raw_notes[:200] + '...' if len(raw_notes) > 200 else raw_notes,
            'processed': False
        }

    try:
        client = openai.OpenAI(api_key=api_key)

        prompt = f"""Analyze these coaching notes for {student_name} ({course}) and extract structured insights.

Notes:
{raw_notes}

Return a JSON object with these fields:
- themes: array of 2-5 key topics/skills discussed (e.g., "FRQ structure", "time management", "Unit 4 content")
- action_items: array of specific next steps or commitments made (e.g., "Complete 2 practice FRQs by Friday")
- concerns: array of struggles, challenges, or areas needing attention
- strengths: array of positive observations, progress made, or things going well
- sentiment: one of "struggling", "mixed", "improving", "strong" - overall tone of the session
- summary: 1-2 sentence summary of the coaching call

Return ONLY valid JSON, no markdown formatting."""

        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=500,
            temperature=0.3
        )

        result_text = response.choices[0].message.content.strip()

        # Clean up potential markdown formatting
        if result_text.startswith('```'):
            result_text = result_text.split('```')[1]
            if result_text.startswith('json'):
                result_text = result_text[4:]
            result_text = result_text.strip()

        extracted = json.loads(result_text)
        extracted['processed'] = True
        return extracted

    except Exception as e:
        print(f"Error extracting insights: {e}")
        return {
            'themes': [],
            'action_items': [],
            'concerns': [],
            'strengths': [],
            'sentiment': 'unknown',
            'summary': raw_notes[:200] + '...' if len(raw_notes) > 200 else raw_notes,
            'processed': False,
            'error': str(e)
        }


def add_coaching_note(student_name, course, raw_notes, call_date=None):
    """Add a coaching note for a student and extract insights."""
    notes = load_coaching_notes()
    key = f"{student_name}|{course}"

    if key not in notes:
        notes[key] = []

    # Extract insights using NLP
    extracted = extract_insights_from_notes(raw_notes, student_name, course)

    note_entry = {
        'date': call_date or datetime.now().strftime('%Y-%m-%d'),
        'timestamp': datetime.now().isoformat(),
        'raw_notes': raw_notes,
        'extracted': extracted
    }

    notes[key].append(note_entry)
    save_coaching_notes(notes)

    return note_entry


def get_student_notes(student_name, course, limit=5):
    """Get recent coaching notes for a student."""
    notes = load_coaching_notes()
    key = f"{student_name}|{course}"
    student_notes = notes.get(key, [])

    # Sort by date descending and limit
    sorted_notes = sorted(student_notes, key=lambda x: x.get('timestamp', ''), reverse=True)
    return sorted_notes[:limit]


def get_coaching_patterns(student_name, course):
    """
    Analyze patterns across all coaching notes for a student.
    Returns aggregated themes, recurring concerns, and progress trajectory.
    """
    notes = get_student_notes(student_name, course, limit=10)

    if not notes:
        return None

    # Aggregate themes, concerns, strengths across all notes
    all_themes = []
    all_concerns = []
    all_strengths = []
    all_action_items = []
    sentiments = []

    for note in notes:
        extracted = note.get('extracted', {})
        all_themes.extend(extracted.get('themes', []))
        all_concerns.extend(extracted.get('concerns', []))
        all_strengths.extend(extracted.get('strengths', []))
        all_action_items.extend(extracted.get('action_items', []))
        if extracted.get('sentiment'):
            sentiments.append(extracted['sentiment'])

    # Count frequency of themes/concerns
    from collections import Counter
    theme_counts = Counter(all_themes)
    concern_counts = Counter(all_concerns)

    # Determine trajectory from sentiments
    sentiment_order = {'struggling': 1, 'mixed': 2, 'improving': 3, 'strong': 4}
    if len(sentiments) >= 2:
        recent_avg = sum(sentiment_order.get(s, 2) for s in sentiments[:2]) / 2
        older_avg = sum(sentiment_order.get(s, 2) for s in sentiments[2:]) / max(len(sentiments[2:]), 1)
        if recent_avg > older_avg + 0.5:
            trajectory = 'improving'
        elif recent_avg < older_avg - 0.5:
            trajectory = 'declining'
        else:
            trajectory = 'stable'
    else:
        trajectory = 'insufficient_data'

    return {
        'total_sessions': len(notes),
        'recurring_themes': [t for t, c in theme_counts.most_common(5)],
        'recurring_concerns': [c for c, _ in concern_counts.most_common(3)],
        'recent_strengths': all_strengths[:5],
        'pending_actions': all_action_items[:5],
        'trajectory': trajectory,
        'latest_sentiment': sentiments[0] if sentiments else 'unknown',
        'latest_date': notes[0].get('date') if notes else None
    }


# =============================================================================
# STUDENT DAILY LOGS (Self-reported work logs)
# =============================================================================

def load_student_logs():
    """Load all student daily logs."""
    if STUDENT_LOGS_FILE.exists():
        try:
            with open(STUDENT_LOGS_FILE, 'r') as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            return {'logs': [], 'by_student': {}}
    return {'logs': [], 'by_student': {}}


def save_student_logs(data):
    """Save student logs."""
    with open(STUDENT_LOGS_FILE, 'w') as f:
        json.dump(data, f, indent=2)


def parse_student_log_text(raw_text):
    """
    Parse student log formats from form submissions or summary reports.

    Format 1 (Raw form data):
    --- AP Human Geography (1 student(s)) ---
    - Grady Swanson: [AP Human Geography] | Today's work: X | Gaps: Y | Prediction: 5 | Tomorrow's focus: Z | Recall: W

    Format 2 (Summary report):
    --- AP Human Geography (5 student(s)) ---
    - Jessica Owenby: Completed 1 Human Geography lesson and reviewed urban change topics; check in on confusing question wording.
    - Grady Swanson: Logged unrelated math work instead of Human Geography; flag: disengaged.
    Priority: Grady Swanson, Jessica Owenby
    Questions: Is Grady actually working on HuGeo?
    """
    import html
    import re

    # Decode HTML entities
    text = html.unescape(raw_text)

    logs = []
    current_course = None
    current_priority = []
    current_questions = []

    lines = text.strip().split('\n')
    i = 0

    while i < len(lines):
        line = lines[i].strip()
        i += 1

        if not line:
            continue

        # Check for course header
        header_match = re.match(r'^---\s*(.+?)\s*\(\d+\s*student', line)
        if header_match:
            current_course = header_match.group(1).strip()
            current_priority = []
            current_questions = []
            continue

        # Check for Priority line
        if line.startswith('Priority:'):
            priority_text = line[9:].strip()
            current_priority = [p.strip() for p in priority_text.split(',')]
            continue

        # Check for Questions line
        if line.startswith('Questions:') or line.startswith('Question:'):
            current_questions.append(line.split(':', 1)[1].strip())
            continue

        # Check for student log line (starts with "- Name:")
        if line.startswith('- '):
            # Extract name (before the colon)
            name_match = re.match(r'^-\s*([^:]+):', line)
            if not name_match:
                continue

            student_name = name_match.group(1).strip()
            rest_of_line = line[name_match.end():].strip()

            # Determine format by checking for pipe separators
            if '|' in rest_of_line:
                # Format 1: Pipe-separated fields
                try:
                    fields = {}

                    # Course in brackets
                    course_match = re.search(r'\[([^\]]+)\]', rest_of_line)
                    if course_match:
                        fields['course'] = course_match.group(1).strip()

                    # Parse pipe-separated fields
                    field_patterns = [
                        (r"Today'?s?\s*work:\s*([^|]+)", 'todays_work'),
                        (r"Gaps?:\s*([^|]+)", 'gaps'),
                        (r"Prediction:\s*(\d+)", 'prediction'),
                        (r"Tomorrow'?s?\s*focus:\s*([^|]+)", 'tomorrows_focus'),
                        (r"Recall:\s*(.+)$", 'recall'),
                    ]

                    for pattern, field_name in field_patterns:
                        match = re.search(pattern, rest_of_line, re.IGNORECASE)
                        if match:
                            value = match.group(1).strip()
                            value = re.sub(r'\s*\|\s*$', '', value)
                            fields[field_name] = value

                    if fields:
                        logs.append({
                            'student': student_name,
                            'course': fields.get('course', current_course or 'Unknown'),
                            'todays_work': fields.get('todays_work', ''),
                            'gaps': fields.get('gaps', ''),
                            'prediction': int(fields.get('prediction', 0)) if fields.get('prediction') else None,
                            'tomorrows_focus': fields.get('tomorrows_focus', ''),
                            'recall': fields.get('recall', ''),
                            'date': datetime.now().strftime('%Y-%m-%d'),
                            'timestamp': datetime.now().isoformat(),
                            'format': 'raw'
                        })
                except Exception as e:
                    print(f"Error parsing raw log line: {line} - {e}")
            else:
                # Format 2: Summary report (no pipes)
                # The rest of the line is a summary with action items
                summary = rest_of_line

                # Try to split on semicolon to separate work done from action items
                parts = summary.split(';')
                work_summary = parts[0].strip() if parts else summary
                action_item = '; '.join(parts[1:]).strip() if len(parts) > 1 else ''

                # Check for flags
                is_flagged = 'flag:' in summary.lower() or 'flag ' in summary.lower()

                logs.append({
                    'student': student_name,
                    'course': current_course or 'Unknown',
                    'todays_work': work_summary,
                    'gaps': '',
                    'prediction': None,
                    'tomorrows_focus': '',
                    'recall': '',
                    'summary': summary,
                    'action_item': action_item,
                    'flagged': is_flagged,
                    'priority': student_name in current_priority,
                    'date': datetime.now().strftime('%Y-%m-%d'),
                    'timestamp': datetime.now().isoformat(),
                    'format': 'summary'
                })

    return logs


def ingest_student_logs(raw_text):
    """Parse and store student logs from form submission text."""
    parsed_logs = parse_student_log_text(raw_text)

    if not parsed_logs:
        return {'success': False, 'error': 'No valid logs found in input', 'count': 0}

    # Load existing logs
    data = load_student_logs()

    # Add new logs
    for log in parsed_logs:
        data['logs'].append(log)

        # Index by student
        student_key = f"{log['student']}|{log['course']}"
        if student_key not in data['by_student']:
            data['by_student'][student_key] = []
        data['by_student'][student_key].append(log)

    # Save
    save_student_logs(data)

    return {
        'success': True,
        'count': len(parsed_logs),
        'students': [f"{l['student']} ({l['course']})" for l in parsed_logs]
    }


def normalize_course_name(course):
    """Normalize course name for matching."""
    course_lower = course.lower()
    if 'geography' in course_lower or course_lower == 'aphg':
        return 'APHG'
    elif 'world' in course_lower or course_lower == 'apwh':
        return 'APWH'
    elif 'united states history' in course_lower or 'us history' in course_lower or course_lower == 'apush':
        return 'APUSH'
    elif 'government' in course_lower or course_lower == 'apgov':
        return 'APGOV'
    elif 'computer science' in course_lower or course_lower == 'apcsa':
        return 'APCSA'
    return course


def get_student_logs(student_name, course, limit=10):
    """Get recent logs for a specific student."""
    data = load_student_logs()

    # Normalize the course we're looking for
    target_course = normalize_course_name(course)

    # Search through all logs for matching student and course
    matching_logs = []
    for log in data.get('logs', []):
        if log.get('student') == student_name:
            log_course = normalize_course_name(log.get('course', ''))
            if log_course == target_course:
                matching_logs.append(log)

    # Sort by timestamp descending
    logs = sorted(matching_logs, key=lambda x: x.get('timestamp', ''), reverse=True)
    return logs[:limit]


def get_recent_logs(limit=50):
    """Get most recent logs across all students."""
    data = load_student_logs()
    logs = sorted(data.get('logs', []), key=lambda x: x.get('timestamp', ''), reverse=True)
    return logs[:limit]


def delete_student_log(timestamp):
    """Delete a log entry by timestamp."""
    data = load_student_logs()

    # Find and remove from main logs list
    original_count = len(data.get('logs', []))
    data['logs'] = [l for l in data.get('logs', []) if l.get('timestamp') != timestamp]
    removed = original_count - len(data['logs'])

    # Also remove from by_student index
    for student_key in data.get('by_student', {}):
        data['by_student'][student_key] = [
            l for l in data['by_student'][student_key]
            if l.get('timestamp') != timestamp
        ]

    if removed > 0:
        save_student_logs(data)
        return {'success': True, 'removed': removed}
    else:
        return {'success': False, 'error': 'Log not found'}


# =============================================================================
# COACHING PLANNER - Need Assessment & Time Allocation
# =============================================================================

def calculate_coaching_need(student):
    """
    Calculate a coaching need score (0-100) for a student.
    Higher = more urgent need for coaching time.
    """
    score = 0
    factors = []

    # Risk level (0-30 points)
    risk = student.get('risk', 'Unknown')
    risk_scores = {'Critical': 30, 'At Risk': 20, 'On Track': 8, 'Strong': 2, 'Unknown': 15}
    risk_pts = risk_scores.get(risk, 15)
    score += risk_pts
    if risk_pts >= 20:
        factors.append(f"Risk: {risk}")

    # Blind spots from coaching insights (0-20 points) - DANGEROUS
    ci = student.get('coaching_insights', {})
    blind_spots = len(ci.get('blind_spots', []))
    blind_pts = min(blind_spots * 7, 20)
    score += blind_pts
    if blind_spots > 0:
        factors.append(f"{blind_spots} blind spot(s)")

    # Confirmed gaps (0-15 points)
    confirmed_gaps = len(ci.get('confirmed_gaps', []))
    gap_pts = min(confirmed_gaps * 5, 15)
    score += gap_pts
    if confirmed_gaps > 0:
        factors.append(f"{confirmed_gaps} confirmed gap(s)")

    # Distance from target (0-15 points)
    predicted = student.get('predicted_score')
    target = student.get('target_score')
    pt_score = student.get('pt_score')
    actual = pt_score or predicted
    if actual and target:
        gap = target - actual
        if gap >= 3:
            score += 15
            factors.append(f"Target gap: {gap} points")
        elif gap >= 2:
            score += 10
            factors.append(f"Target gap: {gap} points")
        elif gap >= 1:
            score += 5

    # Coaching trajectory (0-10 points)
    trajectory = ci.get('coaching_trajectory', 'unknown')
    if trajectory == 'declining':
        score += 10
        factors.append("Declining trajectory")
    elif trajectory == 'stable':
        score += 3
    elif trajectory == 'improving':
        score += 0  # Good trajectory, less urgent

    # Time pressure - late for PT deadline (0-10 points)
    if student.get('late_for_pt'):
        score += 10
        factors.append("Behind schedule")

    # FRQ weakness (0-10 points) - common critical gap
    frq_accuracy = student.get('tb_frq_accuracy')
    if frq_accuracy is not None and frq_accuracy < 60:
        score += 8
        factors.append(f"FRQ weak ({frq_accuracy:.0f}%)")
    elif student.get('frq_weak'):
        score += 6
        factors.append("FRQ flagged")

    # Neglected - no recent coaching (0-5 points)
    patterns = student.get('coaching_patterns')
    if not patterns or patterns.get('total_sessions', 0) == 0:
        score += 5
        factors.append("No coaching sessions yet")

    # Low confidence despite need (0-5 points)
    confidence = student.get('confidence')
    if confidence is not None and confidence < 40 and risk in ('Critical', 'At Risk'):
        score += 5
        factors.append("Low confidence")

    # Cap at 100
    score = min(score, 100)

    return {
        'score': score,
        'factors': factors,
        'priority': 'Critical' if score >= 70 else 'High' if score >= 50 else 'Medium' if score >= 30 else 'Low'
    }


def generate_session_agenda(student):
    """
    Generate a coaching session agenda based on student's needs.
    """
    ci = student.get('coaching_insights', {})
    survey = student.get('survey')
    patterns = student.get('coaching_patterns')

    agenda = {
        'student': student.get('student'),
        'course': student.get('course'),
        'duration_min': 30,  # Default
        'sections': []
    }

    # 1. Check-in (5 min)
    checkin = {
        'title': 'Check-in',
        'duration': 5,
        'content': ['How are you feeling about the exam?', 'Any wins or struggles since last time?']
    }

    # Add specific check-in items based on data
    if patterns and patterns.get('pending_actions'):
        checkin['content'].append(f"Follow up: {patterns['pending_actions'][0]}")
    if survey and survey.get('worry'):
        worry = survey['worry'][:80] + '...' if len(survey.get('worry', '')) > 80 else survey.get('worry', '')
        checkin['content'].append(f"Check on worry: \"{worry}\"")

    agenda['sections'].append(checkin)

    # 2. Priority Focus (15-20 min)
    priority_actions = ci.get('priority_actions', [])
    focus = {
        'title': 'Focus Area',
        'duration': 15,
        'content': []
    }

    # Add priority items
    for pa in priority_actions[:3]:
        action_type = pa.get('type', '')
        action = pa.get('action', '')

        if action_type == 'Blind Spot':
            focus['content'].append(f"🔴 ADDRESS BLIND SPOT: {action}")
        elif action_type == 'Confirmed Gap':
            focus['content'].append(f"🟠 Work on gap: {action}")
        elif action_type == 'Coach Observed':
            focus['content'].append(f"🔵 Continue working on: {action}")
        elif action_type == 'Reassurance':
            focus['content'].append(f"🟢 Reassure: {action}")

    # Add FRQ practice if needed
    if student.get('frq_weak') or (student.get('tb_frq_accuracy') and student.get('tb_frq_accuracy') < 70):
        focus['content'].append("📝 FRQ practice: Work through one question together")
        focus['duration'] = 20

    if not focus['content']:
        focus['content'].append("Review recent progress and identify next focus area")

    agenda['sections'].append(focus)

    # 3. Practice/Application (5-10 min)
    practice = {
        'title': 'Practice',
        'duration': 5,
        'content': []
    }

    # Suggest specific practice based on gaps
    weak_units = student.get('weak_units', [])
    if weak_units:
        unit = weak_units[0]
        practice['content'].append(f"Quick review: {unit.get('unit_name', 'Weak unit')} concepts")

    blind_spots = ci.get('blind_spots', [])
    if blind_spots:
        practice['content'].append(f"Reality check: Quiz on {blind_spots[0].get('area', 'blind spot area')}")

    if not practice['content']:
        practice['content'].append("Review a practice question or concept together")

    agenda['sections'].append(practice)

    # 4. Commitments (5 min)
    commitments = {
        'title': 'Commitments & Next Steps',
        'duration': 5,
        'content': [
            'What will you complete before our next session?',
            'Set specific, measurable goal'
        ]
    }

    # Suggest based on need
    recommendation = student.get('recommendation', '')
    if recommendation == 'FRQ':
        commitments['content'].append("Suggested: Complete 2 practice FRQs")
    elif recommendation in ('Holes', 'Hole-Fill', 'Hole+FRQ'):
        commitments['content'].append("Suggested: Review weak unit(s) on Austin Way")
    elif recommendation == 'PT':
        commitments['content'].append("Suggested: Take practice test")
    elif recommendation == 'Stay':
        commitments['content'].append("Suggested: Continue current pace")

    agenda['sections'].append(commitments)

    # Calculate total duration
    agenda['duration_min'] = sum(s['duration'] for s in agenda['sections'])

    return agenda


def determine_primary_coaching_need(student):
    """
    Determine if a student's primary need is FRQ/essay skills or content gaps.
    Returns 'frq' for essay/writing focus, 'content' for knowledge gaps.
    """
    ci = student.get('coaching_insights', {})

    # Check for FRQ-related issues
    frq_indicators = 0

    # FRQ accuracy below threshold
    if student.get('tb_frq_accuracy') is not None and student.get('tb_frq_accuracy') < 70:
        frq_indicators += 2
    if student.get('frq_weak'):
        frq_indicators += 2

    # Survey indicates FRQ concerns
    survey = student.get('survey', {})
    if survey:
        frq_avg = survey.get('frq_avg')
        if frq_avg is not None and frq_avg < 2.5:
            frq_indicators += 2
        # Check if worry mentions FRQ/essay/writing
        worry = (survey.get('worry') or '').lower()
        if any(x in worry for x in ['frq', 'essay', 'writing', 'thesis', 'evidence', 'dbq', 'leq', 'saq']):
            frq_indicators += 1

    # Check coaching insights for FRQ-related gaps
    for gap in ci.get('confirmed_gaps', []):
        if 'FRQ' in gap.get('area', ''):
            frq_indicators += 2

    # Check for content gaps
    content_indicators = 0

    # Unit-level blind spots or confirmed gaps
    for item in ci.get('blind_spots', []) + ci.get('confirmed_gaps', []):
        area = item.get('area', '')
        if 'Unit' in area or 'Period' in area:
            content_indicators += 1

    # Weak units from quantitative data
    weak_units = student.get('weak_units', [])
    content_indicators += len(weak_units)

    # Recommendation suggests content work
    rec = student.get('recommendation', '')
    if rec in ('Holes', 'Hole-Fill', 'Hole+FRQ'):
        content_indicators += 2

    # Determine primary need
    if frq_indicators >= content_indicators and frq_indicators > 0:
        return 'frq', frq_indicators, content_indicators
    elif content_indicators > 0:
        return 'content', frq_indicators, content_indicators
    else:
        return 'general', frq_indicators, content_indicators


def calculate_coaching_plan(students, adam_hours=3, external_hours=2):
    """
    Generate a coaching plan that allocates time between two coaches:
    - Adam: Focuses on FRQ/essay skills (LEQ, DBQ, thesis, evidence, etc.)
    - External: Content specialist who handles knowledge gaps + overflow

    Returns:
    - Prioritized student list with coach assignments
    - Utilization stats for both coaches
    """
    # Calculate need scores and determine primary coaching need
    student_needs = []
    for s in students:
        need = calculate_coaching_need(s)
        primary_need, frq_score, content_score = determine_primary_coaching_need(s)
        student_needs.append({
            **s,
            'need_score': need['score'],
            'need_factors': need['factors'],
            'need_priority': need['priority'],
            'agenda': generate_session_agenda(s),
            'primary_need': primary_need,
            'frq_need_score': frq_score,
            'content_need_score': content_score
        })

    # Sort by need score (highest first)
    student_needs.sort(key=lambda x: x['need_score'], reverse=True)

    # Track allocation for both coaches
    adam_available_min = adam_hours * 60
    external_available_min = external_hours * 60
    adam_allocated_min = 0
    external_allocated_min = 0

    allocated = []

    for s in student_needs:
        need = s['need_score']
        priority = s['need_priority']
        primary_need = s['primary_need']

        # Determine session frequency and duration based on priority
        if priority == 'Critical':
            frequency = 'weekly'
            session_min = 45
            sessions_per_week = 1
        elif priority == 'High':
            frequency = 'weekly'
            session_min = 30
            sessions_per_week = 1
        elif priority == 'Medium':
            frequency = 'biweekly'
            session_min = 30
            sessions_per_week = 0.5
        else:  # Low
            frequency = 'monthly'
            session_min = 20
            sessions_per_week = 0.25

        weekly_min = session_min * sessions_per_week

        # Assign coach based on primary need and availability
        # Adam handles FRQ/essay needs
        # External handles content gaps + overflow
        if primary_need == 'frq' and adam_allocated_min + weekly_min <= adam_available_min:
            assigned_coach = 'Adam'
            coach_focus = 'FRQ/Essay Skills'
            adam_allocated_min += weekly_min
        elif primary_need == 'content':
            assigned_coach = 'External'
            coach_focus = 'Content Gaps'
            external_allocated_min += weekly_min
        elif adam_allocated_min + weekly_min <= adam_available_min:
            # General need, Adam has capacity
            assigned_coach = 'Adam'
            coach_focus = 'General Support'
            adam_allocated_min += weekly_min
        else:
            # Overflow to external
            assigned_coach = 'External'
            coach_focus = 'Overflow' if primary_need != 'content' else 'Content Gaps'
            external_allocated_min += weekly_min

        allocated.append({
            'student': s['student'],
            'course': s['course'],
            'need_score': need,
            'need_priority': priority,
            'need_factors': s['need_factors'],
            'frequency': frequency,
            'session_duration': session_min,
            'weekly_minutes': weekly_min,
            'agenda': s['agenda'],
            'risk': s.get('risk'),
            'pt_score': s.get('pt_score'),
            'confidence': s.get('confidence'),
            'recommendation': s.get('recommendation'),
            'coaching_sessions': s.get('coaching_patterns', {}).get('total_sessions', 0) if s.get('coaching_patterns') else 0,
            'assigned_coach': assigned_coach,
            'coach_focus': coach_focus,
            'primary_need': primary_need,
            'frq_need_score': s['frq_need_score'],
            'content_need_score': s['content_need_score']
        })

    # Calculate baseline utilization (before upgrades)
    baseline_adam_min = adam_allocated_min
    baseline_external_min = external_allocated_min

    # =========================================================================
    # SURPLUS ALLOCATION: Upgrade students if we have extra capacity
    # =========================================================================
    upgrades_made = []

    def try_upgrade_student(student, coach_available, coach_allocated):
        """Try to upgrade a student's session. Returns (new_allocated, upgrade_desc) or None."""
        freq = student['frequency']
        duration = student['session_duration']
        current_weekly = student['weekly_minutes']

        # Upgrade priority: frequency first, then duration
        upgrade_options = []

        # Frequency upgrades
        if freq == 'monthly':
            # monthly (0.25/wk) -> biweekly (0.5/wk)
            new_weekly = duration * 0.5
            extra_needed = new_weekly - current_weekly
            if coach_allocated + extra_needed <= coach_available:
                upgrade_options.append({
                    'frequency': 'biweekly',
                    'sessions_per_week': 0.5,
                    'session_duration': duration,
                    'weekly_minutes': new_weekly,
                    'extra': extra_needed,
                    'desc': f"Monthly → Biweekly (+{extra_needed:.0f} min/wk)"
                })

        if freq == 'biweekly':
            # biweekly (0.5/wk) -> weekly (1/wk)
            new_weekly = duration * 1
            extra_needed = new_weekly - current_weekly
            if coach_allocated + extra_needed <= coach_available:
                upgrade_options.append({
                    'frequency': 'weekly',
                    'sessions_per_week': 1,
                    'session_duration': duration,
                    'weekly_minutes': new_weekly,
                    'extra': extra_needed,
                    'desc': f"Biweekly → Weekly (+{extra_needed:.0f} min/wk)"
                })

        # Duration upgrades (only for weekly sessions)
        if freq == 'weekly':
            if duration == 20:
                new_weekly = 30 * 1
                extra_needed = new_weekly - current_weekly
                if coach_allocated + extra_needed <= coach_available:
                    upgrade_options.append({
                        'frequency': 'weekly',
                        'sessions_per_week': 1,
                        'session_duration': 30,
                        'weekly_minutes': new_weekly,
                        'extra': extra_needed,
                        'desc': f"20min → 30min sessions (+{extra_needed:.0f} min/wk)"
                    })
            elif duration == 30:
                new_weekly = 45 * 1
                extra_needed = new_weekly - current_weekly
                if coach_allocated + extra_needed <= coach_available:
                    upgrade_options.append({
                        'frequency': 'weekly',
                        'sessions_per_week': 1,
                        'session_duration': 45,
                        'weekly_minutes': new_weekly,
                        'extra': extra_needed,
                        'desc': f"30min → 45min sessions (+{extra_needed:.0f} min/wk)"
                    })
            elif duration == 45:
                new_weekly = 60 * 1
                extra_needed = new_weekly - current_weekly
                if coach_allocated + extra_needed <= coach_available:
                    upgrade_options.append({
                        'frequency': 'weekly',
                        'sessions_per_week': 1,
                        'session_duration': 60,
                        'weekly_minutes': new_weekly,
                        'extra': extra_needed,
                        'desc': f"45min → 60min sessions (+{extra_needed:.0f} min/wk)"
                    })

        # Return the best upgrade (frequency upgrades prioritized by being first)
        if upgrade_options:
            return upgrade_options[0]
        return None

    # Keep upgrading until we can't anymore
    # Prioritize by need_score (highest first) - already sorted
    made_upgrade = True
    while made_upgrade:
        made_upgrade = False

        # Try Adam's students first
        adam_students = [s for s in allocated if s['assigned_coach'] == 'Adam']
        adam_students.sort(key=lambda x: x['need_score'], reverse=True)

        for student in adam_students:
            upgrade = try_upgrade_student(student, adam_available_min, adam_allocated_min)
            if upgrade:
                # Apply upgrade
                student['frequency'] = upgrade['frequency']
                student['session_duration'] = upgrade['session_duration']
                student['weekly_minutes'] = upgrade['weekly_minutes']
                student['upgraded'] = True
                student['upgrade_desc'] = upgrade['desc']
                adam_allocated_min += upgrade['extra']
                upgrades_made.append({
                    'student': student['student'],
                    'coach': 'Adam',
                    'upgrade': upgrade['desc']
                })
                made_upgrade = True
                break  # Re-sort and try again

        if made_upgrade:
            continue

        # Try External's students
        external_students = [s for s in allocated if s['assigned_coach'] == 'External']
        external_students.sort(key=lambda x: x['need_score'], reverse=True)

        for student in external_students:
            upgrade = try_upgrade_student(student, external_available_min, external_allocated_min)
            if upgrade:
                # Apply upgrade
                student['frequency'] = upgrade['frequency']
                student['session_duration'] = upgrade['session_duration']
                student['weekly_minutes'] = upgrade['weekly_minutes']
                student['upgraded'] = True
                student['upgrade_desc'] = upgrade['desc']
                external_allocated_min += upgrade['extra']
                upgrades_made.append({
                    'student': student['student'],
                    'coach': 'External',
                    'upgrade': upgrade['desc']
                })
                made_upgrade = True
                break

    # Calculate final utilization
    adam_utilization = (adam_allocated_min / adam_available_min * 100) if adam_available_min > 0 else 0
    external_utilization = (external_allocated_min / external_available_min * 100) if external_available_min > 0 else 0
    total_needed = adam_allocated_min + external_allocated_min
    total_available = adam_available_min + external_available_min
    total_utilization = (total_needed / total_available * 100) if total_available > 0 else 0

    # Calculate minimum hours needed (baseline before upgrades)
    baseline_total = baseline_adam_min + baseline_external_min
    minimum_hours_needed = round(baseline_total / 60, 1)

    # Calculate shortage
    hours_short = max(0, minimum_hours_needed - (adam_hours + external_hours))
    adam_hours_short = max(0, round((baseline_adam_min - adam_available_min) / 60, 1))
    external_hours_short = max(0, round((baseline_external_min - external_available_min) / 60, 1))

    # Count by priority
    priority_counts = {
        'Critical': len([a for a in allocated if a['need_priority'] == 'Critical']),
        'High': len([a for a in allocated if a['need_priority'] == 'High']),
        'Medium': len([a for a in allocated if a['need_priority'] == 'Medium']),
        'Low': len([a for a in allocated if a['need_priority'] == 'Low'])
    }

    # Count by coach
    coach_counts = {
        'Adam': len([a for a in allocated if a['assigned_coach'] == 'Adam']),
        'External': len([a for a in allocated if a['assigned_coach'] == 'External'])
    }

    # Identify common skill gaps
    common_gaps = []
    frq_weak_count = sum(1 for s in student_needs if s.get('frq_weak') or (s.get('tb_frq_accuracy') and s.get('tb_frq_accuracy', 100) < 70))
    if frq_weak_count >= 3:
        common_gaps.append({'gap': 'FRQ Skills', 'count': frq_weak_count, 'suggestion': 'Adam could run group FRQ workshop'})

    content_gap_count = sum(1 for s in student_needs if len(s.get('weak_units', [])) > 0)
    if content_gap_count >= 3:
        common_gaps.append({'gap': 'Content Gaps', 'count': content_gap_count, 'suggestion': 'External coach focus area'})

    # Check capacity status
    under_capacity = hours_short > 0
    need_more_external = external_hours_short > 0
    need_more_adam = adam_hours_short > 0

    return {
        'students': allocated,
        'summary': {
            'total_students': len(allocated),
            'priority_counts': priority_counts,
            'coach_counts': coach_counts,
            'common_gaps': common_gaps,
            # Adam stats
            'adam_hours': adam_hours,
            'adam_allocated': round(adam_allocated_min / 60, 1),
            'adam_utilization': round(adam_utilization, 0),
            'adam_hours_short': adam_hours_short,
            # External stats
            'external_hours': external_hours,
            'external_allocated': round(external_allocated_min / 60, 1),
            'external_utilization': round(external_utilization, 0),
            'external_hours_short': external_hours_short,
            # Combined stats
            'total_hours': adam_hours + external_hours,
            'total_allocated': round(total_needed / 60, 1),
            'total_utilization': round(total_utilization, 0),
            # Capacity info
            'minimum_hours_needed': minimum_hours_needed,
            'hours_short': hours_short,
            'under_capacity': under_capacity,
            # Upgrades made
            'upgrades_made': upgrades_made,
            'upgrade_count': len(upgrades_made),
            # Alerts
            'need_more_external': need_more_external,
            'need_more_adam': need_more_adam
        }
    }


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
    Send recommendation message to all channels:
    1. Slack DM to student
    2. Slack DM to coach (record keeping)
    3. Email to student
    4. Slack DM to guide
    5. Email to guide
    """
    results = {
        'slack_student': {'success': False, 'message': 'Not attempted'},
        'slack_coach': {'success': False, 'message': 'Not attempted'},
        'email_student': {'success': False, 'message': 'Not attempted'},
        'slack_guide': {'success': False, 'message': 'Not attempted'},
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

    # 4. Slack to guide
    if guide_email:
        guide_name = guide_info.get('name', 'Guide')
        guide_msg = f"[FYI] Coaching message sent to {student_name}:\n\n{message_text}"
        success, msg = send_slack_dm(guide_email, guide_msg)
        results['slack_guide'] = {'success': success, 'message': msg}
    else:
        results['slack_guide'] = {'success': False, 'message': 'No guide assigned'}

    # 5. Email to guide
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

    # 6. Self-assessment survey data
    surveys = load_self_assessment_data()

    return {
        'tracker': tracker_students,
        'practice': practice_recent,
        'aw_mastery': aw_summary,
        'aw_mastery_raw': aw_mastery_raw,
        'aw_activity': aw_activity,
        'aw_daily_raw': aw_daily_raw,
        'timeback': timeback_data,
        'lesson_details': lesson_details,
        'surveys': surveys
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
    surveys = data.get('surveys', {})

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

        # Get self-assessment survey data
        survey = get_student_survey(surveys, student_name, course)

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
            # Self-assessment survey data
            'survey': survey,
            'confidence': survey.get('confidence') if survey else None,
            'predicted_score': survey.get('predicted_score') if survey else None,
            'target_score': survey.get('target_score') if survey else None,
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

        # Get coaching notes and patterns
        rows[-1]['coaching_notes'] = get_student_notes(student_name, course, limit=3)
        rows[-1]['coaching_patterns'] = get_coaching_patterns(student_name, course)

        # Generate unified coaching insights (triangulating survey + quantitative data + coaching notes)
        rows[-1]['coaching_insights'] = generate_coaching_insights(rows[-1])

        # Add student daily logs data
        student_logs = get_student_logs(student_name, course, limit=5)
        rows[-1]['daily_logs'] = student_logs
        if student_logs:
            latest_log = student_logs[0]
            rows[-1]['last_log_date'] = latest_log.get('date')
            rows[-1]['last_log_flagged'] = latest_log.get('flagged', False)
            rows[-1]['last_log_prediction'] = latest_log.get('prediction')
            rows[-1]['log_action_item'] = latest_log.get('action_item', '')
            # Check for engagement - no log in last 3 days = potentially disengaged
            try:
                last_log_dt = datetime.strptime(latest_log.get('date', ''), '%Y-%m-%d').date()
                days_since_log = (datetime.now().date() - last_log_dt).days
                rows[-1]['days_since_log'] = days_since_log
                rows[-1]['log_stale'] = days_since_log > 3
            except:
                rows[-1]['days_since_log'] = None
                rows[-1]['log_stale'] = False
        else:
            rows[-1]['last_log_date'] = None
            rows[-1]['last_log_flagged'] = False
            rows[-1]['last_log_prediction'] = None
            rows[-1]['log_action_item'] = ''
            rows[-1]['days_since_log'] = None
            rows[-1]['log_stale'] = None  # No logs yet, can't determine

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

        .log-flag {
            background: #ef4444;
            color: #fff;
            padding: 1px 4px;
            border-radius: 3px;
            font-size: 9px;
            font-weight: bold;
        }
        .log-stale {
            color: #ff8844;
        }
        .log-fresh {
            color: #44ff44;
        }
        .log-none {
            color: #666;
        }
    </style>
</head>
<body>
    <h1>AP Social Studies Dashboard</h1>
    <p class="subtitle">APHG / AP World / APUSH / AP Gov — {{ students|length }} students — Exam: May 2026</p>

    <nav class="nav">
        <a href="/">Dashboard</a>
        <a href="/planner">Planner</a>
        <a href="/external-scheduler">External</a>
        <a href="/coaching">Coaching</a>
        <a href="/comms">Communications</a>
        <a href="/refresh">Refresh Data</a>
        <a href="/student-logs">Student Logs</a>
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
                <th data-sort="confidence">Confidence</th>
                <th data-sort="timeback_progress">Timeback</th>
                <th data-sort="aw_mastery">Austin Way</th>
                <th data-sort="combined_progress">Combined</th>
                <th data-sort="progress_vs_last_week">vs Last Wk</th>
                <th data-sort="xp_to_90">XP to 90%</th>
                <th data-sort="projected_90">Proj 90%</th>
                <th data-sort="locked_recommendation" class="locked-col">Rec (Locked)</th>
                <th data-sort="recommendation">Rec (Live)</th>
                <th data-sort="daily_xp">XP/SchoolDay</th>
                <th data-sort="last_log_date">Last Log</th>
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
                <td class="metric {% if s.confidence and s.confidence >= 67 %}metric-good{% elif s.confidence and s.confidence >= 33 %}metric-ok{% elif s.confidence is not none %}metric-bad{% else %}metric-null{% endif %}">
                    {% if s.confidence is not none %}{{ s.confidence }}%{% else %}<span class="metric-null">—</span>{% endif %}
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
                <td class="{% if s.last_log_flagged %}log-flag{% elif s.log_stale %}log-stale{% elif s.last_log_date %}log-fresh{% else %}log-none{% endif %}">
                    {% if s.last_log_date %}
                    {{ s.last_log_date }}{% if s.last_log_flagged %} <span class="log-flag">FLAG</span>{% endif %}{% if s.log_stale %} ({{ s.days_since_log }}d ago){% endif %}
                    {% else %}
                    <span class="log-none">No logs</span>
                    {% endif %}
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
            const columns = ['student', 'course', 'risk', 'pt_score', 'confidence', 'timeback_progress', 'aw_mastery', 'combined_progress', 'progress_vs_last_week', 'xp_to_90', 'projected_90', 'locked_recommendation', 'recommendation', 'daily_xp'];
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
        <a href="/planner">Planner</a>
        <a href="/external-scheduler">External</a>
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

    {# Unified Coaching Insights Section - Triangulates Survey + Quantitative Data #}
    {% if student.coaching_insights %}
    {% set ci = student.coaching_insights %}
    <div class="chart-container" style="border-left: 4px solid #4da6ff;">
        <div class="chart-title" style="color: #4da6ff;">
            Coaching Insights {% if student.survey %}(Data + Self-Assessment){% else %}(Data Only){% endif %}
        </div>

        {# Priority Actions - Most Important #}
        {% if ci.priority_actions %}
        <div style="margin: 15px 0;">
            <p style="color: #fff; font-weight: bold; margin-bottom: 10px;">Priority Actions:</p>
            {% for action in ci.priority_actions %}
            <div style="background: #1a1a2e; padding: 12px; border-radius: 6px; margin-bottom: 8px; border-left: 3px solid {% if action.type == 'Blind Spot' %}#ff4444{% elif action.type == 'Confirmed Gap' %}#ff8844{% elif action.type == 'Mindset' %}#aa88ff{% else %}#44ff44{% endif %};">
                <span style="color: {% if action.type == 'Blind Spot' %}#ff4444{% elif action.type == 'Confirmed Gap' %}#ff8844{% elif action.type == 'Mindset' %}#aa88ff{% else %}#44ff44{% endif %}; font-weight: bold;">{{ action.type }}</span>
                <span style="color: #ccc; margin-left: 10px;">{{ action.action }}</span>
                <div style="color: #666; font-size: 11px; margin-top: 4px;">{{ action.source }}</div>
            </div>
            {% endfor %}
        </div>
        {% endif %}

        {# Blind Spots - High Priority #}
        {% if ci.blind_spots %}
        <div style="margin-top: 20px;">
            <p style="color: #ff4444; font-weight: bold; margin-bottom: 10px;">
                Blind Spots (Confident but Data Shows Weakness)
            </p>
            <div style="display: grid; gap: 10px;">
                {% for bs in ci.blind_spots %}
                <div style="background: #2d1a1a; padding: 12px; border-radius: 6px; border: 1px solid #ff4444;">
                    <div style="display: flex; justify-content: space-between; align-items: center;">
                        <span style="color: #ff4444; font-weight: bold;">{{ bs.area }}</span>
                        <span style="color: #888; font-size: 12px;">{{ bs.detail[:30] }}{% if bs.detail|length > 30 %}...{% endif %}</span>
                    </div>
                    <div style="display: flex; gap: 20px; margin-top: 8px; font-size: 13px;">
                        <span style="color: #ffaa00;">Self: {{ bs.self_rating }}</span>
                        <span style="color: #ff4444;">Data: {{ bs.actual }}</span>
                    </div>
                    <div style="color: #ccc; font-size: 12px; margin-top: 6px;">{{ bs.action }}</div>
                </div>
                {% endfor %}
            </div>
        </div>
        {% endif %}

        {# Confirmed Gaps - Both Sources Agree #}
        {% if ci.confirmed_gaps %}
        <div style="margin-top: 20px;">
            <p style="color: #ff8844; font-weight: bold; margin-bottom: 10px;">
                Confirmed Gaps (Both Student and Data Agree)
            </p>
            <div style="display: grid; gap: 10px;">
                {% for cg in ci.confirmed_gaps %}
                <div style="background: #2d2a1a; padding: 12px; border-radius: 6px; border: 1px solid #ff8844;">
                    <div style="display: flex; justify-content: space-between; align-items: center;">
                        <span style="color: #ff8844; font-weight: bold;">{{ cg.area }}</span>
                    </div>
                    <div style="display: flex; gap: 20px; margin-top: 8px; font-size: 13px;">
                        <span style="color: #ffaa00;">Self: {{ cg.self_rating }}</span>
                        <span style="color: #ff8844;">Data: {{ cg.actual }}</span>
                    </div>
                    <div style="color: #ccc; font-size: 12px; margin-top: 6px;">{{ cg.action }}</div>
                </div>
                {% endfor %}
            </div>
        </div>
        {% endif %}

        {# False Worries - Reassurance Needed #}
        {% if ci.false_worries %}
        <div style="margin-top: 20px;">
            <p style="color: #44ff44; font-weight: bold; margin-bottom: 10px;">
                False Worries (Worried but Data Shows Strength)
            </p>
            <div style="display: grid; gap: 10px;">
                {% for fw in ci.false_worries %}
                <div style="background: #1a2d1a; padding: 12px; border-radius: 6px; border: 1px solid #44ff44;">
                    <div style="display: flex; justify-content: space-between; align-items: center;">
                        <span style="color: #44ff44; font-weight: bold;">{{ fw.area }}</span>
                    </div>
                    <div style="display: flex; gap: 20px; margin-top: 8px; font-size: 13px;">
                        <span style="color: #ffaa00;">Self: {{ fw.self_rating }}</span>
                        <span style="color: #44ff44;">Data: {{ fw.actual }}</span>
                    </div>
                    <div style="color: #ccc; font-size: 12px; margin-top: 6px;">{{ fw.action }}</div>
                </div>
                {% endfor %}
            </div>
        </div>
        {% endif %}

        {# Data-Only Gaps (No Survey) #}
        {% if ci.data_only_gaps %}
        <div style="margin-top: 20px;">
            <p style="color: #ffaa00; font-weight: bold; margin-bottom: 10px;">
                Data-Identified Gaps (No Self-Assessment Available)
            </p>
            <div style="display: flex; flex-wrap: wrap; gap: 8px;">
                {% for dg in ci.data_only_gaps %}
                <span style="background: #2d2a1a; color: #ffaa00; padding: 6px 12px; border-radius: 4px; font-size: 12px;">
                    {{ dg.area }}: {{ dg.actual }}
                </span>
                {% endfor %}
            </div>
        </div>
        {% endif %}

        {# Insights from Coaching Notes #}
        {% if ci.from_coaching %}
        <div style="margin-top: 20px;">
            <p style="color: #44dddd; font-weight: bold; margin-bottom: 10px;">
                From Coaching Sessions ({{ ci.coaching_sessions }} sessions)
                {% if ci.coaching_trajectory %}
                <span style="margin-left: 10px; font-size: 12px; color: {% if ci.coaching_trajectory == 'improving' %}#44ff44{% elif ci.coaching_trajectory == 'declining' %}#ff4444{% else %}#888{% endif %};">
                    Trajectory: {{ ci.coaching_trajectory }}
                </span>
                {% endif %}
            </p>
            <div style="display: grid; gap: 8px;">
                {% for item in ci.from_coaching %}
                <div style="background: #1a2d2d; padding: 10px; border-radius: 6px; border-left: 3px solid {% if item.type == 'Recurring Concern' %}#ff8844{% elif item.type == 'Action Item' %}#4da6ff{% else %}#44ff44{% endif %};">
                    <span style="color: {% if item.type == 'Recurring Concern' %}#ff8844{% elif item.type == 'Action Item' %}#4da6ff{% else %}#44ff44{% endif %}; font-weight: bold; font-size: 11px;">{{ item.type }}</span>
                    <span style="color: #ccc; margin-left: 8px;">{{ item.detail }}</span>
                </div>
                {% endfor %}
            </div>
        </div>
        {% endif %}
    </div>
    {% endif %}

    {# Self-Assessment Details (if survey exists) #}
    {% if student.survey %}
    <div class="chart-container" style="border-left: 4px solid #888;">
        <div class="chart-title" style="color: #888;">
            Self-Assessment Details
        </div>

        <div style="display: grid; grid-template-columns: repeat(auto-fit, minmax(100px, 1fr)); gap: 10px; margin: 15px 0;">
            <div style="background: #1a1a2e; padding: 10px; border-radius: 6px; text-align: center;">
                <div style="font-size: 20px; font-weight: bold; color: {% if student.confidence >= 67 %}#44ff44{% elif student.confidence >= 33 %}#ffaa00{% else %}#ff4444{% endif %};">
                    {{ student.confidence }}%
                </div>
                <div style="font-size: 10px; color: #888;">Confidence</div>
            </div>
            <div style="background: #1a1a2e; padding: 10px; border-radius: 6px; text-align: center;">
                <div style="font-size: 20px; font-weight: bold; color: {% if student.predicted_score and student.predicted_score >= 4 %}#44ff44{% elif student.predicted_score and student.predicted_score >= 3 %}#ffaa00{% else %}#ff4444{% endif %};">
                    {{ student.predicted_score|default('—') }}
                </div>
                <div style="font-size: 10px; color: #888;">Predicted</div>
            </div>
            <div style="background: #1a1a2e; padding: 10px; border-radius: 6px; text-align: center;">
                <div style="font-size: 20px; font-weight: bold; color: #4da6ff;">
                    {{ student.target_score|default('—') }}
                </div>
                <div style="font-size: 10px; color: #888;">Target</div>
            </div>
            {% if student.survey.unit_avg %}
            <div style="background: #1a1a2e; padding: 10px; border-radius: 6px; text-align: center;">
                <div style="font-size: 20px; font-weight: bold; color: {% if student.survey.unit_avg >= 3 %}#44ff44{% elif student.survey.unit_avg >= 2 %}#ffaa00{% else %}#ff4444{% endif %};">
                    {{ student.survey.unit_avg }}/4
                </div>
                <div style="font-size: 10px; color: #888;">Content</div>
            </div>
            {% endif %}
            {% if student.survey.frq_avg %}
            <div style="background: #1a1a2e; padding: 10px; border-radius: 6px; text-align: center;">
                <div style="font-size: 20px; font-weight: bold; color: {% if student.survey.frq_avg >= 3 %}#44ff44{% elif student.survey.frq_avg >= 2 %}#ffaa00{% else %}#ff4444{% endif %};">
                    {{ student.survey.frq_avg }}/4
                </div>
                <div style="font-size: 10px; color: #888;">FRQ</div>
            </div>
            {% endif %}
        </div>

        {% if student.survey.worry and student.survey.worry not in ['n/a', 'N/A', 'no', 'No', 'None', ''] %}
        <div style="margin-top: 15px; padding: 12px; background: #1a1a2e; border-radius: 8px;">
            <p style="color: #ff8844; font-weight: bold; font-size: 12px;">Biggest worry:</p>
            <p style="color: #ccc; margin-top: 4px; font-style: italic; font-size: 13px;">"{{ student.survey.worry }}"</p>
        </div>
        {% endif %}

        {% if student.survey.help_needed and student.survey.help_needed not in ['n/a', 'N/A', 'no', 'No', 'None', '', 'Not necessarily. I just want help with my FRQ coaching. '] %}
        <div style="margin-top: 10px; padding: 12px; background: #1a1a2e; border-radius: 8px;">
            <p style="color: #4da6ff; font-weight: bold; font-size: 12px;">Help requested:</p>
            <p style="color: #ccc; margin-top: 4px; font-style: italic; font-size: 13px;">"{{ student.survey.help_needed }}"</p>
        </div>
        {% endif %}

        {% if student.survey.other and student.survey.other not in ['n/a', 'N/A', 'no', 'No', 'None', '', 'Nothing other than I just need help with my FRQs. '] %}
        <div style="margin-top: 10px; padding: 12px; background: #1a1a2e; border-radius: 8px;">
            <p style="color: #888; font-weight: bold; font-size: 12px;">Additional notes:</p>
            <p style="color: #ccc; margin-top: 4px; font-style: italic; font-size: 13px;">"{{ student.survey.other }}"</p>
        </div>
        {% endif %}

        <p style="color: #555; font-size: 10px; margin-top: 10px;">Survey: {{ student.survey.timestamp }}</p>
    </div>
    {% endif %}

    {# Coaching Notes Section #}
    <div class="chart-container" style="border-left: 4px solid #44dddd;">
        <div class="chart-title" style="color: #44dddd;">
            Coaching Notes
            {% if student.coaching_patterns %}
            <span style="font-size: 12px; font-weight: normal; color: #888; margin-left: 10px;">
                {{ student.coaching_patterns.total_sessions }} session(s)
            </span>
            {% endif %}
        </div>

        {# Add New Note Form #}
        <div style="margin: 15px 0; padding: 15px; background: #1a1a2e; border-radius: 8px;">
            <p style="color: #888; font-size: 12px; margin-bottom: 10px;">Add coaching call notes:</p>
            <textarea id="new-notes" rows="4" style="width: 100%; padding: 10px; background: #252540; border: 1px solid #444; border-radius: 4px; color: #fff; font-family: inherit; font-size: 13px; resize: vertical;" placeholder="Enter notes from your coaching call...&#10;&#10;Example: Worked on FRQ structure today. Student understands the thesis format but struggles to find specific evidence quickly. Assigned 2 practice FRQs for this week. Mood was positive, feeling more confident about MCQs."></textarea>
            <div style="display: flex; gap: 10px; margin-top: 10px; align-items: center;">
                <input type="date" id="call-date" style="padding: 8px; background: #252540; border: 1px solid #444; border-radius: 4px; color: #fff;" value="{{ today }}">
                <button onclick="submitCoachingNote()" style="padding: 8px 16px; background: #44dddd; color: #1a1a2e; border: none; border-radius: 4px; cursor: pointer; font-weight: bold;">Save Note</button>
                <span id="save-status" style="color: #888; font-size: 12px;"></span>
            </div>
        </div>

        {# Recent Notes History #}
        {% if student.coaching_notes %}
        <div style="margin-top: 20px;">
            <p style="color: #888; font-weight: bold; margin-bottom: 10px;">Recent Notes:</p>
            {% for note in student.coaching_notes %}
            <div style="background: #1a1a2e; padding: 15px; border-radius: 8px; margin-bottom: 10px;">
                <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 10px;">
                    <span style="color: #44dddd; font-weight: bold;">{{ note.date }}</span>
                    {% if note.extracted.sentiment %}
                    <span style="padding: 2px 8px; border-radius: 3px; font-size: 11px; background: {% if note.extracted.sentiment == 'strong' %}#1a3d1a{% elif note.extracted.sentiment == 'improving' %}#2d3d1a{% elif note.extracted.sentiment == 'struggling' %}#3d1a1a{% else %}#2d2d2d{% endif %}; color: {% if note.extracted.sentiment == 'strong' %}#44ff44{% elif note.extracted.sentiment == 'improving' %}#88cc44{% elif note.extracted.sentiment == 'struggling' %}#ff4444{% else %}#888{% endif %};">
                        {{ note.extracted.sentiment }}
                    </span>
                    {% endif %}
                </div>

                {% if note.extracted.summary %}
                <p style="color: #ccc; margin-bottom: 10px; font-size: 13px;">{{ note.extracted.summary }}</p>
                {% endif %}

                {% if note.extracted.themes %}
                <div style="margin-bottom: 8px;">
                    <span style="color: #888; font-size: 11px;">Themes:</span>
                    {% for theme in note.extracted.themes %}
                    <span style="background: #252540; color: #4da6ff; padding: 2px 8px; border-radius: 3px; font-size: 11px; margin-left: 5px;">{{ theme }}</span>
                    {% endfor %}
                </div>
                {% endif %}

                {% if note.extracted.action_items %}
                <div style="margin-bottom: 8px;">
                    <span style="color: #888; font-size: 11px;">Actions:</span>
                    {% for action in note.extracted.action_items %}
                    <div style="color: #ffaa00; font-size: 12px; margin-left: 10px;">• {{ action }}</div>
                    {% endfor %}
                </div>
                {% endif %}

                {% if note.extracted.concerns %}
                <div style="margin-bottom: 8px;">
                    <span style="color: #888; font-size: 11px;">Concerns:</span>
                    {% for concern in note.extracted.concerns %}
                    <div style="color: #ff8844; font-size: 12px; margin-left: 10px;">• {{ concern }}</div>
                    {% endfor %}
                </div>
                {% endif %}

                {% if note.extracted.strengths %}
                <div style="margin-bottom: 8px;">
                    <span style="color: #888; font-size: 11px;">Strengths:</span>
                    {% for strength in note.extracted.strengths %}
                    <div style="color: #44ff44; font-size: 12px; margin-left: 10px;">• {{ strength }}</div>
                    {% endfor %}
                </div>
                {% endif %}

                <details style="margin-top: 10px;">
                    <summary style="color: #666; font-size: 11px; cursor: pointer;">View raw notes</summary>
                    <p style="color: #888; font-size: 12px; margin-top: 8px; white-space: pre-wrap; background: #0d0d1a; padding: 10px; border-radius: 4px;">{{ note.raw_notes }}</p>
                </details>
            </div>
            {% endfor %}
        </div>
        {% else %}
        <p style="color: #666; font-size: 13px; margin-top: 15px;">No coaching notes yet. Add notes after your coaching calls to track progress and patterns.</p>
        {% endif %}
    </div>

    {# Student Daily Logs Section #}
    <div class="chart-container" style="border-left: 4px solid #ff8844;">
        <div class="chart-title" style="color: #ff8844;">
            Student Daily Logs
            {% if student.daily_logs %}
            <span style="font-size: 12px; font-weight: normal; color: #888; margin-left: 10px;">
                {{ student.daily_logs|length }} recent log(s)
            </span>
            {% endif %}
            {% if student.last_log_flagged %}
            <span style="background: #ef4444; color: #fff; padding: 2px 8px; border-radius: 4px; font-size: 11px; font-weight: bold; margin-left: 10px;">FLAGGED</span>
            {% endif %}
            {% if student.log_stale %}
            <span style="color: #ff8844; font-size: 12px; margin-left: 10px;">No log in {{ student.days_since_log }} days</span>
            {% endif %}
        </div>

        {% if student.daily_logs %}
        <div style="margin-top: 15px;">
            {% for log in student.daily_logs %}
            <div style="background: {% if log.flagged %}#3a2020{% elif log.priority %}#3a3020{% else %}#1a1a2e{% endif %}; padding: 15px; border-radius: 8px; margin-bottom: 10px;">
                <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 10px;">
                    <span style="color: #ff8844; font-weight: bold;">{{ log.date }}</span>
                    <div>
                        {% if log.flagged %}<span style="background: #ef4444; color: #fff; padding: 2px 6px; border-radius: 3px; font-size: 10px; margin-right: 5px;">FLAG</span>{% endif %}
                        {% if log.priority %}<span style="background: #f97316; color: #fff; padding: 2px 6px; border-radius: 3px; font-size: 10px;">PRIORITY</span>{% endif %}
                        {% if log.prediction %}<span style="background: {% if log.prediction >= 4 %}#22c55e{% elif log.prediction == 3 %}#eab308{% else %}#ef4444{% endif %}; color: {% if log.prediction >= 3 %}#000{% else %}#fff{% endif %}; padding: 2px 8px; border-radius: 4px; font-size: 11px; font-weight: bold; margin-left: 5px;">Pred: {{ log.prediction }}</span>{% endif %}
                    </div>
                </div>

                <p style="color: #ccc; font-size: 13px; margin-bottom: 8px;">{{ log.todays_work }}</p>

                {% if log.gaps %}
                <p style="color: #888; font-size: 12px; margin-bottom: 5px;"><strong style="color: #ffaa00;">Gaps:</strong> {{ log.gaps }}</p>
                {% endif %}

                {% if log.tomorrows_focus %}
                <p style="color: #888; font-size: 12px; margin-bottom: 5px;"><strong style="color: #44dddd;">Tomorrow:</strong> {{ log.tomorrows_focus }}</p>
                {% endif %}

                {% if log.action_item %}
                <p style="color: #44dddd; font-size: 12px; margin-top: 8px;"><strong>Action:</strong> {{ log.action_item }}</p>
                {% endif %}
            </div>
            {% endfor %}
        </div>
        {% else %}
        <p style="color: #666; font-size: 13px; margin-top: 15px;">No daily logs for this student yet. Logs are ingested from the <a href="/student-logs" style="color: #4da6ff;">Student Logs</a> page.</p>
        {% endif %}
    </div>

    <script>
    function submitCoachingNote() {
        const notes = document.getElementById('new-notes').value.trim();
        const date = document.getElementById('call-date').value;
        const status = document.getElementById('save-status');

        if (!notes) {
            status.textContent = 'Please enter some notes';
            status.style.color = '#ff4444';
            return;
        }

        status.textContent = 'Saving...';
        status.style.color = '#888';

        fetch('/api/coaching-notes/{{ student.student|urlencode }}/{{ student.course }}', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({notes: notes, date: date})
        })
        .then(r => r.json())
        .then(data => {
            if (data.success) {
                status.textContent = 'Saved! Refreshing...';
                status.style.color = '#44ff44';
                setTimeout(() => location.reload(), 1000);
            } else {
                status.textContent = data.error || 'Error saving';
                status.style.color = '#ff4444';
            }
        })
        .catch(err => {
            status.textContent = 'Error: ' + err;
            status.style.color = '#ff4444';
        });
    }
    </script>
</body>
</html>
'''

PLANNER_HTML = '''
<!DOCTYPE html>
<html>
<head>
    <title>Coaching Planner</title>
    <style>
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
            background: #1a1a2e;
            color: #eee;
            padding: 20px;
        }
        h1 { margin-bottom: 5px; color: #fff; }
        h2 { margin: 20px 0 10px; color: #fff; font-size: 18px; }
        .subtitle { color: #888; margin-bottom: 20px; }
        .nav { margin-bottom: 20px; }
        .nav a { color: #4da6ff; margin-right: 20px; text-decoration: none; }
        .nav a:hover { text-decoration: underline; }

        .summary-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
            gap: 15px;
            margin-bottom: 30px;
        }
        .summary-card {
            background: #252540;
            padding: 15px;
            border-radius: 8px;
            text-align: center;
        }
        .summary-value { font-size: 28px; font-weight: bold; }
        .summary-label { color: #888; font-size: 12px; margin-top: 5px; }

        .alert-box {
            padding: 15px;
            border-radius: 8px;
            margin-bottom: 20px;
        }
        .alert-warning { background: #3d2a1a; border-left: 4px solid #ff8844; }
        .alert-success { background: #1a3d1a; border-left: 4px solid #44ff44; }
        .alert-info { background: #1a2a3d; border-left: 4px solid #4da6ff; }

        .settings-form {
            background: #252540;
            padding: 15px;
            border-radius: 8px;
            margin-bottom: 20px;
            display: flex;
            align-items: center;
            gap: 15px;
        }
        .settings-form input {
            padding: 8px;
            background: #1a1a2e;
            border: 1px solid #444;
            border-radius: 4px;
            color: #fff;
            width: 80px;
        }
        .settings-form button {
            padding: 8px 16px;
            background: #4da6ff;
            color: #fff;
            border: none;
            border-radius: 4px;
            cursor: pointer;
        }

        .student-card {
            background: #252540;
            padding: 15px;
            border-radius: 8px;
            margin-bottom: 15px;
            border-left: 4px solid #444;
        }
        .student-card.priority-Critical { border-left-color: #ff4444; }
        .student-card.priority-High { border-left-color: #ff8844; }
        .student-card.priority-Medium { border-left-color: #ffaa00; }
        .student-card.priority-Low { border-left-color: #44ff44; }

        .student-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 10px;
        }
        .student-name {
            font-weight: bold;
            font-size: 16px;
        }
        .student-name a { color: #fff; text-decoration: none; }
        .student-name a:hover { color: #4da6ff; }

        .badge {
            padding: 3px 8px;
            border-radius: 4px;
            font-size: 11px;
            font-weight: bold;
        }
        .badge-critical { background: #ff4444; color: #fff; }
        .badge-high { background: #ff8844; color: #fff; }
        .badge-medium { background: #ffaa00; color: #1a1a2e; }
        .badge-low { background: #44ff44; color: #1a1a2e; }

        .student-meta {
            display: flex;
            gap: 15px;
            font-size: 13px;
            color: #888;
            margin-bottom: 10px;
        }

        .factors {
            display: flex;
            flex-wrap: wrap;
            gap: 6px;
            margin-bottom: 10px;
        }
        .factor {
            background: #1a1a2e;
            padding: 3px 8px;
            border-radius: 3px;
            font-size: 11px;
            color: #ff8844;
        }

        .allocation {
            background: #1a1a2e;
            padding: 10px;
            border-radius: 6px;
            margin-top: 10px;
        }
        .allocation-header {
            display: flex;
            justify-content: space-between;
            margin-bottom: 8px;
        }

        .agenda {
            margin-top: 10px;
        }
        .agenda-section {
            padding: 8px;
            margin: 5px 0;
            background: #1a1a2e;
            border-radius: 4px;
            border-left: 3px solid #4da6ff;
        }
        .agenda-title {
            font-weight: bold;
            font-size: 12px;
            color: #4da6ff;
            margin-bottom: 5px;
        }
        .agenda-item {
            font-size: 12px;
            color: #ccc;
            margin-left: 10px;
        }

        details summary {
            cursor: pointer;
            color: #4da6ff;
            font-size: 13px;
        }
        details summary:hover {
            text-decoration: underline;
        }

        .priority-filter {
            margin-bottom: 15px;
        }
        .priority-filter button {
            padding: 6px 12px;
            margin-right: 8px;
            background: #252540;
            border: 1px solid #444;
            border-radius: 4px;
            color: #888;
            cursor: pointer;
        }
        .priority-filter button.active {
            background: #4da6ff;
            color: #fff;
            border-color: #4da6ff;
        }

        .coach-filter {
            margin-bottom: 15px;
        }
        .coach-filter button {
            padding: 6px 12px;
            margin-right: 8px;
            background: #252540;
            border: 1px solid #444;
            border-radius: 4px;
            color: #888;
            cursor: pointer;
        }
        .coach-filter button.active {
            color: #fff;
            border-color: #4da6ff;
        }
        .coach-filter button.active-adam {
            background: #1a3d5c;
            border-color: #4da6ff;
        }
        .coach-filter button.active-external {
            background: #1a4d4d;
            border-color: #44dddd;
        }

        .coach-badge {
            padding: 3px 8px;
            border-radius: 4px;
            font-size: 11px;
            font-weight: bold;
            margin-left: 8px;
        }
        .coach-adam {
            background: #1a3d5c;
            color: #4da6ff;
            border: 1px solid #4da6ff;
        }
        .coach-external {
            background: #1a4d4d;
            color: #44dddd;
            border: 1px solid #44dddd;
        }
        .coach-focus {
            font-size: 11px;
            color: #888;
            font-weight: normal;
        }
    </style>
</head>
<body>
    <nav class="nav">
        <a href="/">Dashboard</a>
        <a href="/planner" style="font-weight: bold;">Planner</a>
        <a href="/coaching">Coaching</a>
        <a href="/comms">Communications</a>
        <a href="/settings">Settings</a>
    </nav>

    <h1>Coaching Planner</h1>
    <p class="subtitle">Prioritize students and allocate coaching time by skill</p>

    {# Settings - Two Coaches #}
    <form class="settings-form" action="/planner/settings" method="POST" style="flex-wrap: wrap;">
        <div style="display: flex; align-items: center; gap: 10px;">
            <span style="color: #4da6ff; font-weight: bold;">Adam</span>
            <span style="color: #888; font-size: 12px;">(FRQ/Essay)</span>
            <input type="number" name="adam_hours" value="{{ adam_hours }}" step="0.5" min="0" max="20" style="width: 60px;">
            <span style="color: #888;">h/wk</span>
        </div>
        <div style="display: flex; align-items: center; gap: 10px;">
            <span style="color: #44dddd; font-weight: bold;">External</span>
            <span style="color: #888; font-size: 12px;">(Content Specialist)</span>
            <input type="number" name="external_hours" value="{{ external_hours }}" step="0.5" min="0" max="20" style="width: 60px;">
            <span style="color: #888;">h/wk</span>
        </div>
        <button type="submit">Update</button>
    </form>

    {# Summary Cards - By Coach #}
    <div class="summary-grid">
        <div class="summary-card">
            <div class="summary-value">{{ plan.summary.total_students }}</div>
            <div class="summary-label">Total Students</div>
        </div>
        <div class="summary-card">
            <div class="summary-value" style="color: #ff4444;">{{ plan.summary.priority_counts.Critical }}</div>
            <div class="summary-label">Critical</div>
        </div>
        <div class="summary-card">
            <div class="summary-value" style="color: #ff8844;">{{ plan.summary.priority_counts.High }}</div>
            <div class="summary-label">High</div>
        </div>
        <div class="summary-card" style="border-left: 3px solid #4da6ff;">
            <div class="summary-value" style="color: #4da6ff;">{{ plan.summary.coach_counts.Adam }}</div>
            <div class="summary-label">Adam's Students</div>
            <div style="font-size: 11px; color: #888; margin-top: 5px;">{{ plan.summary.adam_allocated }}h / {{ plan.summary.adam_hours }}h</div>
            <div style="font-size: 11px; color: {% if plan.summary.adam_utilization > 100 %}#ff4444{% elif plan.summary.adam_utilization > 80 %}#ffaa00{% else %}#44ff44{% endif %};">{{ plan.summary.adam_utilization|int }}% util</div>
        </div>
        <div class="summary-card" style="border-left: 3px solid #44dddd;">
            <div class="summary-value" style="color: #44dddd;">{{ plan.summary.coach_counts.External }}</div>
            <div class="summary-label">External's Students</div>
            <div style="font-size: 11px; color: #888; margin-top: 5px;">{{ plan.summary.external_allocated }}h / {{ plan.summary.external_hours }}h</div>
            <div style="font-size: 11px; color: {% if plan.summary.external_utilization > 100 %}#ff4444{% elif plan.summary.external_utilization > 80 %}#ffaa00{% else %}#44ff44{% endif %};">{{ plan.summary.external_utilization|int }}% util</div>
        </div>
        <div class="summary-card">
            <div class="summary-value" style="color: {% if plan.summary.total_utilization > 100 %}#ff4444{% elif plan.summary.total_utilization > 80 %}#ffaa00{% else %}#44ff44{% endif %};">
                {{ plan.summary.total_utilization|int }}%
            </div>
            <div class="summary-label">Total Utilization</div>
            <div style="font-size: 11px; color: #888; margin-top: 5px;">{{ plan.summary.total_allocated }}h / {{ plan.summary.total_hours }}h</div>
        </div>
    </div>

    {# BIG WARNING: Under Capacity #}
    {% if plan.summary.under_capacity %}
    <div style="background: linear-gradient(135deg, #5c1a1a 0%, #3d1a1a 100%); border: 3px solid #ff4444; border-radius: 12px; padding: 25px; margin-bottom: 25px; text-align: center;">
        <div style="font-size: 48px; margin-bottom: 10px;">⚠️</div>
        <div style="font-size: 24px; font-weight: bold; color: #ff6666; margin-bottom: 10px;">INSUFFICIENT COACHING HOURS</div>
        <div style="font-size: 18px; color: #fff; margin-bottom: 15px;">
            Need <strong>{{ plan.summary.minimum_hours_needed }}h/week</strong> minimum, but only <strong>{{ plan.summary.total_hours }}h/week</strong> available
        </div>
        <div style="font-size: 16px; color: #ffaaaa;">
            Short by <strong>{{ "%.1f"|format(plan.summary.hours_short) }}h/week</strong>
            {% if plan.summary.adam_hours_short > 0 and plan.summary.external_hours_short > 0 %}
            (Adam: +{{ "%.1f"|format(plan.summary.adam_hours_short) }}h, External: +{{ "%.1f"|format(plan.summary.external_hours_short) }}h)
            {% elif plan.summary.adam_hours_short > 0 %}
            (Adam needs +{{ "%.1f"|format(plan.summary.adam_hours_short) }}h)
            {% elif plan.summary.external_hours_short > 0 %}
            (External needs +{{ "%.1f"|format(plan.summary.external_hours_short) }}h)
            {% endif %}
        </div>
        <div style="margin-top: 15px; font-size: 13px; color: #ccc;">
            Students are getting less coaching than their priority level requires. Consider adding hours or group sessions.
        </div>
    </div>
    {% endif %}

    {# Upgrades Made (surplus hours allocated) #}
    {% if plan.summary.upgrade_count > 0 %}
    <div class="alert-box alert-success" style="border-left-width: 4px;">
        <strong style="color: #44ff44;">✨ Surplus Hours Allocated</strong><br>
        <span style="color: #ccc;">{{ plan.summary.upgrade_count }} student(s) upgraded to improve outcomes:</span>
        <div style="margin-top: 10px; display: flex; flex-wrap: wrap; gap: 8px;">
            {% for upgrade in plan.summary.upgrades_made[:10] %}
            <span style="background: #1a3d1a; padding: 4px 10px; border-radius: 4px; font-size: 12px;">
                <strong>{{ upgrade.student }}</strong>: {{ upgrade.upgrade }}
            </span>
            {% endfor %}
            {% if plan.summary.upgrade_count > 10 %}
            <span style="color: #888; font-size: 12px;">...and {{ plan.summary.upgrade_count - 10 }} more</span>
            {% endif %}
        </div>
    </div>
    {% endif %}

    {# Capacity Status (when not critically under) #}
    {% if not plan.summary.under_capacity %}
        {% if plan.summary.total_utilization > 90 %}
        <div class="alert-box alert-info">
            <strong>Near Capacity</strong><br>
            <span style="color: #ccc;">{{ plan.summary.total_utilization|int }}% utilized. All students receiving minimum recommended coaching.</span>
        </div>
        {% elif plan.summary.upgrade_count == 0 %}
        <div class="alert-box alert-success">
            <strong>Capacity OK</strong><br>
            <span style="color: #ccc;">Both coaches have sufficient capacity. All students at minimum levels.</span>
        </div>
        {% endif %}
    {% endif %}

    {# Common Gaps #}
    {% if plan.summary.common_gaps %}
    <div class="alert-box alert-info">
        <strong>Common Skill Gaps Detected</strong><br>
        {% for gap in plan.summary.common_gaps %}
        <span style="color: #ccc;">{{ gap.gap }}: {{ gap.count }} high-need students. {{ gap.suggestion }}</span><br>
        {% endfor %}
    </div>
    {% endif %}

    {# Coach Filter #}
    <div class="coach-filter">
        <strong style="color: #888; margin-right: 10px;">Coach:</strong>
        <button class="active" onclick="filterCoach('all')">All ({{ plan.summary.total_students }})</button>
        <button onclick="filterCoach('Adam')" style="color: #4da6ff;">Adam ({{ plan.summary.coach_counts.Adam }})</button>
        <button onclick="filterCoach('External')" style="color: #44dddd;">External ({{ plan.summary.coach_counts.External }})</button>
    </div>

    {# Priority Filter #}
    <div class="priority-filter">
        <strong style="color: #888; margin-right: 10px;">Priority:</strong>
        <button class="active" onclick="filterPriority('all')">All</button>
        <button onclick="filterPriority('Critical')">Critical ({{ plan.summary.priority_counts.Critical }})</button>
        <button onclick="filterPriority('High')">High ({{ plan.summary.priority_counts.High }})</button>
        <button onclick="filterPriority('Medium')">Medium ({{ plan.summary.priority_counts.Medium }})</button>
        <button onclick="filterPriority('Low')">Low ({{ plan.summary.priority_counts.Low }})</button>
    </div>

    <h2>Student Priorities</h2>

    {# Student Cards #}
    {% for s in plan.students %}
    <div class="student-card priority-{{ s.need_priority }}" data-priority="{{ s.need_priority }}" data-coach="{{ s.assigned_coach }}">
        <div class="student-header">
            <div class="student-name">
                <a href="/student/{{ s.student|urlencode }}/{{ s.course }}">{{ s.student }}</a>
                <span style="color: #888; font-weight: normal; font-size: 13px; margin-left: 8px;">{{ s.course }}</span>
                <span class="coach-badge coach-{{ s.assigned_coach|lower }}">{{ s.assigned_coach }}</span>
                <span class="coach-focus">{{ s.coach_focus }}</span>
            </div>
            <div>
                <span class="badge badge-{{ s.need_priority|lower }}">{{ s.need_priority }}</span>
                <span style="color: #888; font-size: 12px; margin-left: 8px;">Score: {{ s.need_score }}</span>
            </div>
        </div>

        <div class="student-meta">
            <span>Risk: <strong style="color: {% if s.risk == 'Critical' %}#ff4444{% elif s.risk == 'At Risk' %}#ff8844{% else %}#888{% endif %};">{{ s.risk }}</strong></span>
            {% if s.pt_score %}<span>PT: <strong>{{ s.pt_score }}</strong></span>{% endif %}
            {% if s.confidence is not none %}<span>Confidence: <strong>{{ s.confidence }}%</strong></span>{% endif %}
            <span>Sessions: <strong>{{ s.coaching_sessions }}</strong></span>
            <span>Rec: <strong>{{ s.recommendation }}</strong></span>
        </div>

        {% if s.need_factors %}
        <div class="factors">
            {% for factor in s.need_factors %}
            <span class="factor">{{ factor }}</span>
            {% endfor %}
        </div>
        {% endif %}

        <div class="allocation">
            <div class="allocation-header">
                <span style="color: {% if s.assigned_coach == 'Adam' %}#4da6ff{% else %}#44dddd{% endif %}; font-weight: bold;">📅 {{ s.frequency|title }} with {{ s.assigned_coach }}</span>
                <span style="color: #888;">{{ s.session_duration }} min/session ({{ s.weekly_minutes }} min/week)</span>
                {% if s.upgraded %}
                <span style="background: #1a3d1a; color: #44ff44; padding: 2px 8px; border-radius: 4px; font-size: 11px; margin-left: 8px;">✨ UPGRADED</span>
                {% endif %}
            </div>
            <div style="font-size: 12px; color: #888; margin-top: 5px;">
                Focus: {{ s.coach_focus }}
                {% if s.frq_need_score and s.content_need_score %}
                <span style="margin-left: 15px;">FRQ Need: {{ s.frq_need_score }} | Content Need: {{ s.content_need_score }}</span>
                {% endif %}
                {% if s.upgrade_desc %}
                <span style="margin-left: 15px; color: #44ff44;">{{ s.upgrade_desc }}</span>
                {% endif %}
            </div>
        </div>

        <details class="agenda">
            <summary>View Session Agenda</summary>
            <div style="margin-top: 10px;">
                {% for section in s.agenda.sections %}
                <div class="agenda-section">
                    <div class="agenda-title">{{ section.title }} ({{ section.duration }} min)</div>
                    {% for item in section.content %}
                    <div class="agenda-item">• {{ item }}</div>
                    {% endfor %}
                </div>
                {% endfor %}
            </div>
        </details>
    </div>
    {% endfor %}

    <script>
    let currentPriorityFilter = 'all';
    let currentCoachFilter = 'all';

    function applyFilters() {
        const cards = document.querySelectorAll('.student-card');
        cards.forEach(card => {
            const matchesPriority = currentPriorityFilter === 'all' || card.dataset.priority === currentPriorityFilter;
            const matchesCoach = currentCoachFilter === 'all' || card.dataset.coach === currentCoachFilter;
            card.style.display = (matchesPriority && matchesCoach) ? 'block' : 'none';
        });
    }

    function filterPriority(priority) {
        currentPriorityFilter = priority;
        const buttons = document.querySelectorAll('.priority-filter button');
        buttons.forEach(b => b.classList.remove('active'));
        event.target.classList.add('active');
        applyFilters();
    }

    function filterCoach(coach) {
        currentCoachFilter = coach;
        const buttons = document.querySelectorAll('.coach-filter button');
        buttons.forEach(b => {
            b.classList.remove('active', 'active-adam', 'active-external');
        });
        if (coach === 'Adam') {
            event.target.classList.add('active', 'active-adam');
        } else if (coach === 'External') {
            event.target.classList.add('active', 'active-external');
        } else {
            event.target.classList.add('active');
        }
        applyFilters();
    }
    </script>
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
        <a href="/planner">Planner</a>
        <a href="/external-scheduler">External</a>
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
    today = datetime.now().strftime('%Y-%m-%d')

    return render_template_string(STUDENT_HTML, student=student, timeseries=timeseries, today=today)


@app.route('/coaching')
def coaching():
    return render_template_string(COACHING_HTML)


@app.route('/planner')
def coaching_planner():
    """Coaching planner - prioritize students and allocate time."""
    data = load_all_data()
    students = build_unified_table(data)

    # Get available hours from config or default
    config = load_config()
    adam_hours = config.get('adam_hours_per_week', 3)
    external_hours = config.get('external_hours_per_week', 2)

    # Generate the coaching plan
    plan = calculate_coaching_plan(students, adam_hours=adam_hours, external_hours=external_hours)

    return render_template_string(PLANNER_HTML, plan=plan, adam_hours=adam_hours, external_hours=external_hours)


@app.route('/planner/settings', methods=['POST'])
def update_planner_settings():
    """Update coaching planner settings."""
    config = load_config()
    adam_hours = request.form.get('adam_hours', 3)
    external_hours = request.form.get('external_hours', 2)
    try:
        config['adam_hours_per_week'] = float(adam_hours)
        config['external_hours_per_week'] = float(external_hours)
        save_config(config)
    except ValueError:
        pass
    return redirect('/planner')


@app.route('/api/coaching-plan')
def api_coaching_plan():
    """API endpoint for coaching plan data."""
    data = load_all_data()
    students = build_unified_table(data)
    config = load_config()
    adam_hours = config.get('adam_hours_per_week', 3)
    external_hours = config.get('external_hours_per_week', 2)
    plan = calculate_coaching_plan(students, adam_hours=adam_hours, external_hours=external_hours)
    return jsonify(plan)


# =============================================================================
# EXTERNAL COACH SCHEDULER
# =============================================================================

def generate_external_schedule(start_date, num_weeks=2):
    """
    Generate available time slots for external coach.

    Schedule (Central Time / Austin):
    - Tuesdays: 1pm - 3pm
    - Wednesdays: 9am - 12pm, 1pm - 2pm (lunch break 12-1)
    - Thursdays: 1:30pm - 3pm
    - Overflow: Mondays/Fridays as needed
    """
    slots = []
    current = start_date

    # Find the start of the week (Monday)
    days_since_monday = current.weekday()
    week_start = current - timedelta(days=days_since_monday)

    for week in range(num_weeks):
        week_base = week_start + timedelta(weeks=week)

        # Monday (overflow) - 1pm - 3pm
        monday = week_base
        if monday >= start_date:
            for hour in [13, 14]:
                for minute in [0, 20, 40]:
                    slot_time = monday.replace(hour=hour, minute=minute, second=0, microsecond=0)
                    if slot_time >= start_date:
                        slots.append({
                            'datetime': slot_time,
                            'day': 'Monday',
                            'date': slot_time.strftime('%Y-%m-%d'),
                            'time': slot_time.strftime('%H:%M'),
                            'display': slot_time.strftime('%a %b %d %H:%M'),
                            'overflow': True
                        })

        # Tuesday - 1pm to 3pm
        tuesday = week_base + timedelta(days=1)
        if tuesday >= start_date or (tuesday.date() == start_date.date() and start_date.hour < 15):
            for hour in [13, 14]:
                for minute in [0, 20, 40]:
                    slot_time = tuesday.replace(hour=hour, minute=minute, second=0, microsecond=0)
                    if slot_time >= start_date:
                        slots.append({
                            'datetime': slot_time,
                            'day': 'Tuesday',
                            'date': slot_time.strftime('%Y-%m-%d'),
                            'time': slot_time.strftime('%H:%M'),
                            'display': slot_time.strftime('%a %b %d %H:%M'),
                            'overflow': False
                        })

        # Wednesday - 9am to 12pm, then 1pm to 2pm
        wednesday = week_base + timedelta(days=2)
        if wednesday.date() >= start_date.date():
            # Morning block: 9am - 12pm
            for hour in [9, 10, 11]:
                for minute in [0, 20, 40]:
                    slot_time = wednesday.replace(hour=hour, minute=minute, second=0, microsecond=0)
                    slots.append({
                        'datetime': slot_time,
                        'day': 'Wednesday',
                        'date': slot_time.strftime('%Y-%m-%d'),
                        'time': slot_time.strftime('%H:%M'),
                        'display': slot_time.strftime('%a %b %d %H:%M'),
                        'overflow': False
                    })
            # Afternoon block: 1pm - 2pm
            for minute in [0, 20, 40]:
                slot_time = wednesday.replace(hour=13, minute=minute, second=0, microsecond=0)
                slots.append({
                    'datetime': slot_time,
                    'day': 'Wednesday',
                    'date': slot_time.strftime('%Y-%m-%d'),
                    'time': slot_time.strftime('%H:%M'),
                    'display': slot_time.strftime('%a %b %d %H:%M'),
                    'overflow': False
                })

        # Thursday - 1:30pm to 3pm
        thursday = week_base + timedelta(days=3)
        for hour_min in [(13, 30), (13, 50), (14, 10), (14, 30)]:
            slot_time = thursday.replace(hour=hour_min[0], minute=hour_min[1], second=0, microsecond=0)
            if slot_time >= start_date:
                slots.append({
                    'datetime': slot_time,
                    'day': 'Thursday',
                    'date': slot_time.strftime('%Y-%m-%d'),
                    'time': slot_time.strftime('%H:%M'),
                    'display': slot_time.strftime('%a %b %d %H:%M'),
                    'overflow': False
                })

        # Friday (overflow) - 1pm - 3pm
        friday = week_base + timedelta(days=4)
        for hour in [13, 14]:
            for minute in [0, 20, 40]:
                slot_time = friday.replace(hour=hour, minute=minute, second=0, microsecond=0)
                if slot_time >= start_date:
                    slots.append({
                        'datetime': slot_time,
                        'day': 'Friday',
                        'date': slot_time.strftime('%Y-%m-%d'),
                        'time': slot_time.strftime('%H:%M'),
                        'display': slot_time.strftime('%a %b %d %H:%M'),
                        'overflow': True
                    })

    # Sort by datetime and filter out overflow initially
    slots.sort(key=lambda x: x['datetime'])
    return slots


def allocate_students_to_slots(students, slots):
    """
    Allocate students to time slots based on priority.
    Critical students get 3 sessions, At Risk get 2, others get none.
    Returns list of bookings with student + slot info.
    """
    bookings = []

    # Determine sessions per student based on risk
    # Only Critical and At Risk students get sessions in crunch time
    sessions_by_risk = {
        'Critical': 3,
        'At Risk': 2,
        'On Track': 0,  # Skip - they're fine
        'Strong': 0,    # Skip - they're fine
    }

    # Filter to only students who need sessions and expand by session count
    students_needing_sessions = []
    for student in students:
        risk = student.get('risk', 'Unknown')
        num_sessions = sessions_by_risk.get(risk, 0)
        if num_sessions > 0:
            for session_num in range(num_sessions):
                students_needing_sessions.append({
                    'student': student,
                    'session_num': session_num + 1,
                    'total_sessions': num_sessions
                })

    # Sort by priority, then spread sessions for same student across different days
    priority_order = {'Critical': 0, 'At Risk': 1}
    students_needing_sessions.sort(key=lambda x: (
        priority_order.get(x['student'].get('risk', 'Unknown'), 5),
        x['session_num'],  # Spread sessions - all session 1s first, then 2s, etc.
        x['student'].get('course', ''),
        x['student'].get('student', '')
    ))

    # Separate main slots from overflow
    main_slots = [s for s in slots if not s['overflow']]
    overflow_slots = [s for s in slots if s['overflow']]

    slot_idx = 0
    overflow_idx = 0

    for entry in students_needing_sessions:
        student = entry['student']
        session_num = entry['session_num']
        total_sessions = entry['total_sessions']

        # Try main slots first
        if slot_idx < len(main_slots):
            slot = main_slots[slot_idx]
            slot_idx += 1
        elif overflow_idx < len(overflow_slots):
            slot = overflow_slots[overflow_idx]
            overflow_idx += 1
        else:
            # No more slots - mark as unscheduled
            bookings.append({
                'student': student,
                'slot': None,
                'scheduled': False,
                'session_num': session_num,
                'total_sessions': total_sessions
            })
            continue

        bookings.append({
            'student': student,
            'slot': slot,
            'scheduled': True,
            'session_num': session_num,
            'total_sessions': total_sessions
        })

    return bookings


def generate_student_briefing(student):
    """Generate a concise 3-line briefing for external coach."""
    lines = []

    # Line 1: Progress snapshot
    progress = student.get('combined_progress')
    tb_progress = student.get('timeback_progress')
    aw_mastery = student.get('aw_mastery')

    if progress:
        progress_str = f"Course progress: {progress:.0f}%"
        if tb_progress and aw_mastery:
            progress_str += f" (Timeback {tb_progress:.0f}%, Austin Way {aw_mastery:.0f}%)"
        lines.append(progress_str)
    else:
        lines.append("Course progress: No data available - may be new or inactive")

    # Line 2: PT performance and key gap
    pt_mcq = student.get('pt_mcq')
    pt_frq = student.get('pt_frq')
    pt_score = student.get('pt_score')

    if pt_mcq and pt_frq:
        gap = pt_mcq - pt_frq
        if gap > 15:
            lines.append(f"Practice test: {pt_score}/5 (MCQ {pt_mcq}%, FRQ {pt_frq}%) — MAJOR FRQ GAP, knows content but can't write")
        elif gap < -15:
            lines.append(f"Practice test: {pt_score}/5 (MCQ {pt_mcq}%, FRQ {pt_frq}%) — Content gaps, writing is fine")
        else:
            lines.append(f"Practice test: {pt_score}/5 (MCQ {pt_mcq}%, FRQ {pt_frq}%) — Balanced performance")
    elif pt_score:
        lines.append(f"Practice test: {pt_score}/5 (detailed breakdown not available)")
    else:
        lines.append("Practice test: NOT TAKEN — critical blind spot, no exam readiness data")

    # Line 3: Weak areas and why they're at this risk level
    weak_units = student.get('weak_units', [])
    risk = student.get('risk', 'Unknown')
    rec = student.get('recommendation', '')

    risk_reasons = []
    if risk == 'Critical':
        if not pt_score:
            risk_reasons.append("no PT score")
        elif pt_score <= 2:
            risk_reasons.append(f"PT score {pt_score}")
        if progress and progress < 50:
            risk_reasons.append(f"only {progress:.0f}% through course")
        if not risk_reasons:
            risk_reasons.append("multiple red flags")
    elif risk == 'At Risk':
        if pt_score and pt_score <= 3:
            risk_reasons.append(f"PT score {pt_score}")
        if progress and progress < 70:
            risk_reasons.append(f"{progress:.0f}% progress")

    if weak_units:
        # Extract unit names from dicts
        unit_names = [u.get('unit_name', u.get('unit_id', str(u))) if isinstance(u, dict) else str(u) for u in weak_units[:3]]
        weak_str = f"Weak units: {', '.join(unit_names)}"
        if len(weak_units) > 3:
            weak_str += f" (+{len(weak_units)-3} more)"
    else:
        weak_str = "Weak units: None identified"

    if risk_reasons:
        lines.append(f"{weak_str}. {risk.upper()} because: {', '.join(risk_reasons)}")
    else:
        lines.append(weak_str)

    return lines


def generate_session_topic(student, session_num=1, total_sessions=1):
    """Generate a specific recommended topic for this session.

    Different sessions get different focuses:
    - Session 1: Address biggest gap
    - Session 2: Secondary focus area
    - Session 3: Exam strategy & confidence
    """
    pt_mcq = student.get('pt_mcq')
    pt_frq = student.get('pt_frq')
    pt_score = student.get('pt_score')
    weak_units = student.get('weak_units', [])
    rec = student.get('recommendation', '')
    course = student.get('course', '')

    # Helper to get unit name from dict
    def get_unit_name(u):
        return u.get('unit_name', u.get('unit_id', str(u))) if isinstance(u, dict) else str(u)

    # Build a priority list of topics for this student
    topics = []

    # Topic: No PT taken - need exam overview
    if not pt_score:
        topics.append({
            'topic': 'Exam Format & What to Expect',
            'detail': 'Walk through exam structure, timing, question types. Reduce anxiety by demystifying the test.',
            'materials': 'Bring sample questions from each section to demonstrate format'
        })

    # Topic: FRQ gap - writing skills
    if pt_mcq and pt_frq and (pt_mcq - pt_frq) > 15:
        frq_types = {
            'APHG': 'FRQ structure (define-explain-example), using specific geographic examples',
            'APUSH': 'SAQ/LEQ/DBQ structure, thesis writing, using specific historical evidence',
            'APWH': 'SAQ/LEQ/DBQ structure, thesis writing, cross-regional comparisons',
            'APGOV': 'FRQ types (Concept Application, SCOTUS Comparison, Argument Essay)'
        }
        topics.append({
            'topic': 'FRQ Writing Workshop',
            'detail': f'MCQ {pt_mcq}% vs FRQ {pt_frq}% = knows content, struggles with writing. Focus on {frq_types.get(course, "essay structure")}.',
            'materials': 'Bring 1-2 sample FRQs with rubrics to practice outlining'
        })

    # Topic: Weak units (can generate multiple)
    for i, unit in enumerate(weak_units[:3]):
        unit_name = get_unit_name(unit)
        topics.append({
            'topic': f'Content Deep-Dive: {unit_name}',
            'detail': f'Targeted review of {unit_name}. Cover key concepts, common misconceptions, and practice questions.',
            'materials': f'Bring summary sheet for {unit_name} and 5-10 MCQs on this topic'
        })

    # Topic: Low score triage
    if pt_score and pt_score <= 2:
        topics.append({
            'topic': 'High-Yield Topics & Triage',
            'detail': 'Identify the 20% of content that appears in 80% of questions. Focus on partial credit strategies.',
            'materials': 'Bring list of most-tested topics and scoring guidelines'
        })

    # Topic: MCQ strategies (if MCQ is weak)
    if pt_mcq and pt_mcq < 50:
        topics.append({
            'topic': 'MCQ Attack Strategies',
            'detail': f'MCQ at {pt_mcq}% - work on process of elimination, reading strategies, and common trap answers.',
            'materials': 'Bring 15-20 MCQs to practice timed, with discussion of wrong answers'
        })

    # Always add exam strategy as a possible topic
    topics.append({
        'topic': 'Exam Day Strategy & Confidence',
        'detail': 'Time management, stress management, day-of logistics. Build confidence going into the exam.',
        'materials': 'Bring timing guide and pacing schedule'
    })

    # Remove duplicates while preserving order
    seen = set()
    unique_topics = []
    for t in topics:
        if t['topic'] not in seen:
            seen.add(t['topic'])
            unique_topics.append(t)

    # Return topic based on session number
    # Session 1 = first priority, Session 2 = second priority, etc.
    idx = min(session_num - 1, len(unique_topics) - 1)
    return unique_topics[idx]


def generate_external_coach_plan(bookings):
    """Generate the full external coach plan with agendas."""
    # Count unique students
    unique_students = set()
    for b in bookings:
        unique_students.add(b['student'].get('student', 'Unknown'))

    plan = {
        'bookings': [],
        'by_day': {},
        'summary': {
            'unique_students': len(unique_students),
            'total_sessions': len([b for b in bookings if b['scheduled']]),
            'scheduled': len([b for b in bookings if b['scheduled']]),
            'unscheduled': len([b for b in bookings if not b['scheduled']]),
            'total_hours': 0
        }
    }

    for booking in bookings:
        if not booking['scheduled']:
            plan['bookings'].append(booking)
            continue

        student = booking['student']
        slot = booking['slot']
        session_num = booking.get('session_num', 1)
        total_sessions = booking.get('total_sessions', 1)

        # Generate agenda for this student
        agenda = generate_session_agenda(student)

        # Generate detailed briefing and session topic (varies by session number)
        briefing = generate_student_briefing(student)
        session_topic = generate_session_topic(student, session_num, total_sessions)

        booking_detail = {
            'student_name': student.get('student', 'Unknown'),
            'course': student.get('course', 'Unknown'),
            'email': student.get('email', ''),
            'risk': student.get('risk', 'Unknown'),
            'pt_mcq': student.get('pt_mcq'),
            'pt_frq': student.get('pt_frq'),
            'pt_score': student.get('pt_score'),
            'recommendation': student.get('recommendation', ''),
            'combined_progress': student.get('combined_progress'),
            'weak_units': student.get('weak_units', []),
            'slot': slot,
            'agenda': agenda,
            'briefing': briefing,
            'session_topic': session_topic,
            'session_num': session_num,
            'total_sessions': total_sessions,
            'scheduled': True
        }

        plan['bookings'].append(booking_detail)

        # Group by day
        day_key = slot['date']
        if day_key not in plan['by_day']:
            plan['by_day'][day_key] = {
                'date': day_key,
                'day_name': slot['day'],
                'display': slot['datetime'].strftime('%A, %B %d'),
                'sessions': []
            }
        plan['by_day'][day_key]['sessions'].append(booking_detail)

    # Calculate total hours (20 min per session)
    plan['summary']['total_hours'] = round(plan['summary']['scheduled'] * 20 / 60, 1)

    return plan


EXTERNAL_SCHEDULER_HTML = '''
<!DOCTYPE html>
<html>
<head>
    <title>External Coach Scheduler</title>
    <style>
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
            background: #1a1a2e;
            color: #eee;
            padding: 20px;
        }
        h1 { margin-bottom: 5px; color: #fff; }
        h2 { margin: 20px 0 10px; color: #fff; font-size: 18px; }
        h3 { margin: 15px 0 8px; color: #44dddd; font-size: 16px; }
        .subtitle { color: #888; margin-bottom: 20px; }
        .nav { margin-bottom: 20px; }
        .nav a { color: #4da6ff; margin-right: 20px; text-decoration: none; }
        .nav a:hover { text-decoration: underline; }

        .summary-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
            gap: 15px;
            margin-bottom: 30px;
        }
        .summary-card {
            background: #252540;
            padding: 15px;
            border-radius: 8px;
            text-align: center;
        }
        .summary-value { font-size: 28px; font-weight: bold; }
        .summary-label { color: #888; font-size: 12px; margin-top: 5px; }

        .actions {
            display: flex;
            gap: 15px;
            margin-bottom: 30px;
            flex-wrap: wrap;
        }
        .btn {
            padding: 12px 24px;
            border: none;
            border-radius: 6px;
            cursor: pointer;
            font-size: 14px;
            font-weight: bold;
            text-decoration: none;
            display: inline-flex;
            align-items: center;
            gap: 8px;
        }
        .btn-primary { background: #4da6ff; color: #fff; }
        .btn-primary:hover { background: #3d96ef; }
        .btn-success { background: #44dd44; color: #1a1a2e; }
        .btn-success:hover { background: #34cd34; }
        .btn-warning { background: #ff8844; color: #fff; }
        .btn-warning:hover { background: #ee7733; }

        .day-section {
            background: #252540;
            border-radius: 8px;
            margin-bottom: 20px;
            overflow: hidden;
        }
        .day-header {
            background: #353560;
            padding: 15px 20px;
            font-weight: bold;
            font-size: 16px;
            display: flex;
            justify-content: space-between;
            align-items: center;
        }
        .day-count {
            background: #44dddd;
            color: #1a1a2e;
            padding: 4px 12px;
            border-radius: 12px;
            font-size: 12px;
        }

        .session-card {
            padding: 15px 20px;
            border-top: 1px solid #333;
        }
        .session-card:hover { background: #2a2a4a; }
        .session-header {
            display: flex;
            justify-content: space-between;
            align-items: flex-start;
            margin-bottom: 10px;
        }
        .session-time {
            background: #44dddd;
            color: #1a1a2e;
            padding: 4px 10px;
            border-radius: 4px;
            font-weight: bold;
            font-size: 14px;
        }
        .session-student {
            font-size: 16px;
            font-weight: bold;
        }
        .session-course {
            display: inline-block;
            padding: 2px 8px;
            border-radius: 4px;
            font-size: 11px;
            margin-left: 8px;
        }
        .course-APHG { background: #2d5a27; }
        .course-APWH { background: #5a2727; }
        .course-APUSH { background: #27415a; }
        .course-APGOV { background: #5a4a27; }

        .session-meta {
            display: flex;
            gap: 15px;
            font-size: 12px;
            color: #888;
            margin-bottom: 10px;
        }
        .session-meta strong { color: #ccc; }

        .risk-Critical { color: #ff4444; }
        .risk-High, .risk-AtRisk { color: #ff8844; }
        .risk-Medium, .risk-OnTrack { color: #ffaa00; }
        .risk-Low, .risk-Strong { color: #44ff44; }

        .briefing {
            background: #1e1e3a;
            border-left: 3px solid #44dddd;
            padding: 10px 12px;
            margin-bottom: 10px;
            font-size: 12px;
            line-height: 1.6;
        }
        .briefing-line { color: #ccc; margin-bottom: 4px; }
        .briefing-line:last-child { margin-bottom: 0; }
        .briefing-highlight { color: #ff8844; font-weight: bold; }

        .session-topic {
            background: #252550;
            border: 1px solid #44dddd;
            border-radius: 6px;
            padding: 12px;
            margin-bottom: 10px;
        }
        .topic-title {
            color: #44dddd;
            font-weight: bold;
            font-size: 14px;
            margin-bottom: 6px;
        }
        .topic-detail { color: #ccc; font-size: 12px; margin-bottom: 6px; }
        .topic-materials {
            color: #888;
            font-size: 11px;
            font-style: italic;
        }

        .agenda-preview {
            background: #1a1a2e;
            padding: 10px;
            border-radius: 6px;
            font-size: 12px;
        }
        .agenda-item {
            padding: 4px 0;
            color: #aaa;
        }
        .agenda-item strong { color: #44dddd; }

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
            max-width: 600px;
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

        .progress-bar {
            background: #333;
            border-radius: 4px;
            height: 20px;
            overflow: hidden;
            margin: 15px 0;
        }
        .progress-fill {
            background: #44dddd;
            height: 100%;
            width: 0%;
            transition: width 0.3s;
        }

        @media print {
            body { background: #fff; color: #000; }
            .nav, .actions, .btn { display: none; }
            .day-section { break-inside: avoid; }
            .session-card { border: 1px solid #ccc; }
        }
    </style>
</head>
<body>
    <nav class="nav">
        <a href="/">Dashboard</a>
        <a href="/planner">Planner</a>
        <a href="/external-scheduler" style="font-weight: bold;">External Scheduler</a>
        <a href="/comms">Communications</a>
    </nav>

    <h1>External Coach Schedule</h1>
    <p class="subtitle">Week of {{ start_date }} - Exam Strategy Sessions (20 min each)</p>

    <div class="summary-grid">
        <div class="summary-card">
            <div class="summary-value">{{ plan.summary.unique_students }}</div>
            <div class="summary-label">Students</div>
        </div>
        <div class="summary-card">
            <div class="summary-value">{{ plan.summary.total_sessions }}</div>
            <div class="summary-label">Total Sessions</div>
        </div>
        <div class="summary-card">
            <div class="summary-value">{{ plan.summary.total_hours }}h</div>
            <div class="summary-label">Total Coach Time</div>
        </div>
        <div class="summary-card">
            <div class="summary-value" style="color: {% if plan.summary.unscheduled > 0 %}#ff8844{% else %}#44ff44{% endif %};">
                {{ plan.summary.unscheduled }}
            </div>
            <div class="summary-label">Unscheduled</div>
        </div>
    </div>

    <div class="actions">
        <a href="/external-scheduler/pdf" class="btn btn-primary" target="_blank">
            Download PDF
        </a>
        <a href="/external-scheduler/signup-csv" class="btn btn-primary" style="background: #6b7280;">
            Download Sign-Up CSV
        </a>
        <button class="btn btn-success" onclick="openBlastModal()">
            Send All Bookings
        </button>
        <button class="btn btn-warning" onclick="window.print()">
            Print Schedule
        </button>
    </div>

    {% for date, day in plan.by_day.items() | sort %}
    <div class="day-section">
        <div class="day-header">
            <span>{{ day.display }}</span>
            <span class="day-count">{{ day.sessions | length }} sessions</span>
        </div>

        {% for session in day.sessions %}
        <div class="session-card">
            <div class="session-header">
                <div>
                    <span class="session-student">{{ session.student_name }}</span>
                    <span class="session-course course-{{ session.course }}">{{ session.course }}</span>
                    {% if session.total_sessions > 1 %}
                    <span style="color: #888; font-size: 12px; margin-left: 8px;">(Session {{ session.session_num }}/{{ session.total_sessions }})</span>
                    {% endif %}
                </div>
                <span class="session-time">{{ session.slot.time }}</span>
            </div>

            <div class="session-meta">
                <span>Risk: <strong class="risk-{{ session.risk | replace(' ', '') }}">{{ session.risk }}</strong></span>
                {% if session.pt_score %}<span>PT Score: <strong>{{ session.pt_score }}</strong></span>{% endif %}
                {% if session.pt_mcq and session.pt_frq %}<span>MCQ: {{ session.pt_mcq }}% / FRQ: {{ session.pt_frq }}%</span>{% endif %}
            </div>

            <div class="briefing">
                {% for line in session.briefing %}
                <div class="briefing-line">{{ line }}</div>
                {% endfor %}
            </div>

            <div class="session-topic">
                <div class="topic-title">Recommended: {{ session.session_topic.topic }}</div>
                <div class="topic-detail">{{ session.session_topic.detail }}</div>
                <div class="topic-materials">{{ session.session_topic.materials }}</div>
            </div>
        </div>
        {% endfor %}
    </div>
    {% endfor %}

    {% if plan.summary.unscheduled > 0 %}
    <div class="day-section" style="border-left: 4px solid #ff8844;">
        <div class="day-header" style="background: #5a3a1a;">
            <span>Unscheduled Students</span>
            <span class="day-count" style="background: #ff8844;">{{ plan.summary.unscheduled }}</span>
        </div>
        {% for booking in plan.bookings if not booking.scheduled %}
        <div class="session-card">
            <span class="session-student">{{ booking.student.student }}</span>
            <span class="session-course course-{{ booking.student.course }}">{{ booking.student.course }}</span>
            <span style="color: #888; margin-left: 15px;">Needs overflow slot</span>
        </div>
        {% endfor %}
    </div>
    {% endif %}

    <!-- Blast Modal -->
    <div class="modal" id="blast-modal">
        <div class="modal-content" style="max-width: 800px;">
            <div class="modal-header">
                <span>Send Booking Notifications</span>
                <button class="close-btn" onclick="closeBlastModal()">&times;</button>
            </div>
            <div class="modal-body">
                <div id="blast-preview">
                    <p style="margin-bottom: 15px;">Send booking confirmations to <strong id="unique-student-count"></strong> students?</p>
                    <p style="color: #888; font-size: 13px; margin-bottom: 15px;">Each student will receive ONE message listing ALL their sessions.</p>

                    <div style="margin-bottom: 15px;">
                        <label style="color: #aaa; font-size: 12px;">Select a student to preview their message:</label>
                        <select id="preview-select" onchange="showPreview()" style="width: 100%; padding: 8px; background: #1a1a2e; color: #fff; border: 1px solid #444; border-radius: 4px; margin-top: 5px;">
                        </select>
                    </div>

                    <div id="message-preview" style="background: #1a1a2e; padding: 15px; border-radius: 6px; font-family: monospace; font-size: 12px; white-space: pre-wrap; max-height: 300px; overflow-y: auto; border: 1px solid #444;">
                    </div>
                </div>
                <div id="blast-progress" style="display: none;">
                    <p>Sending notifications...</p>
                    <div class="progress-bar">
                        <div class="progress-fill" id="progress-fill"></div>
                    </div>
                    <p id="progress-text" style="color: #888; font-size: 12px;">0 / 0</p>
                    <div id="blast-results" style="margin-top: 15px; max-height: 200px; overflow-y: auto; font-size: 12px;"></div>
                </div>
            </div>
            <div class="modal-footer">
                <button class="btn" style="background: #666;" onclick="closeBlastModal()">Cancel</button>
                <button class="btn btn-success" id="blast-send-btn" onclick="executeBlast()">Send All</button>
            </div>
        </div>
    </div>

    <script>
    const bookings = {{ bookings_json | safe }};

    // Consolidate bookings by student
    function getConsolidatedBookings() {
        const byStudent = {};
        for (const b of bookings.filter(b => b.scheduled)) {
            const key = b.student_name + '|' + b.course;
            if (!byStudent[key]) {
                byStudent[key] = {
                    student_name: b.student_name,
                    course: b.course,
                    sessions: []
                };
            }
            byStudent[key].sessions.push({
                date: b.slot.display,
                time: b.slot.time,
                topic: b.session_topic?.topic || 'Exam Strategy'
            });
        }
        // Sort sessions by date
        for (const key in byStudent) {
            byStudent[key].sessions.sort((a, b) => a.date.localeCompare(b.date));
        }
        return Object.values(byStudent);
    }

    function generateMessage(consolidated) {
        const sessionList = consolidated.sessions.map((s, i) =>
            `Session ${i + 1}: ${s.date} at ${s.time} (Central Time)\\n   Topic: ${s.topic}`
        ).join('\\n\\n');

        return `Hi ${consolidated.student_name}!

Your AP Exam Strategy sessions have been scheduled:

${sessionList}

Each session is 20 minutes. Please be ready a few minutes early.

Looking forward to helping you prepare for the ${consolidated.course} exam!`;
    }

    function openBlastModal() {
        const consolidated = getConsolidatedBookings();
        document.getElementById('unique-student-count').textContent = consolidated.length;

        // Populate preview dropdown
        const select = document.getElementById('preview-select');
        select.innerHTML = consolidated.map((c, i) =>
            `<option value="${i}">${c.student_name} (${c.course}) - ${c.sessions.length} session(s)</option>`
        ).join('');

        showPreview();

        document.getElementById('blast-preview').style.display = 'block';
        document.getElementById('blast-progress').style.display = 'none';
        document.getElementById('blast-send-btn').style.display = 'inline-block';
        document.getElementById('blast-modal').classList.add('open');
    }

    function showPreview() {
        const consolidated = getConsolidatedBookings();
        const idx = parseInt(document.getElementById('preview-select').value) || 0;
        const message = generateMessage(consolidated[idx]);
        document.getElementById('message-preview').textContent = message;
    }

    function closeBlastModal() {
        document.getElementById('blast-modal').classList.remove('open');
    }

    async function executeBlast() {
        document.getElementById('blast-preview').style.display = 'none';
        document.getElementById('blast-progress').style.display = 'block';
        document.getElementById('blast-send-btn').style.display = 'none';

        const consolidated = getConsolidatedBookings();
        const total = consolidated.length;
        let completed = 0;
        let successCount = 0;
        let resultsHtml = '';

        for (const student of consolidated) {
            try {
                const message = generateMessage(student);

                const resp = await fetch('/api/comms/send', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({
                        student_name: student.student_name,
                        course: student.course,
                        message: message
                    })
                });
                const data = await resp.json();

                const anyOk = data.slack_student?.success || data.email_student?.success ||
                             data.slack_guide?.success || data.email_guide?.success;
                const icon = anyOk ? '✓' : '✗';
                const color = anyOk ? '#44ff44' : '#ff4444';
                const channels = [];
                if (data.slack_student?.success) channels.push('Slack');
                if (data.email_student?.success) channels.push('Email');
                if (data.slack_guide?.success) channels.push('Guide-Slack');
                if (data.email_guide?.success) channels.push('Guide-Email');
                resultsHtml += `<div style="color: ${color};">${icon} ${student.student_name} (${student.sessions.length} sessions) - ${channels.join(', ') || 'failed'}</div>`;
                if (anyOk) successCount++;
            } catch (e) {
                resultsHtml += `<div style="color: #ff4444;">✗ ${student.student_name}: ${e.message}</div>`;
            }

            completed++;
            const pct = (completed / total) * 100;
            document.getElementById('progress-fill').style.width = pct + '%';
            document.getElementById('progress-text').textContent = `${completed} / ${total}`;
            document.getElementById('blast-results').innerHTML = resultsHtml;

            await new Promise(r => setTimeout(r, 300));
        }

        document.getElementById('progress-text').textContent = `Done! ${successCount} succeeded, ${total - successCount} failed`;
    }
    </script>
</body>
</html>
'''


@app.route('/external-scheduler')
def external_scheduler():
    """External coach scheduling page."""
    data = load_all_data()
    students = build_unified_table(data)

    # Start from today at 1pm Central time
    start_date = datetime.now().replace(hour=13, minute=0, second=0, microsecond=0)

    # Generate slots for 2 weeks
    slots = generate_external_schedule(start_date, num_weeks=2)

    # Allocate students to slots
    bookings = allocate_students_to_slots(students, slots)

    # Generate full plan
    plan = generate_external_coach_plan(bookings)

    # Prepare JSON for JavaScript
    bookings_for_js = []
    for b in plan['bookings']:
        if b.get('scheduled'):
            bookings_for_js.append({
                'student_name': b['student_name'],
                'course': b['course'],
                'slot': b['slot'],
                'agenda': b['agenda'],
                'session_topic': b.get('session_topic', {}),
                'briefing': b.get('briefing', []),
                'scheduled': True
            })

    return render_template_string(
        EXTERNAL_SCHEDULER_HTML,
        plan=plan,
        start_date=start_date.strftime('%B %d, %Y'),
        bookings_json=json.dumps(bookings_for_js, default=str)
    )


@app.route('/external-scheduler/pdf')
def external_scheduler_pdf():
    """Generate PDF of external coach schedule."""
    data = load_all_data()
    students = build_unified_table(data)

    start_date = datetime.now().replace(hour=13, minute=0, second=0, microsecond=0)
    slots = generate_external_schedule(start_date, num_weeks=2)
    bookings = allocate_students_to_slots(students, slots)
    plan = generate_external_coach_plan(bookings)

    # Generate HTML for PDF
    html = f'''<!DOCTYPE html>
<html>
<head>
    <title>External Coach Schedule</title>
    <style>
        body {{ font-family: Arial, sans-serif; padding: 20px; color: #333; }}
        h1 {{ color: #2c5282; border-bottom: 2px solid #2c5282; padding-bottom: 10px; }}
        h2 {{ color: #2d3748; margin-top: 30px; background: #e2e8f0; padding: 10px; }}
        .summary {{ display: flex; gap: 30px; margin-bottom: 30px; }}
        .summary-item {{ text-align: center; }}
        .summary-value {{ font-size: 32px; font-weight: bold; color: #2c5282; }}
        .summary-label {{ color: #718096; font-size: 12px; }}
        .session {{ border: 1px solid #e2e8f0; padding: 15px; margin-bottom: 15px; page-break-inside: avoid; }}
        .session-header {{ display: flex; justify-content: space-between; margin-bottom: 10px; }}
        .student-name {{ font-size: 18px; font-weight: bold; }}
        .time {{ background: #2c5282; color: white; padding: 5px 15px; border-radius: 4px; }}
        .meta {{ color: #718096; font-size: 12px; margin-bottom: 10px; }}
        .briefing {{ background: #edf2f7; border-left: 3px solid #2c5282; padding: 10px; margin-bottom: 10px; font-size: 12px; }}
        .briefing-line {{ margin: 4px 0; color: #4a5568; }}
        .topic-box {{ background: #ebf8ff; border: 1px solid #2c5282; border-radius: 4px; padding: 10px; margin-bottom: 10px; }}
        .topic-title {{ color: #2c5282; font-weight: bold; font-size: 14px; margin-bottom: 4px; }}
        .topic-detail {{ color: #4a5568; font-size: 12px; margin-bottom: 4px; }}
        .topic-materials {{ color: #718096; font-size: 11px; font-style: italic; }}
        .agenda {{ background: #f7fafc; padding: 10px; border-radius: 4px; }}
        .agenda-item {{ margin: 5px 0; font-size: 13px; }}
        .course {{ display: inline-block; padding: 2px 8px; border-radius: 4px; font-size: 11px; color: white; }}
        .APHG {{ background: #48bb78; }}
        .APWH {{ background: #f56565; }}
        .APUSH {{ background: #4299e1; }}
        .APGOV {{ background: #ed8936; }}
        @page {{ margin: 1cm; }}
    </style>
</head>
<body>
    <h1>External Coach Schedule</h1>
    <p>Generated: {datetime.now().strftime('%B %d, %Y at %H:%M')}</p>

    <div class="summary">
        <div class="summary-item">
            <div class="summary-value">{plan['summary']['scheduled']}</div>
            <div class="summary-label">Sessions</div>
        </div>
        <div class="summary-item">
            <div class="summary-value">{plan['summary']['total_hours']}h</div>
            <div class="summary-label">Total Time</div>
        </div>
    </div>
'''

    for date in sorted(plan['by_day'].keys()):
        day = plan['by_day'][date]
        html += f'<h2>{day["display"]} ({len(day["sessions"])} sessions)</h2>'

        for session in day['sessions']:
            # Build briefing lines
            briefing_html = ''
            for line in session.get('briefing', []):
                briefing_html += f'<div class="briefing-line">{line}</div>'

            # Build session topic
            topic = session.get('session_topic', {})
            topic_html = f'''
            <div class="topic-box">
                <div class="topic-title">Recommended: {topic.get('topic', 'Exam Strategy')}</div>
                <div class="topic-detail">{topic.get('detail', '')}</div>
                <div class="topic-materials">{topic.get('materials', '')}</div>
            </div>'''

            html += f'''
    <div class="session">
        <div class="session-header">
            <div>
                <span class="student-name">{session['student_name']}</span>
                <span class="course {session['course']}">{session['course']}</span>
            </div>
            <span class="time">{session['slot']['time']}</span>
        </div>
        <div class="meta">
            Risk: {session['risk']} | PT Score: {session.get('pt_score', 'N/A')}
        </div>
        <div class="briefing">
            {briefing_html}
        </div>
        {topic_html}
    </div>
'''

    html += '</body></html>'

    # Return as downloadable HTML (browsers can save as PDF)
    from flask import Response
    response = Response(html, mimetype='text/html')
    response.headers['Content-Disposition'] = 'attachment; filename=external_coach_schedule.html'
    return response


@app.route('/external-scheduler/signup-csv')
def external_scheduler_signup_csv():
    """Generate a blank sign-up sheet CSV with all available slots."""
    import csv
    from io import StringIO

    start_date = datetime.now().replace(hour=13, minute=0, second=0, microsecond=0)
    slots = generate_external_schedule(start_date, num_weeks=2)

    # Filter to only non-overflow slots (main schedule)
    main_slots = [s for s in slots if not s['overflow']]

    # Create CSV
    output = StringIO()
    writer = csv.writer(output)

    # Header row
    writer.writerow(['Date', 'Day', 'Time (Central)', 'Student Name', 'Course', 'Email', 'Notes'])

    # One row per slot
    for slot in main_slots:
        writer.writerow([
            slot['date'],
            slot['day'],
            slot['time'],
            '',  # Student Name - blank for sign-up
            '',  # Course - blank
            '',  # Email - blank
            ''   # Notes - blank
        ])

    # Return as CSV download
    from flask import Response
    csv_content = output.getvalue()
    response = Response(csv_content, mimetype='text/csv')
    response.headers['Content-Disposition'] = 'attachment; filename=coaching_signup_sheet.csv'
    return response


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


@app.route('/api/coaching-notes/<student_name>/<course>', methods=['GET'])
def api_get_coaching_notes(student_name, course):
    """Get coaching notes for a student."""
    notes = get_student_notes(student_name, course, limit=10)
    patterns = get_coaching_patterns(student_name, course)
    return jsonify({
        'notes': notes,
        'patterns': patterns
    })


@app.route('/api/coaching-notes/<student_name>/<course>', methods=['POST'])
def api_add_coaching_note(student_name, course):
    """Add a coaching note for a student."""
    data = request.get_json() or {}
    raw_notes = data.get('notes', '').strip()
    call_date = data.get('date')

    if not raw_notes:
        return jsonify({'success': False, 'error': 'Notes cannot be empty'}), 400

    note_entry = add_coaching_note(student_name, course, raw_notes, call_date)

    return jsonify({
        'success': True,
        'note': note_entry
    })


@app.route('/api/coaching-notes/<student_name>/<course>/reprocess', methods=['POST'])
def api_reprocess_notes(student_name, course):
    """Reprocess all notes for a student to extract insights (if NLP failed before)."""
    notes = load_coaching_notes()
    key = f"{student_name}|{course}"

    if key not in notes:
        return jsonify({'success': False, 'error': 'No notes found'}), 404

    reprocessed = 0
    for note in notes[key]:
        if not note.get('extracted', {}).get('processed', False):
            note['extracted'] = extract_insights_from_notes(
                note['raw_notes'], student_name, course
            )
            reprocessed += 1

    save_coaching_notes(notes)

    return jsonify({
        'success': True,
        'reprocessed': reprocessed
    })


# =============================================================================
# STUDENT LOGS API & PAGE
# =============================================================================

STUDENT_LOGS_HTML = '''
<!DOCTYPE html>
<html>
<head>
    <title>Student Daily Logs - AP Dashboard</title>
    <style>
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
            background: #1a1a2e;
            color: #eee;
            padding: 20px;
        }
        h1 { margin-bottom: 5px; color: #fff; }
        h2 { margin: 20px 0 10px; color: #fff; font-size: 18px; }
        .subtitle { color: #888; margin-bottom: 20px; }
        .nav { margin-bottom: 20px; }
        .nav a { color: #4da6ff; margin-right: 20px; text-decoration: none; }
        .nav a:hover { text-decoration: underline; }

        .card {
            background: #252540;
            padding: 20px;
            border-radius: 8px;
            margin-bottom: 20px;
        }
        .card h3 { margin-bottom: 15px; color: #44dddd; }

        textarea {
            width: 100%;
            height: 200px;
            padding: 12px;
            border: 1px solid #444;
            border-radius: 6px;
            background: #1a1a2e;
            color: #fff;
            font-family: monospace;
            font-size: 13px;
            margin-bottom: 15px;
        }
        .btn {
            padding: 12px 24px;
            border: none;
            border-radius: 6px;
            cursor: pointer;
            font-size: 14px;
            font-weight: bold;
        }
        .btn-primary { background: #4da6ff; color: #fff; }
        .btn-primary:hover { background: #3d96ef; }

        .result {
            padding: 15px;
            border-radius: 6px;
            margin-top: 15px;
            display: none;
        }
        .result.success { background: #1a3a1a; border: 1px solid #44ff44; }
        .result.error { background: #3a1a1a; border: 1px solid #ff4444; }

        .log-table {
            width: 100%;
            border-collapse: collapse;
        }
        .log-table th, .log-table td {
            padding: 10px;
            text-align: left;
            border-bottom: 1px solid #333;
        }
        .log-table th {
            background: #353560;
            color: #fff;
        }
        .log-table tr:hover { background: #2a2a4a; }

        .prediction {
            display: inline-block;
            padding: 2px 8px;
            border-radius: 4px;
            font-weight: bold;
        }
        .pred-5 { background: #22c55e; color: #000; }
        .pred-4 { background: #84cc16; color: #000; }
        .pred-3 { background: #eab308; color: #000; }
        .pred-2 { background: #f97316; color: #fff; }
        .pred-1 { background: #ef4444; color: #fff; }

        .course-tag {
            display: inline-block;
            padding: 2px 6px;
            border-radius: 4px;
            font-size: 11px;
            margin-left: 5px;
        }
        .course-APHG { background: #2d5a27; }
        .course-APUSH { background: #27415a; }
        .course-APWH { background: #5a2727; }
        .course-APGOV { background: #5a4a27; }
        .course-APCSA { background: #4a275a; }

        .log-detail {
            font-size: 12px;
            color: #aaa;
        }
        .log-field { margin: 3px 0; }
        .log-field strong { color: #44dddd; }

        .flag-badge {
            background: #ef4444;
            color: #fff;
            padding: 2px 6px;
            border-radius: 4px;
            font-size: 10px;
            font-weight: bold;
            margin-left: 5px;
        }
        .priority-badge {
            background: #f97316;
            color: #fff;
            padding: 2px 6px;
            border-radius: 4px;
            font-size: 10px;
            font-weight: bold;
            margin-left: 5px;
        }
        .action-item {
            color: #44dddd;
            font-size: 11px;
            margin-top: 4px;
        }
    </style>
</head>
<body>
    <nav class="nav">
        <a href="/">Dashboard</a>
        <a href="/planner">Planner</a>
        <a href="/external-scheduler">External</a>
        <a href="/comms">Communications</a>
        <a href="/student-logs" style="font-weight: bold;">Student Logs</a>
        <a href="/refresh">Refresh Data</a>
    </nav>

    <h1>Student Daily Logs</h1>
    <p class="subtitle">Ingest and view student self-reported daily work logs</p>

    <div class="card">
        <h3>Paste Log Data</h3>
        <p style="color: #888; font-size: 13px; margin-bottom: 10px;">
            Paste the raw log text from form submissions (e.g., from Slack or email notifications)
        </p>
        <textarea id="log-input" placeholder="--- AP Human Geography (1 student(s)) ---
- Grady Swanson: [AP Human Geography] | Today's work: Did math | Gaps: Did math | Prediction: 5 | Tomorrow's focus: 20 topics in good timeback | Recall: Email me if you see this."></textarea>
        <button class="btn btn-primary" onclick="ingestLogs()">Ingest Logs</button>
        <div id="ingest-result" class="result"></div>
    </div>

    <div class="card">
        <h3>Recent Logs ({{ logs | length }} entries)</h3>
        {% if logs %}
        <table class="log-table">
            <thead>
                <tr>
                    <th>Date</th>
                    <th>Student</th>
                    <th>Status</th>
                    <th>Summary / Today's Work</th>
                    <th>Action Item</th>
                    <th></th>
                </tr>
            </thead>
            <tbody>
                {% for log in logs %}
                <tr id="log-{{ loop.index0 }}"{% if log.flagged %} style="background: #3a2020;"{% elif log.priority %} style="background: #3a3020;"{% endif %}>
                    <td>{{ log.date }}</td>
                    <td>
                        {{ log.student }}
                        <span class="course-tag course-{{ log.course | replace(' ', '') | replace('APHumanGeography', 'APHG') | replace('APUSHistory', 'APUSH') | replace('APWorldHistory:Modern', 'APWH') | replace('APUSGovernment&Politics', 'APGOV') | replace('APComputerScienceA', 'APCSA') | replace('APUnitedStatesHistory', 'APUSH') }}">
                            {{ log.course }}
                        </span>
                        {% if log.flagged %}<span class="flag-badge">FLAG</span>{% endif %}
                        {% if log.priority %}<span class="priority-badge">PRIORITY</span>{% endif %}
                    </td>
                    <td>
                        {% if log.prediction %}
                        <span class="prediction pred-{{ log.prediction }}">{{ log.prediction }}</span>
                        {% else %}
                        -
                        {% endif %}
                    </td>
                    <td style="max-width: 350px;">
                        {{ log.todays_work[:150] }}{% if log.todays_work | length > 150 %}...{% endif %}
                        {% if log.gaps %}<div class="action-item">Gaps: {{ log.gaps[:80] }}</div>{% endif %}
                    </td>
                    <td style="max-width: 200px;">
                        {% if log.action_item %}
                        {{ log.action_item[:100] }}{% if log.action_item | length > 100 %}...{% endif %}
                        {% elif log.tomorrows_focus %}
                        Tomorrow: {{ log.tomorrows_focus[:80] }}{% if log.tomorrows_focus | length > 80 %}...{% endif %}
                        {% else %}
                        -
                        {% endif %}
                    </td>
                    <td>
                        <button onclick="deleteLog('{{ log.timestamp }}')" style="background: #ef4444; color: #fff; border: none; padding: 4px 8px; border-radius: 4px; cursor: pointer; font-size: 11px;">Delete</button>
                    </td>
                </tr>
                {% endfor %}
            </tbody>
        </table>
        {% else %}
        <p style="color: #888;">No logs ingested yet. Paste log data above to get started.</p>
        {% endif %}
    </div>

    <script>
    async function ingestLogs() {
        const input = document.getElementById('log-input').value.trim();
        const resultDiv = document.getElementById('ingest-result');

        if (!input) {
            resultDiv.className = 'result error';
            resultDiv.style.display = 'block';
            resultDiv.innerHTML = 'Please paste log data first.';
            return;
        }

        try {
            const resp = await fetch('/api/student-logs/ingest', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({ text: input })
            });
            const data = await resp.json();

            if (data.success) {
                resultDiv.className = 'result success';
                resultDiv.innerHTML = `Ingested ${data.count} log(s):<br>${data.students.join('<br>')}`;
                // Reload page after 2 seconds to show new logs
                setTimeout(() => location.reload(), 2000);
            } else {
                resultDiv.className = 'result error';
                resultDiv.innerHTML = `Error: ${data.error}`;
            }
        } catch (e) {
            resultDiv.className = 'result error';
            resultDiv.innerHTML = `Error: ${e.message}`;
        }

        resultDiv.style.display = 'block';
    }

    async function deleteLog(timestamp) {
        if (!confirm('Delete this log entry?')) return;

        try {
            const resp = await fetch('/api/student-logs/delete', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({ timestamp: timestamp })
            });
            const data = await resp.json();

            if (data.success) {
                location.reload();
            } else {
                alert('Error: ' + data.error);
            }
        } catch (e) {
            alert('Error: ' + e.message);
        }
    }
    </script>
</body>
</html>
'''


@app.route('/student-logs')
def student_logs_page():
    """Student daily logs page."""
    logs = get_recent_logs(limit=100)
    return render_template_string(STUDENT_LOGS_HTML, logs=logs)


@app.route('/api/student-logs/ingest', methods=['POST'])
def api_ingest_student_logs():
    """Ingest student logs from pasted text."""
    req = request.json
    raw_text = req.get('text', '')

    if not raw_text:
        return jsonify({'success': False, 'error': 'No text provided'}), 400

    result = ingest_student_logs(raw_text)
    return jsonify(result)


@app.route('/api/student-logs/recent')
def api_recent_student_logs():
    """Get recent student logs."""
    limit = request.args.get('limit', 50, type=int)
    logs = get_recent_logs(limit=limit)
    return jsonify({'logs': logs})


@app.route('/api/student-logs/<student_name>/<course>')
def api_student_logs(student_name, course):
    """Get logs for a specific student."""
    limit = request.args.get('limit', 10, type=int)
    logs = get_student_logs(student_name, course, limit=limit)
    return jsonify({'logs': logs})


@app.route('/api/student-logs/delete', methods=['POST'])
def api_delete_student_log():
    """Delete a student log by timestamp."""
    req = request.json
    timestamp = req.get('timestamp', '')

    if not timestamp:
        return jsonify({'success': False, 'error': 'No timestamp provided'}), 400

    result = delete_student_log(timestamp)
    return jsonify(result)


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
        <a href="/planner">Planner</a>
        <a href="/external-scheduler">External</a>
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
        <a href="/planner">Planner</a>
        <a href="/external-scheduler">External</a>
        <a href="/coaching">Coaching</a>
        <a href="/comms">Communications</a>
        <a href="/refresh">Refresh Data</a>
        <a href="/student-logs">Student Logs</a>
        <a href="/settings">Settings</a>
    </nav>

    {% if not openai_configured %}
    <div class="config-warning">
        <strong>OpenAI not configured.</strong> Add your API key in <a href="/settings">Settings</a> to enable AI-generated messages.
    </div>
    {% endif %}

    <!-- Survey Blast Section -->
    <div class="rec-section" style="margin-bottom: 30px;">
        <div class="rec-header" onclick="toggleSection(this)">
            <div>
                <span class="rec-title">Survey Blast</span>
                <span class="rec-count">{{ survey_links|length }} configured</span>
            </div>
        </div>
        <div class="rec-body open">
            <p style="color: #aaa; margin-bottom: 15px;">Send comfort level surveys to students by course. Configure your Google Form links below.</p>

            <div style="display: grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); gap: 15px; margin-bottom: 20px;">
                {% for course_id, course_name in [('aphg', 'APHG'), ('apgov', 'AP Gov'), ('apush', 'APUSH'), ('apworld', 'AP World')] %}
                <div style="background: #1a1a2e; padding: 15px; border-radius: 8px;">
                    <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 10px;">
                        <strong style="color: #fff;">{{ course_name }}</strong>
                        <span style="color: #666; font-size: 12px;">{{ student_counts.get(course_id, 0) }} students</span>
                    </div>
                    <input type="text"
                           id="survey-link-{{ course_id }}"
                           placeholder="Paste Google Form URL here..."
                           value="{{ survey_links.get(course_id, '') }}"
                           style="width: 100%; padding: 8px; border: 1px solid #444; border-radius: 4px; background: #252540; color: #fff; font-size: 12px; margin-bottom: 10px;">
                    <div style="display: flex; gap: 8px;">
                        <button class="btn btn-secondary btn-sm" onclick="saveSurveyLink('{{ course_id }}')">Save Link</button>
                        <button class="btn btn-primary btn-sm" onclick="blastSurvey('{{ course_id }}', '{{ course_name }}')" id="blast-btn-{{ course_id }}">
                            Send to {{ student_counts.get(course_id, 0) }} Students
                        </button>
                    </div>
                </div>
                {% endfor %}
            </div>

            <div style="background: #1a1a2e; padding: 15px; border-radius: 8px;">
                <strong style="color: #fff;">Custom Message (optional)</strong>
                <p style="color: #666; font-size: 12px; margin: 5px 0 10px;">This message will be sent along with the survey link.</p>
                <textarea id="survey-custom-message" style="min-height: 80px;" placeholder="Hi! Please take 5 minutes to complete this quick survey about how you're feeling about the upcoming AP exam. Your honest answers help us focus our prep sessions on what you need most.">{{ survey_message }}</textarea>
                <button class="btn btn-secondary btn-sm" onclick="saveSurveyMessage()" style="margin-top: 10px;">Save Message</button>
            </div>
        </div>
    </div>

    <!-- Survey Blast Modal -->
    <div class="modal" id="survey-modal">
        <div class="modal-content">
            <div class="modal-header">
                <span id="survey-modal-title">Send Survey</span>
                <button class="close-btn" onclick="closeSurveyModal()">&times;</button>
            </div>
            <div class="modal-body">
                <div id="survey-confirm-section">
                    <p style="margin-bottom: 15px; color: #ccc;">
                        You're about to send the comfort survey to <strong id="survey-student-count"></strong> <strong id="survey-course-name"></strong> students.
                    </p>
                    <div id="survey-student-list" style="background: #1a1a2e; padding: 10px; border-radius: 4px; margin-bottom: 15px; max-height: 150px; overflow-y: auto; font-size: 12px; color: #888;"></div>

                    <!-- Channel Selection -->
                    <div style="background: #1a1a2e; padding: 15px; border-radius: 4px; margin-bottom: 15px;">
                        <strong style="color: #888; display: block; margin-bottom: 10px;">Send via:</strong>
                        <label style="display: inline-flex; align-items: center; margin-right: 20px; cursor: pointer; color: #ccc;">
                            <input type="checkbox" id="survey-channel-slack" checked style="margin-right: 8px; width: 18px; height: 18px;">
                            Slack
                        </label>
                        <label style="display: inline-flex; align-items: center; cursor: pointer; color: #ccc;">
                            <input type="checkbox" id="survey-channel-email" checked style="margin-right: 8px; width: 18px; height: 18px;">
                            Email
                        </label>
                    </div>

                    <div style="background: #1a1a2e; padding: 10px; border-radius: 4px; margin-bottom: 15px;">
                        <strong style="color: #888;">Message preview:</strong>
                        <div id="survey-message-preview" style="color: #ccc; margin-top: 8px; white-space: pre-wrap; font-size: 13px;"></div>
                    </div>
                </div>
                <div id="survey-progress-section" style="display: none;">
                    <p style="margin-bottom: 10px; color: #ccc;">Sending surveys...</p>
                    <div style="background: #333; border-radius: 4px; height: 20px; overflow: hidden; margin-bottom: 15px;">
                        <div id="survey-progress-fill" style="background: #4da6ff; height: 100%; width: 0%; transition: width 0.3s;"></div>
                    </div>
                    <div id="survey-progress-text" style="color: #888; font-size: 12px;">0 / 0</div>
                    <div id="survey-results" style="margin-top: 15px; max-height: 200px; overflow-y: auto; font-size: 12px;"></div>
                </div>
            </div>
            <div class="modal-footer">
                <button class="btn btn-secondary" onclick="closeSurveyModal()">Cancel</button>
                <button class="btn btn-primary" id="survey-send-btn" onclick="executeSurveyBlast()">Send Survey to All</button>
            </div>
        </div>
    </div>

    <!-- Survey Follow-up Section -->
    <div class="rec-section" style="margin-bottom: 30px; border-left: 4px solid {% if survey_missing_total > 0 %}#ff8844{% else %}#44ff44{% endif %};">
        <div class="rec-header" onclick="toggleSection(this)">
            <div>
                <span class="rec-title">📋 Survey Follow-up</span>
                <span class="rec-count" style="background: {% if survey_missing_total > 0 %}#ff8844{% else %}#44ff44{% endif %};">{{ survey_missing_total }} missing</span>
                <span style="color: #888; font-size: 12px; margin-left: 10px;">{{ survey_completed_total }} completed</span>
            </div>
            <div class="rec-actions">
                {% if survey_missing_total > 0 %}
                <button class="btn btn-primary btn-sm" onclick="event.stopPropagation(); sendFollowupAll()">Send Follow-up to All Missing</button>
                {% endif %}
            </div>
        </div>
        <div class="rec-body{% if survey_missing_total > 0 %} open{% endif %}">
            {% if survey_missing_total == 0 %}
            <div style="padding: 20px; text-align: center; color: #44ff44;">
                <span style="font-size: 24px;">✓</span><br>
                All students have completed the survey!
            </div>
            {% else %}
            <p style="padding: 15px 20px 0; color: #aaa;">Students who haven't completed the comfort survey yet:</p>

            {% for course, missing_students in survey_missing.items() %}
            <div style="padding: 10px 20px;">
                <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px;">
                    <strong style="color: #fff;">{{ course }}</strong>
                    <button class="btn btn-secondary btn-sm" onclick="sendFollowupCourse('{{ course }}')">Send to {{ missing_students|length }} Students</button>
                </div>
                <div style="display: flex; flex-wrap: wrap; gap: 8px;">
                    {% for s in missing_students %}
                    <span style="background: #1a1a2e; padding: 4px 10px; border-radius: 4px; font-size: 12px; display: inline-flex; align-items: center; gap: 6px;"
                          data-followup-student="{{ s.student }}" data-followup-course="{{ s.course }}">
                        {{ s.student }}
                        {% if s.risk == 'Critical' %}<span style="color: #ff4444;">●</span>
                        {% elif s.risk == 'At Risk' %}<span style="color: #ff8844;">●</span>
                        {% endif %}
                    </span>
                    {% endfor %}
                </div>
            </div>
            {% endfor %}

            <div style="padding: 15px 20px; background: #1a1a2e; margin-top: 10px;">
                <strong style="color: #888;">Follow-up Message:</strong>
                <textarea id="followup-message" style="min-height: 80px; margin-top: 10px;">{{ survey_followup_message }}</textarea>
                <button class="btn btn-secondary btn-sm" onclick="saveFollowupMessage()" style="margin-top: 10px;">Save Message</button>
            </div>
            {% endif %}
        </div>
    </div>

    <!-- Follow-up Modal -->
    <div class="modal" id="followup-modal">
        <div class="modal-content">
            <div class="modal-header">
                <span id="followup-modal-title">Send Survey Follow-up</span>
                <button class="close-btn" onclick="closeFollowupModal()">&times;</button>
            </div>
            <div class="modal-body">
                <div id="followup-confirm-section">
                    <p style="margin-bottom: 15px; color: #ccc;">
                        Send follow-up reminder to <strong id="followup-count"></strong> student(s) who haven't completed the survey:
                    </p>
                    <div id="followup-student-list" style="background: #1a1a2e; padding: 10px; border-radius: 4px; margin-bottom: 15px; max-height: 150px; overflow-y: auto; font-size: 12px; color: #888;"></div>

                    <div style="background: #1a1a2e; padding: 10px; border-radius: 4px; margin-bottom: 15px;">
                        <strong style="color: #888;">Message:</strong>
                        <div id="followup-message-preview" style="color: #ccc; margin-top: 8px; white-space: pre-wrap; font-size: 13px;"></div>
                    </div>
                </div>
                <div id="followup-progress-section" style="display: none;">
                    <p style="margin-bottom: 10px; color: #ccc;">Sending follow-ups...</p>
                    <div style="background: #333; border-radius: 4px; height: 20px; overflow: hidden; margin-bottom: 15px;">
                        <div id="followup-progress-fill" style="background: #ff8844; height: 100%; width: 0%; transition: width 0.3s;"></div>
                    </div>
                    <div id="followup-progress-text" style="color: #888; font-size: 12px;">0 / 0</div>
                    <div id="followup-results" style="margin-top: 15px; max-height: 200px; overflow-y: auto; font-size: 12px;"></div>
                </div>
            </div>
            <div class="modal-footer">
                <button class="btn btn-secondary" onclick="closeFollowupModal()">Cancel</button>
                <button class="btn btn-primary" id="followup-send-btn" onclick="executeFollowupSend()" style="background: #ff8844;">Send Follow-up</button>
            </div>
        </div>
    </div>

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
    <div class="modal" id="preview-modal">
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

        // ============================================================
        // SURVEY BLAST FUNCTIONS
        // ============================================================

        let surveyStudents = [];
        let surveyCourse = '';
        let surveyCourseName = '';

        async function saveSurveyLink(courseId) {
            const link = document.getElementById(`survey-link-${courseId}`).value.trim();
            try {
                const resp = await fetch('/api/survey/save-link', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({course: courseId, link: link})
                });
                const data = await resp.json();
                if (data.success) {
                    // Enable/disable the blast button based on whether link exists
                    const blastBtn = document.getElementById(`blast-btn-${courseId}`);
                    blastBtn.disabled = !link;
                    alert('Survey link saved!');
                } else {
                    alert('Failed to save: ' + data.error);
                }
            } catch (e) {
                alert('Error saving link: ' + e.message);
            }
        }

        async function saveSurveyMessage() {
            const message = document.getElementById('survey-custom-message').value.trim();
            try {
                const resp = await fetch('/api/survey/save-message', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({message: message})
                });
                const data = await resp.json();
                if (data.success) {
                    alert('Survey message saved!');
                } else {
                    alert('Failed to save: ' + data.error);
                }
            } catch (e) {
                alert('Error saving message: ' + e.message);
            }
        }

        async function blastSurvey(courseId, courseName) {
            surveyCourse = courseId;
            surveyCourseName = courseName;

            // Get students for this course
            try {
                const resp = await fetch(`/api/survey/students/${courseId}`);
                const data = await resp.json();
                surveyStudents = data.students || [];

                if (surveyStudents.length === 0) {
                    alert('No students found for ' + courseName);
                    return;
                }

                // Populate modal
                document.getElementById('survey-modal-title').textContent = `Send ${courseName} Comfort Survey`;
                document.getElementById('survey-student-count').textContent = surveyStudents.length;
                document.getElementById('survey-course-name').textContent = courseName;
                document.getElementById('survey-student-list').innerHTML = surveyStudents.map(s => s.name).join(', ');

                // Build message preview
                const customMsg = document.getElementById('survey-custom-message').value.trim();
                const surveyLink = document.getElementById(`survey-link-${courseId}`).value.trim();
                const fullMessage = customMsg + '\\n\\n' + surveyLink;
                document.getElementById('survey-message-preview').textContent = fullMessage;

                // Reset modal state
                document.getElementById('survey-confirm-section').style.display = 'block';
                document.getElementById('survey-progress-section').style.display = 'none';
                document.getElementById('survey-send-btn').style.display = 'inline-block';
                document.getElementById('survey-send-btn').disabled = false;
                document.getElementById('survey-send-btn').textContent = 'Send Survey to All';

                document.getElementById('survey-modal').classList.add('open');
            } catch (e) {
                alert('Error loading students: ' + e.message);
            }
        }

        function closeSurveyModal() {
            document.getElementById('survey-modal').classList.remove('open');
            surveyStudents = [];
        }

        async function executeSurveyBlast() {
            // Get selected channels
            const useSlack = document.getElementById('survey-channel-slack').checked;
            const useEmail = document.getElementById('survey-channel-email').checked;

            if (!useSlack && !useEmail) {
                alert('Please select at least one channel (Slack or Email)');
                return;
            }

            document.getElementById('survey-confirm-section').style.display = 'none';
            document.getElementById('survey-progress-section').style.display = 'block';
            document.getElementById('survey-send-btn').disabled = true;
            document.getElementById('survey-send-btn').textContent = 'Sending...';

            const customMsg = document.getElementById('survey-custom-message').value.trim();
            const surveyLink = document.getElementById(`survey-link-${surveyCourse}`).value.trim();
            const fullMessage = customMsg + '\\n\\n' + surveyLink;

            const total = surveyStudents.length;
            let completed = 0;
            let successCount = 0;
            let resultsHtml = '';

            for (const student of surveyStudents) {
                try {
                    const resp = await fetch('/api/survey/send', {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({
                            student_name: student.name,
                            course: surveyCourse,
                            message: fullMessage,
                            channels: {slack: useSlack, email: useEmail}
                        })
                    });
                    const data = await resp.json();

                    const slackOk = !useSlack || data.slack_student?.success;
                    const emailOk = !useEmail || data.email_student?.success;
                    const anyOk = (useSlack && data.slack_student?.success) || (useEmail && data.email_student?.success);

                    let statusParts = [];
                    if (useSlack) statusParts.push(data.slack_student?.success ? 'Slack OK' : 'Slack fail');
                    if (useEmail) statusParts.push(data.email_student?.success ? 'Email OK' : 'Email fail');

                    const icon = anyOk ? '&#10004;' : '&#10008;';
                    const color = anyOk ? '#44ff44' : '#ff4444';
                    resultsHtml += `<div style="color: ${color};">${icon} ${student.name}: ${statusParts.join(', ')}</div>`;

                    if (anyOk) successCount++;
                } catch (e) {
                    resultsHtml += `<div style="color: #ff4444;">&#10008; ${student.name}: ${e.message}</div>`;
                }

                completed++;
                const pct = (completed / total) * 100;
                document.getElementById('survey-progress-fill').style.width = pct + '%';
                document.getElementById('survey-progress-text').textContent = `${completed} / ${total}`;
                document.getElementById('survey-results').innerHTML = resultsHtml;

                // Small delay between sends
                await new Promise(r => setTimeout(r, 300));
            }

            document.getElementById('survey-progress-text').textContent = `Done! ${successCount} succeeded, ${total - successCount} failed`;
            document.getElementById('survey-send-btn').textContent = 'Complete';
        }

        // ============================================================
        // SURVEY FOLLOW-UP FUNCTIONS
        // ============================================================

        let followupStudents = [];

        function sendFollowupAll() {
            // Get all students missing survey
            followupStudents = [];
            document.querySelectorAll('[data-followup-student]').forEach(el => {
                followupStudents.push({
                    name: el.dataset.followupStudent,
                    course: el.dataset.followupCourse
                });
            });
            openFollowupModal();
        }

        function sendFollowupCourse(course) {
            // Get students missing survey for specific course
            followupStudents = [];
            document.querySelectorAll(`[data-followup-course="${course}"]`).forEach(el => {
                followupStudents.push({
                    name: el.dataset.followupStudent,
                    course: el.dataset.followupCourse
                });
            });
            openFollowupModal();
        }

        function openFollowupModal() {
            if (followupStudents.length === 0) {
                alert('No students to send follow-up to');
                return;
            }

            const message = document.getElementById('followup-message').value.trim();

            document.getElementById('followup-count').textContent = followupStudents.length;
            document.getElementById('followup-student-list').innerHTML = followupStudents.map(s => `${s.name} (${s.course})`).join(', ');
            document.getElementById('followup-message-preview').textContent = message;

            // Reset to confirm section
            document.getElementById('followup-confirm-section').style.display = 'block';
            document.getElementById('followup-progress-section').style.display = 'none';
            document.getElementById('followup-send-btn').style.display = 'inline-block';
            document.getElementById('followup-send-btn').disabled = false;
            document.getElementById('followup-send-btn').textContent = 'Send Follow-up';

            document.getElementById('followup-modal').classList.add('open');
        }

        function closeFollowupModal() {
            document.getElementById('followup-modal').classList.remove('open');
            followupStudents = [];
        }

        async function saveFollowupMessage() {
            const message = document.getElementById('followup-message').value.trim();
            try {
                const resp = await fetch('/api/survey/save-followup-message', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({message: message})
                });
                const data = await resp.json();
                if (data.success) {
                    alert('Follow-up message saved!');
                } else {
                    alert('Failed to save: ' + data.error);
                }
            } catch (e) {
                alert('Error saving message: ' + e.message);
            }
        }

        async function executeFollowupSend() {
            const message = document.getElementById('followup-message').value.trim();
            if (!message) {
                alert('Please enter a follow-up message');
                return;
            }

            document.getElementById('followup-confirm-section').style.display = 'none';
            document.getElementById('followup-progress-section').style.display = 'block';
            document.getElementById('followup-send-btn').disabled = true;

            const total = followupStudents.length;
            let completed = 0;
            let successCount = 0;
            let resultsHtml = '';

            for (const student of followupStudents) {
                try {
                    const resp = await fetch('/api/comms/send', {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({
                            student_name: student.name,
                            course: student.course,
                            message: message
                        })
                    });
                    const data = await resp.json();

                    const slackOk = data.slack_student?.success;
                    const emailOk = data.email_student?.success;
                    const anyOk = slackOk || emailOk;
                    const icon = anyOk ? '&#10004;' : '&#10008;';
                    const color = anyOk ? '#44ff44' : '#ff4444';
                    resultsHtml += `<div style="color: ${color};">${icon} ${student.name}: Slack ${slackOk ? 'OK' : 'fail'}, Email ${emailOk ? 'OK' : 'fail'}</div>`;

                    if (anyOk) successCount++;
                } catch (e) {
                    resultsHtml += `<div style="color: #ff4444;">&#10008; ${student.name}: ${e.message}</div>`;
                }

                completed++;
                const pct = (completed / total) * 100;
                document.getElementById('followup-progress-fill').style.width = pct + '%';
                document.getElementById('followup-progress-text').textContent = `${completed} / ${total}`;
                document.getElementById('followup-results').innerHTML = resultsHtml;

                // Small delay between sends
                await new Promise(r => setTimeout(r, 300));
            }

            document.getElementById('followup-progress-text').textContent = `Done! ${successCount} succeeded, ${total - successCount} failed`;
            document.getElementById('followup-send-btn').textContent = 'Complete';
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

    # Survey blast data
    survey_links = config.get('survey_links', {})
    survey_message = config.get('survey_message', 'Hi! Please take 5 minutes to complete this quick survey about how you\'re feeling about the upcoming AP exam. Your honest answers help us focus our prep sessions on what you need most.')

    # Count students per course
    student_counts = {}
    for s in students:
        course = s.get('course', '').lower()
        # Normalize course names
        if course in ['aphg', 'ap human geography']:
            course = 'aphg'
        elif course in ['apgov', 'ap gov', 'ap government']:
            course = 'apgov'
        elif course in ['apush', 'ap us history']:
            course = 'apush'
        elif course in ['apworld', 'ap world', 'apwh', 'ap world history']:
            course = 'apworld'
        student_counts[course] = student_counts.get(course, 0) + 1

    # Survey follow-up: students who haven't completed the survey
    surveys = load_self_assessment_data()
    survey_missing = get_students_without_survey(students, surveys)
    survey_missing_total = sum(len(v) for v in survey_missing.values())
    survey_completed_total = len(surveys)
    survey_followup_message = config.get('survey_followup_message', "Hey! Just a quick reminder - we haven't received your AP exam comfort survey yet. It only takes 5 minutes and helps us customize your prep sessions.\n\nPlease complete it when you get a chance - your input really helps!")

    return render_template_string(COMMS_HTML,
                                  by_recommendation=by_rec,
                                  openai_configured=openai_configured,
                                  survey_links=survey_links,
                                  survey_message=survey_message,
                                  student_counts=student_counts,
                                  survey_missing=survey_missing,
                                  survey_missing_total=survey_missing_total,
                                  survey_completed_total=survey_completed_total,
                                  survey_followup_message=survey_followup_message)


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
# SURVEY BLAST API
# =============================================================================

@app.route('/api/survey/save-link', methods=['POST'])
def api_survey_save_link():
    """Save a survey link for a course."""
    req = request.json
    course = req.get('course', '').lower()
    link = req.get('link', '').strip()

    if course not in ['aphg', 'apgov', 'apush', 'apworld']:
        return jsonify({'success': False, 'error': 'Invalid course'}), 400

    config = load_config()
    if 'survey_links' not in config:
        config['survey_links'] = {}
    config['survey_links'][course] = link
    save_config(config)

    return jsonify({'success': True})


@app.route('/api/survey/save-message', methods=['POST'])
def api_survey_save_message():
    """Save the custom survey message."""
    req = request.json
    message = req.get('message', '').strip()

    config = load_config()
    config['survey_message'] = message
    save_config(config)

    return jsonify({'success': True})


@app.route('/api/survey/save-followup-message', methods=['POST'])
def api_survey_save_followup_message():
    """Save the follow-up survey reminder message."""
    req = request.json
    message = req.get('message', '').strip()

    config = load_config()
    config['survey_followup_message'] = message
    save_config(config)

    return jsonify({'success': True})


@app.route('/api/survey/students/<course>')
def api_survey_students(course):
    """Get list of students for a course."""
    course = course.lower()

    data = load_all_data()
    students = build_unified_table(data)

    # Filter by course (normalize course names)
    course_students = []
    for s in students:
        student_course = s.get('course', '').lower()
        # Normalize
        if student_course in ['aphg', 'ap human geography']:
            student_course = 'aphg'
        elif student_course in ['apgov', 'ap gov', 'ap government']:
            student_course = 'apgov'
        elif student_course in ['apush', 'ap us history']:
            student_course = 'apush'
        elif student_course in ['apworld', 'ap world', 'apwh', 'ap world history']:
            student_course = 'apworld'

        if student_course == course:
            course_students.append({
                'name': s.get('student', ''),
                'email': s.get('email', ''),
                'course': s.get('course', '')
            })

    return jsonify({'students': course_students})


@app.route('/api/survey/send', methods=['POST'])
def api_survey_send():
    """Send survey message to a student via selected channels."""
    req = request.json
    student_name = req.get('student_name')
    course = req.get('course')
    message_text = req.get('message')
    channels = req.get('channels', {'slack': True, 'email': True})

    if not message_text:
        return jsonify({'error': 'No message provided'}), 400

    # Load student data
    data = load_all_data()
    students = build_unified_table(data)

    # Find student (more flexible matching)
    student = None
    for s in students:
        if s.get('student') == student_name:
            student_course = s.get('course', '').lower()
            # Normalize course for comparison
            if student_course in ['aphg', 'ap human geography']:
                student_course = 'aphg'
            elif student_course in ['apgov', 'ap gov', 'ap government']:
                student_course = 'apgov'
            elif student_course in ['apush', 'ap us history']:
                student_course = 'apush'
            elif student_course in ['apworld', 'ap world', 'apwh', 'ap world history']:
                student_course = 'apworld'

            if student_course == course.lower():
                student = s
                break

    if not student:
        return jsonify({'error': 'Student not found'}), 404

    # Send to selected channels only
    results = {
        'slack_student': {'success': False, 'message': 'Not selected'},
        'email_student': {'success': False, 'message': 'Not selected'}
    }

    # Get student email
    student_info = STUDENTS.get(student_name, {})
    student_email = student_info.get('email', '')
    if not student_email and student_name:
        student_email = student_name.lower().replace(' ', '.') + '@alpha.school'

    # Send via Slack if selected
    if channels.get('slack') and student_email:
        success, msg = send_slack_dm(student_email, message_text)
        results['slack_student'] = {'success': success, 'message': msg}

    # Send via Email if selected
    if channels.get('email') and student_email:
        course_name = student.get('course', 'AP Course')
        subject = f"{course_name} - Comfort Survey"
        html_body = message_text.replace('\n\n', '<br><br>').replace('\n', '<br>')
        success, msg = send_email(student_email, subject, message_text, html_body)
        results['email_student'] = {'success': success, 'message': msg}

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
        <a href="/planner">Planner</a>
        <a href="/external-scheduler">External</a>
        <a href="/coaching">Coaching</a>
        <a href="/comms">Communications</a>
        <a href="/refresh">Refresh Data</a>
        <a href="/student-logs">Student Logs</a>
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
