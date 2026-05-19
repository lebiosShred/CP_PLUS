# Watsonx Orchestrate Webchat Embedding Guide

This document explains how the CP Plus Agent was successfully embedded into a local web application using the watsonx Orchestrate webchat functionality.

## Architecture Overview

```
┌─────────────────┐     ┌──────────────┐     ┌─────────────────────┐
│  chat-ui.html   │────▶│    ngrok     │────▶│  unified_backend.py │
│  (localhost:8080)│     │ (public URL) │     │   (localhost:8100)  │
└────────┬────────┘     └──────────────┘     └─────────────────────┘
         │                                              │
         │ Embed Widget                                 │ Mistral AI
         ▼                                              ▼
┌─────────────────────────────────────┐     ┌─────────────────────┐
│   watsonx Orchestrate Cloud         │────▶│    │
│   (ap-southeast-1.watson-orchestrate)│     │   (LLM Processing)  │
└─────────────────────────────────────┘     └─────────────────────┘
```

---

## Step 1: Generate the Correct Embed Code

The embed code must be generated using the **ADK CLI**, not manually constructed.

```bash
orchestrate channels webchat embed --agent-name=CP_Plus_Official --env=live
```

This returns the official embed script with correct parameters:

```html
<script>
    window.wxOConfiguration = {
        orchestrationID: "20250731-1611-5869-4024-6e99f4269f1b_20251013-0928-4707-90f5-b7083dde17bd",
        hostURL: "https://ap-southeast-1.dl.watson-orchestrate.ibm.com",
        rootElementID: "root",
        showLauncher: true,
        chatOptions: {
            agentId: "62d46e48-fd30-4995-a97f-4e52cc7c1d08",
            agentEnvironmentId: "797b0065-b68f-4225-a3ee-28897a8a892b"
        }
    };

    setTimeout(function () {
        const script = document.createElement('script');
        script.src = `${window.wxOConfiguration.hostURL}/wxochat/wxoLoader.js?embed=true`;
        script.addEventListener('load', function () {
            wxoLoader.init();
        });
        document.head.appendChild(script);
    }, 0);
</script>
```

### Key Configuration Values

| Parameter | Value | Description |
|-----------|-------|-------------|
| `orchestrationID` | Composite UUID | Format: `{prefix}_{instanceId}` - **Must use the full composite ID** |
| `hostURL` | `https://ap-southeast-1.dl.watson-orchestrate.ibm.com` | **No `api.` prefix** |
| `agentId` | UUID | The specific agent's ID |
| `agentEnvironmentId` | UUID | The environment (draft/live) |

---

## Step 2: Disable Webchat Security (for Local Development)

By default, webchat security blocks anonymous/localhost access. Disable it via the API:

```powershell
# Use the configure_security.ps1 script or direct API call
$headers = @{
    "Authorization" = "ZenApiKey YOUR_API_KEY"
    "Content-Type" = "application/json"
}

$body = @{
    security_enabled = $false
} | ConvertTo-Json

Invoke-RestMethod -Method Put `
    -Uri "https://api.ap-southeast-1.dl.watson-orchestrate.ibm.com/instances/{INSTANCE_ID}/webchat/security" `
    -Headers $headers `
    -Body $body
```

> **Note:** The security API uses `api.` prefix, but the embed `hostURL` does NOT.

---

## Step 3: Configure Tools with ngrok URL

The agent's tools (OpenAPI endpoints) must point to a publicly accessible URL:

1. **Start ngrok to expose local backend:**
   ```bash
   ngrok http --region ap 8100
   ```

2. **Update `cp_plus_tools.json` with ngrok URL:**
   ```json
   {
     "servers": [
       {
         "url": "https://YOUR_NGROK_URL.ngrok-free.app"
       }
     ]
   }
   ```

3. **Import updated tools:**
   ```bash
   orchestrate tools import --kind openapi --file cp_plus_tools.json
   ```

4. **Re-deploy the agent:**
   ```bash
   orchestrate agents deploy --name CP_Plus_Official
   ```

---

## Step 4: Enable CORS on Backend

The backend needs CORS headers for browser requests:

```python
from flask import Flask
from flask_cors import CORS

app = Flask(__name__)
CORS(app)  # Enable CORS for all routes
```

Install: `pip install flask-cors`

---

## Step 5: Update the LLM Model

If the model is unsupported, update `cp_plus_agent.yaml`:

```yaml
llm: watsonx/openai/gpt-oss-120b
```

Then re-import and deploy:
```bash
orchestrate agents import -f cp_plus_agent.yaml
orchestrate agents deploy --name CP_Plus_Official
```

---

## Troubleshooting

### Issue: Widget appears but shows "localhost connection refused"
**Cause:** Tools are cached with old localhost URL  
**Fix:** Re-import tools with updated ngrok URL and redeploy agent

### Issue: Widget doesn't appear
**Cause:** Incorrect `orchestrationID` or `hostURL`  
**Fix:** Use the CLI to generate fresh embed code:
```bash
orchestrate channels webchat embed --agent-name=CP_Plus_Official --env=live
```

### Issue: "Error processing message"
**Cause:** Security is enabled blocking anonymous access  
**Fix:** Disable webchat security via API

### Issue: ngrok URL changed
**Fix:** Every time ngrok restarts, update and re-import tools:
```bash
# Update cp_plus_tools.json with new URL
orchestrate tools import --kind openapi --file cp_plus_tools.json
```

---

## File Reference

| File | Purpose |
|------|---------|
| `chat-ui.html` | Main HTML page with embedded webchat widget |
| `cp_plus_tools.json` | OpenAPI specification with tool endpoints |
| `cp_plus_agent.yaml` | Agent configuration (name, LLM, tools) |
| `unified_backend.py` | Flask backend with RFP/Product/Crossmatch APIs |
| `configure_security.ps1` | PowerShell script to manage webchat security |

---

## Quick Start Commands

```bash
# 1. Start backend
cd backend_llm/alt_arch_cp_plus_poc
python unified_backend.py

# 2. Start ngrok
ngrok http --region ap 8100

# 3. Update tools with new ngrok URL (edit cp_plus_tools.json first)
orchestrate tools import --kind openapi --file cp_plus_tools.json

# 4. Serve the web UI
cd cp_plus_poc
python -m http.server 8080

# 5. Open browser
# http://localhost:8080/chat-ui.html
```
