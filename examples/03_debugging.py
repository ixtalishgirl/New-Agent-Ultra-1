#!/usr/bin/env python3
"""Example 03: Debugging — error analysis and auto-fix.

Demonstrates ErrorAnalyzer and AutoFixer on real failures.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from agent.debugging import ErrorAnalyzer, AutoFixer
from agent.execution.python_runner import run_python


def main():
    analyzer = ErrorAnalyzer()
    fixer = AutoFixer()

    # 1. Analyze a NameError
    print("=" * 50)
    print("Example 1: NameError\n")

    r = run_python("print(undefined_variable)")
    print(f"Exit: OK={r.ok}")
    print(f"Stderr: {r.stderr[:200]}")

    analysis = analyzer.analyze(r.stderr)
    print(f"\nException type: {analysis.exception_type}")
    print(f"Category: {analysis.category}")
    print(f"Root cause: {analysis.root_cause}")

    fixes = fixer.fix(analysis, {"code": "print(undefined_variable)"})
    print(f"\nSuggested fixes ({len(fixes)}):")
    for f in fixes:
        print(f"  [{f.confidence:.0%}] {f.description}")
        if f.new:
            print(f"      → {f.new[:100]}")

    # 2. Analyze a SyntaxError
    print("\n" + "=" * 50)
    print("Example 2: SyntaxError\n")

    r2 = run_python("def foo(:\n    pass")
    analysis2 = analyzer.analyze(r2.stderr)
    print(f"Exception type: {analysis2.exception_type}")
    if analysis2.frames:
        print(f"Line: {analysis2.frames[0].line_number}")

    # 3. Analyze an ImportError
    print("\n" + "=" * 50)
    print("Example 3: ImportError\n")

    r3 = run_python("import nonexistent_module_xyz")
    analysis3 = analyzer.analyze(r3.stderr)
    print(f"Exception type: {analysis3.exception_type}")
    print(f"Message: {analysis3.message[:100]}")

    fixes3 = fixer.fix(analysis3, {})
    print(f"\nSuggested fixes:")
    for f in fixes3:
        print(f"  [{f.confidence:.0%}] {f.description}")


if __name__ == "__main__":
    main()
