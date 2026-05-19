"""
CP Plus Agentic Suite — Evaluation Runner (P5.3)

Runs all test cases from evaluation_dataset.json against the live backend,
validates tool routing and response content, and produces a pass/fail scorecard.

Usage:
    python run_eval.py                         # default: http://localhost:8100
    python run_eval.py --base-url https://xxx.ngrok-free.app
    python run_eval.py --verbose                # show full responses
"""

import json
import sys
import os
import time
import argparse
import requests

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
EVAL_DATASET = os.path.join(
    os.path.dirname(__file__), "..", "cp_plus_poc", "evaluation_dataset.json"
)

# Map expected_tool → backend route + tool field
TOOL_ROUTE_MAP = {
    "analyze_rfp":      {"route": "/rfp/run",     "tool": "rfp"},
    "lookup_product":   {"route": "/product/run", "tool": "product"},
    "match_compliance": {"route": "/matcher/run", "tool": "matcher"},
}


def load_dataset(path: str) -> list[dict]:
    """Load evaluation dataset from JSON file."""
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def run_test_case(base_url: str, case: dict, verbose: bool = False) -> dict:
    """Execute a single test case and return result dict."""
    case_id = case["id"]
    expected_tool = case.get("expected_tool", "")
    expected_params = case.get("expected_params", {})
    expected_contains = case.get("expected_answer_contains", [])

    # Build request body
    mapping = TOOL_ROUTE_MAP.get(expected_tool)
    if not mapping:
        return {"id": case_id, "status": "SKIP", "reason": f"Unknown tool: {expected_tool}"}

    url = f"{base_url}{mapping['route']}"
    body = dict(expected_params)

    result = {
        "id": case_id,
        "category": case.get("category", ""),
        "route": mapping["route"],
        "status": "FAIL",
        "checks": {},
        "elapsed_ms": 0,
        "error": None,
    }

    # Execute request
    try:
        start = time.time()
        resp = requests.post(url, json=body, timeout=120, headers={
            "ngrok-skip-browser-warning": "true"
        })
        result["elapsed_ms"] = round((time.time() - start) * 1000)

        if resp.status_code != 200:
            result["error"] = f"HTTP {resp.status_code}: {resp.text[:200]}"
            return result

        data = resp.json()
        answer = data.get("answer", "")

        # Check 1: Response is non-empty
        result["checks"]["non_empty"] = bool(answer and len(str(answer)) > 10)

        # Check 2: Expected keywords present (case-insensitive)
        answer_lower = str(answer).lower()
        keyword_hits = {}
        for kw in expected_contains:
            keyword_hits[kw] = kw.lower() in answer_lower
        result["checks"]["keywords"] = keyword_hits
        all_keywords_found = all(keyword_hits.values()) if keyword_hits else True

        # Check 3: No error in response
        result["checks"]["no_error"] = "error" not in data or data.get("answer")

        # Overall pass
        result["status"] = "PASS" if (
            result["checks"]["non_empty"] and
            all_keywords_found and
            result["checks"]["no_error"]
        ) else "FAIL"

        if verbose:
            result["response_preview"] = str(answer)[:300]

    except requests.exceptions.Timeout:
        result["error"] = "Request timed out (120s)"
    except requests.exceptions.ConnectionError:
        result["error"] = f"Cannot connect to {url}"
    except Exception as e:
        result["error"] = str(e)

    return result


