"""
Zoho File Uploader — Cloud Run Service

Downloads file bytes from a URL and uploads them as real multipart
attachments to a Zoho CRM Deal. Replaces the attachmentUrl approach
(which only creates link-type attachments) with actual file uploads
that the WorkDrive Deluge function can move.

Designed to work alongside Barbara's secure PDF system:
  - Downloads from original URLs (within the 5-min window before deletion)
  - Downloads from tokenised URLs (Barbara's secure endpoint)
  - Downloads from any public URL (client photo uploads)

Endpoints:
  POST /upload         — Upload a single file to a Deal
  POST /upload-batch   — Upload multiple files to a Deal in one call
  GET  /health         — Health check
"""

import os
import io
import time
import logging
import requests
from flask import Flask, request, jsonify

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger('zoho-file-uploader')

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Pre-shared secret for authenticating requests from GCP Application Integration
SERVICE_SECRET = os.environ.get('SERVICE_SECRET', '')

# Zoho OAuth (EU data centre)
ZOHO_CLIENT_ID     = os.environ.get('ZOHO_CLIENT_ID', '')
ZOHO_CLIENT_SECRET = os.environ.get('ZOHO_CLIENT_SECRET', '')
ZOHO_REFRESH_TOKEN = os.environ.get('ZOHO_REFRESH_TOKEN', '')
ZOHO_TOKEN_URL     = 'https://accounts.zoho.eu/oauth/v2/token'
ZOHO_API_BASE      = 'https://www.zohoapis.eu/crm/v6'

# File download settings
DOWNLOAD_TIMEOUT  = 30   # seconds
MAX_FILE_SIZE     = 20 * 1024 * 1024  # 20 MB

# Token cache (in-memory, refreshed when expired)
_token_cache = {
    'access_token': None,
    'expires_at': 0
}


# ---------------------------------------------------------------------------
# Zoho OAuth
# ---------------------------------------------------------------------------

def get_zoho_token():
    """Refresh and cache a Zoho access token."""
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
    _token_cache['expires_at'] = now + 3300  # 55 minutes
    logger.info('Zoho token refreshed successfully')
    return data['access_token']


# ---------------------------------------------------------------------------
# Auth middleware
# ---------------------------------------------------------------------------

def check_auth():
    """Validate the pre-shared secret from the request."""
    if not SERVICE_SECRET:
        return True  # No secret configured = open (dev mode)

    token = request.headers.get('X-Service-Secret', '')
    if not token:
        body = request.get_json(silent=True) or {}
        token = body.get('service_secret', '')

    return token == SERVICE_SECRET


# ---------------------------------------------------------------------------
# File download
# ---------------------------------------------------------------------------

def download_file(url):
    """
    Download file bytes from a URL.
    Works with:
      - Original WPForms URLs (before 5-min deletion)
      - Barbara's tokenised URLs (?innovate_pdf=...)
      - Direct client upload URLs
    Returns (bytes, content_type, filename) or raises.
    """
    logger.info(f'Downloading: {url[:100]}...')

    resp = requests.get(url, timeout=DOWNLOAD_TIMEOUT, stream=True,
                        allow_redirects=True)
    resp.raise_for_status()

    # Check size
    content_length = resp.headers.get('Content-Length')
    if content_length and int(content_length) > MAX_FILE_SIZE:
        raise ValueError(f'File too large: {content_length} bytes')

    # Read bytes
    file_bytes = resp.content
    if len(file_bytes) > MAX_FILE_SIZE:
        raise ValueError(f'File too large: {len(file_bytes)} bytes')

    # Determine content type
    content_type = resp.headers.get('Content-Type', 'application/octet-stream')
    # Strip charset etc
    if ';' in content_type:
        content_type = content_type.split(';')[0].strip()

    # Determine filename
    filename = None
    cd = resp.headers.get('Content-Disposition', '')
    if 'filename=' in cd:
        # Parse Content-Disposition header
        parts = cd.split('filename=')
        if len(parts) > 1:
            filename = parts[1].strip('" ')

    if not filename:
        # Extract from URL path
        from urllib.parse import urlparse, unquote
        path = urlparse(url).path
        if path and '/' in path:
            filename = unquote(path.split('/')[-1])

    if not filename or filename == '/':
        # Last resort
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
# Zoho upload
# ---------------------------------------------------------------------------

