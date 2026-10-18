"""Verification modules for the agent control loop.

These modules implement the VERIFY stage of the control loop:
    PLAN -> RESEARCH -> SELECT TOOL -> CHECK PERMISSIONS -> EXECUTE
    -> INSPECT RESULT -> DEBUG -> TEST -> IMPROVE -> VERIFY -> SAVE MEMORY

Every tool result must pass through verification before the agent may act
on it. The agent must use tools; it cannot fabricate output. Verification
is how fabricated, hallucinated, or erroneous results are caught.

Modules:
    result_verifier:      schema validation, sanity checks, confidence scoring
    consistency_checker:  cross-run / cross-tool / temporal contradiction detection
    proof_collector:      evidence collection with hash-chained chain of custody
"""

from .result_verifier import (
    ResultVerifier,
    VerificationReport,
    CheckResult,
    VerificationStrategy,
    ShellOutputVerifier,
    PythonOutputVerifier,
    WebContentVerifier,
    FileOperationVerifier,
    SchemaValidator,
)
from .consistency_checker import (
    ConsistencyChecker,
    Contradiction,
    ConflictResolutionStrategy,
    TemporalFactStore,
)
from .proof_collector import (
    ProofCollector,
    Evidence,
    ProofBundle,
    EvidenceType,
)

__all__ = [
    # result_verifier
    "ResultVerifier",
    "VerificationReport",
    "CheckResult",
    "VerificationStrategy",
    "ShellOutputVerifier",
    "PythonOutputVerifier",
    "WebContentVerifier",
    "FileOperationVerifier",
    "SchemaValidator",
    # consistency_checker
    "ConsistencyChecker",
    "Contradiction",
    "ConflictResolutionStrategy",
    "TemporalFactStore",
    # proof_collector
    "ProofCollector",
    "Evidence",
    "ProofBundle",
    "EvidenceType",
]
