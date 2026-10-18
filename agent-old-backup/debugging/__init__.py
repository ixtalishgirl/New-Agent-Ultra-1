"""Debugging module — DEBUG phase of the control loop."""
from .error_analyzer import (
    ErrorAnalyzer, ErrorClassifier, ErrorAnalysis, ErrorCategory,
    StackFrame,
)
from .auto_fixer import AutoFixer, FixApplier, Fix, FixAction
from .regression_detector import (
    RegressionDetector, RegressionResult, RegressionSeverity,
    RegressionSuite, TestCase, Bisector,
)

__all__ = [
    "ErrorAnalyzer", "ErrorClassifier", "ErrorAnalysis", "ErrorCategory",
    "StackFrame", "AutoFixer", "FixApplier", "Fix", "FixAction",
    "RegressionDetector", "RegressionResult", "RegressionSeverity",
    "RegressionSuite", "TestCase", "Bisector",
]
