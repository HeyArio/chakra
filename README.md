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
one document per person, named after them. No ZIP: the client forwards each person their own
report, so they need separate files. The **VPS sends each PDF to Telegram itself**, paced
(with proper `429` back-off) so the bot never gets flood-limited or banned.

Built for **Nazarban Studio** / nazarbanai.com. Persian-first (RTL, Vazirmatn), dark
editorial report design.

---

## What it does (end to end)

```
Owner (Telegram)
   │  1. sends the Porsline survey .xlsx (one person — or a batch export)
   ▼
n8n (cloud) ──2. POST /deliver (file + chat id)──►  VPS service
                    ◄─ {ok, accepted} in <1 s        (then renders + sends on its own)
                                                       │
   the VPS, on a background thread:                    │
     • posts a "⏳ working…" note                       │
     • renders every respondent → its own PDF          │
     • sends each PDF into the chat, ~2 s apart  ◄──────┘
     • clears the note (or posts an error)
   ▼
Owner receives one PDF per person
```

- **Only the owner uses the bot.** It's single-user by design.
- **No "who is this for?" step** — each respondent's name is a field in the survey
  («نام و نام خانوادگی»), so the service reads it from the uploaded file and uses it as both
  the in-report name and the PDF filename. (An explicit `name` can still be POSTed to
  override it — single-person files only; a batch always names from the file.)
- **n8n just forwards.** It hands the file + chat id to `/deliver` and is done in under a
  second; the VPS renders and sends everything itself. A burst of files can't collide (nothing
  is keyed by chat) — send 50 files, or one batch file with 21 rows; every report comes back.
  n8n holds no state, no Code nodes — **3 nodes total**.

---

## Architecture

Two moving parts on two machines:

### 1. The VPS service (this repo)
A small Flask app (`server.py`) that wraps the scoring + PDF logic (`nazarban_service.py`).
Runs under **systemd** as the `chakra` service, behind **gunicorn** (2 workers).
- Host: `185.221.237.90`, port `8099`
- Port locked with an iptables rule to only accept traffic from the n8n server + localhost
- Renders PDFs with headless Chromium via Playwright
- **Sends the finished PDFs to Telegram itself** (outbound to `api.telegram.org`), with
  pacing + `429` back-off. Needs `TELEGRAM_BOT_TOKEN` in the environment.

### 2. n8n (cloud) — the orchestration
A tiny **3-node** workflow (`chakra_bot_workflow.json`) that:
- Listens for Telegram messages (Telegram Trigger, "Download Files" ON)
- Routes on message type (IF: is it a document?)
- POSTs the file + chat id to the VPS `/deliver` and returns — the VPS does **all** the
  rendering and Telegram sending itself, so n8n never loops or waits

n8n is imported/configured through the n8n Cloud UI — it is **not** in this repo, but the
workflow JSON to import lives here.

---

## Files

| File | Runs on | Purpose |
|------|---------|---------|
| `server.py` | VPS | Flask HTTP service + Telegram delivery. Endpoints: `/health`, `/deliver` (what the bot uses), `/upload`, `/render`, `/report`, `/file/<token>/<idx>`. |
| `nazarban_service.py` | VPS | The engine: reads the survey, scores it, builds the HTML report, renders the PDF. Scoring + template + renderer bundled; program copy lives in `program_content.py`. |
| `scoring_model.xlsx` | VPS | **The scoring brain.** The client's master workbook: 70 questions, their 4 options, 1–4 scores, reverse flags, and per-metric weights. The uploaded survey carries only answers; this file supplies the model. Edit weights/options here → restart. Must sit next to `nazarban_service.py`. |
| `program_content.py` | VPS | The 4-week program copy: 7 chakras × 3 score tiers × 6 sections (sleep, water affirmation, practice, frequency, incense, diet). Pure data — edit copy here, no logic. Must sit next to `nazarban_service.py`. |
| `fonts_b64.json` | VPS | Vazirmatn font weights (Farsi-Digits variant), base64-embedded so the PDF has zero external font deps. Must sit next to `nazarban_service.py`. |
| `requirements.txt` | VPS | Python deps: openpyxl, flask, gunicorn, playwright, requests. |

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
| POST | `/deliver` | multipart: `file` (xlsx), `chat` (chat id) | `{"ok":true,"accepted":true}` **immediately**, then the VPS renders + sends every report into that chat itself |
| POST | `/upload` | multipart: `file` (xlsx), `chat` (chat id) | `{"ok":true,"chat":"..."}` — stashes the file keyed by chat |
| POST | `/render` | form or JSON: `chat` (chat id); `name` **optional** | manifest JSON (see below), one-time use — deletes the stash |
| POST | `/report` | multipart: `file`; `name` **optional** | manifest JSON in one shot (no chat needed; kept for convenience) |
| GET | `/file/<token>/<idx>` | — (token + index from the manifest) | one `<person>.pdf` (`application/pdf`); 404 once swept |

