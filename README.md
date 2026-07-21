# Chakra — Telegram Energy/Chakra Report Bot

A Telegram bot that turns a filled 70-question chakra/energy survey (`.xlsx`) into a
branded PDF report (wordmark: «شاهراه ثروت»). The owner uploads the spreadsheet and the bot
sends back a polished 6-page PDF **named after the respondent** — the person's name is read
straight out of the survey, so nobody has to type it. The report is 2 analysis pages plus a
personalized **4-week energy growth & balance program** (one chakra per week).

The survey is a **Porsline** export (`Results` sheet) that contains only the answers, the
person's name, phone and the dates. The scoring model (questions, options, 1–4 scores,
reverse flags, per-metric weights) lives in the bundled `scoring_model.xlsx`; the service
maps each text answer back onto it to compute the scores.

The export may hold **one respondent or a whole batch** (one row each — Porsline's
"download all results" file). Either way **every respondent comes back as its own PDF** —
one document per person, named after them — sent into the chat one at a time. No ZIP: the
client wanted the plain PDFs, and the bot **paces** the sends so Telegram never flood-limits
the chat.

Built for **Nazarban Studio** / nazarbanai.com. Persian-first (RTL, Vazirmatn), dark
editorial report design.

---

## What it does (end to end)

```
Owner (Telegram)
   │  1. sends the Porsline survey .xlsx (one person — or a batch export)
   ▼
n8n (cloud)  ──2. POST /report (file)──►  VPS service
   │             ◄─ manifest {count, token,   reads names → scores → renders every PDF →
   │                files:[…]}                holds them under a one-time token
   │  3. for each report: GET /file/<token>/<idx> → send <name>.pdf into the chat
   │     (one at a time, ~2 s apart, so Telegram doesn't rate-limit the bot)
   ▼
Owner receives one PDF per person
```

- **Only the owner uses the bot.** It's single-user by design.
- **No "who is this for?" step** — each respondent's name is a field in the survey
  («نام و نام خانوادگی»), so the service reads it from the uploaded file and uses it as both
  the in-report name and the PDF filename. (An explicit `name` can still be POSTed to
  override it — single-person files only; a batch always names from the file.)
- **One render call per file** (`/report`) returns a manifest; the PDFs are then pulled one
  per person from `/file/<token>/<idx>`. Nothing is keyed by chat, so a burst of files can't
  overwrite each other — send 50 files, or one batch file with 21 rows; every report comes
  back. n8n holds no state, no Code nodes.

---

## Architecture

Two moving parts on two machines:

### 1. The VPS service (this repo)
A small Flask app (`server.py`) that wraps the scoring + PDF logic (`nazarban_service.py`).
Runs under **systemd** as the `chakra` service, behind **gunicorn** (2 workers).
- Host: `185.221.237.90`, port `8099`
- Port locked with an iptables rule to only accept traffic from the n8n server + localhost
- Renders PDFs with headless Chromium via Playwright

### 2. n8n (cloud) — the orchestration
A workflow (`chakra_bot_workflow.json`) that:
- Listens for Telegram messages (Telegram Trigger, "Download Files" ON)
- Routes on message type (IF: is it a document?)
- POSTs the file to the VPS and gets back a manifest of rendered reports
- Loops the manifest, fetching and sending each PDF as its own document — **paced** (one
  every ~2 s, with retry) so Telegram never flood-limits the chat

n8n is imported/configured through the n8n Cloud UI — it is **not** in this repo, but the
workflow JSON to import lives here.

---

## Files

| File | Runs on | Purpose |
|------|---------|---------|
| `server.py` | VPS | Flask HTTP service. Endpoints: `/health`, `/upload`, `/render`, `/report`, `/file/<token>/<idx>`. |
| `nazarban_service.py` | VPS | The engine: reads the survey, scores it, builds the HTML report, renders the PDF. Scoring + template + renderer bundled; program copy lives in `program_content.py`. |
| `scoring_model.xlsx` | VPS | **The scoring brain.** The client's master workbook: 70 questions, their 4 options, 1–4 scores, reverse flags, and per-metric weights. The uploaded survey carries only answers; this file supplies the model. Edit weights/options here → restart. Must sit next to `nazarban_service.py`. |
| `program_content.py` | VPS | The 4-week program copy: 7 chakras × 3 score tiers × 6 sections (sleep, water affirmation, practice, frequency, incense, diet). Pure data — edit copy here, no logic. Must sit next to `nazarban_service.py`. |
| `fonts_b64.json` | VPS | Vazirmatn font weights (Farsi-Digits variant), base64-embedded so the PDF has zero external font deps. Must sit next to `nazarban_service.py`. |
| `requirements.txt` | VPS | Python deps: openpyxl, flask, gunicorn, playwright. |

