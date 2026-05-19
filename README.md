# CP Plus Agentic Suite

## Project Purpose
The CP Plus Agentic Suite is an AI-driven, multi-agent platform designed to automate and augment the bid management and RFP (Request for Proposal) response process for CP Plus. This system analyzes massive RFP documents, retrieves precise product specifications from datasheets, and performs cross-matching compliance checks to ensure CP Plus products meet the required technical criteria for large-scale tenders (e.g., government, enterprise, and infrastructure projects).

It connects IBM watsonx Orchestrate (via the `CP_Plus_Official` agent) to a unified Python backend powered by Mistral and Gemini LLMs.

## Architecture

The project is split into two primary domains:

1. **Agent Definitions (`cp_plus_poc/`)**
   - Contains the configuration for the watsonx Orchestrate agent (`cp_plus_agent.yaml`).
   - Contains the OpenAPI schema (`cp_plus_tools_unified.yaml`) which defines how the agent talks to the Python backend.
   - Scripts to dynamically update endpoint URLs (`update_ngrok_url.ps1`).

2. **Unified Backend (`backend_llm/`)**
   - The Flask-based core intelligence engine (`unified_backend.py`).
   - Ingests and processes massive PDFs (RFP documents, Product Datasheets, User Manuals) into localized ChromaDB vector databases for semantic search.
   - Handles three main skill domains:
     - **RFP Analysis:** Extracts core requirements, compliance lists, and scoring criteria from tenders.
     - **Product Lookup:** Queries local product catalogs and manuals for technical specifications.
     - **Matcher/Compliance Check:** Cross-references the RFP requirements against CP Plus product capabilities to generate automated compliance matrices.

## How It Works

1. **Data Ingestion:** PDFs in the `rfp_pdfs/` and `product_sheet/` directories are parsed (using PyMuPDF and Mistral OCR for complex tables).
2. **Vectorization:** The text is embedded and stored locally in ChromaDB instances (`*_vector_db/`) to enable high-speed semantic retrieval.
3. **Agent Invocation:** A user asks the `CP_Plus_Official` agent in watsonx to evaluate an RFP against a specific product.
4. **Tool Routing:** The agent hits the `POST /agent/run` endpoint on the backend, routing the query to the correct internal subsystem.
5. **LLM Chain:** The backend searches the vector DBs, builds a highly relevant context window, and passes it to Mistral (Tier 1) or Gemini (Tier 3 fallback) to generate the final response.

## Getting Started

1. Set up the Python environment:
   ```bash
   cd backend_llm/alt_arch_cp_plus_poc
   pip install -r requirements.txt
   ```
2. Configure your `.env` file (copy from `.env.example`).
3. Run the backend:
   ```bash
   python unified_backend.py
   ```
4. Expose the port (e.g., 8100) via ngrok and use `update_ngrok_url.ps1` to update the OpenAPI spec for watsonx Orchestrate.
