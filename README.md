# Zoho File Uploader — Cloud Run Service

Downloads file bytes from URLs and uploads them as **real multipart attachments** to Zoho CRM Deals. Replaces the `attachmentUrl` approach (which only creates link-type attachments) with actual file uploads that the WorkDrive Deluge function can move.

## Why This Exists

Barbara's secure PDF system moves generated PDFs (NDAs, idea review forms) to private storage and deletes the originals after 5 minutes. The old `attachmentUrl` method told Zoho to fetch from the URL, but Zoho only stored a link reference — which became a 404 after deletion.

This service **downloads the actual file bytes** within seconds of submission and uploads them as real files to Zoho. Barbara's security stays intact.

---

## Deployment

### 1. Create a GitHub repo

Create `zoho-file-uploader` in the `kc-innovate-Design` org. Push these three files:
- `main.py`
- `Dockerfile`
- `requirements.txt`

### 2. Deploy to Cloud Run

**Option A — Cloud Shell (quickest):**

```bash
cd zoho-file-uploader

gcloud run deploy zoho-file-uploader \
  --source . \
  --region europe-west2 \
  --project idea-submission-automation \
  --allow-unauthenticated \
  --set-env-vars "SERVICE_SECRET=YOUR_SECRET_HERE" \
  --set-env-vars "ZOHO_CLIENT_ID=YOUR_CLIENT_ID" \
  --set-env-vars "ZOHO_CLIENT_SECRET=YOUR_CLIENT_SECRET" \
  --set-env-vars "ZOHO_REFRESH_TOKEN=YOUR_REFRESH_TOKEN" \
  --memory 512Mi \
  --timeout 120
```

**Option B — GitHub auto-deploy:**
Set up GCP Developer Connect (same as wpformsProxy) to auto-deploy on push to main.

### 3. Set environment variables

| Variable | Source |
|----------|--------|
| `SERVICE_SECRET` | Generate a new random string (e.g. `openssl rand -hex 32`) |
| `ZOHO_CLIENT_ID` | From Secret Manager or api-console.zoho.com |
| `ZOHO_CLIENT_SECRET` | From Secret Manager or api-console.zoho.com |
| `ZOHO_REFRESH_TOKEN` | From Secret Manager — scope: `ZohoCRM.modules.ALL` |

### 4. Test

```bash
# Health check
curl https://zoho-file-uploader-657178339595.europe-west2.run.app/health

# Single upload test (use a test Deal ID and a real file URL)
curl -X POST https://zoho-file-uploader-657178339595.europe-west2.run.app/upload \
  -H "Content-Type: application/json" \
  -H "X-Service-Secret: YOUR_SECRET_HERE" \
  -d '{
    "deal_id": "287850000178089003",
    "file_url": "https://innovate-design.co.uk/wp-content/uploads/wpforms/326275-xxx/test.jpg",
    "file_type": "attachment"
  }'
```

---

## Integration with GCP Application Integration

### Option A: Replace Tasks 20/24/25 with a single batch call

Replace all three upload tasks with ONE REST task that calls `/upload-batch`:

**URL:** `https://zoho-file-uploader-{...}.europe-west2.run.app/upload-batch`
**Method:** POST
**Headers:** `X-Service-Secret: {your secret}`
**Body:**
```json
{
  "deal_id": "$zoho_deal_id$",
  "files": [
    {"url": "$clean_attachment_url$", "type": "attachment"},
    {"url": "$review_form_url$",      "type": "review_form"},
    {"url": "$nda_url$",              "type": "nda"}
  ]
}
```

This handles all three files in one call, skips empty URLs gracefully, and triggers WorkDrive sync once at the end.

### Option B: Keep Tasks 20/24/25 separate, change URLs

Update each task's URL from:
```
https://www.zohoapis.eu/crm/v6/Deals/$zoho_deal_id$/Attachments?attachmentUrl=...
```
to:
```
https://zoho-file-uploader-{...}.europe-west2.run.app/upload
```

With body:
```json
{
  "deal_id": "$zoho_deal_id$",
  "file_url": "$clean_attachment_url$",
  "file_type": "attachment"
}
```

### Option A is recommended — fewer tasks, one sync trigger, simpler.

---

## How Task 21 Changes

If using `/upload-batch`, the service triggers WorkDrive sync automatically after uploading. You can **remove Task 21** entirely, or keep it as a fallback.

If using `/upload` per file, set `"trigger_sync": true` on the LAST upload task only.

---

## Error Handling

| Scenario | Behaviour |
|----------|-----------|
| Empty/missing `file_url` | Returns `200` with `skipped: true` — non-fatal |
| File download 404 | Returns `200` with `ok: false` — file already deleted |
| File too large (>20MB) | Returns `400` |
| Zoho upload failure | Returns `502` with Zoho error detail |
| Zoho token expired | Auto-refreshes and retries |

The service never throws a 500 on empty URLs — this matches the existing "non-fatal" behaviour of Tasks 20/24/25.

---

## Security

- Pre-shared secret in `X-Service-Secret` header (same pattern as wpformsProxy)
- Zoho credentials in environment variables (move to Secret Manager for production)
- Only downloads from HTTPS URLs
- File size capped at 20MB
- No file storage — bytes are held in memory only during the request
