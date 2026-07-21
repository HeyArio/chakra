#!/usr/bin/env python3
"""
Chakra report HTTP service.

Designed so n8n needs NO code nodes and NO state of its own.
The VPS remembers the last uploaded file per Telegram chat.

Endpoints:
  GET  /health
  POST /report        file=<xlsx>, name=<str>       -> manifest JSON (one-shot)
  POST /upload        file=<xlsx>, chat=<chat_id>    -> {ok}  (stash by chat)
  POST /render        chat=<chat_id>, name=<str>     -> manifest JSON (stashed file)
  GET  /file/<tok>/<i>                               -> one PDF (pick-up by index)

A Porsline export may carry one respondent (the old per-person file) or a
whole batch, one row each. Either way every respondent is rendered to its
OWN PDF and the request returns a small JSON manifest — no ZIP. The rendered
PDFs are held on disk under a one-time token; n8n reads the manifest, then
fetches and sends each PDF to Telegram as its own document, PACED so
Telegram's per-chat flood limit is never tripped (see the n8n workflow's
"Loop Over Reports" + "Pace" nodes). Held files auto-expire on the render
sweep even if a pick-up never happens.

Pending uploads live in a temp dir, one slot per chat, auto-expire after TTL.
"""
import os, tempfile, shutil, json, re, time, logging, fcntl
from contextlib import contextmanager
from flask import Flask, request, send_file, jsonify

import nazarban_service as svc

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 10 * 1024 * 1024  # 10 MB

TOKEN = os.environ.get('NAZARBAN_TOKEN', '')

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger('chakra')

STORE_DIR = os.path.join(tempfile.gettempdir(), 'chakra_uploads')
RENDER_DIR = os.path.join(tempfile.gettempdir(), 'chakra_render')
os.makedirs(STORE_DIR, exist_ok=True)
os.makedirs(RENDER_DIR, exist_ok=True)
UPLOAD_TTL = 1800  # 30 min
# Largest batch rendered in one request — fixed at 30, and the bot's
# "working…" note QUOTES this number to the owner, so change them together
# (n8n workflow + here). Why 30 is comfortable everywhere, measured:
#   * Telegram (hard wall): a bot can send at most 50 MB and each PDF zips
#     to ~1.0 MB -> ~48 people can never fit; 30 uses ~60% of the wall.
#   * gunicorn --timeout 300: rendering is ~1-2 s/PDF on the 1-vCPU VPS and
#     the render lock queues concurrent batches, so a worker's whole request
#     (wait + render) must fit the timeout: three stacked 30-batches ≈ 180 s.
#   * RAM is no ceiling: chromium stays flat (~240 MB) at any batch size,
#     and the lock keeps it to one chromium total.
# CHAKRA_MAX_BATCH overrides for special cases; never set above 45.
MAX_BATCH = int(os.environ.get('CHAKRA_MAX_BATCH', '30'))

# One Chromium at a time, across ALL gunicorn workers. The VPS has 1 vCPU
# and 2 GB RAM: two concurrent renders don't finish any sooner (they share
# the core) but they double peak memory into OOM territory. flock releases
# by itself if a worker dies, so a killed render can't wedge the queue.
_RENDER_LOCK = os.path.join(tempfile.gettempdir(), 'chakra_render.lock')

@contextmanager
def _render_slot():
    with open(_RENDER_LOCK, 'w') as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        yield

def _auth_ok():
    return (not TOKEN) or (request.headers.get('X-Auth-Token') == TOKEN)

def _chat_key(chat):
    # only allow digits / minus (telegram chat ids) to build a safe filename
    return re.sub(r'[^0-9\-]', '', str(chat))

class BatchTooLarge(Exception):
    """A file carries more respondents than MAX_BATCH. Not a fault — the
    message is bilingual and gets relayed straight into the chat."""

