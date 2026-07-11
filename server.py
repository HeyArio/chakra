#!/usr/bin/env python3
"""
Chakra report HTTP service.

Designed so n8n needs NO code nodes and NO state of its own.
The VPS remembers the last uploaded file per Telegram chat.

Endpoints:
  GET  /health
  POST /report   file=<xlsx>, name=<str>            -> PDF   (one-shot, optional)
  POST /upload   file=<xlsx>, chat=<chat_id>         -> {ok}  (stash by chat)
  POST /render   chat=<chat_id>, name=<str>          -> PDF   (render stashed file)

Pending uploads live in a temp dir, one slot per chat, auto-expire after TTL.
"""
import io, os, tempfile, json, re, time, logging
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

def _auth_ok():
    return (not TOKEN) or (request.headers.get('X-Auth-Token') == TOKEN)

def _safe_name(name):
    safe = re.sub(r'[^\w\u0600-\u06FF\- ]', '', name).strip() if name else ''
    safe = re.sub(r'\s+', '_', safe)
    return f"{safe}.pdf" if safe else "chakra-report.pdf"

def _chat_key(chat):
    # only allow digits / minus (telegram chat ids) to build a safe filename
    return re.sub(r'[^0-9\-]', '', str(chat))

def _render_response(in_path, name, date=''):
    data = svc.score_workbook(in_path)
    fonts = svc._load_fonts()
    html_str = svc.build_html(data, fonts, person_name=name, date_str=date)
    # render into a self-cleaning temp dir and serve from memory,
    # so nothing is left behind on disk after the response
    with tempfile.TemporaryDirectory() as td:
        out_path = os.path.join(td, 'out.pdf')
        svc.render_html_to_pdf(html_str, out_path)
        with open(out_path, 'rb') as fh:
            pdf_bytes = fh.read()
    resp = send_file(io.BytesIO(pdf_bytes), mimetype='application/pdf',
                     as_attachment=True, download_name=_safe_name(name))
    resp.headers['X-Overall'] = str(data['overall_score'])
    resp.headers['X-Dominant'] = data['dominant']
    resp.headers['X-Archetype'] = data['archetype']
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
    name = request.form.get('name', '')
    with tempfile.TemporaryDirectory() as td:
        in_path = os.path.join(td, 'in.xlsx')
        request.files['file'].save(in_path)
        try:
            return _render_response(in_path, name)
        except Exception as e:
            log.exception('report failed')
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
        # keep the stashed upload so the owner can retry by re-sending
        # just the name (it still expires after UPLOAD_TTL)
        log.exception('render failed chat=%s (upload kept for retry)', chat)
        return jsonify(ok=False, error=str(e)), 500
    try: os.remove(path)
    except OSError: pass
    log.info('render ok chat=%s name=%s overall=%s', chat, name,
             resp.headers.get('X-Overall'))
    return resp

if __name__ == '__main__':
    log.info('chakra starting on port %s', os.environ.get('PORT', '8099'))
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 8099)))