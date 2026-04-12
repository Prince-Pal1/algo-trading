#!/usr/bin/env python3
"""Backtest Engine Verification — Full Report

Runs all 3 layers and prints a human-readable summary.
Usage: python3 scripts/run_verification.py
"""

import subprocess
import sys


def run_tests(test_file: str, label: str) -> tuple[bool, str]:
    """Run a test file and return (passed, output)."""
    result = subprocess.run(
        [sys.executable, "-m", "pytest", test_file, "-v", "--tb=short", "-s"],
        capture_output=True, text=True, timeout=300,
    )
    passed = result.returncode == 0
    return passed, result.stdout + result.stderr


def main():
    print("=" * 70)
    print("  BACKTEST ENGINE VERIFICATION REPORT")
    print("=" * 70)

    # Layer 1
    print("\n--- Layer 1: Deterministic Unit Tests ---")
    l1_pass, l1_out = run_tests("tests/test_backtest/test_engine_unit.py", "Layer 1")
    # Count passed/failed
    for line in l1_out.split("\n"):
        if "passed" in line or "failed" in line:
            print(f"  {line.strip()}")
            break
    print(f"  Status: {'PASSED' if l1_pass else 'FAILED'}")

    # Layer 2 + 3
    print("\n--- Layer 2: RSI(2) Cross-Validation ---")
    print("--- Layer 3: Manual Trade Audit ---")
    l2_pass, l2_out = run_tests("tests/test_backtest/test_engine_rsi2.py", "Layer 2+3")
    for line in l2_out.split("\n"):
        if "passed" in line or "failed" in line:
            print(f"  {line.strip()}")
            break
        if "RSI(2) stats" in line:
            print(f"  {line.strip()}")
    print(f"  Status: {'PASSED' if l2_pass else 'FAILED'}")

    # Summary
    print("\n" + "=" * 70)
    all_pass = l1_pass and l2_pass
    if all_pass:
        print("  VERDICT: ALL LAYERS PASSED — ENGINE VERIFIED")
        print()
        print("  Bugs found and fixed during verification:")
        print("  1. Double-commission bug (equity subtracted commission twice)")
        print("  2. Last-bar signal crash (continue skipped equity_history.append)")
        print()
        print("  The backtest engine produces mathematically correct results.")
        print("  Strategy results can now be trusted.")
    else:
        print("  VERDICT: VERIFICATION FAILED")
        if not l1_pass:
            print("  Layer 1 failed — engine mechanics have bugs")
        if not l2_pass:
            print("  Layer 2/3 failed — engine diverges from reference")
    print("=" * 70)

    return 0 if all_pass else 1


if __name__ == "__main__":
    sys.exit(main())
