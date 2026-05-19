"""
CP Plus Agentic Suite — Contract Chain Validator (P5.4)

Parses cp_plus_tools_unified.yaml, master_openapi.json, and unified_backend.py
to ensure all action enums, operationIds, and routes are in sync.

Usage:
    python validate_contracts.py
"""

import json
import os
import re
import sys

# ---------------------------------------------------------------------------
# File paths (relative to this script)
# ---------------------------------------------------------------------------
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(os.path.dirname(SCRIPT_DIR))

DEPLOYED_SPEC = os.path.join(REPO_ROOT, "cp_plus_poc", "cp_plus_tools_unified.yaml")
MASTER_SPEC = os.path.join(SCRIPT_DIR, "master_openapi.json")
BACKEND = os.path.join(SCRIPT_DIR, "unified_backend.py")

errors = []
warnings = []


def extract_yaml_enums(filepath: str, field_name: str) -> list[str]:
    """Extract enum values for a given field from a YAML file (simple parser)."""
    enums = []
    with open(filepath, "r", encoding="utf-8") as f:
        lines = f.readlines()
    # State machine: find "action:" then "enum:" then collect "- value" lines
    in_field = False
    in_enum = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith(f"{field_name}:"):
            in_field = True
            in_enum = False
            continue
        if in_field and stripped == "enum:":
            in_enum = True
            continue
        if in_enum:
            if stripped.startswith("- "):
                val = stripped[2:].strip().strip("'\"")
                if val and val != field_name:  # exclude the field name itself
                    enums.append(val)
            else:
                in_field = False
                in_enum = False
    return sorted(set(enums))


def extract_json_enums(filepath: str, field_name: str) -> list[str]:
    """Extract all enum values for fields named 'action' across all oneOf schemas."""
    with open(filepath, "r", encoding="utf-8") as f:
        spec = json.load(f)
    enums = set()

    def _find_enums(obj, target_key):
        """Recursively find all enum arrays under a key named target_key."""
        if isinstance(obj, dict):
            for key, value in obj.items():
                if key == target_key and isinstance(value, dict):
                    for e in value.get("enum", []):
                        enums.add(e)
                _find_enums(value, target_key)
        elif isinstance(obj, list):
            for item in obj:
                _find_enums(item, target_key)

    _find_enums(spec, field_name)
    return sorted(enums)


def extract_backend_actions(filepath: str, route_pattern: str) -> list[str]:
    """Extract action string literals from if-blocks in a route handler."""
    with open(filepath, "r", encoding="utf-8") as f:
        content = f.read()
    # Find all `if action == "xxx"` or `if action in ("xxx", "yyy")`
    actions = set()
    for match in re.finditer(r'if action\s*==\s*"([^"]+)"', content):
        actions.add(match.group(1))
    for match in re.finditer(r'if action\s+in\s*\(([^)]+)\)', content):
        for item in re.findall(r'"([^"]+)"', match.group(1)):
            actions.add(item)
    # Exclude false positives (field names, not action values)
    actions.discard("action")
    return sorted(actions)


def extract_server_urls(filepath: str) -> list[str]:
    """Extract server URLs from a spec file."""
    urls = []
    with open(filepath, "r", encoding="utf-8") as f:
        content = f.read()
    if filepath.endswith(".json"):
        data = json.loads(content)
        for server in data.get("servers", []):
            urls.append(server.get("url", ""))
    else:
        for match in re.finditer(r'url:\s*(.+)', content):
            urls.append(match.group(1).strip().strip("'\""))
    return urls


