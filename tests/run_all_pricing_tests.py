#!/usr/bin/env python3
"""
Run All Pricing and Credit Tests

Comprehensive test suite for the billing/pricing system.
"""

import subprocess
import sys
import os

def run_test_file(filepath: str) -> tuple[int, int]:
    """Run a test file and return (passed, failed) counts."""
    result = subprocess.run(
        [sys.executable, filepath],
        capture_output=True,
        text=True,
        cwd=os.path.dirname(filepath)
    )
    
    # Parse output for results
    output = result.stdout
    print(output)
    
    # Extract counts from "RESULTS: X passed, Y failed"
    for line in output.split('\n'):
        if 'RESULTS:' in line:
            parts = line.split()
            passed = int(parts[1])
            failed = int(parts[3])
            return passed, failed
    
    return 0, 1  # If we can't parse, assume failure


def main():
    print("=" * 70)
    print("COMPREHENSIVE PRICING & CREDIT SYSTEM TEST SUITE")
    print("=" * 70)
    
    test_dir = os.path.dirname(os.path.abspath(__file__))
    
    test_files = [
        os.path.join(test_dir, "test_pricing_probes.py"),
        os.path.join(test_dir, "test_credit_deduction_probes.py"),
    ]
    
    total_passed = 0
    total_failed = 0
    
    for test_file in test_files:
        if os.path.exists(test_file):
            print(f"\n{'='*70}")
            print(f"Running: {os.path.basename(test_file)}")
            print("=" * 70)
            passed, failed = run_test_file(test_file)
            total_passed += passed
            total_failed += failed
        else:
            print(f"⚠ Test file not found: {test_file}")
    
    print("\n" + "=" * 70)
    print("FINAL SUMMARY")
    print("=" * 70)
    print(f"Total Tests: {total_passed + total_failed}")
    print(f"Passed: {total_passed} ✓")
    print(f"Failed: {total_failed} ✗")
    print("=" * 70)
    
    if total_failed == 0:
        print("\n✅ ALL TESTS PASSED!")
        print("\nPricing System Verified:")
        print("  • Developer: $0/month, 10,000 credits")
        print("  • Plus: $49/month, 75,000 credits")
        print("  • Enterprise: Custom pricing")
        print("  • Credit rate: 1 credit = $0.001")
        print("  • Token costs: 10/1K input, 30/1K output")
        print("  • Provider multipliers: OpenAI 1.0x, Anthropic 1.2x, Groq 0.5x")
    else:
        print(f"\n❌ {total_failed} TESTS FAILED - Review output above")
    
    return 0 if total_failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
