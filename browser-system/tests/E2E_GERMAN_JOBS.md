# E2E_GERMAN_JOBS.md — end-to-end research test plan

**Status: NEEDS LIVE RUN.** Nothing below has been executed. Every step is
written so a human (or an eligible browser-capable agent) can run it and
record PASS/FAIL honestly. Do not mark anything passed without running it.

**What this validates:** the full chain, end to end —
`Node app → Python browser service (via German egress) → arbeitsagentur.de
job search → extract results → screenshot → German IP verification`.

**Why arbeitsagentur.de:** it is the German Federal Employment Agency's
public job portal — a legitimate, public, no-login job search. It is also a
good honesty test: it is a government site that may show different content
or bot protections depending on egress, and it must never be gamed.

**Preconditions (all must be true before starting):**

- [ ] `CONTRACT_CHECK.md` reconciliation decided and implemented (flat shim
      OR Node rewrite) — the Node↔service path must actually work
- [ ] Browser service running with German egress configured (Path A or B in
      `germany/GERMANY.md`) — or explicitly run in "direct routing" mode and
      record that
- [ ] `python3 germany/verify_german_ip.py --status-url <service-base>` run
      **first**; record the browser-egress verdict
- [ ] Vision model optional — record whether vision was on or off

---

## Test script

### E1 — German egress is real (before any browsing)

1. Run `python3 germany/verify_german_ip.py --status-url http://127.0.0.1:8080`
   (adjust base URL to the reconciled contract/port).
2. **Expect:** `appears German [browser]: yes`, with country/city/ASN evidence.
3. **Record:** the egress IP, country, city, ASN in the run log.
4. If the verdict is `no`: STOP. Do not proceed and claim German results —
   run the plan in "direct mode" and label every observation accordingly.

### E2 — Job search: navigate + detect_blocks

1. Navigate to `https://www.arbeitsagentur.de/jobsuche/` (the public job search).
2. Call `detect_blocks` immediately.
3. **Expect:** page loads (HTTP 200); `blocked: false`.
4. **Honest-failure case:** if `blocked: true` (Cloudflare/bot check on a
   government site is plausible), STOP browsing that page — record
   `block_type`, take no further action, do not retry rapidly, do not attempt
   any bypass. The test outcome is "blocked — reported honestly", not a failure
   of the system.

### E3 — Search for a job term

1. On the job search page, find the search field (via extract → form elements,
   or fill_form if `BROWSER_ALLOW_FORMS=1`; otherwise record that form-fill
   was skipped per default policy).
2. Search for `Softwareentwickler` in `Berlin`.
3. **Expect:** a results list loads with job titles, employers, locations.
4. Extract the page: record the first 5 results (title, employer, location)
   as `facts` — verbatim, no interpretation.

### E4 — Screenshot + timestamp stamp

1. Take a viewport screenshot of the results page.
2. Run `tests/test_screenshot_url.py <results-page-url>` (or capture via the
   Node `/api/browser/screenshot` route).
3. **Expect:** valid PNG (>5KB), sidecar JSON with URL + title + ISO timestamp.
4. Open the PNG and **visually confirm** it shows the job results (not a
   block page, not a blank render).

### E5 — Open one job detail page

1. From the extract's indexed links, open the first result's detail page.
2. Extract: job title, employer, location, posted date.
3. **Expect:** detail page loads; extracted facts match what the screenshot
   shows.

### E6 — History recorded (if enabled)

1. If `BROWSER_ENABLE_HISTORY=1`: `GET /history` shows observations for the
   visited URLs (url, title, summary, timestamp).
2. **Expect:** entries exist; no form values or secrets stored.

### E7 — Task cleanup

1. `DELETE /tasks/{task_id}`.
2. **Expect:** `{ ok: true, closed: <id> }`; subsequent tool calls with that
   task_id return `task_not_found`.

### E8 — Response contract check (if Node wiring is live)

1. Trigger a `browser_search` for "Softwareentwickler Berlin" through the
   Node `/api/agent/generate` flow.
2. **Expect** the chat answer to follow `RESPONSE_CONTRACT.md`: clickable
   source URLs with page titles, access timestamp in PKT, separate
   "Facts from the page" vs "Halye's interpretation" sections, no invented
   data.

---

## Success criteria

- E1 verdict recorded (German or direct — either is a valid result, **lying
  about it is not**).
- E2–E5: real job listings extracted from the live rendered pages, with a
  matching screenshot and timestamped sidecar.
- Every block or failure reported honestly with its `block_type` / error —
  no bypasses, no retries-as-workaround, no fabricated listings.
- E6–E7: history (if enabled) and task lifecycle behave.

## Honest-failure cases (these are VALID outcomes, not system bugs)

| Case | Correct behavior |
|---|---|
| arbeitsagentur.de shows a bot challenge | Record `block_type`, stop, report. Never bypass. |
| German egress not actually German | Record the real verdict, run as "direct mode". |
| Site renders differently / job search needs JS the headless browser can't do | Record what was observed; screenshot is the evidence. |
| Rate-limited after several runs | Back off per `BROWSER_MIN_REQUEST_INTERVAL_MS`; record the 429. |
| No vision model configured | Observations are text-only, labeled "vision unavailable — text-only". |

## Run log template

```
Date (PKT): 
Runner: 
Contract mode: flat-shim | tools-direct
E1 egress verdict: German yes/no — IP: ___, city: ___, ASN: ___
E2 navigate: OK / BLOCKED(block_type=___)
E3 results extracted: ___ jobs (first: "___" @ ___)
E4 screenshot: OK, sha256=___, matches page visually: yes/no
E5 detail page: OK — title "___", employer "___"
E6 history: enabled/disabled, entries=___
E7 cleanup: OK
E8 response contract: OK / N/A (Node wiring not live)
Notes / deviations:
```
