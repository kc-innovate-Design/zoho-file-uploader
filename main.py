"""
File Uploader — Cloud Run Service (v2: Google Drive)

Downloads file bytes from a URL (WPForms uploads, the secure NDA endpoint,
client photos) and files them into the Google Drive folder linked to the
Zoho CRM Deal (Drive Folder ID field).

STORAGE_TARGET=drive (default)  -> upload into the deal's Drive folder
STORAGE_TARGET=zoho             -> old behaviour: Zoho attachment + WorkDrive
                                   Deluge sync (rollback switch, no redeploy)

Endpoints:
  POST /upload         — Upload a single file for a Deal
  POST /upload-batch   — Upload multiple files for a Deal in one call
  GET  /health         — Health check
"""

import os
import io
import time
import hmac
import logging
from datetime import datetime
from urllib.parse import urlparse, unquote

import requests
import google.auth
from google.auth.transport.requests import Request as GoogleAuthRequest
from flask import Flask, request, jsonify

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger('file-uploader')

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

SERVICE_SECRET = os.environ.get('SERVICE_SECRET', '')
ALLOW_OPEN     = os.environ.get('ALLOW_OPEN', '').lower() == 'true'  # dev only

ZOHO_CLIENT_ID     = os.environ.get('ZOHO_CLIENT_ID', '')
ZOHO_CLIENT_SECRET = os.environ.get('ZOHO_CLIENT_SECRET', '')
ZOHO_REFRESH_TOKEN = os.environ.get('ZOHO_REFRESH_TOKEN', '')
ZOHO_TOKEN_URL     = 'https://accounts.zoho.eu/oauth/v2/token'
ZOHO_API_BASE      = 'https://www.zohoapis.eu/crm/v6'

STORAGE_TARGET = os.environ.get('STORAGE_TARGET', 'drive').lower()

# Zoho API names of the Drive fields on Deals (check in Setup > Modules > Deals)
DRIVE_FOLDER_FIELD = os.environ.get('DRIVE_FOLDER_FIELD', 'gdriveextension__Drive_Folder_ID')
DRIVE_URL_FIELD    = os.environ.get('DRIVE_URL_FIELD', 'gdriveextension__Drive_URL')

# If the deal has no folder after waiting, create one under this parent.
# Leave blank to fail instead (files are then reported, not lost silently).
DRIVE_FALLBACK_PARENT_ID = os.environ.get('DRIVE_FALLBACK_PARENT_ID', '')
FOLDER_WAIT_SECONDS      = int(os.environ.get('FOLDER_WAIT_SECONDS', '30'))
FOLDER_POLL_INTERVAL     = 5

DRIVE_API    = 'https://www.googleapis.com/drive/v3'
DRIVE_UPLOAD = 'https://www.googleapis.com/upload/drive/v3'

DOWNLOAD_TIMEOUT = 30
MAX_FILE_SIZE    = 20 * 1024 * 1024  # 20 MB

TYPE_LABELS = {
    'nda':         'NDA',
    'review_form': 'Review Form',
    'attachment':  'Client File',
}

_token_cache  = {'access_token': None, 'expires_at': 0}
_google_creds = None


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

def check_auth():
    """Validate the pre-shared secret. Closed by default if none configured."""
    if not SERVICE_SECRET:
        return ALLOW_OPEN
    token = request.headers.get('X-Service-Secret', '')
    if not token:
        body = request.get_json(silent=True) or {}
        token = str(body.get('service_secret', ''))
    return hmac.compare_digest(token, SERVICE_SECRET)


def get_zoho_token():
    now = time.time()
    if _token_cache['access_token'] and now < _token_cache['expires_at']:
        return _token_cache['access_token']

    resp = requests.post(ZOHO_TOKEN_URL, data={
        'grant_type':    'refresh_token',
        'client_id':     ZOHO_CLIENT_ID,
        'client_secret': ZOHO_CLIENT_SECRET,
        'refresh_token': ZOHO_REFRESH_TOKEN,
    }, timeout=15)
    data = resp.json()
    if 'access_token' not in data:
        logger.error(f'Zoho token refresh failed: {resp.text}')
        raise Exception(f'Zoho token refresh failed: {data}')

    _token_cache['access_token'] = data['access_token']
    _token_cache['expires_at'] = now + 3300
    return data['access_token']


