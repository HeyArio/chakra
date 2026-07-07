#!/usr/bin/env python3
"""
Nazarban report HTTP service.
POST /report  with the filled .xlsx as multipart file field "file"
              (optional form fields: name, date)
Returns: the generated PDF (application/pdf) as the response body.

Run:  python3 server.py            # listens on 0.0.0.0:8099
n8n calls this with a single HTTP Request node (see the workflow JSON).
"""
import os, tempfile, json, traceback
from flask import Flask, request, send_file, jsonify

# import the bundled logic
import nazarban_service as svc

app = Flask(__name__)

# simple shared-secret so only your n8n can call it (set NAZARBAN_TOKEN in env)
TOKEN = os.environ.get('NAZARBAN_TOKEN', '')

@app.route('/health')
def health():
    return jsonify(ok=True, service='nazarban-report')

@app.route('/report', methods=['POST'])
def report():
    if TOKEN:
        if request.headers.get('X-Auth-Token') != TOKEN:
            return jsonify(ok=False, error='unauthorized'), 401
    if 'file' not in request.files:
        return jsonify(ok=False, error='no file field'), 400

    name = request.form.get('name', '')
    date = request.form.get('date', '')

    with tempfile.TemporaryDirectory() as td:
        in_path = os.path.join(td, 'in.xlsx')
        out_path = os.path.join(td, 'out.pdf')
        request.files['file'].save(in_path)
        try:
            data = svc.score_workbook(in_path)
            fonts = svc._load_fonts()
            html_str = svc.build_html(data, fonts, person_name=name, date_str=date)
            svc.render_html_to_pdf(html_str, out_path)
        except Exception as e:
            return jsonify(ok=False, error=str(e),
                           trace=traceback.format_exc()), 500

        # filename with the person's name if given (sanitized for filesystem safety)
        import re
        safe = re.sub(r'[^\w\u0600-\u06FF\- ]', '', name).strip() if name else ''
        safe = re.sub(r'\s+', '_', safe)
        fname = f"{safe}.pdf" if safe else "nazarban-report.pdf"
        resp = send_file(out_path, mimetype='application/pdf',
                         as_attachment=True, download_name=fname)
        # expose the computed summary in headers (n8n can read these)
        resp.headers['X-Overall'] = str(data['overall_score'])
        resp.headers['X-Dominant'] = data['dominant']
        resp.headers['X-Archetype'] = data['archetype']
        return resp

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 8099)))