def print_scorecard(results: list[dict]):
    """Print a formatted scorecard summary."""
    total = len(results)
    passed = sum(1 for r in results if r["status"] == "PASS")
    failed = sum(1 for r in results if r["status"] == "FAIL")
    skipped = sum(1 for r in results if r["status"] == "SKIP")

    print("\n" + "=" * 70)
    print("CP PLUS AGENTIC SUITE — EVALUATION SCORECARD")
    print("=" * 70)

    # Category breakdown
    categories = {}
    for r in results:
        cat = r.get("category", "other")
        if cat not in categories:
            categories[cat] = {"pass": 0, "fail": 0, "skip": 0}
        categories[cat][r["status"].lower()] += 1

    print(f"\n{'Category':<25} {'Pass':>6} {'Fail':>6} {'Skip':>6}")
    print("-" * 50)
    for cat, counts in sorted(categories.items()):
        print(f"{cat:<25} {counts['pass']:>6} {counts['fail']:>6} {counts['skip']:>6}")
    print("-" * 50)
    print(f"{'TOTAL':<25} {passed:>6} {failed:>6} {skipped:>6}")

    # Detailed results
    print(f"\n{'ID':<35} {'Status':>6} {'Time':>8}  Notes")
    print("-" * 80)
    for r in results:
        notes = ""
        if r.get("error"):
            notes = f"ERR: {r['error'][:40]}"
        elif r["status"] == "FAIL":
            failed_kws = [k for k, v in r.get("checks", {}).get("keywords", {}).items() if not v]
            if failed_kws:
                notes = f"Missing: {', '.join(failed_kws)}"
            elif not r.get("checks", {}).get("non_empty"):
                notes = "Empty response"
        time_str = f"{r['elapsed_ms']}ms" if r["elapsed_ms"] else "—"
        status_icon = "✅" if r["status"] == "PASS" else ("⏭️" if r["status"] == "SKIP" else "❌")
        print(f"{r['id']:<35} {status_icon} {r['status']:>4} {time_str:>8}  {notes}")

    # Final verdict
    pass_rate = (passed / total * 100) if total > 0 else 0
    print(f"\n{'=' * 70}")
    verdict = "✅ ALL TESTS PASSED" if failed == 0 else f"❌ {failed} TEST(S) FAILED"
    print(f"  {verdict}  —  {pass_rate:.0f}% pass rate ({passed}/{total})")
    print(f"{'=' * 70}\n")

    return failed == 0


def main():
    parser = argparse.ArgumentParser(description="CP Plus Evaluation Runner")
    parser.add_argument("--base-url", default="http://localhost:8100",
                        help="Backend base URL (default: http://localhost:8100)")
    parser.add_argument("--dataset", default=EVAL_DATASET,
                        help="Path to evaluation dataset JSON")
    parser.add_argument("--verbose", action="store_true",
                        help="Show response previews")
    parser.add_argument("--category", default=None,
                        help="Run only tests in this category")
    args = parser.parse_args()

    # Load dataset
    dataset = load_dataset(args.dataset)
    if args.category:
        dataset = [c for c in dataset if c.get("category") == args.category]

    print(f"\nLoaded {len(dataset)} test cases from {os.path.basename(args.dataset)}")
    print(f"Target: {args.base_url}")

    # Health check
    try:
        health = requests.get(f"{args.base_url}/health", timeout=5,
                              headers={"ngrok-skip-browser-warning": "true"})
        if health.status_code == 200:
            print(f"Health check: ✅ OK")
        else:
            print(f"Health check: ⚠️ HTTP {health.status_code}")
    except Exception:
        print(f"Health check: ❌ Cannot connect to {args.base_url}")
        print("Make sure the backend is running: python unified_backend.py")
        sys.exit(1)

    # Run tests
    results = []
    for i, case in enumerate(dataset):
        print(f"\n  [{i+1}/{len(dataset)}] {case['id']}...", end="", flush=True)
        result = run_test_case(args.base_url, case, verbose=args.verbose)
        results.append(result)
        icon = "✅" if result["status"] == "PASS" else "❌"
        print(f" {icon} ({result['elapsed_ms']}ms)")

    # Print scorecard
    all_passed = print_scorecard(results)

    # Save results to file
    out_path = os.path.join(os.path.dirname(__file__), "eval_results.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2)
    print(f"Full results saved to: {out_path}")

    sys.exit(0 if all_passed else 1)


if __name__ == "__main__":
    main()
