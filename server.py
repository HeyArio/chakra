#!/usr/bin/env python3
"""
Chakra report HTTP service.

Designed so n8n needs NO code nodes and NO state of its own.
The VPS remembers the last uploaded file per Telegram chat.

Endpoints:
  GET  /health
  POST /report   file=<xlsx>, name=<str>            -> PDF or ZIP (one-shot)
  POST /upload   file=<xlsx>, chat=<chat_id>         -> {ok}  (stash by chat)
  POST /render   chat=<chat_id>, name=<str>          -> PDF or ZIP (stashed file)

A Porsline export may carry one respondent (the old per-person file) or a
whole batch, one row each. One respondent -> a PDF, exactly as before.
Several -> every report is rendered and returned as a single ZIP of PDFs
named after the people; n8n forwards it to Telegram like any document.

Pending uploads live in a temp dir, one slot per chat, auto-expire after TTL.
"""
import os, tempfile, shutil, json, re, time, logging, zipfile
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
# Largest batch rendered in one request. Two ceilings, measured:
#   * Telegram: a bot can send at most 50 MB, and each PDF is ~1.26 MB raw /
#     ~1.0 MB zipped -> ~48 people is the hard wall, 40 keeps 20% headroom.
#   * gunicorn --timeout: a batch renders at ~1.4 s/PDF on the VPS, ~2.8 s
#     if both workers render batches at once. --timeout 120 therefore only
#     covers ~35 under contention; 40 needs --timeout 300 in the unit.
# Default stays 35 = safe under the stock unit. Set CHAKRA_MAX_BATCH=40
# together with --timeout 300 to raise it (see README).
MAX_BATCH = int(os.environ.get('CHAKRA_MAX_BATCH', '35'))

def _auth_ok():
    return (not TOKEN) or (request.headers.get('X-Auth-Token') == TOKEN)

def _chat_key(chat):
    # only allow digits / minus (telegram chat ids) to build a safe filename
    return re.sub(r'[^0-9\-]', '', str(chat))

def _render_response(in_path, name, date=''):
    # under RENDER_DIR so _sweep() reclaims it if a worker dies mid-render
    # (e.g. gunicorn timeout) and call_on_close never runs — a killed batch
    # would otherwise strand up to ~90 MB of PDFs in /tmp forever
    td = tempfile.mkdtemp(dir=RENDER_DIR)
    try:
        datas = svc.score_workbook_all(in_path)
        if len(datas) > MAX_BATCH:
            raise ValueError(f'batch of {len(datas)} respondents exceeds the '
                             f'limit of {MAX_BATCH}; split the export')
        # The survey carries each respondent's name, so an explicitly-typed
        # name is optional; it overrides only a single-person file.
        reports = svc.render_reports(datas, td, name_override=name,
                                     date_str=date)
    except Exception:
        shutil.rmtree(td, ignore_errors=True)
        raise
    if len(reports) == 1:
        out_path, person, data = reports[0]
        fh = open(out_path, 'rb')
        # delete the tree NOW: the open fd keeps the bytes streamable (POSIX)
        # and is closed by the WSGI file wrapper when the response ends.
        # (response.call_on_close never fires for send_file responses — the
        # WSGI iterable is the bare file wrapper, not the Response — so a
        # delete-on-close callback silently leaks the dir instead.)
        shutil.rmtree(td, ignore_errors=True)
        resp = send_file(fh, mimetype='application/pdf',
                         as_attachment=True,
                         download_name=svc.safe_pdf_name(person))
        resp.headers['X-Overall'] = str(data['overall_score'])
        resp.headers['X-Dominant'] = data['dominant']
        resp.headers['X-Archetype'] = data['archetype']
    else:
        zip_path = os.path.join(td, 'reports.zip')
        with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as z:
            for pdf_path, _person, _data in reports:
                z.write(pdf_path, arcname=os.path.basename(pdf_path))
        fh = open(zip_path, 'rb')
        shutil.rmtree(td, ignore_errors=True)
        resp = send_file(fh, mimetype='application/zip',
                         as_attachment=True,
                         download_name=f'chakra-reports-{len(reports)}.zip')
    resp.headers['X-Count'] = str(len(reports))
    return resp

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
            resp = _render_response(in_path, name)
            log.info('report ok file=%s', f.filename)
            return resp
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
        resp = _render_response(path, name)
    except Exception as e:
        log.exception('render failed chat=%s', chat)
        return jsonify(ok=False, error=str(e)), 500
    finally:
        try: os.remove(path)
        except OSError: pass
    return resp

if __name__ == '__main__':
    log.info('chakra starting on port %s', os.environ.get('PORT', '8099'))
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 8099)))