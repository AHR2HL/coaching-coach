# Austin Way (AP History Forge) API Guide

> **What is this?** Austin Way is a mastery-tracking platform for AP History courses (APUSH, AP World, AP Euro, APHG). This guide shows you how to pull student progress data from its API.
>
> **Last updated**: 2026-03-27

---

## Quick Start

```python
import requests

# 1. Get your auth cookie (see "Authentication" below)
AUTH_TOKEN = "your_aph_auth_cookie_value"

# 2. Set up session
session = requests.Session()
session.cookies.set('aph_auth', AUTH_TOKEN, domain='api.aphistoryforge.com')
session.headers.update({
    'Accept': '*/*',
    'Origin': 'https://www.aphistoryforge.com',
    'Referer': 'https://www.aphistoryforge.com/',
})

# 3. Get all your students
students = session.get('https://api.aphistoryforge.com/api/guide/dashboard').json()['students']

# 4. Get detailed data for each student
for student in students:
    details = session.get(
        f'https://api.aphistoryforge.com/api/guide/students/{student["id"]}',
        params={'days': 30}
    ).json()
    print(f"{student['displayName']}: {details['mastery']['overallPct']}% mastery")
```

---

## Table of Contents

1. [Authentication](#1-authentication)
2. [API Base URL](#2-api-base-url)
3. [Get All Students (Dashboard)](#3-get-all-students-dashboard)
4. [Get Student Details](#4-get-student-details)
5. [Response Data Structures](#5-response-data-structures)
6. [Complete Python Example](#6-complete-python-example)
7. [Troubleshooting](#7-troubleshooting)

---

## 1. Authentication

Austin Way uses **cookie-based authentication**. You need the `aph_auth` cookie from your browser session.

### Getting the Cookie (Manual Method)

1. Go to https://www.aphistoryforge.com/guide/
2. Sign in with your Austin Way account
3. Open DevTools (F12)
4. Go to **Application** tab (Chrome) or **Storage** tab (Firefox)
5. Click **Cookies** > `api.aphistoryforge.com`
6. Find the cookie named `aph_auth`
7. Copy its **Value** (it's a long JWT token starting with `eyJ...`)

### Cookie Lifetime

The `aph_auth` cookie is a JWT that expires periodically. If your requests start returning `401 Unauthorized`, you need to:
1. Sign into aphistoryforge.com again in your browser
2. Get a fresh cookie value

### Using the Cookie in Python

```python
import requests

session = requests.Session()

# Set the auth cookie
session.cookies.set('aph_auth', 'YOUR_TOKEN_HERE', domain='api.aphistoryforge.com')

# Set headers to mimic browser requests
session.headers.update({
    'Accept': '*/*',
    'Accept-Language': 'en-GB,en-US;q=0.9,en;q=0.8',
    'Origin': 'https://www.aphistoryforge.com',
    'Referer': 'https://www.aphistoryforge.com/',
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
})
```

---

## 2. API Base URL

```
https://api.aphistoryforge.com/api/guide
```

All endpoints are relative to this base.

---

## 3. Get All Students (Dashboard)

Returns a list of all students you have access to (based on your account's classes/sections).

### Request

```
GET https://api.aphistoryforge.com/api/guide/dashboard
```

### Response

```json
{
  "students": [
    {
      "id": "abc123-def456-...",
      "displayName": "John Smith",
      "email": "jsmith@school.edu",
      "courses": ["apush", "aphg"]
    },
    {
      "id": "xyz789-uvw012-...",
      "displayName": "Jane Doe",
      "email": "jdoe@school.edu",
      "courses": ["apworld"]
    }
  ]
}
```

### Field Reference

| Field | Type | Description |
|-------|------|-------------|
| `id` | string | Unique student ID (UUID format) |
| `displayName` | string | Student's display name |
| `email` | string | Student's email address |
| `courses` | array | List of course IDs the student is enrolled in |

### Course ID Values

| Course ID | Full Name |
|-----------|-----------|
| `apush` | AP US History |
| `apworld` | AP World History |
| `apeuro` | AP European History |
| `aphg` | AP Human Geography |

---

## 4. Get Student Details

Returns detailed mastery data for a single student, including per-unit skill breakdown.

### Request

```
GET https://api.aphistoryforge.com/api/guide/students/{student_id}
```

### Query Parameters

| Parameter | Required | Default | Description |
|-----------|----------|---------|-------------|
| `days` | No | 30 | Number of days of historical data to include |

### Example

```
GET https://api.aphistoryforge.com/api/guide/students/abc123-def456?days=30
```

### Response

```json
{
  "student": {
    "id": "abc123-def456-...",
    "displayName": "John Smith",
    "email": "jsmith@school.edu",
    "courses": ["apush"]
  },
  "mastery": {
    "overallPct": 72.5
  },
  "skillBreakdown": [
    {
      "courseId": "apush",
      "unitId": "unit-1",
      "unitName": "Period 1: 1491-1607",
      "mastered": 8,
      "inProgress": 4,
      "notLearned": 3,
      "total": 15
    },
    {
      "courseId": "apush",
      "unitId": "unit-2",
      "unitName": "Period 2: 1607-1754",
      "mastered": 12,
      "inProgress": 6,
      "notLearned": 2,
      "total": 20
    }
  ],
  "masteryOverTime": [
    {
      "date": "2026-03-20",
      "courseId": "apush",
      "averagePct": 68.2,
      "totalSkills": 150,
      "masteredSkills": 102
    },
    {
      "date": "2026-03-27",
      "courseId": "apush",
      "averagePct": 72.5,
      "totalSkills": 150,
      "masteredSkills": 109
    }
  ]
}
```

---

## 5. Response Data Structures

### Student Object

```json
{
  "id": "string (UUID)",
  "displayName": "string",
  "email": "string",
  "courses": ["string", ...]
}
```

### Mastery Summary

```json
{
  "overallPct": 72.5  // Overall mastery percentage (0-100)
}
```

### Skill Breakdown (Per Unit)

```json
{
  "courseId": "apush",           // Which course this unit belongs to
  "unitId": "unit-1",            // Internal unit identifier
  "unitName": "Period 1: ...",   // Human-readable unit name
  "mastered": 8,                 // Skills fully mastered
  "inProgress": 4,               // Skills partially learned
  "notLearned": 3,               // Skills not yet attempted
  "total": 15                    // Total skills in this unit
}
```

**Calculating unit mastery percentage:**
```python
unit_mastery_pct = (mastered / total) * 100 if total > 0 else 0
```

### Mastery Over Time

```json
{
  "date": "2026-03-20",          // ISO date string
  "courseId": "apush",           // Course this snapshot is for
  "averagePct": 68.2,            // Mastery % on this date
  "totalSkills": 150,            // Total skills in course
  "masteredSkills": 102          // Skills mastered by this date
}
```

This array is ordered chronologically. The last entry represents the most recent state.

---

## 6. Complete Python Example

This script fetches all students and their detailed mastery data, then outputs to CSV.

```python
#!/usr/bin/env python3
"""
Austin Way Data Fetcher

Pulls student mastery data from the Austin Way (AP History Forge) API.

Usage:
    1. Set AUTH_TOKEN to your aph_auth cookie value
    2. Run: python austin_way_fetch.py
    3. Output: austin_way_mastery.csv
"""

import csv
import time
import requests
from pathlib import Path

# ============================================================
# CONFIGURATION - Edit this!
# ============================================================

# Get this from your browser cookies after signing into aphistoryforge.com
AUTH_TOKEN = "YOUR_APH_AUTH_COOKIE_HERE"

# Output file
OUTPUT_FILE = Path("austin_way_mastery.csv")

# ============================================================
# API SETUP
# ============================================================

API_BASE = "https://api.aphistoryforge.com/api/guide"

HEADERS = {
    'Accept': '*/*',
    'Accept-Language': 'en-GB,en-US;q=0.9,en;q=0.8',
    'Origin': 'https://www.aphistoryforge.com',
    'Referer': 'https://www.aphistoryforge.com/',
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
}


def create_session(auth_token):
    """Create a requests session with auth cookie set."""
    session = requests.Session()
    session.headers.update(HEADERS)
    session.cookies.set('aph_auth', auth_token, domain='api.aphistoryforge.com')
    return session


def get_all_students(session):
    """Fetch list of all students from dashboard endpoint."""
    resp = session.get(f'{API_BASE}/dashboard')
    resp.raise_for_status()
    return resp.json().get('students', [])


def get_student_details(session, student_id, days=30):
    """Fetch detailed mastery data for a single student."""
    resp = session.get(f'{API_BASE}/students/{student_id}', params={'days': days})
    resp.raise_for_status()
    return resp.json()


def parse_student_data(data):
    """
    Parse the student details response into flat rows for CSV.
    Returns one row per unit per student.
    """
    rows = []

    student = data.get('student', {})
    student_name = student.get('displayName', 'Unknown')
    student_email = student.get('email', '')
    student_courses = student.get('courses', [])

    mastery = data.get('mastery', {})
    overall_pct = mastery.get('overallPct', 0)

    skill_breakdown = data.get('skillBreakdown', [])

    # Build a lookup for most recent per-course mastery from masteryOverTime
    mastery_over_time = data.get('masteryOverTime', [])
    course_mastery = {}
    for entry in mastery_over_time:
        course_id = entry.get('courseId')
        if course_id:
            # Keep overwriting - last entry is most recent
            course_mastery[course_id] = {
                'averagePct': entry.get('averagePct', 0),
                'totalSkills': entry.get('totalSkills', 0),
                'masteredSkills': entry.get('masteredSkills', 0),
            }

    # If no skill breakdown data, create a summary row per course
    if not skill_breakdown:
        for course in student_courses:
            cm = course_mastery.get(course, {})
            rows.append({
                'student_name': student_name,
                'student_email': student_email,
                'course': course,
                'unit_id': '',
                'unit_name': '(No unit data)',
                'mastered': 0,
                'in_progress': 0,
                'not_learned': 0,
                'total': 0,
                'unit_mastery_pct': 0,
                'course_mastery_pct': cm.get('averagePct', 0),
                'overall_mastery_pct': overall_pct,
            })
        return rows

    # Process each unit in skill breakdown
    for unit in skill_breakdown:
        course_id = unit.get('courseId', '')
        unit_id = unit.get('unitId', '')
        unit_name = unit.get('unitName', '')
        mastered = unit.get('mastered', 0)
        in_progress = unit.get('inProgress', 0)
        not_learned = unit.get('notLearned', 0)
        total = unit.get('total', 0)

        # Calculate unit mastery percentage
        unit_mastery_pct = round((mastered / total) * 100, 1) if total > 0 else 0

        # Get course-level mastery
        cm = course_mastery.get(course_id, {})

        rows.append({
            'student_name': student_name,
            'student_email': student_email,
            'course': course_id,
            'unit_id': unit_id,
            'unit_name': unit_name,
            'mastered': mastered,
            'in_progress': in_progress,
            'not_learned': not_learned,
            'total': total,
            'unit_mastery_pct': unit_mastery_pct,
            'course_mastery_pct': cm.get('averagePct', 0),
            'overall_mastery_pct': overall_pct,
        })

    return rows


def main():
    print("Austin Way Data Fetcher")
    print("=" * 50)

    # Validate token
    if AUTH_TOKEN == "YOUR_APH_AUTH_COOKIE_HERE":
        print("ERROR: Please set AUTH_TOKEN to your aph_auth cookie value!")
        print("\nTo get your cookie:")
        print("1. Go to https://www.aphistoryforge.com/guide/")
        print("2. Sign in")
        print("3. Open DevTools (F12) > Application > Cookies")
        print("4. Copy the value of 'aph_auth'")
        return

    # Create session
    session = create_session(AUTH_TOKEN)

    # Fetch student list
    print("\nFetching student list...")
    try:
        students = get_all_students(session)
    except requests.exceptions.HTTPError as e:
        if e.response.status_code == 401:
            print("ERROR: Authentication failed (401)")
            print("Your cookie has expired. Get a fresh one from your browser.")
        else:
            print(f"ERROR: {e}")
        return

    print(f"Found {len(students)} students")

    # Fetch details for each student
    all_rows = []
    for i, student in enumerate(students):
        student_id = student.get('id')
        student_name = student.get('displayName', 'Unknown')

        print(f"  [{i+1}/{len(students)}] {student_name}...", end=' ', flush=True)

        try:
            details = get_student_details(session, student_id, days=30)
            rows = parse_student_data(details)
            all_rows.extend(rows)
            print(f"OK ({len(rows)} units)")
        except requests.exceptions.HTTPError as e:
            print(f"ERROR: {e}")

        # Rate limiting - be nice to the API
        time.sleep(0.2)

    # Write CSV
    if not all_rows:
        print("\nNo data to write!")
        return

    fieldnames = [
        'student_name', 'student_email', 'course', 'unit_id', 'unit_name',
        'mastered', 'in_progress', 'not_learned', 'total',
        'unit_mastery_pct', 'course_mastery_pct', 'overall_mastery_pct'
    ]

    with open(OUTPUT_FILE, 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(all_rows)

    print(f"\nWrote {len(all_rows)} rows to {OUTPUT_FILE}")

    # Print summary
    print("\n" + "=" * 50)
    print("SUMMARY BY COURSE")
    print("=" * 50)

    from collections import defaultdict
    course_stats = defaultdict(lambda: {'students': set(), 'total_mastered': 0, 'total_skills': 0})

    for row in all_rows:
        course = row['course']
        course_stats[course]['students'].add(row['student_email'])
        course_stats[course]['total_mastered'] += row['mastered']
        course_stats[course]['total_skills'] += row['total']

    for course, stats in sorted(course_stats.items()):
        n_students = len(stats['students'])
        avg_mastery = round((stats['total_mastered'] / stats['total_skills']) * 100, 1) if stats['total_skills'] > 0 else 0
        print(f"  {course}: {n_students} students, {avg_mastery}% average mastery")


if __name__ == '__main__':
    main()
```

---

## 7. Troubleshooting

### 401 Unauthorized

**Cause**: Your `aph_auth` cookie has expired.

**Fix**:
1. Sign into https://www.aphistoryforge.com/guide/ in your browser
2. Get a fresh cookie from DevTools
3. Update your script with the new token

### 403 Forbidden

**Cause**: You don't have access to the requested student, or the API endpoint has changed.

**Fix**: Check that you're fetching students that are in your classes/sections.

### Empty Student List

**Cause**: Your account may not have any students assigned, or you're signed into the wrong account type.

**Fix**: Make sure you're signed in as a teacher/tutor account, not a student account.

### Connection Errors

**Cause**: Network issues or the API is temporarily down.

**Fix**: Add retry logic:

```python
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

session = requests.Session()
retry = Retry(total=3, backoff_factor=1, status_forcelist=[429, 500, 502, 503, 504])
session.mount('https://', HTTPAdapter(max_retries=retry))
```

### CORS Errors (if calling from browser)

The Austin Way API doesn't allow direct browser requests from other origins. You must call it from a backend (Python, Node.js, etc.), not from client-side JavaScript.

---

## API Endpoint Summary

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/guide/dashboard` | GET | List all students you have access to |
| `/api/guide/students/{id}` | GET | Get detailed mastery data for one student |

### Query Parameters

| Endpoint | Parameter | Type | Default | Description |
|----------|-----------|------|---------|-------------|
| `/students/{id}` | `days` | int | 30 | Days of history to include |

---

## CSV Output Format

The complete script outputs a CSV with these columns:

| Column | Description |
|--------|-------------|
| `student_name` | Student's display name |
| `student_email` | Student's email |
| `course` | Course ID (apush, apworld, apeuro, aphg) |
| `unit_id` | Internal unit identifier |
| `unit_name` | Human-readable unit name |
| `mastered` | Number of skills fully mastered |
| `in_progress` | Number of skills partially learned |
| `not_learned` | Number of skills not attempted |
| `total` | Total skills in unit |
| `unit_mastery_pct` | Unit mastery % (mastered/total * 100) |
| `course_mastery_pct` | Overall course mastery % |
| `overall_mastery_pct` | Cross-course mastery % |

---

*This guide documents the Austin Way (AP History Forge) API as observed from browser network traffic. It's not an official API - endpoints may change without notice.*
