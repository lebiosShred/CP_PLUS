import logging
import os
import json
import time
import random

from flask import Flask, jsonify, request
from google import genai
from google.genai import types
from google.genai import errors

API_KEY = os.environ.get("GEMINI_API_KEY", "AIzaSyDcOMSWHVkTZhy4mEfcHt5rB6YlcRAZcdQ")
RFP_MAP_PATH = "rfp_file_map.json"
PRODUCT_MAP_PATH = "product_file_map.json"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s %(message)s",
)
log = logging.getLogger("matcher-universal-agent")

app = Flask(__name__)
client = genai.Client(api_key=API_KEY)


def load_map(path: str) -> dict:
    if os.path.exists(path):
        with open(path, "r") as f:
            return json.load(f)
    log.warning(f"Map file not found: {path}")
    return {}


rfp_map = load_map(RFP_MAP_PATH)
product_map = load_map(PRODUCT_MAP_PATH)
log.info(f"Loaded {len(rfp_map)} RFPs and {len(product_map)} Products.")


def detect_matcher_mode(query: str | None) -> str:
    if not query:
        return "full"

    q = query.lower()

    full_triggers = [
        "full compliance matrix",
        "full compliance report",
        "crossmatch all",
        "cross match all",
        "full crossmatch",
        "generate full matrix",
        "generate full report",
        "cross-match all products",
        "crossmatch rfp",
        "crossmatch this compliance",
        "cross match this compliance",
        "cross match this rfp",
        "compare all products",
        "compare full compliance",
        "overall compliance",
        "complete compliance",
    ]

    gap_triggers = [
        "what are we missing",
        "non compliant",
        "non-compliant",
        "gap analysis",
        "list gaps",
        "non compliance",
        "non-compliance",
        "missing items",
        "missing requirements",
        "where do we fail",
        "where are we weak",
        "only show gaps",
        "only gaps",
    ]

    if any(k in q for k in gap_triggers):
        return "gap"
    if any(k in q for k in full_triggers):
        return "full"
    if "crossmatch" in q or "cross match" in q:
        return "full"

    return "specific"


def detect_direction(query: str | None, product_map: dict) -> str:
    if not query:
        return "rfp_to_product"

    q = query.lower()

    for name in product_map.keys():
        name_lower = name.lower()
        base = name_lower.replace(".pdf", "")
        if name_lower in q or base in q:
            return "product_to_rfp"

    if "cp-" in q or "cp " in q:
        return "product_to_rfp"

    return "rfp_to_product"


def parse_user_output_style(query: str | None) -> dict:
    """
    Inspect the query to let the model decide how to shape the tables.
    This is passed into the prompt as guidance.
    """
    style = {
        "per_product_tables": False,
        "per_rfp_item_matrix": False,
        "gap_only": False,
        "include_scores": False,
        "top_n": None,
        "product_focus": None,
    }

    if not query:
        style["per_rfp_item_matrix"] = True
        return style

    q = query.lower()

    if "per product" in q or "for each product" in q or "product wise" in q or "product-wise" in q:
        style["per_product_tables"] = True

    if "per item" in q or "per rfp item" in q or "item wise" in q or "item-wise" in q:
        style["per_rfp_item_matrix"] = True

    if "gap only" in q or "only gaps" in q or "only non compliant" in q:
        style["gap_only"] = True

    if "score" in q or "weighted" in q or "percentage" in q or "%" in q:
        style["include_scores"] = True

    # Simple top N detection
    for n in [3, 5, 10]:
        if f"top {n}" in q or f"best {n}" in q:
            style["top_n"] = n
            break

    if "camera" in q:
        style["product_focus"] = "camera"
    if "nvr" in q or "network video recorder" in q:
        style["product_focus"] = "nvr"

    # Default matrix if nothing else is explicit
    if not style["per_product_tables"] and not style["per_rfp_item_matrix"]:
        style["per_rfp_item_matrix"] = True

    return style


def select_named_products_from_query(
    query: str,
    product_map: dict,
    max_products: int = 5,
) -> list[dict]:
    q_lower = query.lower()
    matches: list[dict] = []

    for name, meta in product_map.items():
        name_lower = name.lower()
        base = name_lower.replace(".pdf", "")
        if name_lower in q_lower or base in q_lower:
            matches.append(meta)
        if len(matches) >= max_products:
            break

    return matches


def filter_products_by_focus(
    products: list[dict],
    product_focus: str | None,
) -> list[dict]:
    if not product_focus:
        return products

    filtered: list[dict] = []

    for meta in products:
        name = meta.get("name") or meta.get("fileName") or meta.get("title") or ""
        name_lower = str(name).lower()

        if product_focus == "camera":
            if "cam" in name_lower or "unc-" in name_lower or "dome" in name_lower or "bullet" in name_lower:
                filtered.append(meta)
        elif product_focus == "nvr":
            if "nvr" in name_lower or "unr-" in name_lower:
                filtered.append(meta)

    if filtered:
        return filtered

    return products