---

## The two files: survey input + scoring model

The system uses **two** workbooks with a clean split of responsibility.

### 1. The uploaded survey — a Porsline export (`Results` sheet)
One sheet, **one row per respondent** — a single row (the old per-person export) or many
(the batch export); blank padding rows are ignored. Columns:
- `A` response link · `B` respondent id
- `C … BT` — the **70 questions**, each cell holding the chosen answer as **text** (not a number)
- «نام و نام خانوادگی …» → the respondent's **name** (drives the report + filename)
- «شماره موبایل» → phone (batch exports only; carried through, not printed on the report)
- «تاریخ شروع» / «تاریخ اتمام» — start / finish timestamps (Jalali); the **finish** time is
  stamped on the report as its date (falls back to start, then today)

The special columns are located by **header text, not column letter** — Porsline shifts
letters when the survey gains a field (the batch export's phone column moved the dates from
`BV`/`BW` to `BW`/`BX`), and header matching absorbs that.

This file carries **no scoring information** — just the answers, the names and the dates.

### 2. `scoring_model.xlsx` — the scoring brain (bundled, not uploaded)
The client's master workbook. Sheet `Questions` has 70 rows; each row: category, main chakra,
question text, the 4 options (`گزینه ۱..۴`), a direct/reverse flag (`نوع امتیاز`: مستقیم/معکوس),
and per-metric weights in columns `J..T` (7 chakras + 4 axes: Wealth, Emotional, Health,
Receiving). Tune scoring by editing weights/options here, then restart the service.

### How they join
For each question the service matches the survey's answer **text** to one of the four options
in the model → recovers the **1–4** choice. Text is normalized (ZWNJ, ی/ي, ک/ك, punctuation) so
Porsline's encoding lines up with the master. Validated 70/70 against the client's real export.

### Scoring (replicates the workbook exactly)
Per question: `F = 5 − answer` if the question is `معکوس` (reverse), else `answer`. Per metric:

```
score = SUMPRODUCT(F, weight) / (4 · SUM(weight)) · 100
```

Then:
- **7 chakra scores** (root, sacral, solar, heart, throat, third-eye, crown)
- **4 axes** (ثروت / wealth, عاطفی / emotional, سلامتی / health, دریافت / receiving)
- **confidence** = % of questions answered · **balance** = 100 − population stdev of the 7 chakras
- **dominant chakra** = highest of the 7 → **archetype**
  (Builder / Creator / Leader / Healer / Messenger / Visionary / Mystic)
- **level bands**: <40 پرچالش · 40–59 نیازمند توجه · 60–77 متعادل نسبی · ≥78 قوی

If you touch scoring, keep it matching `scoring_model.xlsx` (`Questions` + `Calculations`).

### The 4-week program (pages 3–6)

Client's rule, implemented in `build_week_plan()` (`nazarban_service.py`):

- Walk the chakras **bottom-up**: root → sacral → solar → heart → throat → third-eye → crown.
- Any chakra scoring **≤ 70** needs work and claims the next free week
  (week 1 = the lowest such chakra on the spine).
- If fewer than 4 chakras need work, remaining weeks are filled with the balanced
  (> 70) chakras — still bottom-up — on their maintenance program.
- Which of the 3 prescriptions a week uses depends only on that chakra's score:
  **< 40** → پاکسازی و بازسازی پایه · **40–70** → تقویت و تثبیت · **> 70** → نگهداری و ارتقا.

Each week page renders the fixed 6-part prescription from `program_content.py`:
sleep, water + affirmation, mindful practice, music/frequency (396–963 Hz per chakra),
incense, and diet (eat more / eat less). Framed as an energy growth & balance program,
explicitly **not** medical treatment (disclaimer in the page footer).

---

## HTTP API (VPS)

