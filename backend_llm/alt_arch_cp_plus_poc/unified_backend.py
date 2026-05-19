import logging
import os
import json
import time
import random
import re
import base64
import math
import threading

from collections import OrderedDict
from flask import Flask, jsonify, request
from flask_cors import CORS
from mistralai import Mistral
from pypdf import PdfReader
from dotenv import load_dotenv

# =============================================================================
# Configuration
# =============================================================================
load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), "..", ".env"))

MISTRAL_API_KEY = os.environ.get("MISTRAL_API_KEY")
if not MISTRAL_API_KEY:
    raise EnvironmentError(
        "MISTRAL_API_KEY not set. Create a .env file in the backend_llm/ directory "
        "with your key. See .env.example for the template."
    )

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

MISTRAL_MODEL = os.environ.get("MISTRAL_MODEL", "mistral-large-latest")
MISTRAL_MODEL_HEAVY = os.environ.get("MISTRAL_MODEL_HEAVY", "mistral-large-latest")
MISTRAL_MODEL_LIGHT = os.environ.get("MISTRAL_MODEL_LIGHT", "mistral-small-latest")
USE_MISTRAL_OCR = os.environ.get("USE_MISTRAL_OCR", "true").lower() == "true"
COLLECT_TRAINING_DATA = os.environ.get("COLLECT_TRAINING_DATA", "false").lower() == "true"

# Gemini as FINAL fallback only (used if both Mistral keys fail)
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
RFP_DIR = "rfp_pdfs"
PRODUCT_DIR = "product_sheet"
OUTPUT_DIR = "output"
MAX_REQUEST_BYTES = 1_048_576  # 1 MB
FILE_LIST_TTL = 30  # seconds before refreshing the directory listing cache

ALLOWED_ORIGINS = os.environ.get(
    "ALLOWED_ORIGINS",
    "https://*.ngrok-free.app,https://*.watson-orchestrate.ibm.com"
).split(",")

from logging.handlers import RotatingFileHandler
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s %(message)s",
    handlers=[
        logging.StreamHandler(),
        RotatingFileHandler("app.log", maxBytes=5_242_880, backupCount=3),
    ]
)
log = logging.getLogger("unified-backend")

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 100 * 1024 * 1024  # 100 MB upload limit
CORS(app, origins=ALLOWED_ORIGINS)

# --- Rate Limiting ---
try:
    from flask_limiter import Limiter
    from flask_limiter.util import get_remote_address
    limiter = Limiter(get_remote_address, app=app, default_limits=["60 per minute"],
                      storage_uri="memory://")
    log.info("Rate limiter enabled (60 req/min default)")
except ImportError:
    limiter = None
    log.warning("flask-limiter not installed. Rate limiting disabled. pip install flask-limiter")

# --- Admin API Key (protects /admin/* endpoints) ---
ADMIN_API_KEY = os.environ.get("ADMIN_API_KEY", "")

def require_admin(f):
    """Decorator to require X-Admin-Key header for sensitive endpoints."""
    from functools import wraps
    @wraps(f)
    def decorated(*args, **kwargs):
        if not ADMIN_API_KEY:
            return jsonify({"error": "Admin endpoints disabled. Set ADMIN_API_KEY in .env"}), 403
        if request.headers.get("X-Admin-Key") != ADMIN_API_KEY:
            return jsonify({"error": "Unauthorized. Provide valid X-Admin-Key header."}), 401
        return f(*args, **kwargs)
    return decorated

# --- Mistral Primary Client ---
mistral_client = Mistral(api_key=MISTRAL_API_KEY)
log.info(f"Mistral PRIMARY client initialized: {MISTRAL_MODEL}")

# --- Mistral Backup Client (separate API key for rate-limit resilience) ---
# NOTE: Set MISTRAL_BACKUP_API_KEY in your .env file for Tier 2 fallback.
MISTRAL_BACKUP_KEY = os.environ.get("MISTRAL_BACKUP_API_KEY")
mistral_backup_client = None
if MISTRAL_BACKUP_KEY:
    mistral_backup_client = Mistral(api_key=MISTRAL_BACKUP_KEY)
    log.info("Mistral BACKUP client initialized (separate API key)")

# --- Gemini Final Fallback ---
gemini_model_client = None
if GEMINI_API_KEY:
    try:
        import google.generativeai as genai
        genai.configure(api_key=GEMINI_API_KEY)
        gemini_model_client = genai.GenerativeModel(GEMINI_MODEL)
        log.info(f"Gemini FINAL FALLBACK enabled: {GEMINI_MODEL}")
    except ImportError:
        log.warning("google-generativeai not installed. Gemini fallback disabled. "
                    "Install with: pip install google-generativeai")
    except Exception as e:
        log.warning(f"Gemini init failed: {e}. Fallback disabled.")
else:
    log.warning("GEMINI_API_KEY not set. Gemini final fallback disabled.")

# =============================================================================
# PDF Text Cache — avoids re-extracting PDFs on every request (LRU-evicted)
# =============================================================================
PDF_CACHE_MAX_ENTRIES = 100  # evict oldest entries when cache exceeds this
_pdf_cache: OrderedDict[str, tuple[float, str]] = OrderedDict()  # path -> (mtime, full_text)
_pdf_pages_cache: OrderedDict[str, tuple[float, list[str]]] = OrderedDict()  # path -> (mtime, [page_texts])
_ocr_cache: dict[str, str] = {}  # path -> OCR markdown text (higher quality than pypdf)

# P5: Training data buffer for fine-tuning export
_training_log: list[dict] = []


def _evict_pdf_cache():
    """Evicts oldest entries if cache exceeds max size."""
    while len(_pdf_cache) > PDF_CACHE_MAX_ENTRIES:
        _pdf_cache.popitem(last=False)
    while len(_pdf_pages_cache) > PDF_CACHE_MAX_ENTRIES:
        _pdf_pages_cache.popitem(last=False)


def extract_text_from_pdf(pdf_path: str) -> str:
    """Extracts text from a local PDF file, with mtime-based caching.
    Prefers OCR-cached text when available (higher quality)."""
    try:
        abs_path = os.path.abspath(pdf_path)

        # Prefer OCR cache (Markdown-structured, table-aware) over pypdf
        if abs_path in _ocr_cache:
            return _ocr_cache[abs_path]

        mtime = os.path.getmtime(abs_path)

        if abs_path in _pdf_cache:
            cached_mtime, cached_text = _pdf_cache[abs_path]
            if cached_mtime == mtime:
                return cached_text

        log.info(f"Extracting text from: {os.path.basename(abs_path)}")
        reader = PdfReader(abs_path)
        pages = []
        for page in reader.pages:
            page_text = page.extract_text() or ""
            pages.append(page_text)

        full_text = "\n".join(pages)
        _pdf_cache[abs_path] = (mtime, full_text)
        _pdf_pages_cache[abs_path] = (mtime, pages)
        _evict_pdf_cache()
        return full_text
    except Exception as e:
        err_type = type(e).__name__
        if "PdfRead" in err_type or "EOF" in str(e) or "password" in str(e).lower():
            log.error(f"PDF corrupted or password-protected: {os.path.basename(pdf_path)}: {e}")
            return f"[Error: This PDF appears to be corrupted or password-protected: {os.path.basename(pdf_path)}]"
        log.error(f"Failed to extract text from {pdf_path}: {e}")
        return ""


# =============================================================================
# P2: Mistral OCR — AI-powered PDF extraction (tables, layouts, scanned docs)
# =============================================================================
def _try_ocr_extract(abs_path: str) -> str | None:
    """Attempts to extract text from a PDF using Mistral OCR API.
    Returns Markdown-structured text or None on failure."""
    if not USE_MISTRAL_OCR:
        return None
    try:
        with open(abs_path, "rb") as f:
            pdf_data = base64.b64encode(f.read()).decode()
        log.info(f"OCR extracting: {os.path.basename(abs_path)}")
        ocr_response = mistral_client.ocr.process(
            model="mistral-ocr-latest",
            document={
                "type": "document_url",
                "document_url": f"data:application/pdf;base64,{pdf_data}",
            },
        )
        # Combine all page markdown into a single string
        text = "\n\n".join(page.markdown for page in ocr_response.pages)
        log.info(f"OCR extracted {len(text)} chars from {os.path.basename(abs_path)}")
        return text
    except AttributeError:
        log.warning("Mistral OCR not available in installed SDK. Run: pip install --upgrade mistralai")
        return None
    except Exception as e:
        log.warning(f"OCR failed for {os.path.basename(abs_path)}: {e}")
        return None


def _ocr_prewarm_all():
    """Background OCR extraction for all PDFs. Progressively upgrades text quality."""
    count = 0
    for directory in (RFP_DIR, PRODUCT_DIR):
        if not os.path.isdir(directory):
            continue
        for fname in sorted(os.listdir(directory)):
            if not fname.lower().endswith(".pdf"):
                continue
            abs_path = os.path.abspath(os.path.join(directory, fname))
            if abs_path not in _ocr_cache:
                text = _try_ocr_extract(abs_path)
                if text:
                    _ocr_cache[abs_path] = text
                    count += 1
    log.info(f"OCR prewarm complete: {count} documents upgraded to OCR quality.")


def get_pdf_pages(pdf_path: str) -> list[str]:
    """Returns per-page text from a PDF, using cache."""
    abs_path = os.path.abspath(pdf_path)
    if abs_path not in _pdf_pages_cache:
        extract_text_from_pdf(pdf_path)  # populates both caches
    if abs_path in _pdf_pages_cache:
        return _pdf_pages_cache[abs_path][1]
    return []


