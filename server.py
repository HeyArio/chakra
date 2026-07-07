#!/usr/bin/env python3
"""
Chakra report HTTP service.

Two ways to use it:

1) One-shot (file + name in a single request):
   POST /report   multipart: file=<xlsx>, name=<str>  -> returns PDF

2) Two-step (lets the Telegram bot ask for the name in a separate message):
   POST /upload   multipart: file=<xlsx>              -> {"id": "<token>"}
   POST /render   json/form: id=<token>, name=<str>   -> returns PDF

Uploaded files are held in a temp dir and auto-expire after UPLOAD_TTL seconds.
"""
import os, tempfile, json, traceback, re, time, uuid, threading
from flask import Flask, request, send_file, jsonify

import nazarban_service as svc

app = Flask(__name__)
TOKEN = os.environ.get('NAZARBAN_TOKEN', '')

# where pending uploads live between /upload and /render
STORE_DIR = os.path.join(tempfile.gettempdir(), 'chakra_uploads')
os.makedirs(STORE_DIR, exist_ok=True)
UPLOAD_TTL = 1800  # 30 minutes

def _safe_name(name):
    safe = re.sub(r'[^\w\u0600-\u06FF\- ]', '', name).strip() if name else ''
    safe = re.sub(r'\s+', '_', safe)
    return f"{safe}.pdf" if safe else "chakra-report.pdf"

def _render_pdf_response(in_path, name, date=''):
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

def _sweep_old():
    """Delete pending uploads older than the TTL."""
    now = time.time()
    for f in os.listdir(STORE_DIR):
        p = os.path.join(STORE_DIR, f)
        try:
            if now - os.path.getmtime(p) > UPLOAD_TTL:
                os.remove(p)
        except OSError:
            pass

def _auth_ok():
    return (not TOKEN) or (request.headers.get('X-Auth-Token') == TOKEN)

@app.route('/health')
def health():
    return jsonify(ok=True, service='chakra-report')

# --- one-shot (kept for convenience / backward compat) ---
@app.route('/report', methods=['POST'])
def report():
    if not _auth_ok():
        return jsonify(ok=False, error='unauthorized'), 401
    if 'file' not in request.files:
        return jsonify(ok=False, error='no file field'), 400
    name = request.form.get('name', '')
    date = request.form.get('date', '')
    with tempfile.TemporaryDirectory() as td:
        in_path = os.path.join(td, 'in.xlsx')
        request.files['file'].save(in_path)
        try:
            return _render_pdf_response(in_path, name, date)
        except Exception as e:
            return jsonify(ok=False, error=str(e), trace=traceback.format_exc()), 500

# --- step 1: stash the uploaded file, return an id ---
@app.route('/upload', methods=['POST'])
def upload():
    if not _auth_ok():
        return jsonify(ok=False, error='unauthorized'), 401
    if 'file' not in request.files:
        return jsonify(ok=False, error='no file field'), 400
    _sweep_old()
    fid = uuid.uuid4().hex
    path = os.path.join(STORE_DIR, fid + '.xlsx')
    request.files['file'].save(path)
    return jsonify(ok=True, id=fid)

# --- step 2: render the stashed file with the provided name ---
@app.route('/render', methods=['POST'])
def render():
    if not _auth_ok():
        return jsonify(ok=False, error='unauthorized'), 401
    body = request.get_json(silent=True) or {}
    fid = (request.form.get('id') or body.get('id') or '').strip()
    name = (request.form.get('name') or body.get('name') or '').strip()
    date = (request.form.get('date') or body.get('date') or '').strip()
    if not fid:
        return jsonify(ok=False, error='no id'), 400
    # guard against path tricks
    if not re.fullmatch(r'[0-9a-f]{32}', fid):
        return jsonify(ok=False, error='bad id'), 400
    path = os.path.join(STORE_DIR, fid + '.xlsx')
    if not os.path.exists(path):
        return jsonify(ok=False, error='file expired or not found'), 404
    try:
        resp = _render_pdf_response(path, name, date)
    except Exception as e:
        return jsonify(ok=False, error=str(e), trace=traceback.format_exc()), 500
    finally:
        try: os.remove(path)   # one-time use
        except OSError: pass
    return resp

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 8099)))