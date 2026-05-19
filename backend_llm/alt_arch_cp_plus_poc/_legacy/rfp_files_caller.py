import logging
import os
import json
import time

from flask import Flask, jsonify, request
from google import genai
from google.genai import types

API_KEY = os.environ.get("GEMINI_API_KEY", "AIzaSyDcOMSWHVkTZhy4mEfcHt5rB6YlcRAZcdQ")
FILE_MAP_PATH = "rfp_file_map.json"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s %(message)s",
)
log = logging.getLogger("rfp-agent-service")

app = Flask(__name__)
client = genai.Client(api_key=API_KEY)


def load_file_map():
    if os.path.exists(FILE_MAP_PATH):
        with open(FILE_MAP_PATH, "r") as f:
            return json.load(f)
    return {}


file_map = load_file_map()


def get_local_file_list():
    rfps = sorted(list(file_map.keys()))
    if not rfps:
        return "No RFP files found in the index."
    table_rows = [f"| {i + 1} | {name} |" for i, name in enumerate(rfps)]
    table = "| No. | RFP File Name |\n| :--- | :--- |\n" + "\n".join(table_rows)
    return f"Here are the available RFP documents:\n\n{table}"


def query_gemini(prompt: str, context_files: list = None):
    model_name = "gemini-flash-latest"
    max_retries = 3

    contents = []
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

    for attempt in range(max_retries):
        try:
            response = client.models.generate_content(
                model=model_name,
                contents=[types.Content(role="user", parts=contents)],
                config=types.GenerateContentConfig(
                    temperature=0.1,
                    max_output_tokens=8192,
                ),
            )
            return response.text
        except Exception as e:
            time.sleep(2 * (attempt + 1))
            log.warning("Gemini Retry %s: %s", attempt + 1, e)

    return "Error: System busy."


def classify_intent(action: str, user_query: str, file_filter: str, section_title: str):
    q = (user_query or "").lower().strip()

    if action == "list_rfps":
        return "list"

    if file_filter or section_title:
        return "section"

    if not q:
        return "general"

    list_keywords = ["list all rfps", "list rfps", "show all rfps", "show all rfp files"]
    if any(kw in q for kw in list_keywords):
        return "list"

    if "technical specification" in q or "technical specifications" in q:
        return "possible_section"

    if "system design and technical requirements" in q:
        return "possible_section"

    if "technical evaluation criteria" in q:
        return "possible_section"

    return "general"


def resolve_target_files(file_filter: str, user_query: str):
    if file_filter and file_filter in file_map:
        return [file_filter]

    q = (user_query or "").lower()

    best_match = None
    for fname in file_map.keys():
        name_lower = fname.lower()
        if name_lower in q:
            best_match = fname
            break
        if len(name_lower) > 12 and name_lower[:12] in q:
            best_match = fname
            break
        base = os.path.splitext(name_lower)[0]
        if base in q:
            best_match = fname
            break

    if best_match:
        return [best_match]

    return sorted(list(file_map.keys()))