def _stash_reports(in_path, name, date=''):
    """Render every respondent to its OWN PDF, hold the PDFs on disk under a
    one-time token dir, and return a JSON-able manifest. n8n then fetches each
    one from /file/<token>/<idx> and sends it as its own Telegram document.

    Returns {ok, count, token, files:[{idx, filename, overall, dominant,
    archetype}, ...]}. Raises BatchTooLarge for an over-cap file; any other
    failure removes the token dir and re-raises.

    The dir lives under RENDER_DIR — the same place a killed render leaves its
    scratch — so _sweep() reclaims it on the TTL even when a pick-up never
    happens (n8n stopped, the batch was forwarded elsewhere, a worker died).
    """
    td = tempfile.mkdtemp(dir=RENDER_DIR)
    token = os.path.basename(td)
    try:
        datas = svc.score_workbook_all(in_path)
        if len(datas) > MAX_BATCH:
            raise BatchTooLarge(
                f'این فایل {len(datas)} نفر دارد؛ حداکثر {MAX_BATCH} نفر در '
                f'هر فایل — لطفاً فایل را تقسیم کنید. '
                f'(batch of {len(datas)} exceeds the limit of {MAX_BATCH})')
        # The survey carries each respondent's name, so an explicitly-typed
        # name is optional; it overrides only a single-person file.
        with _render_slot():
            reports = svc.render_reports(datas, td, name_override=name,
                                         date_str=date)
    except Exception:
        shutil.rmtree(td, ignore_errors=True)
        raise
    # render_reports already writes safe, collision-deduped filenames into td,
    # so the on-disk basename doubles as the Telegram document name; the numeric
    # idx (not the Persian name) is what the pick-up URL carries.
    files, manifest = [], []
    for idx, (pdf_path, _person, data) in enumerate(reports):
        fname = os.path.basename(pdf_path)
        files.append({'idx': idx, 'filename': fname,
                      'overall': data['overall_score'],
                      'dominant': data['dominant'],
                      'archetype': data['archetype']})
        manifest.append({'idx': idx, 'filename': fname, 'file': fname})
    with open(os.path.join(td, 'manifest.json'), 'w', encoding='utf-8') as mf:
        json.dump(manifest, mf, ensure_ascii=False)
    return {'ok': True, 'count': len(files), 'token': token, 'files': files}

def _sweep():
    now = time.time()
    for f in os.listdir(STORE_DIR):
        p = os.path.join(STORE_DIR, f)
        try:
            if now - os.path.getmtime(p) > UPLOAD_TTL:
                os.remove(p)
        except OSError:
            pass
    # orphaned render dirs (worker killed mid-render); live renders are far
    # younger than the TTL, so this only ever touches leftovers
    for f in os.listdir(RENDER_DIR):
        p = os.path.join(RENDER_DIR, f)
        try:
            if now - os.path.getmtime(p) > UPLOAD_TTL:
                shutil.rmtree(p, ignore_errors=True)
        except OSError:
            pass

@app.route('/health')
def health():
    return jsonify(ok=True, service='chakra-report')

@app.route('/report', methods=['POST'])
def report():
    if not _auth_ok():
        return jsonify(ok=False, error='unauthorized'), 401
    if 'file' not in request.files:
        return jsonify(ok=False, error='no file field'), 400
    f = request.files['file']
    if not f.filename or not f.filename.lower().endswith('.xlsx'):
        return jsonify(ok=False, error='only .xlsx files accepted'), 400
    head = f.stream.read(4)
    f.stream.seek(0)
    if head[:2] != b'PK':
        return jsonify(ok=False, error='invalid xlsx file'), 400
    name = request.form.get('name', '')
    _sweep()
    with tempfile.TemporaryDirectory() as td:
        in_path = os.path.join(td, 'in.xlsx')
        f.save(in_path)
        try:
            payload = _stash_reports(in_path, name)
            log.info('report ok file=%s count=%d', f.filename, payload['count'])
            return jsonify(payload)
        except BatchTooLarge as e:
            # a user error, not a fault: 200 so n8n parses the message cleanly
            log.info('report over-cap file=%s: %s', f.filename, e)
            return jsonify(ok=False, error=str(e)), 200
        except Exception as e:
            log.exception('report failed file=%s', f.filename)
            return jsonify(ok=False, error=str(e)), 500