**`/deliver` is the endpoint the bot uses.** It validates the file, returns `accepted`
within a second, and hands off to a **background thread** that: posts the «⏳ working…» note,
renders every respondent, sends each PDF into the chat (paced `CHAKRA_SEND_INTERVAL`, default
2 s, honouring Telegram's `429` `retry_after`), then deletes the note. An over-cap file or a
render error is reported straight into the chat; a partial send ("X of N") is reported too.
Requires **`TELEGRAM_BOT_TOKEN`** in the environment — without it `/deliver` returns 500.

The **`/report` + `/render` + `/file` trio** below is the older manifest/pick-up flow, kept
for direct API use; the bot no longer needs it. Its render response is a small JSON manifest —
one entry per respondent, single-person or batch alike:

```json
{ "ok": true, "count": 2, "token": "tmpAbC123",
  "files": [ { "idx": 0, "filename": "<person>.pdf", "overall": 74,
               "dominant": "heart", "archetype": "Healer" } ] }
```

The rendered PDFs are held on disk under `token`; fetch each one at `GET /file/<token>/<idx>`
(`application/pdf`, filename in `Content-Disposition`). Duplicate person names are deduped
`_2`, `_3`…. Held files auto-expire on the render sweep (~30 min), so pick them up promptly.

Batches above the cap — `CHAKRA_MAX_BATCH`, **default 30** — are rejected with a bilingual
"split the export" message: `/deliver` posts it straight into the chat, `/report` returns it
as `{"ok":false,"error":…}` (HTTP 200). **30 is the number the bot's «⏳ working…» note quotes
to the owner** — if you override the env var, update the note text too (it now lives in
`server.py`'s `_WORKING_NOTE`, not the n8n workflow). The old 50 MB ZIP wall is gone (each PDF
is sent on its own, ~1 MB), so the cap is about **render time and send pacing**, not file
size: ~2 s/PDF to render plus ~2 s/PDF to send, so 30 people is roughly two minutes end to
end. **Don't set it above 45** without widening the render timeout and `CHAKRA_SEND_INTERVAL`.

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

The unit sets two secrets in its environment and runs gunicorn:

```ini
Environment=NAZARBAN_TOKEN=<shared secret, must match the n8n X-Auth-Token>
Environment=TELEGRAM_BOT_TOKEN=<the BotFather token, for sending PDFs to Telegram>
# optional: Environment=CHAKRA_SEND_INTERVAL=2   (seconds between sends)
ExecStart=/usr/local/bin/gunicorn -w 2 -b 0.0.0.0:8099 --timeout 300 server:app
```

`TELEGRAM_BOT_TOKEN` is required now that the VPS sends the reports itself — without it
`/deliver` returns 500. After adding it: `systemctl daemon-reload && systemctl restart chakra`.
The VPS must be able to reach `api.telegram.org` outbound (verify with
`curl -s -o /dev/null -w "%{http_code}\n" https://api.telegram.org`).

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
widen `--timeout` and `CHAKRA_SEND_INTERVAL`, and update `_WORKING_NOTE`). `/tmp` is tmpfs
(RAM): a rendering batch holds ~65 MB of PDFs there until delivery finishes (the worker
deletes each token dir when done); orphans are swept after ~30 min.

> **Always use gunicorn, not `python3 server.py`.** The Flask dev server is single-threaded
> and hangs when Chromium launches inside a request. gunicorn's worker processes fix this.

### Editing → deploying loop
1. Edit a file in `/chakra`
2. `systemctl restart chakra`
3. `curl -s http://localhost:8099/health` to confirm it came back

---

## The n8n workflow

Import `chakra_bot_workflow.json` into n8n Cloud. **Three nodes**, no Code nodes, no state —
all the real work is on the VPS now, so there's almost nothing here to break:

1. **Telegram Trigger** — Download Files ON. Each uploaded file is its own message.
2. **Is it a file?** (IF) — TRUE = a document → hand it over. FALSE = plain text → ignored.
3. **Deliver (VPS)** (HTTP) — POST `/deliver` with the xlsx binary **and** the chat id. The
   VPS answers `{ok, accepted}` in under a second and does everything else on its own
   background thread: the «⏳ working…» note, rendering, sending each PDF (paced, with `429`
   back-off), clearing the note, and reporting any error straight into the chat.

The working note, the per-report sending, the retries, the error message, and the cleanup all
moved into `server.py` (`_deliver_worker`) — where they can be tested and where the pacing can
honour Telegram's `retry_after`.

### Config after import
- Attach the Telegram bot credential (BotFather token) to the **Trigger** — that's the only
  Telegram node left; the VPS does the actual sending with its own `TELEGRAM_BOT_TOKEN`.
- Set `X-Auth-Token` = your `NAZARBAN_TOKEN` in the **Deliver (VPS)** HTTP node.
- URL is pre-filled with the VPS IP (`185.221.237.90:8099/deliver`).
- On the VPS, set `TELEGRAM_BOT_TOKEN` (the same BotFather token) and confirm it can reach
  `api.telegram.org` (see "Running the service").
- Activate, then smoke-test: a normal file (⏳ appears → PDF arrives → ⏳ vanishes), a batch
  file (several PDFs arrive one after another, ~2 s apart), and an over-30-row file
  (⏳ → ❌ "split the export" → ⏳ vanishes).

### Many people at once (batches)
Two ways, both supported:

**One batch file** (how the client sends them now): the Porsline "download all results"
export, one row per person. The VPS scores every row, renders every PDF in a single Chromium
session, and **sends each report as its own document**, ~2 s apart. Capped at **30 people per
file** (`CHAKRA_MAX_BATCH` — see "Batch capacity" above); the working note states the limit,
and an over-the-cap file gets a bilingual "split the export" message in the chat.

**Many single files:** Telegram delivers each file as its own message, so 50 files = 50
independent `/deliver` calls. Nothing is keyed by chat, so they can't overwrite each other —
unlike the old `/upload`+`/render` pair, which kept one stashed file per chat and would
clobber itself under concurrency.

Throughput is bounded by two things: PDF rendering (**~2.4 s per single file** — browser
launch + render; scoring is ~0.1 s), and **paced delivery** (~2 s per report). Renders are
**serialized by a global lock**; sending is I/O, not CPU, and the lock is released *before*
it, so one batch's sending never blocks the next batch's render. A 30-row batch is roughly a
minute to render plus a minute to send.

### Design decisions worth knowing
- **No Code nodes, and now barely any nodes.** Code nodes are flaky on n8n Cloud (caused an
  "unknown error" earlier); n8n now only forwards the file, and all logic lives on the VPS.
- **The VPS sends to Telegram, not n8n.** Delivering many separate PDFs means many sends, and
  doing that through an n8n loop proved fragile (field-passing across Split Out, binary
  handling, per-item retries — two different failures in practice). Moving it into `server.py`
  makes it testable and lets the pacing honour Telegram's `429` `retry_after`, which n8n's
  fixed retry could not. Trade-off: the VPS needs the bot token and must reach
  `api.telegram.org` (confirmed reachable). See `_deliver_worker`.
- **Background delivery.** `/deliver` returns immediately and renders+sends on a daemon thread,
  so neither n8n nor a gunicorn worker waits on the ~minute of work. Errors and partial-send
  summaries ("X of N sent") go straight into the chat, so a failure is never silent.
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
- **`TELEGRAM_BOT_TOKEN`** now lives on the VPS too (needed to send the reports). Keep it in
  the systemd env, same as `NAZARBAN_TOKEN` — not in the repo. Outbound TLS to
  `api.telegram.org` only.
- Chat ids and filenames (and the `/file` token) are sanitized before touching the filesystem.
- Upload validation: rejects non-.xlsx files and non-zip content (magic bytes).
- Request size limit: 10 MB max.
- Structured logging (INFO level) on uploads, delivery, and errors.
- Inbound is still HTTP (not HTTPS) — fine for a private single-owner bot behind the firewall.
  Outbound to Telegram is HTTPS. Upgrade path: put the service behind a reverse proxy with a
  TLS cert if it ever grows or handles sensitive data — no code changes needed.

---

## Disclaimer

The report is a **self-knowledge / personal-development tool**, not medical, psychological,
or financial diagnosis. The scoring weights are a first-version product model and are not
statistically validated. This disclaimer appears on the report and in the source workbook.