def zoho_headers():
    return {'Authorization': f'Zoho-oauthtoken {get_zoho_token()}'}


def get_google_token():
    """Uses the Cloud Run service account (must be a member of the Shared Drive)."""
    global _google_creds
    if _google_creds is None:
        _google_creds, _ = google.auth.default(
            scopes=['https://www.googleapis.com/auth/drive'])
    if not _google_creds.valid:
        _google_creds.refresh(GoogleAuthRequest())
    return _google_creds.token


def drive_headers():
    return {'Authorization': f'Bearer {get_google_token()}'}


# ---------------------------------------------------------------------------
# File download
# ---------------------------------------------------------------------------

def download_file(url):
    """Returns (bytes, content_type, filename) or raises."""
    logger.info(f'Downloading: {url[:100]}...')
    resp = requests.get(url, timeout=DOWNLOAD_TIMEOUT, stream=True,
                        allow_redirects=True)
    resp.raise_for_status()

    content_length = resp.headers.get('Content-Length')
    if content_length and int(content_length) > MAX_FILE_SIZE:
        raise ValueError(f'File too large: {content_length} bytes')

    file_bytes = resp.content
    if len(file_bytes) > MAX_FILE_SIZE:
        raise ValueError(f'File too large: {len(file_bytes)} bytes')

    content_type = resp.headers.get('Content-Type', 'application/octet-stream')
    content_type = content_type.split(';')[0].strip()

    filename = None
    cd = resp.headers.get('Content-Disposition', '')
    if 'filename=' in cd:
        filename = cd.split('filename=')[1].split(';')[0].strip('" ')

    if not filename:
        path = urlparse(url).path
        if path and '/' in path:
            filename = unquote(path.split('/')[-1])

    if not filename or filename == '/':
        ext = {
            'application/pdf': '.pdf',
            'image/jpeg': '.jpg',
            'image/png': '.png',
            'image/gif': '.gif',
        }.get(content_type, '.bin')
        filename = f'attachment{ext}'

    logger.info(f'Downloaded {len(file_bytes)} bytes — {filename} ({content_type})')
    return file_bytes, content_type, filename


# ---------------------------------------------------------------------------
# Zoho deal lookups / updates
# ---------------------------------------------------------------------------

def get_deal(deal_id):
    resp = requests.get(
        f'{ZOHO_API_BASE}/Deals/{deal_id}',
        params={'fields': f'Deal_Name,Contact_Name,Contact_Email,{DRIVE_FOLDER_FIELD},{DRIVE_URL_FIELD}'},
        headers=zoho_headers(), timeout=15)
    resp.raise_for_status()
    return resp.json()['data'][0]


def update_deal(deal_id, fields, fire_workflow=False):
    """trigger: [] explicitly stops Zoho running workflow rules
    (omitting it would run them by default)."""
    body = {
        'data': [{'id': deal_id, **fields}],
        'trigger': ['workflow'] if fire_workflow else [],
    }
    resp = requests.put(f'{ZOHO_API_BASE}/Deals', json=body,
                        headers=zoho_headers(), timeout=15)
    logger.info(f'Deal update {list(fields)}: {resp.status_code}')
    return resp.status_code


# ---------------------------------------------------------------------------
# Google Drive
# ---------------------------------------------------------------------------

def create_drive_folder(name, parent_id):
    resp = requests.post(
        f'{DRIVE_API}/files',
        params={'supportsAllDrives': 'true', 'fields': 'id,webViewLink'},
        json={'name': name,
              'mimeType': 'application/vnd.google-apps.folder',
              'parents': [parent_id]},
        headers=drive_headers(), timeout=15)
    resp.raise_for_status()
    d = resp.json()
    return d['id'], d.get('webViewLink') or f'https://drive.google.com/drive/folders/{d["id"]}'


