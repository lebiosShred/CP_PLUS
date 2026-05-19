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
PRODUCT_MAP_PATH = "product_file_map.json"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s %(message)s",
)
log = logging.getLogger("product-universal-agent")

app = Flask(__name__)
client = genai.Client(api_key=API_KEY)


def load_product_map() -> dict:
    if os.path.exists(PRODUCT_MAP_PATH):
        with open(PRODUCT_MAP_PATH, "r") as f:
            return json.load(f)
    log.error(f"Product map not found at {PRODUCT_MAP_PATH}")
    return {}


file_map = load_product_map()
log.info("Loaded %s products from map.", len(file_map))


def get_local_product_list() -> str:
    products = sorted(list(file_map.keys()))
    if not products:
        return "No product datasheets found in the index."

    rows = [f"| {i+1} | {name} |" for i, name in enumerate(products)]
    table = "| No. | Product Datasheet |\n| :--- | :--- |\n" + "\n".join(rows)
    return f"Here are the available CP Plus product datasheets:\n\n{table}"


def query_gemini(prompt: str, context_files: list[str] | None = None) -> str:
    model_name = "gemini-flash-latest"
    max_retries = 3
    base_delay = 2.0

    contents: list[types.Part] = []

    if context_files:
        for uri in context_files:
            contents.append(
                types.Part(
                    file_data=types.FileData(
                        file_uri=uri,
                        mime_type="application/pdf",
                    )
                )
            )

    contents.append(types.Part(text=prompt))

    config = types.GenerateContentConfig(
        temperature=0.1,
        max_output_tokens=8192,
    )

    for attempt in range(max_retries):
        try:
            response = client.models.generate_content(
                model=model_name,
                contents=[types.Content(role="user", parts=contents)],
                config=config,
            )
            return response.text
        except errors.ClientError as e:
            error_str = str(e)
            if "429" in error_str or "RESOURCE_EXHAUSTED" in error_str:
                wait_time = base_delay * (2 ** attempt) + random.uniform(0, 1)
                log.warning(
                    "Gemini product call rate limited. Retry %s in %.1f seconds.",
                    attempt + 1,
                    wait_time,
                )
                time.sleep(wait_time)
                continue
            log.warning("Gemini product client error: %s", e)
            return "Error: Product AI API error."
        except Exception as e:
            log.warning("Gemini product call failed: %s", e)
            return "Error: Product service is busy. Please try again later."

    return "Error: Product service is busy. Please try again later."


@app.post("/product/run")
def run_action():
    try:
        body = request.get_json(force=True) or {}
        action = body.get("action")

        if action in ("product_chat", "product_search"):
            user_query = (body.get("query") or "").strip()
            if not user_query:
                return jsonify({"error": "Query required"}), 400

            q_lower = user_query.lower()

            if (
                ("list" in q_lower or "show all" in q_lower or "available" in q_lower)
                and (
                    "product" in q_lower
                    or "camera" in q_lower
                    or "nvr" in q_lower
                    or "datasheet" in q_lower
                    or "model" in q_lower
                )
            ):
                return jsonify({"answer": get_local_product_list()})

            target_uris: list[str] = []
            system_prompt: str

            found_file: str | None = None
            for fname in file_map.keys():
                base = fname
                if base.lower().endswith(".pdf"):
                    base = base[:-4]

                if fname.lower() in q_lower or base.lower() in q_lower:
                    found_file = fname
                    break

            if found_file:
                meta = file_map.get(found_file, {})
                uri = meta.get("uri")
                if uri:
                    target_uris = [uri]
                system_prompt = f"""
You are a CP Plus Product Advisor analyzing the product datasheet file "{found_file}".

You work only from the attached datasheet and must not use external knowledge.
"""
            else:
                target_uris = [
                    meta["uri"]
                    for meta in file_map.values()
                    if isinstance(meta, dict) and "uri" in meta
                ]
                system_prompt = """
You are a CP Plus Product Advisor. You have access to the attached CP Plus product datasheets.

You work only from these datasheets and must not use external knowledge.
"""

            if not target_uris:
                return jsonify(
                    {"answer": "No product datasheets are available in the current index."}
                )

            full_prompt = f"""
{system_prompt}

User Query: "{user_query}"

INSTRUCTIONS:

1. Internal reasoning:
   - Before you answer, silently scan the attached datasheets to understand which products and fields are relevant.
   - Do not expose your internal reasoning steps. Only output the final answer.

2. Intent handling:
   - If the user asks for "specs", "full specification", or a specific model:
     - Treat this as a specific lookup.
     - Return a detailed spec sheet in Markdown table form.
     - Use a stable table such as:
       | SN | Feature | Specification |
       | :-- | :-- | :-- |
     - Include all relevant rows and parameters from the datasheet for that model. Do not truncate long requirement text.

   - If the user describes a scenario or use case:
     - Treat this as a recommendation query.
     - Identify one or more suitable models.
     - For each recommended model, show a short spec sheet table plus a short explanation why it fits.

   - If the user asks to compare named models:
     - Build a comparison matrix with models as columns and key features as rows.
     - Use Markdown tables so that it is easy to read.

3. Listing:
   - If the user asks to list products, cameras, NVRs, or datasheets, and you are not already in a dedicated "list_products" action:
     - You may show a concise table of models and a few key attributes.
     - Keep it compact.

4. Honesty:
   - If a field is not found in any attached datasheet, say "Not specified in datasheet" instead of guessing.
   - Do not invent values.

Remember: work only from the attached PDF datasheets and the user question. Output only the final tables and text that the user should see.
"""
            log.info(
                "Running Universal Product Query: %s (files attached: %s)",
                user_query,
                len(target_uris),
            )
            answer = query_gemini(full_prompt, target_uris)
            return jsonify({"answer": answer})

        if action == "list_products":
            return jsonify({"answer": get_local_product_list()})

        return jsonify({"error": f"Unknown action: {action}"}), 400

    except Exception as e:
        log.exception("Server Error in product_files_caller")
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    log.info("Starting Versatile Product Agent on port 8000")
    app.run(host="0.0.0.0", port=8000, debug=True)