def prewarm_pdf_cache():
    """Pre-extracts all PDFs at startup so first requests have zero extraction delay."""
    count = 0
    for directory in (RFP_DIR, PRODUCT_DIR):
        if not os.path.isdir(directory):
            continue
        for fname in os.listdir(directory):
            if fname.lower().endswith(".pdf"):
                extract_text_from_pdf(os.path.join(directory, fname))
                count += 1
    log.info(f"Pre-warmed PDF cache: {count} documents ready.")
    # Build product fingerprints after PDFs are cached
    build_product_fingerprints()
    # P3: Build embedding index from fingerprints
    build_product_embeddings()
    # P2: OCR extraction in background (upgrades text quality progressively)
    if USE_MISTRAL_OCR:
        threading.Thread(target=_ocr_prewarm_all, daemon=True).start()
        log.info("OCR prewarm started in background thread.")
    # P0.2: LLM-powered structured spec extraction in background
    threading.Thread(target=_llm_spec_prewarm, daemon=True).start()
    log.info("LLM spec extraction started in background thread.")


# =============================================================================
# Product Fingerprints — compact spec summaries for fast crossmatch
# =============================================================================
_product_fingerprints: dict[str, str] = {}  # filename -> compact spec summary


def _extract_fingerprint(pdf_path: str) -> str:
    """Extracts a compact structured spec summary from a product PDF.
    Focuses on the comparison-relevant fields only.
    This is the FAST regex-based extractor used for embedding generation."""
    text = extract_text_from_pdf(pdf_path)
    if not text:
        return f"Product: {os.path.basename(pdf_path)} — No data available"

    # Take first 3000 chars (product datasheets have specs on first pages)
    snippet = text[:3000]
    fname = os.path.basename(pdf_path).replace('.pdf', '')

    # Extract key spec indicators from the text
    lines = []
    lines.append(f"PRODUCT: {fname}")

    spec_keywords = {
        "resolution": ["resolution", "megapixel", "mp ", "2mp", "4mp", "5mp", "8mp"],
        "lens": ["lens", "focal", "mm ", "varifocal", "fixed"],
        "ir_range": ["ir ", "infrared", "night vision", "ir range", "smart ir"],
        "ip_rating": ["ip66", "ip67", "ip65", "ik10", "weatherproof"],
        "wdr": ["wdr", "wide dynamic", "dwdr", "digital wdr"],
        "codec": ["h.264", "h.265", "h265", "h264", "hevc"],
        "type": ["dome", "bullet", "ptz", "turret", "box", "nvr", "dvr"],
        "poe": ["poe", "power over ethernet", "12v dc"],
        "storage": ["sd card", "micro sd", "nas", "storage", "hdd"],
        "onvif": ["onvif", "profile s", "profile t"],
    }

    text_lower = snippet.lower()
    for category, keywords in spec_keywords.items():
        matches = [kw for kw in keywords if kw in text_lower]
        if matches:
            # Find the line containing the first match for context
            for line in snippet.split('\n'):
                if any(kw in line.lower() for kw in matches):
                    clean = line.strip()[:120]
                    if clean:
                        lines.append(f"  {category}: {clean}")
                        break

    return '\n'.join(lines)


# =============================================================================
# P0.2: LLM-Powered Structured Spec Extraction (replaces regex for matching)
# =============================================================================
_product_specs: dict[str, dict] = {}  # filename -> structured spec JSON
_SPEC_EXTRACT_PROMPT = """Extract the technical specifications from this product datasheet into a JSON object.
Return ONLY valid JSON with these fields (use null for missing values):
{
  "model": "exact model number",
  "type": "dome|bullet|ptz|turret|box|nvr|dvr|other",
  "resolution_mp": 2.0,
  "resolution_px": "1920x1080",
  "lens_mm": "2.8mm" or "2.8-12mm",
  "lens_type": "fixed|varifocal|motorized",
  "ir_range_m": 30,
  "ip_rating": "IP67",
  "ik_rating": "IK10",
  "wdr": "120dB DWDR" or "true" or null,
  "codec": ["H.265", "H.264"],
  "poe": true,
  "power": "12V DC / PoE (802.3af)",
  "storage": "MicroSD up to 256GB",
  "onvif": true,
  "channels": null,
  "max_recording_resolution": null,
  "hdd_bays": null,
  "audio": "1 in / 1 out" or null,
  "alarm_io": "2 in / 1 out" or null,
  "key_features": ["Smart IR", "3D DNR", "ROI"]
}"""


def _llm_extract_product_specs(pdf_path: str) -> dict | None:
    """Uses LLM to extract structured specs from a product datasheet.
    Returns structured dict or None on failure."""
    fname = os.path.basename(pdf_path)
    text = extract_text_from_pdf(pdf_path)
    if not text or len(text) < 50:
        return None
    try:
        result = query_llm(
            prompt=f"Product: {fname}\n\n{text[:5000]}",
            system_prompt=_SPEC_EXTRACT_PROMPT,
            temperature=0.0,
            max_tokens=600,
            model=MISTRAL_MODEL_LIGHT,  # cheap and fast
            json_output=True,
        )
        spec = json.loads(result)
        spec["_source_file"] = fname
        log.info(f"LLM spec extracted: {fname} -> {spec.get('model', 'unknown')} ({spec.get('type', '?')})")
        return spec
    except json.JSONDecodeError:
        log.warning(f"LLM spec extraction returned invalid JSON for {fname}")
        return None
    except Exception as e:
        log.warning(f"LLM spec extraction failed for {fname}: {e}")
        return None


def _llm_spec_prewarm():
    """Background thread: extracts structured specs for all products using LLM."""
    count = 0
    if not os.path.isdir(PRODUCT_DIR):
        return
    for fname in sorted(get_pdf_list(PRODUCT_DIR)):
        if fname in _product_specs:
            continue
        spec = _llm_extract_product_specs(os.path.join(PRODUCT_DIR, fname))
        if spec:
            _product_specs[fname] = spec
            count += 1
    log.info(f"LLM spec extraction complete: {count} products with structured specs.")


def get_structured_specs_context(product_files: list[str]) -> str:
    """Builds a structured spec context block for matcher prompts.
    Uses LLM-extracted JSON specs when available, falls back to fingerprints."""
    parts = []
    for fpath in product_files:
        fname = os.path.basename(fpath)
        if fname in _product_specs:
            spec = _product_specs[fname]
            parts.append(f"--- PRODUCT: {spec.get('model', fname)} ({fname}) ---")
            parts.append(json.dumps(spec, indent=2))
        elif fname in _product_fingerprints:
            parts.append(f"--- PRODUCT: {fname} (fingerprint only) ---")
            parts.append(_product_fingerprints[fname])
        else:
            parts.append(f"--- PRODUCT: {fname} (no specs available) ---")
    return "\n\n".join(parts)


def build_product_fingerprints():
    """Builds fingerprints for all products. Called during prewarm."""
    if not os.path.isdir(PRODUCT_DIR):
        return
    count = 0
    for fname in get_pdf_list(PRODUCT_DIR):
        fp = _extract_fingerprint(os.path.join(PRODUCT_DIR, fname))
        _product_fingerprints[fname] = fp
        count += 1
    log.info(f"Built {count} product fingerprints ({sum(len(v) for v in _product_fingerprints.values())} total chars)")


# =============================================================================
# P3: Mistral Embeddings — semantic search for products & RFP pages
# =============================================================================
_product_embeddings: dict[str, list[float]] = {}  # filename -> embedding vector


def generate_embedding(text: str) -> list[float]:
    """Generate embedding vector using Mistral embed model."""
    try:
        response = mistral_client.embeddings.create(
            model="mistral-embed",
            inputs=[text[:8000]],  # Cap input to avoid token limits
        )
        return response.data[0].embedding
    except Exception as e:
        log.warning(f"Embedding generation failed: {e}")
        return []


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    """Cosine similarity between two embedding vectors."""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


def build_product_embeddings():
    """Build embedding index for all product fingerprints using batch API."""
    if not _product_fingerprints:
        return
    fnames = list(_product_fingerprints.keys())
    texts = [_product_fingerprints[f][:8000] for f in fnames]
    try:
        response = mistral_client.embeddings.create(
            model="mistral-embed",
            inputs=texts,
        )
        for i, data in enumerate(response.data):
            _product_embeddings[fnames[i]] = data.embedding
        dim = len(response.data[0].embedding) if response.data else 0
        log.info(f"Built {len(_product_embeddings)} product embeddings in single batch ({dim}D vectors)")
    except Exception as e:
        log.warning(f"Batch embedding failed, falling back to sequential: {e}")
        count = 0
        for fname, fingerprint in _product_fingerprints.items():
            emb = generate_embedding(fingerprint)
            if emb:
                _product_embeddings[fname] = emb
                count += 1
        log.info(f"Built {count} product embeddings sequentially")


def find_similar_products(query: str, top_k: int = 5) -> list[str]:
    """Find most similar products to a query using cosine similarity."""
    if not _product_embeddings:
        return []
    query_emb = generate_embedding(query)
    if not query_emb:
        return []
    scores = [
        (fname, _cosine_similarity(query_emb, emb))
        for fname, emb in _product_embeddings.items()
    ]
    scores.sort(key=lambda x: x[1], reverse=True)
    top = scores[:top_k]
    log.info(f"Semantic search: top {top_k} = {[(f, f'{s:.3f}') for f, s in top]}")
    return [fname for fname, _ in top]