def resolve_drive_folder(deal_id):
    """
    Read the deal's Drive Folder ID. The folder may be created by a Zoho
    workflow that hasn't finished yet, so poll briefly before falling back.
    Returns (folder_id, how) where how is 'existing' or 'created'.
    """
    deadline = time.time() + FOLDER_WAIT_SECONDS
    while True:
        deal = get_deal(deal_id)
        folder_id = str(deal.get(DRIVE_FOLDER_FIELD) or '').strip()
        if folder_id:
            return folder_id, 'existing'
        if time.time() >= deadline:
            break
        time.sleep(FOLDER_POLL_INTERVAL)

    if not DRIVE_FALLBACK_PARENT_ID:
        raise RuntimeError(f'Deal {deal_id} has no {DRIVE_FOLDER_FIELD} '
                           f'after {FOLDER_WAIT_SECONDS}s and no fallback parent set')

    # Match the Drive extension's convention: "First Last - email"
    contact = (deal.get('Contact_Name') or {}).get('name', '')
    email = deal.get('Contact_Email') or ''
    name = f'{contact} - {email}' if contact and email else (deal.get('Deal_Name') or f'Deal {deal_id}')
    folder_id, folder_url = create_drive_folder(name, DRIVE_FALLBACK_PARENT_ID)
    update_deal(deal_id, {DRIVE_FOLDER_FIELD: folder_id, DRIVE_URL_FIELD: folder_url})
    logger.warning(f'Created fallback Drive folder {folder_id} for deal {deal_id}')
    return folder_id, 'created'


def find_in_folder(folder_id, filename):
    safe = filename.replace('\\', '\\\\').replace("'", "\\'")
    resp = requests.get(
        f'{DRIVE_API}/files',
        params={
            'q': f"name = '{safe}' and '{folder_id}' in parents and trashed = false",
            'supportsAllDrives': 'true',
            'includeItemsFromAllDrives': 'true',
            'fields': 'files(id,size,webViewLink)',
        },
        headers=drive_headers(), timeout=15)
    resp.raise_for_status()
    return resp.json().get('files', [])


def upload_to_drive(folder_id, file_bytes, filename, content_type):
    """Resumable upload (handles files over 5 MB). Skips exact duplicates
    so an integration retry doesn't file the same document twice."""
    for f in find_in_folder(folder_id, filename):
        if str(f.get('size')) == str(len(file_bytes)):
            logger.info(f'Duplicate skipped: {filename}')
            return {'id': f['id'], 'webViewLink': f.get('webViewLink'), 'duplicate': True}

    init = requests.post(
        f'{DRIVE_UPLOAD}/files',
        params={'uploadType': 'resumable', 'supportsAllDrives': 'true',
                'fields': 'id,webViewLink'},
        json={'name': filename, 'parents': [folder_id]},
        headers={**drive_headers(),
                 'X-Upload-Content-Type': content_type,
                 'X-Upload-Content-Length': str(len(file_bytes))},
        timeout=15)
    init.raise_for_status()

    resp = requests.put(init.headers['Location'], data=file_bytes,
                        headers={'Content-Type': content_type}, timeout=120)
    resp.raise_for_status()
    return resp.json()


# ---------------------------------------------------------------------------
# Legacy Zoho path (STORAGE_TARGET=zoho)
# ---------------------------------------------------------------------------

def upload_to_zoho(deal_id, file_bytes, filename, content_type):
    resp = requests.post(
        f'{ZOHO_API_BASE}/Deals/{deal_id}/Attachments',
        files={'file': (filename, io.BytesIO(file_bytes), content_type)},
        headers=zoho_headers(), timeout=30)
    logger.info(f'Zoho upload response: {resp.status_code} — {resp.text[:300]}')
    return resp.status_code, resp.json() if resp.text else {}


# ---------------------------------------------------------------------------
# Core processing
# ---------------------------------------------------------------------------