def build_full_prompt(user_query: str, file_filter: str, section_title: str, target_files: list):
    files_text = ""
    if target_files:
        files_text = "You have access to these RFP documents:\n" + "\n".join(
            f"- {name}" for name in target_files
        )

    section_hint_block = ""
    if section_title:
        section_hint_block = f"""
Target section hint:
- The user is focused on a specific section or item that is described as: "{section_title}".
- You must treat this as the primary anchor when searching headings and item labels.
- Look for exact matches, close variants, and numbered items that clearly correspond to this text.
- If you cannot find any section or item that confidently matches this hint, you must clearly say that the section was not found and do not guess the specification.
"""

    base_instructions = f"""
{files_text}

User Query:
"{user_query or ""}"

You are working with one or more RFP documents in PDF form. You must rely only on what is written in the attached RFPs and not on external knowledge.

1. Internal reasoning:
- Before writing your answer, silently think step by step through the relevant parts of the RFP.
- First scan the table of contents and section headings.
- Then identify all candidate sections that might answer the user request.
- For each candidate section, read the full content including tables, bullet lists, and any text that continues on subsequent pages.
- Make sure you do not miss rows at the bottom of pages or items that continue on the next page.
- Do not expose these internal steps in your answer. Only output the final answer.

2. Large sections and menus:
- If the user asks for a very large section such as:
  - all technical specifications for the RFP
  - the entire technical specification section
  - all system design and technical requirements
  - the complete CCTV technical requirements
then in your first answer you must:
- Build and return a clean "Menu of Technical Specifications" or "Menu of Sections or Items" that lists each main item or subsection with a short label and page reference.
- After the menu, briefly suggest what the user can do next to get details, for example that they can ask for a specific item number or a subset of items.
- In this first answer do not include any full specification tables or long detailed spec blocks. Only the menu plus your natural language guidance on next steps.
- When the user mentions "compliance" but still asks for technical specification content, treat it as a technical specification request and prepare the answer so that it can later be used for compliance checks.

3. Follow up and narrowed scope:
- When the user then asks about a specific item, a numbered entry from the menu, a small subset of items, or a clearly narrowed scope (for example "show specs for item 1 and 2" or "show the PTZ camera spec"), then you must return the detailed technical specifications for that scope.
- Be exhaustive inside that narrower scope and include all rows and fields that belong to that specification, even if the text is long.
- If the specification spans more than one page, continue extracting until the end of that specification block, not just the first page.

4. Specific values:
- If the user asks for a small number of specific values, such as EMD, tender fee, completion period, warranty period, or a single parameter like IP rating or IR range, answer directly with those values and include page citations when visible.
- You do not need to show a menu first in that case.

5. Layout mirroring:
- Try to mirror the layout style used in the RFP:
  - Tables remain tables.
  - Bullet or numbered lists remain lists.
  - Prose remains short clear paragraphs.
- Only convert prose into tables when the user explicitly asks for a table, matrix, spec sheet, or side by side comparison.

6. Technical specification tables:
- When you output detailed technical specification tables for items such as cameras, NVRs, storage, switches, or similar equipment, use this default structure unless the RFP clearly uses a different but equally structured layout that you must preserve:
  - Header row:
    | SN | Feature | Requirement |
    | :-- | :-- | :-- |
- Each data row must have exactly three cells:
  - The serial number from the RFP (or a logical serial number if the RFP does not provide one).
  - The feature name exactly as written or with only minor formatting cleanup.
  - The full requirement text as written in the RFP. Do not truncate or summarize the requirement. Bring over the complete text, even if it is long.
- Do not produce tables where the header has two columns but rows have three or more cells.
- Do not drop requirement text to shorten an answer. It is better to include the full requirement text for each row.

7. Honesty and missing information:
- If a required value or field cannot be found anywhere in the attached RFP files, say "Not found in the provided RFP files" for that field or for the whole answer as appropriate.
- Do not invent values or specifications.

8. Multi step verification:
- Treat your work as a multi step process with a verification loop.
- Phase A: Interpret the intent
  - Decide if the user wants a menu of sections, a full technical specification for a specific section, or a small set of values.
- Phase B: Locate candidate sections
  - Find all headings, numbered items, and tables that could match the target section or parameters.
  - When a section title hint is provided, treat it as the main anchor and search for exact and close matches.
- Phase C: Draft the answer
  - Build the menu or specification table by copying values directly from the RFP text.
- Phase D: Verification loop
  - Re scan the same RFP pages that you used for the answer.
  - For each row in your final table, verify that the key feature name and requirement text appear in the source document.
  - If any value seems inferred or approximate rather than directly supported by the text, replace it with "Not found in the provided RFP files" or remove that row.
  - Confirm that no obvious rows or parameters from the source table are missing from your output inside the selected scope.
- Only after completing this internal verification loop should you output the final answer.
- Never describe these phases or the verification loop in the answer. They are internal guidance only.

Remember: perform your detailed reasoning and verification internally and output only the final tables, lists, or paragraphs, not the reasoning steps themselves.
"""

    return base_instructions + section_hint_block


@app.post("/run")
def run_action():
    try:
        body = request.get_json(force=True) or {}
        action = body.get("action")

        if action == "list_rfps":
            return jsonify({"answer": get_local_file_list()})

        if action in ("rfp_chat", "rfp_search", "rfp_focus_section"):
            user_query = (body.get("query") or "").strip()
            file_filter = body.get("fileName")
            section_title = body.get("sectionTitle")

            if not user_query and section_title:
                user_query = f"Extract the section: {section_title}"

            intent = classify_intent(action, user_query, file_filter, section_title)

            if intent == "list":
                return jsonify({"answer": get_local_file_list()})

            target_files = resolve_target_files(file_filter, user_query)
            if not target_files:
                return jsonify({"answer": "No RFP files found in the index."})

            target_uris = [file_map[name]["uri"] for name in target_files if name in file_map]

            if not target_uris:
                return jsonify({"answer": "No valid RFP file URIs found in the index."})

            full_prompt = build_full_prompt(user_query, file_filter, section_title, target_files)

            log.info("Sending Query to AI. Files attached: %s", len(target_uris))
            answer = query_gemini(full_prompt, target_uris)
            return jsonify({"answer": answer})

        return jsonify({"error": f"Unknown action: {action}"}), 400

    except Exception as e:
        log.exception("Server Error")
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    log.info("Starting Fast Path RFP Service on port 8001")
    app.run(host="0.0.0.0", port=8001, debug=True)
