#!/usr/bin/env python3
"""Build 2 AP US Gov Practice Tests on Timeback."""

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

# AP GOV MCQ Distribution (55 questions)
# Unit 1: Foundations (15-20%) = 10 questions
# Unit 2: Branches (25-35%) = 17 questions
# Unit 3: Civil Liberties/Rights (13-18%) = 9 questions
# Unit 4: Ideologies (10-15%) = 7 questions
# Unit 5: Political Participation (20-27%) = 12 questions
MCQ_DISTRIBUTION = {
    '1': 10,  # Foundations
    '2': 17,  # Branches
    '3': 9,   # Civil Liberties/Rights
    '4': 7,   # Ideologies
    '5': 12,  # Political Participation
    # Total: 55
}

# Instructions
INSTRUCTIONS = {
    'sec1': {
        'title': 'Section I: Multiple Choice Instructions',
        'content': """<h2>Section I: Multiple-Choice Questions</h2>
<p><b>Time:</b> 80 minutes</p>
<p><b>Questions:</b> 55 questions</p>
<p><b>Weight:</b> 50% of your exam score</p>
<p>---</p>
<h3>Content Coverage</h3>
<p>Foundations of American Democracy: 15-20%</p>
<p>Interactions Among Branches: 25-35%</p>
<p>Civil Liberties and Civil Rights: 13-18%</p>
<p>American Political Ideologies and Beliefs: 10-15%</p>
<p>Political Participation: 20-27%</p>
<h3>Tips</h3>
<p>Pace yourself at about 1.5 minutes per question. Questions include data interpretation and Supreme Court references. No penalty for guessing.</p>
<p><b>Good luck!</b></p>"""
    },
    'sec2': {
        'title': 'Section II: Free Response Instructions',
        'content': """<h2>Section II: Free-Response Questions</h2>
<p><b>Time:</b> 100 minutes</p>
<p><b>Questions:</b> 4 questions (answer all)</p>
<p><b>Weight:</b> 50% of your exam score</p>
<p>---</p>
<h3>Question Types</h3>
<p><b>FRQ 1 - Concept Application:</b> Apply political concepts to a real-world scenario</p>
<p><b>FRQ 2 - Quantitative Analysis:</b> Analyze data (chart, graph, table) about government/politics</p>
<p><b>FRQ 3 - SCOTUS Comparison:</b> Compare a non-required Supreme Court case to a required case</p>
<p><b>FRQ 4 - Argument Essay:</b> Develop an argument with evidence on a political concept</p>
<h3>Tips</h3>
<p>Spend about 25 minutes per question. Answer each part clearly. Use specific evidence and examples. For SCOTUS, know your required cases!</p>
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

# Load inventory
with open('uworld_apgov_inventory.json') as f:
    inventory = json.load(f)

mcq_by_unit = inventory['mcq_by_unit']
frq_list = inventory['frq_list']

# Categorize FRQs by type
frq_by_type = {
    'Concept Application': [],
    'Quantitative Analysis': [],
    'SCOTUS Comparison': [],
    'Argument Essay': [],
}
for frq in frq_list:
    for frq_type in frq_by_type.keys():
        if frq_type.replace(' ', '-') in frq['title'] or frq_type.replace(' ', '') in frq['title'].replace(' ', ''):
            frq_by_type[frq_type].append(frq)
            break

print("FRQs by type:")
for frq_type, frqs in frq_by_type.items():
    print(f"  {frq_type}: {len(frqs)}")
    for f in frqs:
        print(f"    - {f['title']} ({f['item_count']} parts)")

used_mcq_ids = set()
used_frq_ids = set()


def select_mcqs(test_num):
    selected = []
    for unit, count in MCQ_DISTRIBUTION.items():
        available = [item for item in mcq_by_unit.get(unit, []) if item['item_id'] not in used_mcq_ids]
        take = min(count, len(available))
        chosen = random.sample(available, take)
        selected.extend(chosen)
        for item in chosen:
            used_mcq_ids.add(item['item_id'])

    print(f"  MCQ: {len(selected)}")
    return selected


def select_frqs(test_num):
    """Select 4 FRQs: one of each type."""
    selected = []
    for frq_type in ['Concept Application', 'Quantitative Analysis', 'SCOTUS Comparison', 'Argument Essay']:
        available = [f for f in frq_by_type[frq_type] if f['test_id'] not in used_frq_ids]
        if available:
            chosen = random.choice(available)
            selected.append({'type': frq_type, **chosen})
            used_frq_ids.add(chosen['test_id'])
            print(f"  FRQ ({frq_type}): {chosen['title']}")
        else:
            print(f"  FRQ ({frq_type}): NONE AVAILABLE")
    return selected


def create_course(course_id, title, course_code):
    payload = {
        "course": {
            "sourcedId": course_id,
            "status": "active",
            "title": title,
            "courseCode": course_code,
            "grades": ["11", "12"],
            "subjects": ["Social Studies"],
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
    if not ok:
        print(f"    {resp.text[:200]}")
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
    print(f"BUILDING AP GOV PRACTICE TEST {test_num}")
    print(f"{'='*60}")

    uid = uuid.uuid4().hex[:8]
    course_id = f"APGOV-PT{test_num}-2026-{uid}"
    course_code = f"APGOV-PT{test_num}-2026"
    course_title = f"AP Gov Practice Test {test_num} 2026"

    # Select items
    print("\nSelecting items...")
    mcqs = select_mcqs(test_num)
    frqs = select_frqs(test_num)

    # Create course
    print("\nCreating course...")
    create_course(course_id, course_title, course_code)

    # Section I: MCQ
    print("\nCreating Section I (MCQ)...")
    sec1_comp_id = f"apgov-pt{test_num}-sec1-{uid}"
    create_component(sec1_comp_id, "Section I: Multiple Choice (55 questions, 80 min)", course_id, 1)

    # MCQ instructions
    instr = INSTRUCTIONS['sec1']
    stim_id = f"apgov-pt{test_num}-sec1-instr-{uid}"
    create_stimulus(stim_id, instr['title'], instr['content'])
    instr_res_id = f"apgov-pt{test_num}-sec1-instr-res-{uid}"
    create_resource(instr_res_id, instr['title'], stim_id, is_article=True)
    link_resource(f"apgov-pt{test_num}-sec1-instr-cr-{uid}", instr['title'], sec1_comp_id, instr_res_id, 0, is_article=True)

    # MCQ test
    mcq_test_id = f"apgov-pt{test_num}-mcq-{uid}"
    mcq_item_ids = [m["item_id"] for m in mcqs]
    create_test(mcq_test_id, f"PT{test_num} MCQ", mcq_item_ids)
    mcq_res_id = f"apgov-pt{test_num}-mcq-res-{uid}"
    create_resource(mcq_res_id, "Multiple Choice Questions", mcq_test_id)
    link_resource(f"apgov-pt{test_num}-mcq-cr-{uid}", "Multiple Choice Questions", sec1_comp_id, mcq_res_id, 1)

    # Section II: FRQ
    print("\nCreating Section II (FRQ)...")
    sec2_comp_id = f"apgov-pt{test_num}-sec2-{uid}"
    create_component(sec2_comp_id, "Section II: Free Response (4 questions, 100 min)", course_id, 2)

    # FRQ instructions
    instr = INSTRUCTIONS['sec2']
    stim_id = f"apgov-pt{test_num}-sec2-instr-{uid}"
    create_stimulus(stim_id, instr['title'], instr['content'])
    instr_res_id = f"apgov-pt{test_num}-sec2-instr-res-{uid}"
    create_resource(instr_res_id, instr['title'], stim_id, is_article=True)
    link_resource(f"apgov-pt{test_num}-sec2-instr-cr-{uid}", instr['title'], sec2_comp_id, instr_res_id, 0, is_article=True)

    # Link each FRQ
    for i, frq in enumerate(frqs, 1):
        frq_title = f"FRQ {i}: {frq['type']}"
        frq_res_id = f"apgov-pt{test_num}-frq{i}-res-{uid}"
        create_resource(frq_res_id, frq_title, frq['test_id'])
        link_resource(f"apgov-pt{test_num}-frq{i}-cr-{uid}", frq_title, sec2_comp_id, frq_res_id, i)
        print(f"    Linked FRQ {i}: {frq['type']}")

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

# Update CSV
print("\nUpdating CSV...")
with open('practice_tests.csv', 'r') as f:
    existing = f.read()

with open('practice_tests.csv', 'w') as f:
    f.write(existing.strip() + '\n')
    for r in results:
        f.write(f"AP Gov Practice Test {r['test_num']},{r['course_title']},{r['course_code']},{r['course_id']}\n")

print("CSV updated: practice_tests.csv")
