#!/usr/bin/env python3
"""Build 2 APHG Practice Tests on Timeback."""

import json
import uuid
import random
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

CLIENT_ID = creds['client_id']
CLIENT_SECRET = creds['client_secret']
print(f'Using credentials for: {creds["owner_name"]}')

QTI_BASE = "https://qti.alpha-1edtech.ai/api"
ONEROSTER_BASE = "https://api.alpha-1edtech.ai"
TOKEN_URL = "https://prod-beyond-timeback-api-2-idp.auth.us-east-1.amazoncognito.com/oauth2/token"
ORG_SOURCED_ID = "346488d3-efb9-4f56-95ea-f4a441de2370"

# APHG MCQ Distribution (60 questions)
# Unit 1: 8-10% = 5-6 questions
# Units 2-7: 12-17% = 7-10 questions each
MCQ_DISTRIBUTION = {
    '1': 5,   # 8-10%
    '2': 9,   # 12-17%
    '3': 9,   # 12-17%
    '4': 9,   # 12-17%
    '5': 10,  # 12-17%
    '6': 9,   # 12-17%
    '7': 9,   # 12-17%
    # Total: 60
}

# Instructions for each section
INSTRUCTIONS = {
    'sec1': {
        'title': 'Section I: Multiple Choice Instructions',
        'content': """<h2>Section I: Multiple-Choice Questions</h2>
<p><b>Time:</b> 60 minutes</p>
<p><b>Questions:</b> 60 questions</p>
<p><b>Weight:</b> 50% of your exam score</p>
<p>---</p>
<h3>Instructions</h3>
<p>Answer <b>all 60 questions</b>. Each question has four answer choices. Select the best answer.</p>
<h3>Content Coverage</h3>
<p>Unit 1 (Thinking Geographically): 8-10%</p>
<p>Unit 2 (Population and Migration): 12-17%</p>
<p>Unit 3 (Cultural Patterns): 12-17%</p>
<p>Unit 4 (Political Patterns): 12-17%</p>
<p>Unit 5 (Agriculture): 12-17%</p>
<p>Unit 6 (Cities and Urban Land Use): 12-17%</p>
<p>Unit 7 (Industrialization and Development): 12-17%</p>
<h3>Tips</h3>
<p>Pace yourself at 1 minute per question. At least 25% of questions include maps or spatial data. No penalty for guessing.</p>
<p><b>Good luck!</b></p>"""
    },
    'sec2': {
        'title': 'Section II: Free Response Instructions',
        'content': """<h2>Section II: Free-Response Questions</h2>
<p><b>Time:</b> 75 minutes</p>
<p><b>Questions:</b> 3 questions (answer all)</p>
<p><b>Weight:</b> 50% of your exam score</p>
<p>---</p>
<h3>Instructions</h3>
<p>Answer <b>all 3 questions</b>. Each question has 7 parts (A through G).</p>
<p>Question 1: Based on a stimulus (map, graph, image, or table)</p>
<p>Questions 2-3: No stimulus provided</p>
<h3>Tips</h3>
<p>Spend about 25 minutes per question. Answer each part separately and clearly label your responses (A, B, C, etc.). Use specific geographic concepts, examples, and vocabulary.</p>
<p>For stimulus-based questions, refer directly to the provided data in your answers.</p>
<p><b>Good luck!</b></p>"""
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
        return {"Authorization": f"Bearer {self._token}", "Content-Type": "application/json"}

    def _refresh(self):
        resp = requests.post(
            TOKEN_URL,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data={"grant_type": "client_credentials", "client_id": self.client_id, "client_secret": self.client_secret},
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        self._token = data["access_token"]
        self._expires_at = datetime.now() + timedelta(seconds=data["expires_in"] - 300)


def make_session():
    session = requests.Session()
    retry = Retry(total=3, backoff_factor=1, status_forcelist=[429, 500, 502, 503, 504])
    session.mount("https://", HTTPAdapter(max_retries=retry))
    return session


session = make_session()
auth = TimebackAuth(CLIENT_ID, CLIENT_SECRET)

# Load APHG inventory
with open('uworld_aphg_inventory.json') as f:
    inventory = json.load(f)

mcq_by_unit = inventory['mcq_by_unit']
frq_with_stim = inventory['frq_with_stim']
frq_no_stim = inventory['frq_no_stim']

used_mcq_ids = set()
used_frq_ids = set()


def select_mcqs(test_num):
    selected = []
    for unit, count in MCQ_DISTRIBUTION.items():
        available = [item for item in mcq_by_unit.get(unit, []) if item['item_id'] not in used_mcq_ids]
        chosen = random.sample(available, min(count, len(available)))
        selected.extend(chosen)
        for item in chosen:
            used_mcq_ids.add(item['item_id'])

    print(f"  MCQ: {len(selected)}")
    return selected


def select_frqs(test_num):
    """Select 3 FRQs: 1 with stimulus, 2 without. Cover at least 3 units."""
    selected = []
    units_used = set()

    # Select 1 FRQ with stimulus
    available_stim = [f for f in frq_with_stim if f['test_id'] not in used_frq_ids]
    if available_stim:
        chosen = random.choice(available_stim)
        selected.append(chosen)
        used_frq_ids.add(chosen['test_id'])
        units_used.add(chosen['unit'])
        print(f"  FRQ (stimulus): Unit {chosen['unit']} - {chosen['title']}")

    # Select 2 FRQs without stimulus, preferring different units
    available_no_stim = [f for f in frq_no_stim if f['test_id'] not in used_frq_ids]

    # First, try to pick from units not yet used
    for _ in range(2):
        candidates = [f for f in available_no_stim if f['unit'] not in units_used and f['test_id'] not in used_frq_ids]
        if not candidates:
            candidates = [f for f in available_no_stim if f['test_id'] not in used_frq_ids]
        if candidates:
            chosen = random.choice(candidates)
            selected.append(chosen)
            used_frq_ids.add(chosen['test_id'])
            units_used.add(chosen['unit'])
            available_no_stim = [f for f in available_no_stim if f['test_id'] != chosen['test_id']]
            print(f"  FRQ (no stimulus): Unit {chosen['unit']} - {chosen['title']}")

    return selected


def create_course(course_id, title, course_code):
    payload = {
        "course": {
            "sourcedId": course_id,
            "status": "active",
            "title": title,
            "courseCode": course_code,
            "grades": ["9", "10", "11", "12"],
            "subjects": ["Geography", "Social Studies"],
            "subjectCodes": [],
            "org": {"sourcedId": ORG_SOURCED_ID},
            "level": "AP",
            "metadata": {
                "publishStatus": "testing",
                "goals": {"dailyXp": 25, "dailyLessons": 1, "dailyAccuracy": 80, "dailyActiveMinutes": 25, "dailyMasteredUnits": 2}
            }
        }
    }
    resp = session.post(f"{ONEROSTER_BASE}/ims/oneroster/rostering/v1p2/courses", headers=auth.get_headers(), json=payload, timeout=30)
    ok = resp.status_code in (200, 201, 409)
    print(f"  Course {course_code}: {'OK' if ok else 'FAIL ' + str(resp.status_code)}")
    return course_id if ok else None


def create_test(test_id, title, item_ids):
    payload = {
        "identifier": test_id,
        "title": title,
        "qti-test-part": [{
            "identifier": "main_part",
            "navigationMode": "linear",
            "submissionMode": "individual",
            "qti-assessment-section": [{
                "identifier": "main_section",
                "title": title,
                "visible": True, "required": True, "fixed": False, "sequence": 1,
                "qti-assessment-item-ref": [{"identifier": iid, "href": f"{iid}.xml"} for iid in item_ids]
            }]
        }],
        "qti-outcome-declaration": [{"identifier": "SCORE", "cardinality": "single", "baseType": "float"}]
    }
    resp = session.post(f"{QTI_BASE}/assessment-tests", headers=auth.get_headers(), json=payload, timeout=30)
    ok = resp.status_code in (200, 201, 409)
    print(f"    Test {test_id}: {'OK' if ok else 'FAIL ' + str(resp.status_code)}")
    return test_id if ok else None


def create_component(comp_id, title, course_id, sort_order):
    payload = {
        "courseComponent": {
            "sourcedId": comp_id, "status": "active", "title": title, "sortOrder": sort_order,
            "courseSourcedId": course_id, "course": {"sourcedId": course_id},
            "parent": None, "prerequisites": [], "prerequisiteCriteria": "ALL", "metadata": {}
        }
    }
    resp = session.post(f"{ONEROSTER_BASE}/ims/oneroster/rostering/v1p2/courses/components", headers=auth.get_headers(), json=payload, timeout=30)
    return comp_id if resp.status_code in (200, 201, 409) else None


def create_resource(res_id, title, test_id, is_article=False):
    if is_article:
        metadata = {
            "type": "qti", "subType": "qti-stimulus", "language": "en-US",
            "lessonType": "alpha-read-article", "assessmentType": "alpha-read",
            "allowRetake": True, "displayType": "interactive", "showResults": True,
            "url": f"{QTI_BASE}/stimuli/{test_id}", "xp": 0
        }
    else:
        metadata = {
            "type": "qti", "subType": "qti-test", "questionType": "custom", "language": "en-US",
            "lessonType": "quiz", "assessmentType": "quiz", "allowRetake": True,
            "displayType": "interactive", "showResults": True,
            "url": f"{QTI_BASE}/assessment-tests/{test_id}", "xp": 100
        }

    payload = {
        "resource": {
            "sourcedId": res_id, "status": "active", "title": title,
            "metadata": metadata,
            "roles": ["primary"], "importance": "primary",
            "vendorResourceId": test_id, "vendorId": "alpha-incept", "applicationId": "incept"
        }
    }
    resp = session.post(f"{ONEROSTER_BASE}/ims/oneroster/resources/v1p2/resources/", headers=auth.get_headers(), json=payload, timeout=30)
    return res_id if resp.status_code in (200, 201, 409) else None


def link_resource(cr_id, title, comp_id, res_id, sort_order, is_article=False):
    payload = {
        "componentResource": {
            "sourcedId": cr_id, "status": "active", "title": title, "sortOrder": sort_order,
            "courseComponent": {"sourcedId": comp_id}, "resource": {"sourcedId": res_id},
            "lessonType": "alpha-read-article" if is_article else "quiz"
        }
    }
    resp = session.post(f"{ONEROSTER_BASE}/ims/oneroster/rostering/v1p2/courses/component-resources", headers=auth.get_headers(), json=payload, timeout=30)
    return cr_id if resp.status_code in (200, 201, 409) else None


def create_stimulus(stim_id, title, content):
    payload = {"identifier": stim_id, "title": title, "content": content}
    resp = session.post(f"{QTI_BASE}/stimuli", headers=auth.get_headers(), json=payload, timeout=30)
    return stim_id if resp.status_code in (200, 201, 409) else None


results = []

for test_num in [1, 2]:
    print(f"\n{'='*60}")
    print(f"BUILDING APHG PRACTICE TEST {test_num}")
    print(f"{'='*60}")

    uid = uuid.uuid4().hex[:8]
    course_id = f"APHG-PT{test_num}-2026-{uid}"
    course_code = f"APHG-PT{test_num}-2026"
    course_title = f"APHG Practice Test {test_num} 2026"

    # Select items
    print("\nSelecting items...")
    mcqs = select_mcqs(test_num)
    frqs = select_frqs(test_num)

    # Create course
    print("\nCreating course...")
    create_course(course_id, course_title, course_code)

    # Create Section I: MCQ
    print("\nCreating Section I (MCQ)...")
    sec1_comp_id = f"aphg-pt{test_num}-sec1-{uid}"
    create_component(sec1_comp_id, "Section I: Multiple Choice (60 questions, 60 min)", course_id, 1)

    # Create MCQ test
    mcq_test_id = f"aphg-pt{test_num}-mcq-{uid}"
    mcq_item_ids = [m["item_id"] for m in mcqs]
    create_test(mcq_test_id, f"PT{test_num} MCQ", mcq_item_ids)

    # Create MCQ resource and link
    mcq_res_id = f"aphg-pt{test_num}-mcq-res-{uid}"
    create_resource(mcq_res_id, "Multiple Choice Questions", mcq_test_id)
    link_resource(f"aphg-pt{test_num}-mcq-cr-{uid}", "Multiple Choice Questions", sec1_comp_id, mcq_res_id, 1)

    # Add Section I instructions
    instr = INSTRUCTIONS['sec1']
    stim_id = f"aphg-pt{test_num}-sec1-instr-{uid}"
    create_stimulus(stim_id, instr['title'], instr['content'])
    instr_res_id = f"aphg-pt{test_num}-sec1-instr-res-{uid}"
    create_resource(instr_res_id, instr['title'], stim_id, is_article=True)
    link_resource(f"aphg-pt{test_num}-sec1-instr-cr-{uid}", instr['title'], sec1_comp_id, instr_res_id, 0, is_article=True)

    # Create Section II: FRQ
    print("\nCreating Section II (FRQ)...")
    sec2_comp_id = f"aphg-pt{test_num}-sec2-{uid}"
    create_component(sec2_comp_id, "Section II: Free Response (3 questions, 75 min)", course_id, 2)

    # Add Section II instructions
    instr = INSTRUCTIONS['sec2']
    stim_id = f"aphg-pt{test_num}-sec2-instr-{uid}"
    create_stimulus(stim_id, instr['title'], instr['content'])
    instr_res_id = f"aphg-pt{test_num}-sec2-instr-res-{uid}"
    create_resource(instr_res_id, instr['title'], stim_id, is_article=True)
    link_resource(f"aphg-pt{test_num}-sec2-instr-cr-{uid}", instr['title'], sec2_comp_id, instr_res_id, 0, is_article=True)

    # Link each FRQ as a separate resource (they're already complete tests in UWorld)
    for i, frq in enumerate(frqs, 1):
        frq_title = f"FRQ {i}: {frq['title']}"
        frq_res_id = f"aphg-pt{test_num}-frq{i}-res-{uid}"
        create_resource(frq_res_id, frq_title, frq['test_id'])
        link_resource(f"aphg-pt{test_num}-frq{i}-cr-{uid}", frq_title, sec2_comp_id, frq_res_id, i)
        print(f"    Linked FRQ {i}: {frq['title']}")

    results.append({
        "test_num": test_num,
        "course_id": course_id,
        "course_code": course_code,
        "course_title": course_title,
        "mcq": len(mcqs),
        "frq": len(frqs)
    })

print("\n" + "=" * 60)
print("COMPLETE!")
print("=" * 60)

for r in results:
    print(f"\n{r['course_title']}")
    print(f"  Course ID: {r['course_id']}")
    print(f"  Course Code: {r['course_code']}")
    print(f"  MCQ: {r['mcq']}, FRQ: {r['frq']}")

# Save results
with open('aphg_practice_tests_built.json', 'w') as f:
    json.dump(results, f, indent=2)

# Update CSV
print("\nUpdating CSV...")
with open('practice_tests.csv', 'r') as f:
    existing = f.read()

with open('practice_tests.csv', 'w') as f:
    f.write(existing.strip() + '\n')
    for r in results:
        f.write(f"APHG Practice Test {r['test_num']},{r['course_title']},{r['course_code']},{r['course_id']}\n")

print("CSV updated: practice_tests.csv")