def build_matcher_prompt(mode: str, direction: str, query: str, output_style: dict) -> str:
    style_block_parts = []

    if output_style.get("per_product_tables"):
        style_block_parts.append(
            """
TABLE LAYOUT PREFERENCE: PER PRODUCT TABLES
- When helpful, output one mini spec sheet table per product.
- For each product, use a heading such as:
  "## Product #1  <MODEL> vs <RFP Item>"
- Under each heading, use a table like:
  | SN | Feature | RFP Requirement | <MODEL> Spec | Compliance | Comment |
- This layout is ideal when the user wants a product by product view."""
        )

    if output_style.get("per_rfp_item_matrix"):
        style_block_parts.append(
            """
TABLE LAYOUT PREFERENCE: PER RFP ITEM MATRIX
- You can also use a matrix with one row per RFP item.
- A good default is:
  | RFP Item | Key RFP Requirements | CP Plus Model | Key CP Plus Specs | Compliance | Comment |
- This layout is ideal for a top down view across many items in the RFP."""
        )

    if output_style.get("gap_only") or mode == "gap":
        style_block_parts.append(
            """
TABLE LAYOUT PREFERENCE: GAPS ONLY
- Focus on requirements that are missing or not fully compliant.
- A good layout is:
  | RFP Item | Key Requirement | Gap Or Non Compliance | Suggested Action |"""
        )

    if output_style.get("include_scores"):
        style_block_parts.append(
            """
SCORING AND PERCENTAGE
- When appropriate, assign a simple numeric score and percentage compliance.
- For example:
  | Feature | Weight | Compliance | Score | Comment |
- Or summarize:
  "Overall estimated compliance for this product is about X percent based on the checked features."
- Make sure the scoring is simple and clearly tied to visible features."""
        )

    top_n = output_style.get("top_n")
    if isinstance(top_n, int) and top_n > 0:
        style_block_parts.append(
            f"""
PRODUCT COUNT CONTROL
- The user is interested in a limited set of top candidates.
- Prefer to highlight about {top_n} of the best matching products.
- You may still mention others briefly, but keep detailed tables for the strongest {top_n} candidates."""
        )

    style_block = "\n".join(style_block_parts)

    if direction == "product_to_rfp":
        direction_block = """
DIRECTION: PRODUCT TO RFP
You must:
1. Treat the named CP Plus product or products in the user request as the starting point.
2. Identify which RFP requirements are relevant for judging those products:
   - For example camera technical specifications, NVR specifications, storage days, resolution, IR distance, IP rating, PoE, etc.
3. Compare each named product against the relevant RFP requirements.
4. Explain clearly why each product is compliant, partially compliant, or non compliant."""
    else:
        direction_block = """
DIRECTION: RFP TO PRODUCT
You must:
1. Treat the RFP technical requirements as the starting point.
2. Identify the main hardware or system items defined in the RFP that are relevant to the user request:
   - For example specific camera types, NVRs, storage systems, switches, workstations, displays, or other CCTV components.
3. For each such RFP item, search the attached CP Plus datasheets for the best matching models.
4. Explain clearly why each chosen model is compliant, partially compliant, or non compliant."""

    base_meta = f"""
You are an Expert Bid Manager for CP Plus.

User Request:
"{query}"

You are comparing:
- DOCUMENT A: The RFP requirements.
- DOCUMENT B: CP Plus product datasheets.

{direction_block}

Before you write the final answer, you must do a private multi step plan in your head:
1. Infer what the user is really asking for:
   - A full crossmatch overview of many items.
   - A focused check on one RFP item or a small set of requirements.
   - A list of gaps and non compliant items.
   - A check of whether a specific CP Plus product satisfies some part of the RFP.
2. Locate the exact RFP sections that matter for the question:
   - Technical specifications tables.
   - Schedules of requirements.
   - System design and technical requirement sections.
3. For those sections, extract the requirements into internal structured features such as:
   - Resolution (MP and pixel dimensions).
   - Lens type and focal length.
   - IR distance.
   - WDR level.
   - IP rating.
   - Power type (PoE, 12 VDC, etc.).
   - Storage capacity and days of recording.
   - Network and protocol support.
4. For the selected CP Plus products, internally extract the same kind of structured features from the datasheets.
5. Perform a strict feature by feature comparison, using these rules:
   - If the RFP requires a minimum numeric value, a product with a lower numeric value is non compliant for that feature.
   - If the RFP requires a specific resolution (for example 8 MP) then any product with a lower resolution (for example 2 MP) is non compliant for that requirement.
   - If the RFP requires IP67, then IP67 is compliant, IP66 is not fully compliant.
   - If a field is missing in the product sheet or RFP, treat it as "Missing" and do not mark it as fully compliant for that feature.
6. Only after you have done this internal comparison, format the final answer.
7. Keep all internal planning and step by step reasoning private.

In your final answer, only show:
- Short natural language summaries.
- Structured Markdown tables with clear compliance indications.
- Short and precise comments that explain why each product is compliant or not.

{style_block}
"""

    if mode == "full":
        return base_meta + """
MODE: FULL COMPLIANCE MATRIX STYLE

Behavior:
1. Identify each main RFP hardware or system item that is relevant to the question.
2. For each item, select the best matching CP Plus product or a small set of candidates.
3. Always run a sanity check before marking any product as compliant:
   - Never claim that a 2 MP camera satisfies an 8 MP requirement.
   - Never claim that a shorter IR distance satisfies a longer IR requirement.
   - Never claim that a weaker IP rating satisfies a stronger IP rating.
4. If you are unsure about compliance due to missing information, treat that feature as non compliant or "Missing", and explain why.

Output:
1. Begin with a short overview paragraph that explains:
   - What you compared.
   - How many RFP items were considered.
   - At a high level how many are compliant versus non compliant.
2. Then output one or more Markdown tables. A good default structure is:
   | RFP Item | Key RFP Requirements | CP Plus Model | Key CP Plus Specs | Compliance | Comment |
   | :--- | :--- | :--- | :--- | :--- | :--- |
3. For some scenarios, if the user clearly asks for product wise tables, you may instead or additionally output multiple product specific tables:
   - One heading per product.
   - Under each heading, a table of the form:
     | SN | Feature | RFP Requirement | Product Spec | Compliance | Comment |
4. If an RFP item has no clear match, still include a row with CP Plus Model set to "No clear match" and Compliance set to a cross mark with a short explanation.
5. At the end, add a short bullet list of suggested follow up questions the user could ask."""

    if mode == "gap":
        return base_meta + """
MODE: GAP ANALYSIS STYLE

Behavior:
1. Focus on where the CP Plus catalog does not fully meet the RFP requirements.
2. Identify each RFP item or requirement where:
   - No product matches, or
   - The chosen product is weaker on at least one key parameter.

Output:
1. Start with a one paragraph summary that describes the overall gap picture.
2. Then output a Markdown table:
   | RFP Item | Key Requirement | Gap Or Non Compliance | Suggested Action |
   | :--- | :--- | :--- | :--- |
3. Add one row per gap. Be specific about the mismatch.
4. In Suggested Action, propose a practical next step.
5. Use only information from the attached documents and clearly label missing fields as "Missing in RFP" or "Missing in product datasheet" where appropriate."""

    return base_meta + """
MODE: SPECIFIC COMPLIANCE CHECK STYLE

Behavior:
1. Interpret the user request as a focused check for one requirement or a small group of related requirements.
2. Locate the exact requirement text in the RFP.
3. Compare that requirement strictly against the selected CP Plus products.
4. Always perform a quick internal sanity check before marking a product compliant.

Output:
1. Prefer a compact Markdown table:
   | Requirement | RFP Value | CP Plus Spec | Compliance | Comment |
   | :--- | :--- | :--- | :--- | :--- |
2. If the user mention per product tables, you can instead show one small table per product.
3. Clearly answer yes or no for each requirement in the Compliance column using a simple check or cross mark and explain briefly in the Comment column.
4. If the required information is missing in either the RFP or the product datasheet, mark Compliance as a cross or "Unknown" and state that it is not specified."""


