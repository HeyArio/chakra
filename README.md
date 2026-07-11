# Chakra — Telegram Energy/Chakra Report Bot

A Telegram bot that turns a filled 140-question chakra/energy survey (`.xlsx`) into a
branded PDF report (wordmark: «شاهراه ثروت»). The owner uploads the spreadsheet, the bot asks who
it's for, and sends back a polished 6-page PDF named after that person: 2 analysis pages
plus a personalized **4-week energy growth & balance program** (one chakra per week).

Built for **Nazarban Studio** / nazarbanai.com. Persian-first (RTL, Vazirmatn), dark
editorial report design.

---

## What it does (end to end)

```
Owner (Telegram)
   │  1. sends filled chakra.xlsx
   ▼
n8n (cloud)  ──2. POST /upload (file + chat_id)──►  VPS service  ──stashes file by chat_id
   │  3. bot replies "who is this for?" (plain message)
   ▼
Owner types a name
   │
n8n  ──4. POST /render (chat_id + name)──►  VPS  ──scores + builds HTML + renders PDF──►  returns PDF
   │  5. bot sends <name>.pdf back into the chat
   ▼
Owner receives the report
```

- **Only the owner uses the bot.** It's single-user by design.
- The two Telegram messages (file, then name) are linked by **Telegram chat ID** — the
  VPS remembers the last uploaded file per chat. n8n holds no state and needs no Code nodes.

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
A 6-node workflow (`chakra_bot_workflow.json`) that:
- Listens for Telegram messages (Telegram Trigger, "Download Files" ON)
- Routes on message type (IF: is it a document?)
- Uploads the file to the VPS, asks for the name, then requests the render
- Sends the returned PDF back to Telegram

n8n is imported/configured through the n8n Cloud UI — it is **not** in this repo, but the
workflow JSON to import lives here.

---

## Files

| File | Runs on | Purpose |
|------|---------|---------|
| `server.py` | VPS | Flask HTTP service. Endpoints: `/health`, `/upload`, `/render`, `/report`. |
| `nazarban_service.py` | VPS | The engine: reads the xlsx, scores it, builds the HTML report, renders the PDF. Scoring + template + renderer bundled; program copy lives in `program_content.py`. |
| `program_content.py` | VPS | The 4-week program copy: 7 chakras × 3 score tiers × 6 sections (sleep, water affirmation, practice, frequency, incense, diet). Pure data — edit copy here, no logic. Must sit next to `nazarban_service.py`. |
| `fonts_b64.json` | VPS | Vazirmatn font weights (Farsi-Digits variant), base64-embedded so the PDF has zero external font deps. Must sit next to `nazarban_service.py`. |
| `requirements.txt` | VPS | Python deps: openpyxl, flask, gunicorn, playwright. |

---

## The input file (`chakra.xlsx`)

The survey workbook has these sheets:
- **README** — description, disclaimer (self-knowledge tool, not medical/financial advice).
- **Questions** — 140 rows. Each: chakra, dimension, 4 options, per-option scores (1–4),
  and multi-dimensional weights (7 chakras + 5 cross-indices: financial, emotional, health,
  receptivity, intuition) + an archetype tag.
- **Responses** — the answer sheet. Column B (`پاسخ (۱ تا ۴)`) is where 1–4 goes for each
  of the 140 questions. **This is the only column the user fills.**
- **Calculations / Dashboard / Interpretation / Archetypes** — the client's own scoring
  model and copy. **Important:** the scoring formulas already exist here.

### Scoring (do not reinvent)
The engine replicates the client's own formulas exactly (verified to the decimal against
LibreOffice). Per metric:

```
score = (SUMPRODUCT(answers, weights, answered) - SUMPRODUCT(weights, answered))
        / (3 * SUMPRODUCT(weights, answered)) * 100
```

i.e. a weighted 1–4 → 0–100 normalization that ignores blank answers. Plus:
- **7 chakra scores** (root, sacral, solar, heart, throat, third-eye, crown)
- **5 cross-indices** (financial, emotional, health, receptivity, intuition)
- **confidence** = % of questions answered
- **balance** = 100 − population stdev of the 7 chakra scores
- **dominant chakra** = highest of the 7 → maps to an **archetype**
  (Builder / Creator / Leader / Healer / Messenger / Visionary / Mystic)
- **level bands**: <35 نیازمند توجه جدی · 35–54 کم‌تعادل · 55–74 متعادل نسبی · 75–100 نقطه قوت

If you touch scoring, keep it matching the workbook's `Calculations` sheet.

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
| POST | `/render` | form or JSON: `chat` (chat id), `name` | the PDF (`application/pdf`), one-time use — deletes the stash |
| POST | `/report` | multipart: `file`, `name` | the PDF in one shot (no chat needed; kept for convenience) |

Pending uploads live in `$TMPDIR/chakra_uploads/`, one slot per chat, auto-expire after
30 min. Response headers `X-Overall`, `X-Dominant`, `X-Archetype` expose the summary.

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

> **Always use gunicorn, not `python3 server.py`.** The Flask dev server is single-threaded
> and hangs when Chromium launches inside a request. gunicorn's worker processes fix this.

### Editing → deploying loop
1. Edit a file in `/chakra`
2. `systemctl restart chakra`
3. `curl -s http://localhost:8099/health` to confirm it came back

---

## The n8n workflow

Import `chakra_bot_workflow.json` into n8n Cloud. Six nodes:

1. **Telegram Trigger** — Download Files ON. Both the file and the name-reply land here.
2. **Is it a file?** (IF) — TRUE = a document arrived → upload path. FALSE = plain text = the name → render path.
3. **Upload file to VPS** (HTTP) — POST `/upload` with the xlsx binary + `chat` id.
4. **Ask for the name** (Telegram) — plain message: "who is this for?". No button, no link.
5. **Generate PDF (VPS)** (HTTP) — POST `/render` with `chat` id + typed `name`, gets the PDF back.
6. **Send PDF Report** (Telegram) — sends the PDF into the chat.

### Config after import
- Attach the Telegram bot credential (BotFather token) to the 3 Telegram nodes.
- Set `X-Auth-Token` = your `NAZARBAN_TOKEN` in the 2 HTTP nodes.
- URLs are pre-filled with the VPS IP (`185.221.237.90:8099`).
- Activate.

### Design decisions worth knowing
- **No Code nodes.** They're flaky on n8n Cloud and caused an "unknown error" earlier.
  All state moved to the VPS (keyed by chat id); n8n only passes values it reads natively.
- **No "Send and Wait for Response" node.** That node forces a button + external link,
  which we explicitly didn't want. Instead the name is a normal message caught by the same
  trigger on a second execution, matched to the pending file via chat id.
- **Known harmless quirk:** if the owner sends random text with no file pending, the render
  node errors ("no pending file", 404) in the execution log. It's benign. Add a guard later
  if it's annoying.

---

## Customizing the report

Edit the report-builder section of `nazarban_service.py`:
- **Colors** — `CHAKRA_COLOR` dict + the CSS palette at the top of the `<style>` block.
- **Wordmark / tagline** — the `.brand` block in the HTML.
- **Copy / interpretations** — `BAND_SUGGEST`, `ARCHE_DESC` (sourced from the workbook's
  Interpretation and Archetypes sheets).
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
