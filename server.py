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
import os, tempfile, json, traceback, re, time
from flask import Flask, request, send_file, jsonify

import nazarban_service as svc

app = Flask(__name__)
TOKEN = os.environ.get('NAZARBAN_TOKEN', '')

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
    td = tempfile.mkdtemp()
    out_path = os.path.join(td, 'out.pdf')
    data = svc.score_workbook(in_path)
    fonts = svc._load_fonts()
    html_str = svc.build_html(data, fonts, person_name=name, date_str=date)
    svc.render_html_to_pdf(html_str, out_path)
    resp = send_file(out_path, mimetype='application/pdf',
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
            return jsonify(ok=False, error=str(e), trace=traceback.format_exc()), 500

@app.route('/upload', methods=['POST'])
def upload():
    if not _auth_ok():
        return jsonify(ok=False, error='unauthorized'), 401
    if 'file' not in request.files:
        return jsonify(ok=False, error='no file field'), 400
    chat = _chat_key(request.form.get('chat', ''))
    if not chat:
        return jsonify(ok=False, error='no chat id'), 400
    _sweep()
    path = os.path.join(STORE_DIR, 'chat_' + chat + '.xlsx')
    request.files['file'].save(path)
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
        return jsonify(ok=False, error=str(e), trace=traceback.format_exc()), 500
    finally:
        try: os.remove(path)
        except OSError: pass
    return resp

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 8099)))