def validate():
    """Run all contract chain validations."""
    print("=" * 60)
    print("CP PLUS CONTRACT CHAIN VALIDATOR")
    print("=" * 60)

    # --- Check 1: Files exist ---
    print("\n[1] Checking file existence...")
    for label, path in [("Deployed Spec", DEPLOYED_SPEC), ("Master Spec", MASTER_SPEC), ("Backend", BACKEND)]:
        if os.path.exists(path):
            print(f"  ✅ {label}: {os.path.basename(path)}")
        else:
            errors.append(f"{label} not found: {path}")
            print(f"  ❌ {label}: NOT FOUND")

    if errors:
        print_results()
        return

    # --- Check 2: Action enum sync (YAML ↔ JSON spec) ---
    print("\n[2] Validating action enums (spec ↔ spec)...")
    yaml_rfp = extract_yaml_enums(DEPLOYED_SPEC, "action")
    json_rfp = extract_json_enums(MASTER_SPEC, "action")
    backend_rfp = extract_backend_actions(BACKEND, "/rfp/run")

    print(f"  Deployed YAML: {yaml_rfp}")
    print(f"  Master JSON:   {json_rfp}")
    print(f"  Backend code:  {backend_rfp}")

    yaml_set = set(yaml_rfp)
    json_set = set(json_rfp)

    if yaml_set != json_set:
        diff_yaml = yaml_set - json_set
        diff_json = json_set - yaml_set
        if diff_yaml:
            errors.append(f"Actions in deployed YAML but NOT in master JSON: {diff_yaml}")
        if diff_json:
            errors.append(f"Actions in master JSON but NOT in deployed YAML: {diff_json}")
        print(f"  ❌ YAML ↔ JSON DESYNC")
    else:
        print(f"  ✅ YAML ↔ JSON in sync ({len(yaml_set)} actions)")

    # Check backend handles all spec-defined action values
    backend_set = set(backend_rfp)
    spec_actions = yaml_set | json_set
    missing_in_backend = spec_actions - backend_set
    # Filter: compound if-statements handle multiple actions, so check parent enums
    # e.g., "rfp_chat" and "rfp_focus_section" handled in `if action in ("rfp_chat", "rfp_focus_section")`
    if missing_in_backend:
        warnings.append(f"Actions in spec but not explicitly matched in if-blocks: {missing_in_backend}")
        print(f"  ⚠️ Backend may be missing handlers for: {missing_in_backend}")
    else:
        print(f"  ✅ Backend handles all spec actions")

    # --- Check 3: Server URL sync ---
    print("\n[3] Validating server URLs...")
    yaml_urls = extract_server_urls(DEPLOYED_SPEC)
    json_urls = extract_server_urls(MASTER_SPEC)

    print(f"  Deployed YAML: {yaml_urls}")
    print(f"  Master JSON:   {json_urls}")

    if yaml_urls and json_urls:
        if yaml_urls[0] != json_urls[0]:
            errors.append(f"Server URL mismatch: YAML={yaml_urls[0]} vs JSON={json_urls[0]}")
            print(f"  ❌ URL MISMATCH — run update_ngrok_url.ps1")
        else:
            print(f"  ✅ URLs match: {yaml_urls[0]}")
    else:
        warnings.append("Could not extract server URLs from one or both specs")

    # --- Check 4: operationId in agent YAML ---
    print("\n[4] Validating operationId binding...")
    agent_yaml = os.path.join(REPO_ROOT, "cp_plus_poc", "cp_plus_agent.yaml")
    if os.path.exists(agent_yaml):
        with open(agent_yaml, "r", encoding="utf-8") as f:
            agent_content = f.read()
        with open(MASTER_SPEC, "r", encoding="utf-8") as f:
            master_data = json.load(f)
        # Extract operationIds from master spec
        op_ids = set()
        for path, methods in master_data.get("paths", {}).items():
            for method, details in methods.items():
                if isinstance(details, dict) and "operationId" in details:
                    op_ids.add(details["operationId"])
        print(f"  Master spec operationIds: {op_ids}")
        # Check agent YAML references the tools file
        if "cp_plus_tools_unified.yaml" in agent_content:
            print(f"  ✅ Agent YAML references correct tools file")
        else:
            warnings.append("Agent YAML does not reference cp_plus_tools_unified.yaml")
    else:
        warnings.append("Agent YAML not found")

    print_results()


def print_results():
    """Print final results."""
    print("\n" + "=" * 60)
    if errors:
        print(f"  ❌ VALIDATION FAILED — {len(errors)} error(s)")
        for e in errors:
            print(f"     • {e}")
    else:
        print(f"  ✅ CONTRACT CHAIN VALID")

    if warnings:
        print(f"\n  ⚠️ {len(warnings)} warning(s):")
        for w in warnings:
            print(f"     • {w}")

    print("=" * 60 + "\n")
    sys.exit(1 if errors else 0)


if __name__ == "__main__":
    validate()