@app.route('/upload', methods=['POST'])
def upload():
    if not _auth_ok():
        return jsonify(ok=False, error='unauthorized'), 401
    if 'file' not in request.files:
        return jsonify(ok=False, error='no file field'), 400
    chat = _chat_key(request.form.get('chat', ''))
    if not chat:
        return jsonify(ok=False, error='no chat id'), 400
    f = request.files['file']
    if not f.filename or not f.filename.lower().endswith('.xlsx'):
        return jsonify(ok=False, error='only .xlsx files accepted'), 400
    # Reject files that aren't valid zip/xlsx (openpyxl reads zip internally)
    head = f.stream.read(4)
    f.stream.seek(0)
    if head[:2] != b'PK':
        return jsonify(ok=False, error='invalid xlsx file'), 400
    _sweep()
    path = os.path.join(STORE_DIR, 'chat_' + chat + '.xlsx')
    f.save(path)
    log.info('upload chat=%s file=%s size=%d', chat, f.filename, os.path.getsize(path))
    return jsonify(ok=True, chat=chat)

@app.route('/render', methods=['POST'])
def render():
    if not _auth_ok():
        return jsonify(ok=False, error='unauthorized'), 401
    body = request.get_json(silent=True) or {}
    chat = _chat_key(request.form.get('chat') or body.get('chat') or '')
    name = (request.form.get('name') or body.get('name') or '').strip()
    if not chat:
        return jsonify(ok=False, error='no chat id'), 400
    path = os.path.join(STORE_DIR, 'chat_' + chat + '.xlsx')
    if not os.path.exists(path):
        return jsonify(ok=False, error='no pending file for this chat'), 404
    try:
        payload = _stash_reports(path, name)
    except BatchTooLarge as e:
        return jsonify(ok=False, error=str(e)), 200
    except Exception as e:
        log.exception('render failed chat=%s', chat)
        return jsonify(ok=False, error=str(e)), 500
    finally:
        try: os.remove(path)
        except OSError: pass
    return jsonify(payload)

@app.route('/file/<token>/<int:idx>', methods=['GET'])
def fetch_file(token, idx):
    """Pick up one rendered PDF by index. n8n calls this once per report in
    the manifest, pacing the calls so Telegram never flood-limits the chat.
    The token dir is left in place for _sweep() to reclaim on the TTL, so a
    retry or a re-send can pick the same file up again within the window."""
    if not _auth_ok():
        return jsonify(ok=False, error='unauthorized'), 401
    # token is a mkdtemp basename; strip anything that isn't in that alphabet
    # so it can never climb out of RENDER_DIR
    safe_token = re.sub(r'[^A-Za-z0-9_]', '', str(token))
    d = os.path.join(RENDER_DIR, safe_token)
    manifest_path = os.path.join(d, 'manifest.json')
    if not safe_token or not os.path.isdir(d) or not os.path.exists(manifest_path):
        return jsonify(ok=False, error='not found or expired'), 404
    with open(manifest_path, encoding='utf-8') as mf:
        manifest = json.load(mf)
    entry = next((m for m in manifest if m.get('idx') == idx), None)
    if not entry:
        return jsonify(ok=False, error='no such report'), 404
    fpath = os.path.join(d, os.path.basename(entry['file']))
    if not os.path.exists(fpath):
        return jsonify(ok=False, error='report gone'), 404
    return send_file(fpath, mimetype='application/pdf', as_attachment=True,
                     download_name=entry['filename'])

if __name__ == '__main__':
    log.info('chakra starting on port %s', os.environ.get('PORT', '8099'))
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 8099)))