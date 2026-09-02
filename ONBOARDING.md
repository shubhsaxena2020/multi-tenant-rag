# Onboarding: Tenant Admin to Working Widget

## Prerequisites

- RAG Service instance running at `https://<host>/api/v1`
- Admin API key (starts with `rk_` for server-side use)
- A text editor and terminal

## Step 1: Create a Tenant

```bash
# Using the Python SDK
from sdk import RagClient

c = RagClient(
    base_url="https://<host>/api/v1",
    api_key="rk_<admin-key>"
)

# Create a new tenant with branding
branding = {
    "accent": "#3b82f6",
    "accent_text": "#ffffff",
    "header_title": "My Company"
}

tenant = c.create_tenant(
    name="my-company",
    plan="standard",
    allowed_groups=["users"],
    branding=branding
)

print(f"Tenant ID: {tenant['tenant_id']}")
print(f"API Key: {tenant['api_key']}")
```

```bash
# Using curl (Admin-Key required)
curl -X POST "https://<host>/api/v1/tenants" \
  -H "Admin-Key: sk_<admin-key>" \
  -H "Content-Type: application/json" \
  -d '{"name": "my-company", "plan": "standard"}'
```

**Response:**
```json
{
  "tenant_id": "t_<uuid>",
  "api_key": "rk_<user-key>",
  "name": "my-company",
  "plan": "standard",
  "branding": {"accent": "#3b82f6", ...}
}
```

## Step 2: Ingest Content

```bash
# Ingest text content
curl -X POST "https://<host>/api/v1/t_<uuid>/documents" \
  -H "Authorization: Bearer rk_<user-key>" \
  -H "Content-Type: application/json" \
  -d '{"title": "FAQ", "content": "Our refund policy is 30 days."}'
```

```bash
# Or via Python SDK
c.ingest_text(tenant="my-company", title="FAQ", content="Our refund policy is 30 days.")
```

## Step 3: Test with the Widget

Embed the widget in a webpage:

```html
<!-- Include the widget loader script -->
<script data-api-key="rk_<user-key>" data-tenant="t_<uuid>" 
        src="https://<host>/widget.html"></script>
```

Or programmatically:

```javascript
// Initialize widget with explicit config
const widget = new RagWidget({
    baseUrl: "https://<host>/api/v1",
    tenant: "t_<uuid>",
    apiKey: "rk_<user-key>"
});
```

## Step 4: Verify It Works

The widget should display:
- A chat interface with input at the bottom
- Queries sent to the RAG service with streaming responses
- Citations shown as clickable links
- Feedback controls (thumbs up/down)

## Troubleshooting

| Symptom | Likely Cause | Fix |
|---------|-------------|-----|
| Widget shows "No Admin-Key" | Missing or invalid Admin-Key | Verify the key is correct and has Admin-Key header access |
| No responses from widget | API endpoint unavailable or key expired | Check service health and key validity |
| Citations not clickable | Source URLs not http(s) | Ensure source URLs use http/https protocol |
| Multi-turn session lost | localStorage unavailable or cleared | Check browser localStorage quota/permissions |

## Need Help?

- Check the admin console at `/admin/console` for tenant status
- View API docs at `/api/v1/openapi.json` (Admin-Key required)
- Run `pytest tests/test_phase_e_admin_console.py` to verify endpoints
