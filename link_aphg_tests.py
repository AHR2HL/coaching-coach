#!/usr/bin/env python3
"""Link APHG tests to courses."""

import json
import requests
from datetime import datetime, timedelta
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# Parse .env
with open('.env') as f:
    lines = f.read().strip().split('\n')
json_lines = [l for l in lines if l.strip().startswith('"')]
content = '{' + ''.join(json_lines) + '}'
creds = json.loads(content)

QTI_BASE = 'https://qti.alpha-1edtech.ai/api'
ONEROSTER_BASE = 'https://api.alpha-1edtech.ai'
TOKEN_URL = 'https://prod-beyond-timeback-api-2-idp.auth.us-east-1.amazoncognito.com/oauth2/token'

# Course IDs just created
COURSES = {
    1: {'id': 'APHG-PT1-2026-7a0c9fd2', 'code': 'APHG-PT1-2026', 'title': 'APHG Practice Test 1 2026'},
    2: {'id': 'APHG-PT2-2026-929bdc56', 'code': 'APHG-PT2-2026', 'title': 'APHG Practice Test 2 2026'},
}

# MCQ tests already created
MCQ_TESTS = {
    1: 'aphg-pt1-mcq-722152e3',
    2: 'aphg-pt2-mcq-6f075c05',
}

# Instructions
INSTRUCTIONS = {
    'sec1': {
        'title': 'Section I: Multiple Choice Instructions',
        'content': '<h2>Section I: Multiple-Choice Questions</h2><p><b>Time:</b> 60 minutes</p><p><b>Questions:</b> 60 questions</p><p><b>Weight:</b> 50% of your exam score</p><p>---</p><p>Answer all 60 questions. Pace yourself at 1 minute per question. No penalty for guessing.</p><p><b>Good luck!</b></p>'
    },
    'sec2': {
        'title': 'Section II: Free Response Instructions',
        'content': '<h2>Section II: Free-Response Questions</h2><p><b>Time:</b> 75 minutes</p><p><b>Questions:</b> 3 questions (answer all)</p><p><b>Weight:</b> 50% of your exam score</p><p>---</p><p>Answer all 3 questions. Each has 7 parts (A-G). Question 1 has a stimulus. Questions 2-3 have no stimulus. Spend about 25 minutes per question.</p><p><b>Good luck!</b></p>'
    },
}


