"""Improvement module — self-improvement pipeline.

Pipeline: OBSERVE → IDENTIFY → HYPOTHESIS → BASELINE → GIT BRANCH →
PATCH → UNIT TESTS → INTEGRATION → SECURITY → REGRESSION →
BENCHMARK → REVIEW → APPROVAL → MERGE/REJECT → ROLLBACK PATH
"""
from .improvement_planner import (
    ImprovementPlanner, Observation, Opportunity, OpportunityType,
    Hypothesis, HypothesisStatus,
)
from .experiment_manager import ExperimentManager
from .benchmark import Benchmark, BenchmarkResult, Comparison
from .regression_suite import (
    RegressionSuite, SuiteRun, SuiteCase, SuiteResult,
    build_standard_suite,
)
from .approval_manager import (
    ApprovalManager, ApprovalRequest, ApprovalStatus, ChangeRisk,
)
from .rollback import RollbackManager, Snapshot, RollbackResult, RollbackStrategy

__all__ = [
    "ImprovementPlanner", "Observation", "Opportunity",
    "OpportunityType", "Hypothesis", "HypothesisStatus",
    "ExperimentManager",
    "Benchmark", "BenchmarkResult", "Comparison",
    "RegressionSuite", "SuiteRun", "SuiteCase", "SuiteResult",
    "build_standard_suite",
    "ApprovalManager", "ApprovalRequest", "ApprovalStatus", "ChangeRisk",
    "RollbackManager", "Snapshot", "RollbackResult", "RollbackStrategy",
]