def build_matcher_config(mode: str) -> types.GenerateContentConfig:
    if mode == "full":
        return types.GenerateContentConfig(
            temperature=0.1,
            max_output_tokens=8192,
        )
    if mode == "gap":
        return types.GenerateContentConfig(
            temperature=0.1,
            max_output_tokens=4096,
        )
    return types.GenerateContentConfig(
        temperature=0.1,
        max_output_tokens=4096,
    )


def generate_compliance_response(
    rfp_uri: str,
    product_uris: list[str],
    mode: str,
    direction: str,
    query: str,
    output_style: dict,
) -> tuple[str | None, str | None]:
    model_name = "gemini-flash-latest"
    max_retries = 2
    base_delay = 2.0

    contents_parts: list[types.Part] = [
        types.Part(text="DOCUMENT A: THE RFP (REQUIREMENTS)"),
        types.Part(
            file_data=types.FileData(
                file_uri=rfp_uri,
                mime_type="application/pdf",
            )
        ),
        types.Part(text="DOCUMENT B: CP PLUS PRODUCT CATALOG"),
    ]

    for p_uri in product_uris:
        contents_parts.append(
            types.Part(
                file_data=types.FileData(
                    file_uri=p_uri,
                    mime_type="application/pdf",
                )
            )
        )

    prompt = build_matcher_prompt(mode, direction, query, output_style)
    contents_parts.append(types.Part(text=prompt))

    config = build_matcher_config(mode)

    for attempt in range(max_retries):
        try:
            log.info(
                "Sending matcher request to %s (mode=%s, direction=%s, attempt=%s, products=%s)",
                model_name,
                mode,
                direction,
                attempt + 1,
                len(product_uris),
            )
            response = client.models.generate_content(
                model=model_name,
                contents=[types.Content(role="user", parts=contents_parts)],
                config=config,
            )
            text = getattr(response, "text", None)

            if not text:
                pieces: list[str] = []
                try:
                    candidates = getattr(response, "candidates", None) or []
                    for cand in candidates:
                        content = getattr(cand, "content", None)
                        if not content:
                            continue
                        parts = getattr(content, "parts", None) or []
                        for part in parts:
                            part_text = getattr(part, "text", None)
                            if part_text:
                                pieces.append(part_text)
                except Exception as e:
                    log.warning("Failed to parse matcher response parts: %s", e)
                if pieces:
                    text = "\n".join(pieces)

            if not text or not str(text).strip():
                log.warning("Matcher model returned no text content.")
                return None, "Model returned no content for this crossmatch request."

            return text, None

        except errors.ClientError as e:
            error_str = str(e)
            if "429" in error_str or "RESOURCE_EXHAUSTED" in error_str:
                wait_time = base_delay * (2 ** attempt) + random.uniform(0, 1)
                log.warning("Matcher rate limit hit. Retrying in %.1f seconds...", wait_time)
                time.sleep(wait_time)
                continue
            return None, f"API Error: {e}"
        except Exception as e:
            log.exception("Matcher system error")
            return None, f"System Error: {str(e)}"

    return None, "Failed: Server busy or quota exceeded."


