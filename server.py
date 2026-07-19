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
os.makedirs(STORE_DIR, exist_ok=True)
UPLOAD_TTL = 1800  # 30 min
# Largest batch rendered in one request. Rendering is ~1s per PDF, but the
# binding limit is Telegram: each PDF is ~1.3 MB (embedded fonts), and a bot
# can send at most 50 MB — 35 keeps the ZIP safely under that. A bigger
# export should be split in Porsline before uploading.
MAX_BATCH = 35

def _auth_ok():
    return (not TOKEN) or (request.headers.get('X-Auth-Token') == TOKEN)

def _chat_key(chat):
    # only allow digits / minus (telegram chat ids) to build a safe filename
    return re.sub(r'[^0-9\-]', '', str(chat))

def _render_response(in_path, name, date=''):
    td = tempfile.mkdtemp()
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
        resp = send_file(out_path, mimetype='application/pdf',
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
        resp = send_file(zip_path, mimetype='application/zip',
                         as_attachment=True,
                         download_name=f'chakra-reports-{len(reports)}.zip')
    resp.headers['X-Count'] = str(len(reports))
    # remove the rendered files once the response has been streamed (matters
    # when many files are processed back-to-back — otherwise temp dirs pile up)
    resp.call_on_close(lambda: shutil.rmtree(td, ignore_errors=True))
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