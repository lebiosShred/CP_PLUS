# CP Plus Agent Backend

This directory contains the backend services for the CP Plus Agentic Suite.

## Structure
- `unified_backend.py`: The main entry point. Combines RFP analysis, Product lookup, and Matcher logic into a single Flask application.
- `master_openapi.json`: The OpenAPI specification for the backend services (must stay in sync with `cp_plus_tools_unified.yaml`).
- `requirements.txt`: Python dependencies.
- `mistral_agents.py`: Scaffolding for Mistral-hosted agents (web search, code execution). Not yet integrated.
- `rfp_pdfs/` & `product_sheet/`: Directories for storing source documents.
- `_legacy/`: Contains deprecated scripts (`gateway.py`, `matcher_caller.py`, etc.).

## How to Run
1.  **Install Dependencies:**
    ```bash
    pip install -r requirements.txt
    ```
2.  **Configure Environment:**
    ```bash
    cp ../env.example ../.env
    # Edit ../.env with your API keys (see .env.example for all required vars)
    ```
3.  **Start the Server:**
    ```bash
    python unified_backend.py
    ```
    The server will start on port **8100**.

4.  **Expose via ngrok:**
    ```bash
    ngrok http 8100
    ```
    Then run `update_ngrok_url.ps1` to auto-patch both OpenAPI specs:
    ```powershell
    powershell -ExecutionPolicy Bypass -File ../cp_plus_poc/update_ngrok_url.ps1
    ```

## API Endpoints
-   `POST /agent/run`: **Unified entry point** — routes to RFP, Product, or Matcher via the `tool` field.
-   `POST /rfp/run`: Analyze RFP documents (direct access). Actions: `rfp_chat`, `list_rfps`, `rfp_focus_section`.
-   `POST /product/run`: Search and retrieve product datasheets (direct access). Actions: `product_chat`, `list_products`, `product_search`.
-   `POST /matcher/run`: Perform compliance cross-matching (direct access). Actions: `matcher_chat`, `crossmatch_rfp`.
-   `GET /health`: Health check and system status.
-   `GET /admin/export-training-data`: Export fine-tuning pairs (**requires `X-Admin-Key` header**).
-   `GET /admin/embeddings-status`: Embedding index diagnostics (**requires `X-Admin-Key` header**).

## LLM Fallback Chain
1. **Tier 1:** Mistral Primary (via `MISTRAL_API_KEY`)
2. **Tier 2:** Mistral Backup (via `MISTRAL_BACKUP_API_KEY`)
3. **Tier 3:** Gemini Flash (via `GEMINI_API_KEY`)

## Key Features
- **Smart Context Windowing** — Pages scored by keyword relevance, header pages always included.
- **Mistral OCR** — AI-powered PDF extraction for tables and scanned documents (set `USE_MISTRAL_OCR=true`).
- **Product Fingerprints** — Compact spec summaries used for fast pre-screening in the matcher.
- **Batch Embeddings** — Semantic search index built in a single API call at startup.
- **Log Rotation** — `app.log` auto-rotated at 5MB with 3 backups.

## Agent Integration
The `CP_Plus_Official` agent in watsonx Orchestrate is configured to use the `/agent/run` endpoint via the `cp_plus_tools_unified.yaml` OpenAPI definition.