def upload_to_zoho(deal_id, file_bytes, filename, content_type):
    """
    Upload file bytes to a Zoho CRM Deal as a real multipart attachment.
    Returns the Zoho API response.
    """
    token = get_zoho_token()
    url = f'{ZOHO_API_BASE}/Deals/{deal_id}/Attachments'

    files = {
        'file': (filename, io.BytesIO(file_bytes), content_type)
    }

    resp = requests.post(url, files=files, headers={
        'Authorization': f'Zoho-oauthtoken {token}'
    }, timeout=30)

    logger.info(f'Zoho upload response: {resp.status_code} — {resp.text[:300]}')
    return resp.status_code, resp.json() if resp.text else {}


def trigger_workdrive_sync(deal_id, sync_message):
    """
    Update Latest_Action_Sync on the Deal with trigger: [workflow].
    This fires the Deluge move_attachments_to_workdrive function.
    """
    token = get_zoho_token()
    url = f'{ZOHO_API_BASE}/Deals'

    body = {
        'data': [{
            'id': deal_id,
            'Latest_Action_Sync': sync_message
        }],
        'trigger': ['workflow']
    }

    resp = requests.put(url, json=body, headers={
        'Authorization': f'Zoho-oauthtoken {token}',
        'Content-Type': 'application/json'
    }, timeout=15)

    logger.info(f'WorkDrive sync trigger: {resp.status_code}')
    return resp.status_code


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.route('/upload', methods=['POST'])
def upload_single():
    """
    Upload a single file to a Zoho CRM Deal.

    Request body (JSON):
    {
        "deal_id":   "287850000178089003",
        "file_url":  "https://innovate-design.co.uk/wp-content/uploads/wpforms/pdf/...",
        "file_type": "nda|review_form|attachment",   (optional, for logging)
        "filename":  "custom-name.pdf",               (optional, overrides auto-detect)
        "trigger_sync": true                           (optional, triggers WorkDrive sync)
    }
    """
    if not check_auth():
        return jsonify({'ok': False, 'error': 'Unauthorized'}), 401

    body = request.get_json(silent=True)
    if not body:
        return jsonify({'ok': False, 'error': 'Missing JSON body'}), 400

    deal_id   = str(body.get('deal_id', '')).strip()
    file_url  = str(body.get('file_url', '')).strip()
    file_type = str(body.get('file_type', 'unknown')).strip()
    custom_fn = str(body.get('filename', '')).strip()
    trigger   = body.get('trigger_sync', False)

    if not deal_id or len(deal_id) < 5:
        return jsonify({'ok': False, 'error': 'Missing or invalid deal_id'}), 400

    if not file_url or not file_url.startswith('http'):
        return jsonify({
            'ok': False,
            'error': 'Missing or invalid file_url',
            'file_type': file_type,
            'skipped': True
        }), 200  # 200 not 400 — empty URLs are expected (non-fatal)

    try:
        # 1. Download the file bytes
        file_bytes, content_type, filename = download_file(file_url)
        if custom_fn:
            filename = custom_fn

        # 2. Upload to Zoho as multipart
        status_code, zoho_resp = upload_to_zoho(deal_id, file_bytes, filename, content_type)

        if status_code < 200 or status_code >= 300:
            return jsonify({
                'ok': False,
                'error': f'Zoho upload failed: HTTP {status_code}',
                'zoho_response': zoho_resp,
                'file_type': file_type
            }), 502

        # 3. Optionally trigger WorkDrive sync
        if trigger:
            from datetime import datetime
            ts = datetime.now().strftime('%d/%m/%Y %H:%M:%S')
            sync_msg = f'File Uploader — {file_type} uploaded — {ts}'
            trigger_workdrive_sync(deal_id, sync_msg)

        return jsonify({
            'ok': True,
            'file_type': file_type,
            'filename': filename,
            'size_bytes': len(file_bytes),
            'zoho_status': status_code,
            'deal_id': deal_id
        }), 200

    except requests.exceptions.HTTPError as e:
        status = e.response.status_code if e.response else 0
        return jsonify({
            'ok': False,
            'error': f'Download failed: HTTP {status}',
            'file_url': file_url[:100],
            'file_type': file_type
        }), 200 if status == 404 else 502

    except Exception as e:
        logger.exception(f'Upload failed for {file_type}')
        return jsonify({
            'ok': False,
            'error': str(e),
            'file_type': file_type
        }), 500