All POST endpoints require header `X-Auth-Token: <NAZARBAN_TOKEN>` (shared secret set in
the systemd env). Returns 401 if it doesn't match.

| Method | Path | Body | Returns |
|--------|------|------|---------|
| GET | `/health` | — | `{"ok":true,"service":"chakra-report"}` |
| POST | `/upload` | multipart: `file` (xlsx), `chat` (chat id) | `{"ok":true,"chat":"..."}` — stashes the file keyed by chat |
| POST | `/render` | form or JSON: `chat` (chat id); `name` **optional** | manifest JSON (see below), one-time use — deletes the stash |
| POST | `/report` | multipart: `file`; `name` **optional** | manifest JSON in one shot (no chat needed; kept for convenience) |
| GET | `/file/<token>/<idx>` | — (token + index from the manifest) | one `<person>.pdf` (`application/pdf`); 404 once swept |

**Render response** (`/report` and `/render`) is a small JSON manifest — one entry per
respondent, single-person or batch alike:

```json
{ "ok": true, "count": 2, "token": "tmpAbC123",
  "files": [ { "idx": 0, "filename": "<person>.pdf", "overall": 74,
               "dominant": "heart", "archetype": "Healer" } ] }
```

The rendered PDFs are held on disk under `token`; fetch each one at `GET /file/<token>/<idx>`
(`application/pdf`, filename in `Content-Disposition`). Duplicate person names are deduped
`_2`, `_3`…. Held files auto-expire on the render sweep (~30 min), so pick them up promptly.

Batches above the cap — `CHAKRA_MAX_BATCH`, **default 30** — return `{"ok":false,"error":…}`
(HTTP 200, so n8n relays the bilingual "split the export" message cleanly into the chat).
**30 is the number the bot's «⏳ working…» note quotes to the owner** — if you override the
env var, update the note text in the n8n workflow too. The old 50 MB ZIP wall is gone (each
PDF is sent on its own, ~1 MB), so the cap is now about **render time and send pacing**, not
file size: at ~2 s/PDF to render plus ~2 s/PDF to send, 30 people is roughly two minutes end
to end. **Don't set it above 45** without widening the render timeout and the n8n pacing.

Renders take a **global lock** (one Chromium at a time across both workers): on a 1-vCPU
box concurrent renders don't finish any sooner, but they double peak RAM. Batches that
arrive together simply queue — both succeed, the later one waits its turn.

> `name` is optional: when omitted (or blank) the service reads each respondent's name from
> the survey («نام و نام خانوادگی») and uses it for both the in-report name and the PDF
> filename. Pass `name` only to override — it applies to single-person files; a batch always
> names from the file.

Pending uploads live in `$TMPDIR/chakra_uploads/`, one slot per chat, auto-expire after
30 min.

---

## Running the service (VPS)

Deps are installed system-wide (no venv, by choice):

```bash
cd /chakra
pip install -r requirements.txt --break-system-packages
python3 -m playwright install chromium
python3 -m playwright install-deps
```

It runs as a systemd unit (`/etc/systemd/system/chakra.service`):

```bash
systemctl restart chakra          # after any code change
systemctl status chakra           # is it running? (green = yes)
journalctl -u chakra -n 30 --no-pager   # logs if something fails
curl -s http://localhost:8099/health    # confirm it answers
```

The unit sets `NAZARBAN_TOKEN` and runs:
`gunicorn -w 2 -b 0.0.0.0:8099 --timeout 120 server:app`

### Batch capacity on this VPS (1 vCPU, 2 GB RAM, tmpfs /tmp)

Measured: rendering is ~1.1 s/PDF on a 4-core dev box — budget **~2 s/PDF** on the VPS's
single KVM core. Chromium memory stays flat (~240 MB) for any batch size, and the render
lock keeps it to one Chromium total, so **RAM no longer limits batch size**. The cap is
**fixed at 30** (the number the bot quotes to the owner); with `--timeout 300` even three
30-person batches stacked behind the lock (~180 s for the last one) finish safely. Sending
is separate: the bot posts each PDF on its own, ~2 s apart, so a 30-person batch adds ~60 s
of paced delivery after the render — well within Telegram's flood limits.

The recommended unit (`/etc/systemd/system/chakra.service`):

```ini
ExecStart=/usr/local/bin/gunicorn -w 2 -b 0.0.0.0:8099 --timeout 300 server:app
```