# =============================================================================
# Directory Listing Cache — avoids calling os.listdir() on every request
# =============================================================================
_dir_list_cache: dict[str, tuple[float, list[str]]] = {}  # dir -> (timestamp, [filenames])


def get_pdf_list(directory: str) -> list[str]:
    """Returns sorted PDF filenames in a directory, cached with TTL."""
    now = time.time()
    if directory in _dir_list_cache:
        cached_time, cached_list = _dir_list_cache[directory]
        if now - cached_time < FILE_LIST_TTL:
            return cached_list
    if not os.path.isdir(directory):
        return []
    files = sorted([f for f in os.listdir(directory) if f.lower().endswith(".pdf")])
    _dir_list_cache[directory] = (now, files)
    return files


def _score_page_relevance(page_text: str, keywords: list[str]) -> float:
    """Scores a page's relevance to a query based on keyword density."""
    if not page_text or not keywords:
        return 0.0
    text_lower = page_text.lower()
    score = sum(text_lower.count(kw) for kw in keywords)
    return score


# Domain keywords for CCTV/surveillance RFP analysis — ensures tech spec pages are always selected
RFP_DOMAIN_KEYWORDS = [
    "camera", "resolution", "megapixel", "lens", "focal", "ir ", "infrared",
    "wdr", "ip66", "ip67", "nvr", "dvr", "ptz", "cctv", "surveillance",
    "video", "recording", "storage", "nas", "poe", "onvif", "codec",
    "h.264", "h.265", "dome", "bullet", "specification", "technical",
]