class TimebackAuth:
    def __init__(self, client_id, client_secret):
        self.client_id = client_id
        self.client_secret = client_secret
        self._token = None
        self._expires_at = None

    def get_headers(self):
        if not self._token or datetime.now() >= self._expires_at:
            self._refresh()
        return {'Authorization': f'Bearer {self._token}', 'Content-Type': 'application/json'}

    def _refresh(self):
        resp = requests.post(
            TOKEN_URL,
            headers={'Content-Type': 'application/x-www-form-urlencoded'},
            data={'grant_type': 'client_credentials', 'client_id': self.client_id, 'client_secret': self.client_secret},
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        self._token = data['access_token']
        self._expires_at = datetime.now() + timedelta(seconds=data['expires_in'] - 300)


def make_session():
    session = requests.Session()
    retry = Retry(total=3, backoff_factor=1, status_forcelist=[429, 500, 502, 503, 504])
    session.mount('https://', HTTPAdapter(max_retries=retry))
    return session


session = make_session()
auth = TimebackAuth(creds['client_id'], creds['client_secret'])

# Load inventory to get actual FRQ test IDs
with open('uworld_aphg_inventory.json') as f:
    inventory = json.load(f)

# Map FRQ titles to actual test IDs
frq_lookup = {}
for f in inventory['frq_with_stim'] + inventory['frq_no_stim']:
    frq_lookup[f['title']] = f['test_id']

print('FRQ test IDs:')
for title, tid in frq_lookup.items():
    print(f'  {title}: {tid}')

# FRQs with actual test IDs
FRQS = {
    1: [
        {'test_id': frq_lookup['Unit6 - Two Stimuli'], 'title': 'Unit6 - Two Stimuli'},
        {'test_id': frq_lookup['Unit3 - No Stimuli'], 'title': 'Unit3 - No Stimuli'},
        {'test_id': frq_lookup['Unit1 - No Stimuli'], 'title': 'Unit1 - No Stimuli'},
    ],
    2: [
        {'test_id': frq_lookup['Unit4 - One Stimulus'], 'title': 'Unit4 - One Stimulus'},
        {'test_id': frq_lookup['Unit7 - No Stimuli'], 'title': 'Unit7 - No Stimuli'},
        {'test_id': frq_lookup['Unit2 - No Stimuli'], 'title': 'Unit2 - No Stimuli'},
    ],
}


def create_component(comp_id, title, course_id, sort_order):
    payload = {
        'courseComponent': {
            'sourcedId': comp_id, 'status': 'active', 'title': title, 'sortOrder': sort_order,
            'courseSourcedId': course_id, 'course': {'sourcedId': course_id},
            'parent': None, 'prerequisites': [], 'prerequisiteCriteria': 'ALL', 'metadata': {}
        }
    }
    resp = session.post(
        f'{ONEROSTER_BASE}/ims/oneroster/rostering/v1p2/courses/components',
        headers=auth.get_headers(), json=payload, timeout=30
    )
    return comp_id if resp.status_code in (200, 201, 409) else None


def create_resource(res_id, title, test_id, is_article=False):
    if is_article:
        metadata = {
            'type': 'qti', 'subType': 'qti-stimulus', 'language': 'en-US',
            'lessonType': 'alpha-read-article', 'assessmentType': 'alpha-read',
            'allowRetake': True, 'displayType': 'interactive', 'showResults': True,
            'url': f'{QTI_BASE}/stimuli/{test_id}', 'xp': 0
        }
    else:
        metadata = {
            'type': 'qti', 'subType': 'qti-test', 'questionType': 'custom', 'language': 'en-US',
            'lessonType': 'quiz', 'assessmentType': 'quiz', 'allowRetake': True,
            'displayType': 'interactive', 'showResults': True,
            'url': f'{QTI_BASE}/assessment-tests/{test_id}', 'xp': 100
        }
    payload = {
        'resource': {
            'sourcedId': res_id, 'status': 'active', 'title': title,
            'metadata': metadata,
            'roles': ['primary'], 'importance': 'primary',
            'vendorResourceId': test_id, 'vendorId': 'alpha-incept', 'applicationId': 'incept'
        }
    }
    resp = session.post(
        f'{ONEROSTER_BASE}/ims/oneroster/resources/v1p2/resources/',
        headers=auth.get_headers(), json=payload, timeout=30
    )
    return res_id if resp.status_code in (200, 201, 409) else None


def link_resource(cr_id, title, comp_id, res_id, sort_order, is_article=False):
    payload = {
        'componentResource': {
            'sourcedId': cr_id, 'status': 'active', 'title': title, 'sortOrder': sort_order,
            'courseComponent': {'sourcedId': comp_id}, 'resource': {'sourcedId': res_id},
            'lessonType': 'alpha-read-article' if is_article else 'quiz'
        }
    }
    resp = session.post(
        f'{ONEROSTER_BASE}/ims/oneroster/rostering/v1p2/courses/component-resources',
        headers=auth.get_headers(), json=payload, timeout=30
    )
    return cr_id if resp.status_code in (200, 201, 409) else None


def create_stimulus(stim_id, title, content):
    payload = {'identifier': stim_id, 'title': title, 'content': content}
    resp = session.post(f'{QTI_BASE}/stimuli', headers=auth.get_headers(), json=payload, timeout=30)
    return stim_id if resp.status_code in (200, 201, 409) else None


results = []

for test_num in [1, 2]:
    print(f'\n{"="*50}')
    print(f'Linking Test {test_num}')
    print(f'{"="*50}')

    course = COURSES[test_num]
    course_id = course['id']
    uid = course_id.split('-')[-1]

    # Section I: MCQ
    print('\nSection I (MCQ)...')
    sec1_comp_id = f'aphg-pt{test_num}-sec1-{uid}'
    create_component(sec1_comp_id, 'Section I: Multiple Choice (60 questions, 60 min)', course_id, 1)

    # MCQ instructions
    instr = INSTRUCTIONS['sec1']
    stim_id = f'aphg-pt{test_num}-sec1-instr-{uid}'
    create_stimulus(stim_id, instr['title'], instr['content'])
    instr_res_id = f'aphg-pt{test_num}-sec1-instr-res-{uid}'
    create_resource(instr_res_id, instr['title'], stim_id, is_article=True)
    link_resource(f'aphg-pt{test_num}-sec1-instr-cr-{uid}', instr['title'], sec1_comp_id, instr_res_id, 0, is_article=True)

    # MCQ test
    mcq_test_id = MCQ_TESTS[test_num]
    mcq_res_id = f'aphg-pt{test_num}-mcq-res-{uid}'
    create_resource(mcq_res_id, 'Multiple Choice Questions', mcq_test_id)
    link_resource(f'aphg-pt{test_num}-mcq-cr-{uid}', 'Multiple Choice Questions', sec1_comp_id, mcq_res_id, 1)
    print('  MCQ linked')

    # Section II: FRQ
    print('\nSection II (FRQ)...')
    sec2_comp_id = f'aphg-pt{test_num}-sec2-{uid}'
    create_component(sec2_comp_id, 'Section II: Free Response (3 questions, 75 min)', course_id, 2)

    # FRQ instructions
    instr = INSTRUCTIONS['sec2']
    stim_id = f'aphg-pt{test_num}-sec2-instr-{uid}'
    create_stimulus(stim_id, instr['title'], instr['content'])
    instr_res_id = f'aphg-pt{test_num}-sec2-instr-res-{uid}'
    create_resource(instr_res_id, instr['title'], stim_id, is_article=True)
    link_resource(f'aphg-pt{test_num}-sec2-instr-cr-{uid}', instr['title'], sec2_comp_id, instr_res_id, 0, is_article=True)

    # FRQs
    for i, frq in enumerate(FRQS[test_num], 1):
        frq_title = f'FRQ {i}: {frq["title"]}'
        frq_res_id = f'aphg-pt{test_num}-frq{i}-res-{uid}'
        create_resource(frq_res_id, frq_title, frq['test_id'])
        link_resource(f'aphg-pt{test_num}-frq{i}-cr-{uid}', frq_title, sec2_comp_id, frq_res_id, i)
        print(f'  FRQ {i} linked: {frq["title"]}')

    results.append({
        'test_num': test_num,
        'course_id': course_id,
        'course_code': course['code'],
        'course_title': course['title'],
    })

print('\n' + '=' * 50)
print('COMPLETE!')
print('=' * 50)

for r in results:
    print(f'{r["course_title"]}')
    print(f'  Course ID: {r["course_id"]}')
    print(f'  Course Code: {r["course_code"]}')

# Update CSV
with open('practice_tests.csv', 'r') as f:
    existing = f.read()

with open('practice_tests.csv', 'w') as f:
    f.write(existing.strip() + '\n')
    for r in results:
        f.write(f'APHG Practice Test {r["test_num"]},{r["course_title"]},{r["course_code"]},{r["course_id"]}\n')

print('\nCSV updated: practice_tests.csv')