@app.route('/upload-batch', methods=['POST'])
def upload_batch():
    """
    Upload multiple files to a Zoho CRM Deal in one call.
    Triggers WorkDrive sync once after all uploads.

    Request body (JSON):
    {
        "deal_id": "287850000178089003",
        "files": [
            {"url": "https://...", "type": "nda"},
            {"url": "https://...", "type": "review_form"},
            {"url": "https://...", "type": "attachment"}
        ]
    }
    """
    if not check_auth():
        return jsonify({'ok': False, 'error': 'Unauthorized'}), 401

    body = request.get_json(silent=True)
    if not body:
        return jsonify({'ok': False, 'error': 'Missing JSON body'}), 400

    deal_id = str(body.get('deal_id', '')).strip()
    files   = body.get('files', [])

    if not deal_id or len(deal_id) < 5:
        return jsonify({'ok': False, 'error': 'Missing or invalid deal_id'}), 400

    results = []
    any_success = False

    for f in files:
        file_url  = str(f.get('url', '')).strip()
        file_type = str(f.get('type', 'unknown')).strip()
        custom_fn = str(f.get('filename', '')).strip()

        if not file_url or not file_url.startswith('http'):
            results.append({
                'ok': False,
                'file_type': file_type,
                'skipped': True,
                'reason': 'Empty or invalid URL'
            })
            continue

        try:
            file_bytes, content_type, filename = download_file(file_url)
            if custom_fn:
                filename = custom_fn

            status_code, zoho_resp = upload_to_zoho(deal_id, file_bytes, filename, content_type)

            success = 200 <= status_code < 300
            if success:
                any_success = True

            results.append({
                'ok': success,
                'file_type': file_type,
                'filename': filename,
                'size_bytes': len(file_bytes),
                'zoho_status': status_code
            })

        except requests.exceptions.HTTPError as e:
            status = e.response.status_code if e.response else 0
            results.append({
                'ok': False,
                'file_type': file_type,
                'error': f'Download HTTP {status}',
            })

        except Exception as e:
            logger.exception(f'Batch upload failed for {file_type}')
            results.append({
                'ok': False,
                'file_type': file_type,
                'error': str(e)
            })

    # Trigger WorkDrive sync once if any file uploaded successfully
    if any_success:
        from datetime import datetime
        ts = datetime.now().strftime('%d/%m/%Y %H:%M:%S')
        uploaded = [r['file_type'] for r in results if r.get('ok')]
        sync_msg = f'File Uploader — {", ".join(uploaded)} — {ts}'
        trigger_workdrive_sync(deal_id, sync_msg)

    return jsonify({
        'ok': any_success,
        'deal_id': deal_id,
        'results': results
    }), 200


@app.route('/health', methods=['GET'])
def health():
    """Health check endpoint."""
    return jsonify({'status': 'ok', 'service': 'zoho-file-uploader'}), 200


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port, debug=False)