def extract_relevant_pages(
    pdf_path: str, query: str, max_chars: int = 60000,
    domain_keywords: list[str] | None = None,
    max_scored_pages: int = 15,
) -> str:
    """Extracts smart sections from a PDF using actual PDF page boundaries.
    Always includes first N pages (header/NIT/EMD), then fills remaining budget
    with the highest keyword-scored pages from the rest of the document.
    max_scored_pages caps how many scored pages are considered (prevents
    broad queries from pulling the entire document)."""
    pages = get_pdf_pages(pdf_path)
    if not pages:
        return extract_text_from_pdf(pdf_path)  # fallback to full text

    full_text = "\n".join(pages)
    if len(full_text) <= max_chars:
        return full_text  # Small enough, send everything

    # Phase 1: Always include first pages (covers NIT, EMD, deadlines, eligibility)
    header_budget = min(8000, max_chars // 4)  # reduced from 20K — cover pages waste tokens
    header_pages = []
    header_chars = 0
    for i, page_text in enumerate(pages):
        if header_chars + len(page_text) > header_budget:
            break
        header_pages.append(i)
        header_chars += len(page_text)

    remaining_budget = max_chars - header_chars
    if remaining_budget < 3000:
        return "\n".join(pages[i] for i in header_pages)

    # Phase 2: Score remaining pages by keyword relevance
    stop_words = {"the", "a", "an", "is", "are", "was", "were", "in", "on", "at",
                  "to", "for", "of", "and", "or", "this", "that", "with", "from",
                  "user", "query", "analyze", "provided", "text", "based", "answer"}
    keywords = [w.lower().strip('"\'.:\ ,') for w in query.split()
                if len(w) > 1 and w.lower().strip('"\'.:\ ,') not in stop_words]
    if domain_keywords:
        keywords.extend(domain_keywords)

    # Score non-header pages
    scored = []
    for i, page_text in enumerate(pages):
        if i in header_pages or not page_text.strip():
            continue
        score = _score_page_relevance(page_text, keywords) if keywords else 0
        scored.append((i, score, page_text))

    if not scored or not keywords:
        # No keywords — take pages sequentially after header
        extra = full_text[header_chars:header_chars + remaining_budget]
        return "\n".join(pages[i] for i in header_pages) + "\n" + extra

    # Select top-scoring pages within budget (capped to prevent broad query flooding)
    scored.sort(key=lambda x: x[1], reverse=True)
    scored = scored[:max_scored_pages]  # cap scored pages
    selected = []
    total = 0
    for idx, score, page_text in scored:
        if total + len(page_text) > remaining_budget:
            break
        selected.append(idx)
        total += len(page_text)

    # Combine header + scored pages in document order
    all_selected = sorted(set(header_pages + selected))
    log.info(f"Smart context: {len(all_selected)}/{len(pages)} pages, "
             f"{header_chars + total} chars (header={len(header_pages)} pages, "
             f"scored={len(selected)} pages)")
    return "\n".join(pages[i] for i in all_selected)


# =============================================================================
# Input Validation Helpers
# =============================================================================
def validate_request_size() -> str | None:
    """Returns error message if request is too large, else None."""
    if request.content_length and request.content_length > MAX_REQUEST_BYTES:
        return f"Request too large ({request.content_length} bytes). Max is {MAX_REQUEST_BYTES}."
    return None


def validate_filename(filename: str, directory: str) -> str | None:
    """Returns error message if filename is invalid or not found, else None."""
    if not filename:
        return "fileName is required."
    if ".." in filename or "/" in filename or "\\" in filename:
        return "Invalid fileName — path traversal is not allowed."
    safe_path = os.path.join(directory, filename)
    if not os.path.isfile(safe_path):
        available = [f for f in os.listdir(directory) if f.lower().endswith(".pdf")] if os.path.isdir(directory) else []
        return f"File '{filename}' not found. Available: {', '.join(available[:10])}"
    return None


# =============================================================================
# LLM Query Engine — Mistral primary, Gemini instant fallback on rate limit
# =============================================================================

SYSTEM_PROMPT_RFP = """You are an Expert RFP Analyst for CP Plus, a leading CCTV and surveillance equipment manufacturer.

Your task is to analyze RFP/tender documents and answer user questions based STRICTLY on the document content provided.

CRITICAL RULES:
1. ONLY extract information that is EXPLICITLY stated in the provided document text.
2. NEVER generate a template of expected specifications. Do NOT list specification categories
   (resolution, lens, IR, WDR, etc.) unless they are actually mentioned in the document.
3. NEVER list items as "Not listed" or "Not specified". If information is not in the document,
   simply do not include it in your response.
4. Use structured Markdown tables when presenting extracted data.
5. Be precise with numbers — do not round or estimate.
6. If the user asks about something that genuinely is not in the provided text, say:
   "This information was not found in the provided document excerpt."
7. NEVER fabricate, assume, or infer specifications that are not written in the document.
8. BE CONCISE. Present findings in compact tables and short summaries. Avoid verbose explanations.
   Only include directly relevant information — do not repeat the question or add filler text.

Capabilities:
- Parse government and enterprise RFP/tender documents (NIT, EMD, BOQ, technical schedules)
- Extract technical specifications, quantities, and compliance requirements
- Identify deadlines, eligibility criteria, and financial terms"""

SYSTEM_PROMPT_PRODUCT = """You are a CP Plus Product Advisor with encyclopedic knowledge of the CP Plus surveillance product catalog.

Your capabilities:
- Retrieve and present full specifications from product datasheets
- Compare products across features (resolution, lens, IR, WDR, IP rating, codec, PoE, etc.)
- Recommend products based on scenario requirements
- Present data in structured Markdown tables

Rules:
- Only use information from the provided datasheets. Never fabricate specifications.
- If a specification is not in the datasheet, state "Not specified in datasheet."
- When recommending, explain WHY each product fits the requirement."""


def _query_gemini(messages: list[dict], temperature: float, max_tokens: int | None) -> str | None:
    """Queries Gemini as fallback. Returns None if unavailable or fails."""
    if not gemini_model_client:
        return None
    try:
        # Convert system+user messages to single prompt for Gemini with role markers
        combined = ""
        for msg in messages:
            role = msg["role"].upper()
            combined += f"[{role}]\n{msg['content']}\n\n"

        gen_config = {"temperature": temperature}
        if max_tokens:
            gen_config["max_output_tokens"] = max_tokens

        log.info(f"Calling Gemini ({GEMINI_MODEL})...")
        response = gemini_model_client.generate_content(
            combined,
            generation_config=gen_config,
            request_options={"timeout": 90},
        )
        log.info("Gemini responded successfully.")
        return response.text
    except Exception as e:
        log.error(f"Gemini fallback failed: {e}")
        return None


# =============================================================================
# P1.1: Circuit Breaker — skip dead tiers to reduce worst-case latency
# =============================================================================
LLM_REQUEST_TIMEOUT = int(os.environ.get("LLM_REQUEST_TIMEOUT", "120"))  # P1.2: hard ceiling
_CB_THRESHOLD = 3   # consecutive failures before opening breaker
_CB_COOLDOWN = 60   # seconds to skip a tripped tier

_circuit_breakers = {
    "tier1": {"failures": 0, "open_until": 0.0},
    "tier2": {"failures": 0, "open_until": 0.0},
    "tier3": {"failures": 0, "open_until": 0.0},
}
_cb_lock = threading.Lock()

def _cb_is_open(tier: str) -> bool:
    """Check if circuit breaker for a tier is open (should skip)."""
    with _cb_lock:
        cb = _circuit_breakers[tier]
        if cb["failures"] >= _CB_THRESHOLD and time.time() < cb["open_until"]:
            return True
        # If cooldown expired, allow a probe attempt
        if cb["failures"] >= _CB_THRESHOLD and time.time() >= cb["open_until"]:
            cb["failures"] = _CB_THRESHOLD - 1  # allow one probe
        return False

def _cb_record_failure(tier: str):
    """Record a failure for a tier's circuit breaker."""
    with _cb_lock:
        cb = _circuit_breakers[tier]
        cb["failures"] += 1
        if cb["failures"] >= _CB_THRESHOLD:
            cb["open_until"] = time.time() + _CB_COOLDOWN
            log.warning(f"Circuit breaker OPEN for {tier} — skipping for {_CB_COOLDOWN}s")

def _cb_record_success(tier: str):
    """Reset circuit breaker on success."""
    with _cb_lock:
        _circuit_breakers[tier] = {"failures": 0, "open_until": 0.0}


def query_llm(
    prompt: str,
    context_files: list[str] | None = None,
    system_prompt: str | None = None,
    model: str | None = None,
    temperature: float = 0.1,
    max_tokens: int | None = None,
    smart_context: bool = True,
    domain_keywords: list[str] | None = None,
    json_output: bool = False,
) -> str:
    """
    Queries LLM with configurable primary model and automatic fallback.
    LLM_PRIMARY="gemini"  → Gemini first, Mistral fallback (fastest)
    LLM_PRIMARY="mistral" → Mistral first, Gemini fallback (default behavior)
    smart_context=True     → sends only relevant pages instead of full PDFs
    """
    model = model or MISTRAL_MODEL
    start_time = time.time()

    # Build context from PDF files with smart windowing
    context_text = ""
    if context_files:
        # Mistral scale-as-you-go: no rate limits, generous context window.
        # Budget expanded to maximize analysis quality.
        if smart_context and domain_keywords and len(context_files) <= 2:
            per_file_max = 60000   # single RFP or product: ~15K tokens — deep analysis
        elif smart_context and domain_keywords:
            per_file_max = 15000   # matcher with many files — balanced depth vs breadth
        elif smart_context:
            per_file_max = 100000  # generous context for quality output
        else:
            per_file_max = 200000  # full document mode — scale-as-you-go can handle it
        for file_path in context_files:
            if os.path.exists(file_path):
                if smart_context:
                    file_text = extract_relevant_pages(
                        file_path, prompt, max_chars=per_file_max,
                        domain_keywords=domain_keywords,
                    )
                else:
                    file_text = extract_text_from_pdf(file_path)
                if file_text:
                    context_text += f"\n\n--- DOCUMENT: {os.path.basename(file_path)} ---\n{file_text}\n"
            else:
                log.warning(f"File not found: {file_path}")

    # Safety cap — Mistral Large supports 128K context; keep headroom for system + output
    max_context = 200000
    if len(context_text) > max_context:
        log.warning(f"Context too large ({len(context_text)} chars), truncating to {max_context//1000}k.")
        context_text = context_text[:max_context] + "\n...(truncated)..."

    log.info(f"Context size: {len(context_text)} chars ({len(context_text)//4} est. tokens)")

    # Build messages
    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})

    user_content = f"{context_text}\n\n{prompt}" if context_text else prompt
    messages.append({"role": "user", "content": user_content})

    kwargs = {"model": model, "messages": messages, "temperature": temperature}
    if max_tokens:
        kwargs["max_tokens"] = max_tokens
    # P0: Structured JSON output mode
    if json_output:
        kwargs["response_format"] = {"type": "json_object"}

    # =========================================================================
    # 3-TIER FALLBACK CHAIN: Mistral Primary → Mistral Backup → Gemini
    # With Circuit Breaker (P1.1) and Hard Timeout (P1.2)
    # =========================================================================
    deadline = start_time + LLM_REQUEST_TIMEOUT  # P1.2: hard 120s ceiling

    # --- Tier 1: Mistral Primary ---
    if not _cb_is_open("tier1"):
        for attempt in range(2):
            if time.time() > deadline:
                log.warning("[TIMEOUT] Hard ceiling reached during Tier 1.")
                return "Error: Request timed out after {}s. Please try again.".format(LLM_REQUEST_TIMEOUT)
            try:
                log.info(f"[Tier 1] Querying Mistral PRIMARY ({model}), attempt {attempt + 1}/2")
                response = mistral_client.chat.complete(**kwargs, timeout_ms=90_000)
                result = response.choices[0].message.content
                elapsed = time.time() - start_time
                log.info(f"LLM response in {elapsed:.1f}s (Mistral PRIMARY)")
                _cb_record_success("tier1")
                _maybe_log_training(messages, result, model)
                return result
            except Exception as e:
                _cb_record_failure("tier1")
                error_str = str(e).lower()
                is_rate_limit = "rate" in error_str and "limit" in error_str
                if is_rate_limit:
                    log.warning(f"[Tier 1] Mistral PRIMARY rate-limited. Moving to Tier 2.")
                    break  # skip to backup
                else:
                    wait_time = 3.0 * (2 ** attempt) + random.uniform(0, 1)
                    log.warning(f"[Tier 1] Mistral PRIMARY error: {e}. Waiting {wait_time:.1f}s")
                    if attempt < 1:
                        time.sleep(wait_time)
    else:
        log.info("[Tier 1] Circuit breaker OPEN — skipping Mistral PRIMARY.")

    # --- Tier 2: Mistral Backup ---
    if mistral_backup_client and not _cb_is_open("tier2"):
        if time.time() > deadline:
            log.warning("[TIMEOUT] Hard ceiling reached before Tier 2.")
            return "Error: Request timed out after {}s. Please try again.".format(LLM_REQUEST_TIMEOUT)
        backup_kwargs = {**kwargs}  # same params, different client
        for attempt in range(2):
            if time.time() > deadline:
                break
            try:
                log.info(f"[Tier 2] Querying Mistral BACKUP ({model}), attempt {attempt + 1}/2")
                response = mistral_backup_client.chat.complete(**backup_kwargs, timeout_ms=90_000)
                result = response.choices[0].message.content
                elapsed = time.time() - start_time
                log.info(f"LLM response in {elapsed:.1f}s (Mistral BACKUP)")
                _cb_record_success("tier2")
                _maybe_log_training(messages, result, model)
                return result
            except Exception as e:
                _cb_record_failure("tier2")
                error_str = str(e).lower()
                is_rate_limit = "rate" in error_str and "limit" in error_str
                if is_rate_limit:
                    log.warning(f"[Tier 2] Mistral BACKUP also rate-limited. Moving to Tier 3.")
                    break
                else:
                    wait_time = 3.0 * (2 ** attempt) + random.uniform(0, 1)
                    log.warning(f"[Tier 2] Mistral BACKUP error: {e}. Waiting {wait_time:.1f}s")
                    if attempt < 1:
                        time.sleep(wait_time)
    elif not mistral_backup_client:
        log.warning("[Tier 2] No Mistral backup client configured. Skipping to Tier 3.")
    else:
        log.info("[Tier 2] Circuit breaker OPEN — skipping Mistral BACKUP.")

    # --- Tier 3: Gemini Final Fallback ---
    if gemini_model_client and not _cb_is_open("tier3"):
        if time.time() > deadline:
            log.warning("[TIMEOUT] Hard ceiling reached before Tier 3.")
            return "Error: Request timed out after {}s. Please try again.".format(LLM_REQUEST_TIMEOUT)
        log.info(f"[Tier 3] Querying Gemini ({GEMINI_MODEL}) as final fallback...")
        gemini_result = _query_gemini(messages, temperature, max_tokens)
        if gemini_result:
            elapsed = time.time() - start_time
            log.info(f"LLM response in {elapsed:.1f}s (Gemini FINAL FALLBACK)")
            _cb_record_success("tier3")
            _maybe_log_training(messages, gemini_result, GEMINI_MODEL)
            return gemini_result
        _cb_record_failure("tier3")
        # One retry after 15s cooldown (only if still within deadline)
        if time.time() + 15 < deadline:
            log.warning("[Tier 3] Gemini rate-limited. Waiting 15s for TPM refresh...")
            time.sleep(15)
            gemini_retry = _query_gemini(messages, temperature, max_tokens)
            if gemini_retry:
                elapsed = time.time() - start_time
                log.info(f"LLM response in {elapsed:.1f}s (Gemini FINAL FALLBACK, retry)")
                _cb_record_success("tier3")
                _maybe_log_training(messages, gemini_retry, GEMINI_MODEL)
                return gemini_retry
            _cb_record_failure("tier3")
    elif gemini_model_client:
        log.info("[Tier 3] Circuit breaker OPEN — skipping Gemini.")

    return "Error: All LLM providers are unavailable (Mistral Primary, Mistral Backup, Gemini). Please try again in 1-2 minutes."


def _maybe_log_training(messages: list[dict], completion: str, model_name: str):
    """P5: Log prompt-completion pair for fine-tuning dataset."""
    if COLLECT_TRAINING_DATA and completion:
        _training_log.append({
            "messages": messages + [{"role": "assistant", "content": completion}],
            "model": model_name,
            "timestamp": time.time(),
        })


# =============================================================================
# P1: Model Routing — use the right model for each task
# =============================================================================
HEAVY_ACTIONS = {"rfp_chat", "rfp_focus_section", "product_chat", "matcher_chat", "crossmatch_rfp"}
LIGHT_ACTIONS = {"list_rfps", "list_products", "product_search"}


def select_model(action: str | None) -> str:
    """Select the appropriate Mistral model based on action complexity.
    Heavy analysis tasks use mistral-large, simple lookups use mistral-small."""
    if action in LIGHT_ACTIONS:
        return MISTRAL_MODEL_LIGHT
    return MISTRAL_MODEL_HEAVY