@app.post("/matcher/run")
def run_action():
    try:
        body = request.get_json(force=True) or {}
        action = body.get("action")

        if action == "matcher_chat" or action == "crossmatch_rfp":
            rfp_name = body.get("fileName")
            user_query = body.get("query")

            if action == "crossmatch_rfp" and not user_query:
                user_query = "Generate a full compliance crossmatch report for this RFP."

            if not rfp_name:
                return jsonify({"error": "fileName is required"}), 400

            if rfp_name not in rfp_map:
                return jsonify({"error": f"RFP file '{rfp_name}' not found in index"}), 404

            rfp_uri = rfp_map[rfp_name]["uri"]

            if not product_map:
                return jsonify({"answer": "Error: No products indexed."})

            mode = detect_matcher_mode(user_query or "")
            direction = detect_direction(user_query or "", product_map)
            output_style = parse_user_output_style(user_query or "")

            max_products_full = 25
            max_products_gap = 25
            max_products_specific = 8

            selected_products: list[dict] = []

            if mode == "specific":
                named_products = select_named_products_from_query(
                    user_query or "",
                    product_map,
                    max_products=max_products_specific,
                )
                if named_products:
                    selected_products = named_products
                else:
                    selected_products = list(product_map.values())[:max_products_specific]
            elif mode == "gap":
                selected_products = list(product_map.values())[:max_products_gap]
            else:
                selected_products = list(product_map.values())[:max_products_full]

            # Optional filter by product type hints like camera or NVR
            selected_products = filter_products_by_focus(
                selected_products,
                output_style.get("product_focus"),
            )

            # Optional top N limit from query
            top_n = output_style.get("top_n")
            if isinstance(top_n, int) and top_n > 0 and len(selected_products) > top_n:
                selected_products = selected_products[:top_n]

            product_uris = [
                p["uri"] for p in selected_products if isinstance(p, dict) and "uri" in p
            ]

            if not product_uris:
                return jsonify({"answer": "Error: No valid product URIs available in index."})

            log.info(
                "Running Matcher Logic: '%s' on '%s' (mode=%s, direction=%s, products=%s)",
                user_query,
                rfp_name,
                mode,
                direction,
                len(product_uris),
            )

            answer, error = generate_compliance_response(
                rfp_uri,
                product_uris,
                mode,
                direction,
                user_query or "",
                output_style,
            )

            if error:
                return jsonify({"answer": f"Error: {error}"})

            if not answer or not str(answer).strip():
                return jsonify(
                    {"answer": "Matcher could not generate any content for this request."}
                )

            return jsonify({"answer": answer})

        return jsonify({"error": f"Unknown action: {action}"}), 400

    except Exception as e:
        log.exception("Server Error")
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    log.info("Starting Universal Matcher Service on port 8002")
    app.run(host="0.0.0.0", port=8002, debug=True)