def process_files(deal_id, files):
    results = []
    folder_id, folder_how = None, None

    valid = [f for f in files if str(f.get('url', '')).strip().startswith('http')]

    if STORAGE_TARGET == 'drive' and valid:
        try:
            folder_id, folder_how = resolve_drive_folder(deal_id)
        except Exception as e:
            logger.exception('Could not resolve Drive folder')
            return {'ok': False, 'deal_id': deal_id, 'error': str(e),
                    'results': [{'ok': False, 'file_type': f.get('type', 'unknown'),
                                 'error': 'No Drive folder'} for f in valid]}

    for f in files:
        file_url  = str(f.get('url', '')).strip()
        file_type = str(f.get('type', 'unknown')).strip()
        custom_fn = str(f.get('filename', '')).strip()

        if not file_url.startswith('http'):
            results.append({'ok': False, 'file_type': file_type,
                            'skipped': True, 'reason': 'Empty or invalid URL'})
            continue

        try:
            file_bytes, content_type, filename = download_file(file_url)
            if custom_fn:
                filename = custom_fn

            if STORAGE_TARGET == 'drive':
                label = TYPE_LABELS.get(file_type, file_type.title())
                drive_name = f'{label} - {filename}'
                d = upload_to_drive(folder_id, file_bytes, drive_name, content_type)
                results.append({
                    'ok': True, 'file_type': file_type, 'filename': drive_name,
                    'size_bytes': len(file_bytes), 'drive_file_id': d.get('id'),
                    'drive_link': d.get('webViewLink'),
                    'duplicate': d.get('duplicate', False),
                })
            else:
                status_code, _ = upload_to_zoho(deal_id, file_bytes, filename, content_type)
                results.append({
                    'ok': 200 <= status_code < 300, 'file_type': file_type,
                    'filename': filename, 'size_bytes': len(file_bytes),
                    'zoho_status': status_code,
                })

        except requests.exceptions.HTTPError as e:
            status = e.response.status_code if e.response is not None else 0
            results.append({'ok': False, 'file_type': file_type,
                            'error': f'HTTP {status}', 'file_url': file_url[:100]})
        except Exception as e:
            logger.exception(f'Upload failed for {file_type}')
            results.append({'ok': False, 'file_type': file_type, 'error': str(e)})

    uploaded = [r['file_type'] for r in results if r.get('ok')]
    if uploaded:
        ts = datetime.now().strftime('%d/%m/%Y %H:%M:%S')
        if STORAGE_TARGET == 'drive':
            # Audit trail only — no workflow, so the WorkDrive Deluge doesn't run
            update_deal(deal_id, {'Latest_Action_Sync':
                        f'Drive — {", ".join(uploaded)} filed — {ts}'})
        else:
            update_deal(deal_id, {'Latest_Action_Sync':
                        f'File Uploader — {", ".join(uploaded)} — {ts}'},
                        fire_workflow=True)

    return {'ok': bool(uploaded), 'deal_id': deal_id, 'storage': STORAGE_TARGET,
            'drive_folder_id': folder_id, 'drive_folder': folder_how,
            'results': results}


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

def _deal_id_from(body):
    deal_id = str(body.get('deal_id', '')).strip()
    return deal_id if deal_id.isdigit() and len(deal_id) >= 5 else None


@app.route('/upload', methods=['POST'])
def upload_single():
    if not check_auth():
        return jsonify({'ok': False, 'error': 'Unauthorized'}), 401
    body = request.get_json(silent=True)
    if not body:
        return jsonify({'ok': False, 'error': 'Missing JSON body'}), 400
    deal_id = _deal_id_from(body)
    if not deal_id:
        return jsonify({'ok': False, 'error': 'Missing or invalid deal_id'}), 400

    out = process_files(deal_id, [{
        'url': body.get('file_url', ''),
        'type': body.get('file_type', 'unknown'),
        'filename': body.get('filename', ''),
    }])
    return jsonify(out), 200


@app.route('/upload-batch', methods=['POST'])
def upload_batch():
    if not check_auth():
        return jsonify({'ok': False, 'error': 'Unauthorized'}), 401
    body = request.get_json(silent=True)
    if not body:
        return jsonify({'ok': False, 'error': 'Missing JSON body'}), 400
    deal_id = _deal_id_from(body)
    if not deal_id:
        return jsonify({'ok': False, 'error': 'Missing or invalid deal_id'}), 400

    return jsonify(process_files(deal_id, body.get('files', []))), 200


@app.route('/health', methods=['GET'])
def health():
    return jsonify({'status': 'ok', 'service': 'file-uploader',
                    'storage': STORAGE_TARGET}), 200


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port, debug=False)