# =============================================================================
# Matcher Intelligence — ported from legacy matcher_caller.py
# =============================================================================
def detect_matcher_mode(query: str) -> str:
    """Detects the matcher mode: full, gap, or specific."""
    if not query:
        return "full"
    q = query.lower()

    full_triggers = [
        "full compliance matrix", "full compliance report", "crossmatch all",
        "cross match all", "full crossmatch", "generate full matrix",
        "generate full report", "cross-match all products", "crossmatch rfp",
        "crossmatch this compliance", "cross match this compliance",
        "cross match this rfp", "compare all products", "compare full compliance",
        "overall compliance", "complete compliance",
    ]
    gap_triggers = [
        "what are we missing", "non compliant", "non-compliant", "gap analysis",
        "list gaps", "non compliance", "non-compliance", "missing items",
        "missing requirements", "where do we fail", "where are we weak",
        "only show gaps", "only gaps",
    ]

    if any(k in q for k in gap_triggers):
        return "gap"
    if any(k in q for k in full_triggers):
        return "full"
    if "crossmatch" in q or "cross match" in q:
        return "full"
    return "specific"


def detect_direction(query: str, product_names: list[str]) -> str:
    """Detects comparison direction: rfp_to_product or product_to_rfp."""
    if not query:
        return "rfp_to_product"
    q = query.lower()
    for name in product_names:
        base = name.lower().replace(".pdf", "")
        if name.lower() in q or base in q:
            return "product_to_rfp"
    if "cp-" in q or "cp " in q:
        return "product_to_rfp"
    return "rfp_to_product"


def parse_output_style(query: str) -> dict:
    """Parses the user's query to determine output layout preferences."""
    style = {
        "per_product_tables": False, "per_rfp_item_matrix": False,
        "gap_only": False, "include_scores": False,
        "top_n": None, "product_focus": None,
    }
    if not query:
        style["per_rfp_item_matrix"] = True
        return style

    q = query.lower()
    if any(k in q for k in ["per product", "for each product", "product wise", "product-wise"]):
        style["per_product_tables"] = True
    if any(k in q for k in ["per item", "per rfp item", "item wise", "item-wise"]):
        style["per_rfp_item_matrix"] = True
    if any(k in q for k in ["gap only", "only gaps", "only non compliant"]):
        style["gap_only"] = True
    if any(k in q for k in ["score", "weighted", "percentage", "%"]):
        style["include_scores"] = True
    for n in [3, 5, 10]:
        if f"top {n}" in q or f"best {n}" in q:
            style["top_n"] = n
            break
    if "camera" in q:
        style["product_focus"] = "camera"
    if "nvr" in q or "network video recorder" in q:
        style["product_focus"] = "nvr"
    if not style["per_product_tables"] and not style["per_rfp_item_matrix"]:
        style["per_rfp_item_matrix"] = True
    return style


def filter_product_files(product_files: list[str], product_focus: str | None) -> list[str]:
    """Filters product files by focus type (camera, nvr)."""
    if not product_focus:
        return product_files
    filtered = []
    for f in product_files:
        name_lower = os.path.basename(f).lower()
        if product_focus == "camera" and any(k in name_lower for k in ["cam", "unc-", "dome", "bullet"]):
            filtered.append(f)
        elif product_focus == "nvr" and any(k in name_lower for k in ["nvr", "unr-"]):
            filtered.append(f)
    return filtered if filtered else product_files


def select_named_products(query: str, all_product_files: list[str], max_count: int = 5) -> list[str]:
    """Selects products mentioned by name in the query."""
    q = query.lower()
    matches = []
    for f in all_product_files:
        base = os.path.basename(f).lower().replace(".pdf", "")
        if base in q or os.path.basename(f).lower() in q:
            matches.append(f)
        if len(matches) >= max_count:
            break
    return matches


def build_matcher_system_prompt(mode: str, direction: str, output_style: dict) -> str:
    """Builds the comprehensive matcher system prompt."""

    style_parts = []
    if output_style.get("per_product_tables"):
        style_parts.append(
            "TABLE LAYOUT: PER PRODUCT — Output one spec sheet table per product. "
            "Use heading '## Product #N <MODEL> vs <RFP Item>' with table: "
            "| SN | Feature | RFP Requirement | Product Spec | Compliance | Comment |"
        )
    if output_style.get("per_rfp_item_matrix"):
        style_parts.append(
            "TABLE LAYOUT: PER RFP ITEM MATRIX — One row per RFP item. "
            "Default table: | RFP Item | Key RFP Requirements | CP Plus Model | Key CP Plus Specs | Compliance | Comment |"
        )
    if output_style.get("gap_only") or mode == "gap":
        style_parts.append(
            "TABLE LAYOUT: GAPS ONLY — Focus on non-compliant/missing items. "
            "Table: | RFP Item | Key Requirement | Gap Or Non Compliance | Suggested Action |"
        )
    if output_style.get("include_scores"):
        style_parts.append(
            "SCORING — Assign numeric scores and percentage compliance. "
            "Table: | Feature | Weight | Compliance | Score | Comment | "
            "Summarize with: 'Overall estimated compliance for this product is about X percent.'"
        )
    top_n = output_style.get("top_n")
    if isinstance(top_n, int) and top_n > 0:
        style_parts.append(f"PRODUCT COUNT — Focus detailed tables on the top {top_n} best-matching products.")

    style_block = "\n".join(style_parts)

    if direction == "product_to_rfp":
        direction_block = (
            "DIRECTION: PRODUCT → RFP. Start from the named CP Plus product(s). "
            "Identify relevant RFP requirements and compare each product against them."
        )
    else:
        direction_block = (
            "DIRECTION: RFP → PRODUCT. Start from RFP technical requirements. "
            "For each RFP item, find the best CP Plus product match from the datasheets."
        )

    if mode == "full":
        mode_block = (
            "MODE: FULL COMPLIANCE MATRIX. "
            "1. Identify each main RFP hardware/system item. "
            "2. Select the best CP Plus product(s) for each. "
            "3. Sanity-check: never claim 2MP satisfies 8MP, never claim IP66 satisfies IP67. "
            "4. Anti-Hallucination: If the catalog lacks a product for an RFP item, DO NOT invent mock CP Plus part numbers. State 'No matching product in catalog'. "
            "5. Output overview paragraph + Markdown compliance table(s). "
            "6. End with suggested follow-up questions."
        )
    elif mode == "gap":
        mode_block = (
            "MODE: GAP ANALYSIS. "
            "1. Focus on where CP Plus catalog does NOT meet RFP requirements. "
            "2. Identify each gap (no product match OR product weaker on key parameter). "
            "3. Anti-Hallucination: DO NOT invent mock products to fill gaps. State 'No product available'. "
            "4. Output summary paragraph + gap table with suggested actions."
        )
    else:
        mode_block = (
            "MODE: SPECIFIC CHECK. "
            "1. Focus on the specific requirement(s) in the user's query. "
            "2. Locate exact requirement text in RFP, compare strictly. "
            "3. Anti-Hallucination: DO NOT make up product names. "
            "4. Output compact table: | Requirement | RFP Value | CP Plus Spec | Compliance | Comment |"
        )

    return f"""You are an Expert Bid Manager for CP Plus, a leading CCTV and surveillance equipment manufacturer.

{direction_block}

INTERNAL REASONING PROTOCOL (keep private, never show to user):
1. Infer what the user is really asking (full crossmatch, focused check, gap analysis, or product check).
2. Locate exact RFP sections that matter (technical specs, schedules, system design).
3. Extract requirements into structured features (Resolution, Lens, IR, WDR, IP rating, Power, Storage, etc.).
4. For selected CP Plus products, extract the same structured features from datasheets.
5. Perform STRICT feature-by-feature comparison:
   - Lower numeric value than RFP minimum = Non-Compliant
   - Lower resolution than required = Non-Compliant
   - IP66 does NOT satisfy IP67 requirement
   - Missing field = "Missing" (never assume compliance)
   - Missing product = "No product in catalog" (NEVER invent part numbers like CP-4G-GW-01 or CP-UPS-...)
6. Only after this internal comparison, format the final answer.

{mode_block}

{style_block}

OUTPUT RULES:
- Short natural language summaries
- Structured Markdown tables with clear compliance indications (✅ / ❌ / ⚠️)
- Short and precise comments explaining each compliance decision
- Never fabricate data — only use what is in the provided documents"""


# =============================================================================
# API Routes
# =============================================================================

