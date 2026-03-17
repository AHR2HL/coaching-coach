# Claude Instructions - Coaching Coach

## Timezone Handling (CRITICAL)

**User location:** London, UK
**Schedule data timezone:** Central Time (Austin, TX)

### DST Transitions Are Different
- **US Central:** Springs forward 2nd Sunday of March, falls back 1st Sunday of November
- **UK:** Springs forward last Sunday of March, falls back last Sunday of October

### Current Offset Calculation
DO NOT assume a fixed offset. Calculate based on the actual date:

| Period | US Central | UK | Offset (London ahead) |
|--------|------------|-----|----------------------|
| Early March (before US DST) | CST (UTC-6) | GMT (UTC+0) | 6 hours |
| Mid-March to late March (US changed, UK hasn't) | CDT (UTC-5) | GMT (UTC+0) | **5 hours** |
| Late March to late October (both on summer time) | CDT (UTC-5) | BST (UTC+1) | 6 hours |
| Late Oct to early Nov (UK changed, US hasn't) | CDT (UTC-5) | GMT (UTC+0) | 5 hours |
| November to early March (both on winter time) | CST (UTC-6) | GMT (UTC+0) | 6 hours |

### 2026 Key Dates
- **March 8, 2026:** US springs forward (CST → CDT)
- **March 29, 2026:** UK springs forward (GMT → BST)
- **October 25, 2026:** UK falls back (BST → GMT)
- **November 1, 2026:** US falls back (CDT → CST)

### When User Gives London Time
1. Check current date
2. Determine if US and UK are in sync or offset
3. Convert to Central time before updating `SCHEDULE` in dashboard
4. Run `python sync_schedule.py` after schedule changes

## Project Structure

- **Schedule source of truth:** `SCHEDULE` list in `coaching_dashboard.py`
- **Sync to markdown:** `python sync_schedule.py`
- **Dashboard:** Flask app on `localhost:5000`
- **Student data:** `student_plans_v3/` (gitignored, private)

## Sending Messages

Via dashboard API:
- Questions: `http://localhost:5000/send-question/Student%20Name/week`
- Smart send (tries Slack, falls back to email): `http://localhost:5000/send/Student%20Name/week`

## Git Workflow

- Student-specific files are gitignored
- Framework code (scripts, templates) is committed
- After editing `SCHEDULE`, run sync script before committing