then `systemctl daemon-reload && systemctl restart chakra`. No `CHAKRA_MAX_BATCH` line is
needed for the standard 30 — the env var exists only to override it (up to ~45; above that,
widen `--timeout` and the n8n pacing, and update the bot's note text). `/tmp` is tmpfs (RAM):
a rendering batch holds ~65 MB of PDFs there until the pick-ups finish or the sweep reclaims
them (~30 min); orphans are swept.

> **Always use gunicorn, not `python3 server.py`.** The Flask dev server is single-threaded
> and hangs when Chromium launches inside a request. gunicorn's worker processes fix this.

### Editing → deploying loop
1. Edit a file in `/chakra`
2. `systemctl restart chakra`
3. `curl -s http://localhost:8099/health` to confirm it came back

---

## The n8n workflow

Import `chakra_bot_workflow.json` into n8n Cloud. Still no Code nodes and no state — and
**safe to run many at once**. The flow renders once, then loops the manifest to send each
report on its own, paced:

1. **Telegram Trigger** — Download Files ON. Each uploaded file is its own message.
2. **Is it a file?** (IF) — TRUE = a document → process it. FALSE = plain text → ignored.
3. **Send Working Note** (Telegram) — instantly posts «⏳ در حال ساخت و ارسال گزارش‌ها…» and
   states the **30-people-per-file limit**. Sits on a **parallel branch**: a Telegram node
   placed inline before the HTTP request would strip the xlsx binary off the item.
4. **Render PDF (VPS)** (HTTP) — POST `/report` with the xlsx binary. The VPS renders every
   respondent and returns a **manifest** `{count, token, files:[…]}` (not a file).
   **Stateless** — no `chat`/`name` passed, so concurrent calls never collide. *On error the
   flow continues* into…
5. **Render OK?** (IF) — is `count > 0`? TRUE → fan out. FALSE → report the failure.
6. **Split Reports** (Split Out) — one item per person, each carrying the `token`.
7. **Loop Over Reports** (Loop Over Items, batch 1) — walks the reports one at a time; its
   "done" output cleans up, its "loop" output sends the next report.
8. **Fetch PDF (VPS)** (HTTP) — GET `/file/<token>/<idx>` → the person's PDF as binary.
9. **Send PDF Report** (Telegram) — sends that one `<name>.pdf` into the chat. **Retries 3×**
   on a Telegram `429`, and continues on error so one bad send doesn't drop the rest.
10. **Pace (Wait)** — waits **2 s**, then loops back. This is the rate-limit guard: ~1 doc /
    2 s to one chat stays under Telegram's ~1 msg/s flood limit. Raise it if you widen the cap.
11. **Once** (Limit 1) — the loop's "done" output carries every item; keep one so the working
    note is deleted a single time.
12. **Send Error** (Telegram) — relays the VPS error into the chat (e.g. the bilingual
    "batch of 52 exceeds the limit of 30 — split the export").
13. **Delete Working Note** (Telegram) — removes the ⏳ note once all reports (or the error)
    have been delivered, so the chat stays clean.

### Config after import
- Attach the Telegram bot credential (BotFather token) to the **Telegram nodes**
  (Working Note, PDF Report, Error, Delete Note) + the Trigger.
- Set `X-Auth-Token` = your `NAZARBAN_TOKEN` in **both** HTTP nodes (Render PDF, Fetch PDF).
- URL is pre-filled with the VPS IP (`185.221.237.90:8099`) in both.
- Activate, then smoke-test all three paths: a normal file (⏳ appears → PDF arrives → ⏳
  vanishes), a batch file (several PDFs arrive one after another, ~2 s apart), and an
  over-30-row file (⏳ → ❌ error message → ⏳ vanishes).

### Many people at once (batches)
Two ways, both supported:

**One batch file** (how the client sends them now): the Porsline "download all results"
export with one row per person. `/report` scores every row and renders every PDF in a single
Chromium session, then returns a manifest; the bot **sends each report as its own document**,
one every ~2 s. Capped at **30 people per file** (`CHAKRA_MAX_BATCH` — see "Batch capacity"
above); the bot's working note states this limit, and an over-the-cap file gets a bilingual
"split the export" message in the chat.

**Many single files:** Telegram delivers each file as a separate message, so 50 files = 50
independent executions, each its own `/report` call. Because `/report` is stateless (nothing
is keyed by chat), they can't overwrite each other — unlike the old `/upload`+`/render` pair,
which kept **one stashed file per chat** and would clobber itself under concurrency.

Throughput is bounded by two things: PDF rendering on the VPS (**~2.4 s per single file** —
browser launch + render; scoring is ~0.1 s), and **paced delivery** (~2 s per report so
Telegram doesn't flood-limit the chat). Renders are **serialized by a global lock** — on a
1-vCPU box parallel renders finish no sooner and only multiply peak RAM, so extra gunicorn
workers add responsiveness (health checks, uploads, rejects answer instantly) but not render
speed. A 30-row batch is roughly a minute to render plus a minute to send. n8n Cloud's own
execution-concurrency cap naturally paces the render requests, so the VPS won't be stampeded.

### Design decisions worth knowing
- **No Code nodes.** They're flaky on n8n Cloud and caused an "unknown error" earlier. n8n only
  passes values it reads natively; all the logic lives on the VPS.
- **Stateless render.** The bot uses the one-shot `/report` (file in → manifest out; each PDF
  then pulled by `token`+`idx`) instead of the `/upload`+`/render` stash. Nothing is keyed by
  chat, so nothing can be clobbered when files arrive together. (`/upload` + `/render` are still
  in the service for the old two-message pattern, but the bot no longer uses them.)
- **Paced sending, not a ZIP.** A batch used to come back as one ZIP; the client wanted the
  plain PDFs. So the bot sends each report as its own document, throttled to ~1 every 2 s by
  the Loop + Wait pair, with a retry on Telegram's `429`. That keeps bulk sends under
  Telegram's per-chat flood limit — the alternative, firing 30 documents at once, risks a
  temporary send ban.
- **No name prompt.** The respondent's name is a field inside the survey («نام و نام
  خانوادگی», located by header text), so the VPS reads it from the uploaded file. This removed the old "who is this for?" message, the
  second Telegram execution, and the state-matching that went with it.
- **Answers are matched by text.** Porsline exports the chosen answer as text, not a 1–4 index,
  so the service maps each answer back to its option in `scoring_model.xlsx`. Keep the option
  wording in the survey identical to the master workbook (the matcher normalizes ZWNJ, ی/ي,
  ک/ك and punctuation, but can't bridge genuinely different wording).

---

## Customizing the report

Edit the report-builder section of `nazarban_service.py`:
- **Colors** — `CHAKRA_COLOR` dict + the CSS palette at the top of the `<style>` block.
- **Wordmark / tagline** — the `.brand` block in the HTML.
- **Copy / interpretations** — `BAND_SUGGEST` (band advice) and `ARCHE_DESC` (archetype role +
  shadow) live in `nazarban_service.py`. Band labels/thresholds follow `scoring_model.xlsx`
  (`Interpretation` sheet); the chakra→archetype mapping is code-only (`ARCHETYPE`).
- **Layout** — two `.page` divs (A4, fixed height). Signature element is the "energy spine"
  (vertical stack of 7 chakra bars). Restyled radar chart, per-chakra interpretation rows.

The report is deliberately distinct from the client's original "AURA" dashboard mockup:
editorial single-column with numbered sections, not an app UI. Branding: the report wordmark is «شاهراه ثروت» (Farsi only).

After editing, redeploy: upload the changed file to `/chakra`, `systemctl restart chakra`.

---

## Security

- Port 8099 is firewalled (iptables) to only the n8n server IP + localhost.
- Shared-secret token (`NAZARBAN_TOKEN`) on every POST endpoint.
- Chat ids and filenames are sanitized before touching the filesystem.
- Upload validation: rejects non-.xlsx files and non-zip content (magic bytes).
- Request size limit: 10 MB max.
- Structured logging (INFO level) on uploads and errors.
- Currently HTTP (not HTTPS). Fine for a private single-owner bot moving non-sensitive
  survey data. Upgrade path: put the service behind a reverse proxy with a TLS cert if it
  ever grows or handles sensitive data — no code changes needed.

---

## Disclaimer

The report is a **self-knowledge / personal-development tool**, not medical, psychological,
or financial diagnosis. The scoring weights are a first-version product model and are not
statistically validated. This disclaimer appears on the report and in the source workbook.