UPLOAD_PAGE_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>CP Plus — Tender Document Upload</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:'Inter',sans-serif;background:#0a0e1a;color:#e0e6f0;min-height:100vh;display:flex;align-items:center;justify-content:center;padding:20px}
.container{max-width:620px;width:100%}
.logo-bar{display:flex;align-items:center;gap:12px;margin-bottom:24px}
.logo-bar .dot{width:10px;height:10px;border-radius:50%;background:#00d4aa;box-shadow:0 0 12px #00d4aa80}
.logo-bar h1{font-size:18px;font-weight:600;letter-spacing:-.3px}
.logo-bar span{color:#00d4aa}
.card{background:#111827;border:1px solid #1e293b;border-radius:16px;padding:32px;position:relative;overflow:hidden;margin-bottom:20px}
.card::before{content:'';position:absolute;top:0;left:0;right:0;height:3px;background:linear-gradient(90deg,#00d4aa,#0ea5e9,#8b5cf6)}
.card h2{font-size:20px;font-weight:700;margin-bottom:6px}
.card p.sub{font-size:13px;color:#94a3b8;margin-bottom:24px;line-height:1.5}
.drop-zone{border:2px dashed #334155;border-radius:12px;padding:36px 20px;text-align:center;cursor:pointer;transition:all .25s ease;margin-bottom:16px}
.drop-zone:hover,.drop-zone.dragging{border-color:#00d4aa;background:#00d4aa08}
.drop-zone .icon{font-size:32px;margin-bottom:8px}
.drop-zone .label{font-size:13px;color:#94a3b8}
.drop-zone .label strong{color:#00d4aa}
.file-info{display:none;align-items:center;gap:12px;background:#1e293b;border-radius:10px;padding:12px 16px;margin-bottom:16px}
.file-info.visible{display:flex}
.file-info .fi-icon{font-size:20px}
.file-info .fi-name{flex:1;font-size:13px;font-weight:500;word-break:break-all}
.file-info .fi-size{font-size:11px;color:#64748b}
.file-info .fi-remove{background:none;border:none;color:#ef4444;cursor:pointer;font-size:16px;padding:4px}
.btn{width:100%;padding:13px;border:none;border-radius:10px;font-family:'Inter',sans-serif;font-size:14px;font-weight:600;cursor:pointer;transition:all .2s ease}
.btn-primary{background:linear-gradient(135deg,#00d4aa,#0ea5e9);color:#0a0e1a}
.btn-primary:hover{transform:translateY(-1px);box-shadow:0 8px 24px #00d4aa30}
.btn-primary:disabled{opacity:.4;cursor:not-allowed;transform:none;box-shadow:none}
.status{margin-top:14px;padding:12px;border-radius:10px;font-size:12px;line-height:1.5;display:none}
.status.success{display:block;background:#00d4aa15;border:1px solid #00d4aa40;color:#00d4aa}
.status.error{display:block;background:#ef444415;border:1px solid #ef444440;color:#ef4444}
.status.loading{display:block;background:#0ea5e915;border:1px solid #0ea5e940;color:#0ea5e9}
/* Jobs Dashboard */
.jobs-card h2{display:flex;align-items:center;gap:8px}
.jobs-card h2 .pulse{width:8px;height:8px;border-radius:50%;background:#00d4aa;animation:pulse 2s infinite}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.3}}
.job-item{display:flex;align-items:center;gap:12px;background:#1e293b;border-radius:10px;padding:14px 16px;margin-bottom:8px;transition:all .3s ease}
.job-item.done{border-left:3px solid #00d4aa}
.job-item.processing{border-left:3px solid #f59e0b}
.job-item.error{border-left:3px solid #ef4444}
.job-icon{font-size:20px;flex-shrink:0}
.job-details{flex:1;min-width:0}
.job-name{font-size:13px;font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.job-meta{font-size:11px;color:#64748b;margin-top:2px}
.job-badge{font-size:11px;font-weight:600;padding:4px 10px;border-radius:6px;flex-shrink:0;text-transform:uppercase;letter-spacing:.5px}
.job-badge.processing{background:#f59e0b20;color:#f59e0b}
.job-badge.done{background:#00d4aa20;color:#00d4aa}
.job-badge.error{background:#ef444420;color:#ef4444}
.download-btn{background:none;border:1px solid #00d4aa;color:#00d4aa;padding:5px 12px;border-radius:6px;font-size:11px;font-weight:600;cursor:pointer;transition:all .2s;flex-shrink:0}
.download-btn:hover{background:#00d4aa;color:#0a0e1a}
.no-jobs{text-align:center;color:#475569;font-size:13px;padding:20px}
/* RFP list */
.rfp-list{margin-top:20px;padding-top:16px;border-top:1px solid #1e293b}
.rfp-list h3{font-size:13px;font-weight:600;color:#94a3b8;margin-bottom:10px}
.rfp-list ul{list-style:none;max-height:140px;overflow-y:auto}
.rfp-list li{font-size:12px;padding:7px 12px;background:#1e293b;border-radius:8px;margin-bottom:4px;display:flex;align-items:center;gap:8px}
/* Toast */
.toast{position:fixed;top:20px;right:20px;background:#111827;border:1px solid #00d4aa40;border-radius:12px;padding:16px 20px;display:none;align-items:center;gap:12px;z-index:9999;box-shadow:0 8px 32px rgba(0,0,0,.5);animation:slideIn .3s ease}
.toast.visible{display:flex}
@keyframes slideIn{from{transform:translateX(100%);opacity:0}to{transform:translateX(0);opacity:1}}
.toast .t-icon{font-size:24px}
.toast .t-text{font-size:13px;font-weight:500}
.toast .t-text strong{color:#00d4aa}
.toast .t-close{background:none;border:none;color:#64748b;cursor:pointer;font-size:16px;padding:4px}
</style>
</head>
<body>
<div class="container">
  <div class="logo-bar">
    <div class="dot"></div>
    <h1><span>CP Plus</span> Pre-Sales Tender Portal</h1>
  </div>

  <!-- Upload Card -->
  <div class="card">
    <h2>Upload Tender Document</h2>
    <p class="sub">Drop your RFP or tender PDF here. Once uploaded, ask the AI agent to analyze it.</p>
    <div class="drop-zone" id="dropZone">
      <div class="icon">&#x1F4C1;</div>
      <div class="label">Drag & drop a <strong>PDF file</strong> here, or <strong>click to browse</strong></div>
    </div>
    <input type="file" id="fileInput" accept=".pdf" hidden>
    <div class="file-info" id="fileInfo">
      <span class="fi-icon">&#x1F4C4;</span>
      <span class="fi-name" id="fileName"></span>
      <span class="fi-size" id="fileSize"></span>
      <button class="fi-remove" id="fileRemove" title="Remove">&#x2715;</button>
    </div>
    <button class="btn btn-primary" id="uploadBtn" disabled>Upload Document</button>
    <div class="status" id="status"></div>
    <div class="rfp-list" id="rfpList"></div>
  </div>

  <!-- Active Documents -->
  <div class="card jobs-card">
    <h2><span class="pulse"></span> Uploaded Documents</h2>
    <p class="sub">Documents available for analysis by the AI agent.</p>
    <div id="jobsList"><div class="no-jobs">No documents uploaded yet. Use the form above to add RFP files.</div></div>
  </div>
</div>

<!-- Toast Notification -->
<div class="toast" id="toast">
  <span class="t-icon">&#x2705;</span>
  <span class="t-text" id="toastText"></span>
  <button class="t-close" id="toastClose">&#x2715;</button>
</div>

<script>
const H={'ngrok-skip-browser-warning':'true'};
const dropZone=document.getElementById('dropZone'),fileInput=document.getElementById('fileInput'),
  fileInfo=document.getElementById('fileInfo'),fileName=document.getElementById('fileName'),
  fileSize=document.getElementById('fileSize'),fileRemove=document.getElementById('fileRemove'),
  uploadBtn=document.getElementById('uploadBtn'),status=document.getElementById('status'),
  rfpList=document.getElementById('rfpList'),jobsList=document.getElementById('jobsList'),
  toast=document.getElementById('toast'),toastText=document.getElementById('toastText'),
  toastClose=document.getElementById('toastClose');

let selectedFile=null,knownDone=new Set();

// Upload logic
dropZone.addEventListener('click',()=>fileInput.click());
dropZone.addEventListener('dragover',e=>{e.preventDefault();dropZone.classList.add('dragging')});
dropZone.addEventListener('dragleave',()=>dropZone.classList.remove('dragging'));
dropZone.addEventListener('drop',e=>{e.preventDefault();dropZone.classList.remove('dragging');if(e.dataTransfer.files.length)selectFile(e.dataTransfer.files[0])});
fileInput.addEventListener('change',()=>{if(fileInput.files.length)selectFile(fileInput.files[0])});
fileRemove.addEventListener('click',clearFile);
toastClose.addEventListener('click',()=>toast.classList.remove('visible'));

function selectFile(f){
  if(!f.name.toLowerCase().endsWith('.pdf')){showStatus('error','Only PDF files are accepted.');return}
  if(f.size>100*1024*1024){showStatus('error','File too large. Maximum 100MB.');return}
  selectedFile=f;fileName.textContent=f.name;fileSize.textContent=(f.size/1024/1024).toFixed(1)+' MB';
  fileInfo.classList.add('visible');dropZone.style.display='none';uploadBtn.disabled=false;hideStatus();
}
function clearFile(){selectedFile=null;fileInput.value='';fileInfo.classList.remove('visible');dropZone.style.display='';uploadBtn.disabled=true;hideStatus()}
function showStatus(t,m){status.className='status '+t;status.textContent=m}
function hideStatus(){status.className='status';status.textContent=''}
function showToast(msg){toastText.innerHTML=msg;toast.classList.add('visible');setTimeout(()=>toast.classList.remove('visible'),8000)}

uploadBtn.addEventListener('click',async()=>{
  if(!selectedFile)return;
  uploadBtn.disabled=true;showStatus('loading','Uploading document...');
  const fd=new FormData();fd.append('file',selectedFile);
  try{
    const r=await fetch('/rfp/upload',{method:'POST',body:fd,headers:H});
    const d=await r.json();
    if(r.ok){showStatus('success','\u2713 '+d.message+' You can now ask the AI agent to analyze it.');clearFile();loadRfpList()}
    else{showStatus('error','\u2717 '+(d.error||'Upload failed'));uploadBtn.disabled=false}
  }catch(e){showStatus('error','Network error: '+e.message);uploadBtn.disabled=false}
});

// RFP list
async function loadRfpList(){
  try{
    const r=await fetch('/rfp/run',{method:'POST',headers:{...H,'Content-Type':'application/json'},body:JSON.stringify({action:'list_rfps'})});
    const d=await r.json();
    if(d.answer){
      const lines=d.answer.split('\\n').filter(l=>l.includes('|')&&!l.includes('No.')).slice(1);
      const names=lines.map(l=>{const c=l.split('|');return c.length>=3?c[2].trim():null}).filter(Boolean);
      if(names.length){rfpList.innerHTML='<h3>Available Documents ('+names.length+')</h3><ul>'+names.map(n=>'<li>&#x1F4C4; '+n+'</li>').join('')+'</ul>'}
    }
  }catch(e){}
}

// Jobs polling
function fmtTime(s){if(!s)return'';const m=Math.floor(s/60),sec=s%60;return m>0?m+'m '+sec+'s':sec+'s'}
async function pollJobs(){
  try{
    const r=await fetch('/rfp/jobs',{headers:H});
    const d=await r.json();
    if(!d.jobs||d.jobs.length===0){jobsList.innerHTML='<div class="no-jobs">No extraction jobs yet. Trigger one from the AI agent chat.</div>';return}
    let html='';
    d.jobs.forEach(j=>{
      const icon=j.status==='done'?'\u2705':j.status==='error'?'\u274C':'\u23F3';
      const badge='<span class="job-badge '+j.status+'">'+j.status+'</span>';
      const meta='Elapsed: '+fmtTime(j.elapsed_seconds);
      let actions='';
      if(j.status==='done'&&j.output_file){actions='<a class="download-btn" href="/rfp/download/'+j.output_file+'">\u2B07 Download Excel</a>'}
      html+='<div class="job-item '+j.status+'"><span class="job-icon">'+icon+'</span><div class="job-details"><div class="job-name">'+j.rfp_name+'</div><div class="job-meta">'+meta+'</div></div>'+badge+actions+'</div>';
      // Toast notification for newly completed jobs
      if(j.status==='done'&&!knownDone.has(j.job_id)){
        knownDone.add(j.job_id);
        showToast('Extraction complete: <strong>'+j.rfp_name+'</strong>');
        if(Notification.permission==='granted'){new Notification('CP Plus Extraction Complete',{body:j.rfp_name+' is ready for download.'})}
      }
    });
    jobsList.innerHTML=html;
  }catch(e){}
}

// Init
loadRfpList();
pollJobs();
setInterval(pollJobs,5000);
if('Notification' in window&&Notification.permission==='default'){Notification.requestPermission()}
</script>
</body>
</html>"""



@app.get("/upload")
def serve_upload_page():
    """Serve the tender document upload page."""
    return UPLOAD_PAGE_HTML, 200, {"Content-Type": "text/html"}


@app.get("/demo")
def serve_demo_page():
    """Serve the CP Plus Agentic Suite demo landing page with embedded chat widget."""
    chat_ui_path = os.path.join(os.path.dirname(__file__), "..", "..", "cp_plus_poc", "chat-ui.html")
    chat_ui_path = os.path.normpath(chat_ui_path)
    if os.path.exists(chat_ui_path):
        with open(chat_ui_path, "r", encoding="utf-8") as f:
            return f.read(), 200, {"Content-Type": "text/html"}
    return "<h1>Demo page not found</h1><p>chat-ui.html is missing from cp_plus_poc/</p>", 404, {"Content-Type": "text/html"}





@app.post("/rfp/upload")
def rfp_upload():
    """Handle PDF file upload for RFP/Tender documents."""
    try:
        if "file" not in request.files:
            return jsonify({"error": "No file provided. Please select a PDF file."}), 400

        file = request.files["file"]
        if not file.filename:
            return jsonify({"error": "Empty filename"}), 400

        if not file.filename.lower().endswith(".pdf"):
            return jsonify({"error": "Only PDF files are accepted."}), 400

        # Sanitize filename (keep original name but remove path traversal)
        safe_name = os.path.basename(file.filename)

        # Ensure RFP directory exists
        os.makedirs(RFP_DIR, exist_ok=True)

        dest_path = os.path.join(RFP_DIR, safe_name)

        if os.path.exists(dest_path):
            return jsonify({"error": f"A file named '{safe_name}' already exists. Please rename your file."}), 409

        file.save(dest_path)
        log.info(f"Uploaded RFP: {safe_name} ({os.path.getsize(dest_path)} bytes)")

        return jsonify({
            "message": f"Document '{safe_name}' uploaded successfully.",
            "fileName": safe_name
        })

    except Exception as e:
        log.error(f"Upload failed: {e}")
        return jsonify({"error": f"Upload failed: {e}"}), 500



@app.get("/health")
def health_check():
    """Health check endpoint for monitoring."""
    return jsonify({
        "status": "ok",
        "tier_1": f"Mistral PRIMARY ({MISTRAL_MODEL})",
        "tier_2": f"Mistral BACKUP ({MISTRAL_MODEL})" if mistral_backup_client else "disabled",
        "tier_3": f"Gemini ({GEMINI_MODEL})" if gemini_model_client else "disabled",
        "circuit_breakers": {
            tier: {"failures": cb["failures"], "open": time.time() < cb["open_until"]}
            for tier, cb in _circuit_breakers.items()
        },
        "model_routing": {
            "heavy": MISTRAL_MODEL_HEAVY,
            "light": MISTRAL_MODEL_LIGHT,
        },
        "ocr_enabled": USE_MISTRAL_OCR,
        "ocr_cached_docs": len(_ocr_cache),
        "structured_specs_count": len(_product_specs),
        "embedding_index_size": len(_product_embeddings),
        "rfp_count": len(get_pdf_list(RFP_DIR)),
        "product_count": len(get_pdf_list(PRODUCT_DIR)),
        "cache_entries": len(_pdf_cache),
        "training_data_count": len(_training_log),
        "request_timeout_s": LLM_REQUEST_TIMEOUT,
    })


@app.post("/product/run")
def product_run():
    try:
        size_error = validate_request_size()
        if size_error:
            return jsonify({"error": size_error}), 413

        body = request.get_json(force=True) or {}
        action = body.get("action")

        if action == "list_products":
            return jsonify({"answer": get_local_product_list()})

        if action in ("product_chat", "product_search"):
            user_query = (body.get("query") or "").strip()
            if not user_query:
                return jsonify({"error": "Query required"}), 400

            q_lower = user_query.lower()
            if "list" in q_lower and "product" in q_lower:
                return jsonify({"answer": get_local_product_list()})

            target_files = []
            if os.path.exists(PRODUCT_DIR):
                all_files = get_pdf_list(PRODUCT_DIR)
                found_file = None
                for fname in all_files:
                    if fname.lower().replace(".pdf", "") in q_lower or fname.lower() in q_lower:
                        found_file = fname
                        break

                if found_file:
                    target_files = [os.path.join(PRODUCT_DIR, found_file)]
                elif _product_embeddings:
                    # P3: Semantic product matching using embeddings
                    similar = find_similar_products(user_query, top_k=5)
                    target_files = [os.path.join(PRODUCT_DIR, f) for f in similar if f in all_files]
                    if not target_files:
                        target_files = [os.path.join(PRODUCT_DIR, f) for f in all_files[:5]]
                else:
                    target_files = [os.path.join(PRODUCT_DIR, f) for f in all_files[:5]]

            # P1: Route to appropriate model based on action complexity
            answer = query_llm(
                prompt=f'User Query: "{user_query}"\nAnswer based on the provided product datasheets.',
                context_files=target_files,
                system_prompt=SYSTEM_PROMPT_PRODUCT,
                model=select_model(action),
            )
            return jsonify({"answer": answer})

        return jsonify({"error": f"Unknown action: {action}"}), 400
    except Exception as e:
        log.exception("Product Error")
        return jsonify({"error": str(e)}), 500


def get_local_product_list() -> str:
    if not os.path.exists(PRODUCT_DIR):
        return "Product directory not found."
    products = get_pdf_list(PRODUCT_DIR)
    if not products:
        return "No product datasheets found."
    rows = [f"| {i + 1} | {name} |" for i, name in enumerate(products)]
    return "| No. | Product Datasheet |\n| :--- | :--- |\n" + "\n".join(rows)


@app.post("/rfp/run")
def rfp_run():
    try:
        size_error = validate_request_size()
        if size_error:
            return jsonify({"error": size_error}), 413

        body = request.get_json(force=True) or {}
        action = body.get("action")

        if action == "list_rfps":
            return jsonify({"answer": get_local_rfp_list()})



        if action in ("rfp_chat", "rfp_focus_section"):
            user_query = (body.get("query") or "").strip()
            if not user_query:
                return jsonify({"error": "Query required"}), 400
            file_filter = body.get("fileName")

            if file_filter:
                fname_error = validate_filename(file_filter, RFP_DIR)
                if fname_error:
                    return jsonify({"error": fname_error}), 400

            target_paths = []
            if os.path.exists(RFP_DIR):
                all_rfps = get_pdf_list(RFP_DIR)

                if file_filter and file_filter in all_rfps:
                    target_paths = [os.path.join(RFP_DIR, file_filter)]
                else:
                    for fname in all_rfps:
                        if fname.lower() in user_query.lower():
                            target_paths.append(os.path.join(RFP_DIR, fname))
                    if not target_paths:
                        target_paths = [os.path.join(RFP_DIR, f) for f in all_rfps[:3]]

            # P1: Route to appropriate model based on action complexity
            answer = query_llm(
                prompt=f'User Query: "{user_query}"\nAnalyze the provided RFP text.',
                context_files=target_paths,
                system_prompt=SYSTEM_PROMPT_RFP,
                domain_keywords=RFP_DOMAIN_KEYWORDS,
                max_tokens=3000,  # cap output for fast, concise responses
                model=select_model(action),
            )
            return jsonify({"answer": answer})

        return jsonify({"error": f"Unknown action: {action}"}), 400
    except Exception as e:
        log.exception("RFP Error")
        return jsonify({"error": str(e)}), 500


def get_local_rfp_list():
    if not os.path.exists(RFP_DIR):
        return "RFP directory not found."
    rfps = get_pdf_list(RFP_DIR)
    if not rfps:
        return "No RFP files found."
    rows = [f"| {i + 1} | {name} |" for i, name in enumerate(rfps)]
    return "| No. | RFP File |\n| :--- | :--- |\n" + "\n".join(rows)


@app.post("/matcher/run")
def matcher_run():
    try:
        size_error = validate_request_size()
        if size_error:
            return jsonify({"error": size_error}), 413

        body = request.get_json(force=True) or {}
        action = body.get("action")

        if action in ("matcher_chat", "crossmatch_rfp"):
            rfp_name = body.get("fileName")
            user_query = body.get("query") or ""

            if action == "crossmatch_rfp" and not user_query:
                user_query = "Generate a full compliance crossmatch report for this RFP."

            if not rfp_name:
                return jsonify({"error": "fileName is required"}), 400
            fname_error = validate_filename(rfp_name, RFP_DIR)
            if fname_error:
                return jsonify({"error": fname_error}), 400

            all_product_files = []
            if os.path.exists(PRODUCT_DIR):
                all_product_files = [
                    os.path.join(PRODUCT_DIR, f)
                    for f in get_pdf_list(PRODUCT_DIR)
                ]

            if not all_product_files:
                return jsonify({"answer": "Error: No products indexed in catalog."}), 404

            mode = detect_matcher_mode(user_query)
            product_names = [os.path.basename(f) for f in all_product_files]
            direction = detect_direction(user_query, product_names)
            output_style = parse_output_style(user_query)

            if mode == "specific":
                selected = select_named_products(user_query, all_product_files, max_count=8)
                if not selected:
                    selected = all_product_files[:8]
            elif mode == "gap":
                selected = all_product_files[:16]
            else:
                selected = all_product_files[:16]

            selected = filter_product_files(selected, output_style.get("product_focus"))

            top_n = output_style.get("top_n")
            if isinstance(top_n, int) and top_n > 0 and len(selected) > top_n:
                selected = selected[:top_n]

            system_prompt = build_matcher_system_prompt(mode, direction, output_style)

            # ── Strategy C: Two-pass crossmatch when many products ──
            if len(selected) > 6 and _product_fingerprints:
                # Pass 1: Lightweight shortlisting using fingerprints
                fingerprint_context = "\n\n".join(
                    _product_fingerprints.get(os.path.basename(f), f"PRODUCT: {os.path.basename(f)}")
                    for f in selected
                )
                rfp_text = extract_relevant_pages(
                    os.path.join(RFP_DIR, rfp_name), user_query,
                    max_chars=15000, domain_keywords=RFP_DOMAIN_KEYWORDS,
                    max_scored_pages=8,
                )
                shortlist_prompt = (
                    f"You are a CP Plus bid analyst. Below are compact specs for {len(selected)} products "
                    f"and an excerpt from the RFP \"{rfp_name}\".\n\n"
                    f"USER REQUEST: \"{user_query}\"\n\n"
                    f"TASK: Return ONLY a JSON array of the top 6 most relevant product filenames "
                    f"for this RFP. Consider resolution, IP rating, camera type, and use case match. "
                    f"Format: [\"product1.pdf\", \"product2.pdf\", ...]\n\n"
                    f"--- RFP EXCERPT ---\n{rfp_text}\n\n"
                    f"--- PRODUCT FINGERPRINTS ---\n{fingerprint_context}"
                )

                log.info(f"Pass 1: Shortlisting {len(selected)} products using fingerprints ({len(fingerprint_context)} chars)")

                shortlist_result = query_llm(
                    prompt=shortlist_prompt,
                    temperature=0.0,
                    max_tokens=500,
                    model=MISTRAL_MODEL_HEAVY,  # P1 Reverted: User prioritized accuracy over cost for the shortlist
                    json_output=True,  # P0: Force JSON output for reliable parsing
                )

                # Parse the shortlist JSON
                try:
                    json_match = re.search(r'\[.*?\]', shortlist_result, re.DOTALL)
                    if json_match:
                        shortlisted_names = json.loads(json_match.group())
                        shortlisted_paths = [
                            f for f in selected
                            if os.path.basename(f) in shortlisted_names
                        ]
                        if shortlisted_paths:
                            log.info(f"Pass 1 shortlisted {len(shortlisted_paths)} products: {[os.path.basename(f) for f in shortlisted_paths]}")
                            selected = shortlisted_paths
                        else:
                            log.warning("Pass 1 returned no valid matches, using all products")
                    else:
                        log.warning("Pass 1 returned no JSON array, using all products")
                except Exception as e:
                    log.warning(f"Pass 1 shortlist parse failed: {e}, using all products")

            # Pass 2 (or single pass): Deep analysis with full context
            target_paths = [os.path.join(RFP_DIR, rfp_name)] + selected

            log.info(
                "Matcher: mode=%s, direction=%s, products=%d, rfp=%s",
                mode, direction, len(selected), rfp_name,
            )

            max_tokens = 6144 if mode == "full" else 4096

            # Strategy A: domain_keywords activates smart context for deep analysis
            # P0.2: Inject structured specs when available
            structured_context = get_structured_specs_context(selected)
            enhanced_prompt = (
                f'User Request: "{user_query}"\n'
                f'Compare the RFP "{rfp_name}" against the attached CP Plus product datasheets.\n\n'
            )
            if structured_context and _product_specs:
                enhanced_prompt += (
                    f'STRUCTURED PRODUCT SPECS (use these for precise comparison):\n'
                    f'{structured_context}\n\n'
                    f'Full datasheets are attached below for additional context.\n'
                )

            answer = query_llm(
                prompt=enhanced_prompt,
                context_files=target_paths,
                system_prompt=system_prompt,
                temperature=0.1,
                max_tokens=max_tokens,
                domain_keywords=RFP_DOMAIN_KEYWORDS,
            )
            return jsonify({"answer": answer})

        return jsonify({"error": f"Unknown action: {action}"}), 400
    except Exception as e:
        log.exception("Matcher Error")
        return jsonify({"error": str(e)}), 500


# =============================================================================
# P5: Admin — Training Data Export for Fine-Tuning
# =============================================================================
@app.get("/admin/export-training-data")
@require_admin
def export_training_data():
    """Export collected prompt-completion pairs as JSONL for Mistral fine-tuning."""
    if not _training_log:
        return jsonify({"count": 0, "message": "No training data collected yet. Set COLLECT_TRAINING_DATA=true in .env.", "data": []})
    # Format as Mistral fine-tuning JSONL format
    formatted = []
    for entry in _training_log:
        formatted.append({
            "messages": entry["messages"],
            "model": entry["model"],
        })
    return jsonify({"count": len(formatted), "data": formatted})


@app.get("/admin/embeddings-status")
@require_admin
def embeddings_status():
    """Diagnostic: show embedding index status and product similarity map."""
    return jsonify({
        "index_size": len(_product_embeddings),
        "indexed_products": list(_product_embeddings.keys()),
        "ocr_cached": list(os.path.basename(p) for p in _ocr_cache.keys()),
    })




# =============================================================================
# Unified Agent Endpoint — single entry point for all tools
# =============================================================================
@app.post("/agent/run")
def agent_run():
    """Unified endpoint that routes to rfp, product, or matcher based on 'tool' field."""
    try:
        size_error = validate_request_size()
        if size_error:
            return jsonify({"error": size_error}), 413

        body = request.get_json(force=True) or {}
        tool = body.get("tool")

        if not tool:
            return jsonify({"error": "Missing 'tool' field. Use 'rfp', 'product', or 'matcher'."}), 400

        if tool == "rfp":
            return rfp_run()
        elif tool == "product":
            return product_run()
        elif tool == "matcher":
            return matcher_run()
        else:
            return jsonify({"error": f"Unknown tool: '{tool}'. Use 'rfp', 'product', or 'matcher'."}), 400

    except Exception as e:
        log.exception("Agent Run Error")
        return jsonify({"error": str(e)}), 500


# =============================================================================
# Main
# =============================================================================
if __name__ == "__main__":
    log.info(f"Starting Unified Backend on port 8100")
    log.info(f"Tier 1: Mistral PRIMARY ({MISTRAL_MODEL})")
    log.info(f"Tier 2: Mistral BACKUP ({'enabled' if mistral_backup_client else 'disabled'})")
    log.info(f"Tier 3: Gemini ({GEMINI_MODEL}) {'enabled' if gemini_model_client else 'disabled'}")
    log.info(f"Model routing: heavy={MISTRAL_MODEL_HEAVY}, light={MISTRAL_MODEL_LIGHT}")
    log.info(f"Mistral OCR: {'enabled' if USE_MISTRAL_OCR else 'disabled'}")
    log.info(f"Training data collection: {'enabled' if COLLECT_TRAINING_DATA else 'disabled'}")
    log.info(f"RFPs: {RFP_DIR}, Products: {PRODUCT_DIR}")
    log.info(f"CORS origins: {ALLOWED_ORIGINS}")

    # Pre-warm PDF cache in background for instant first requests
    def _safe_prewarm():
        try:
            prewarm_pdf_cache()
        except Exception:
            log.exception("PDF prewarm failed — first requests may be slower")
    threading.Thread(target=_safe_prewarm, daemon=True).start()

    is_debug = os.environ.get("FLASK_DEBUG", "false").lower() == "true"
    app.run(host="0.0.0.0", port=8100, debug=is_debug)
