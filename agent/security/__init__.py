"""Security module — 5 hard denials enforced at execution layer.

Per ahsan's order: NO other rules. No sandbox. No approval gates.
"""
from .policy import (
    GRANTED_PERMISSIONS, ALL_GRANTED, SCOPED_GRANTED_ITEMS,
    WITHHELD_ITEMS, DENIAL_DESCRIPTIONS,
)
from .enforcer import (
    PolicyEnforcer, EnforcementResult, Denial,
    get_enforcer, is_allowed,
)
from .audit_log import AuditLog
from .secrets import SecretsManager, SecretRef, redact, redact_dict
from .network_policy import NetworkPolicy, NetworkVerdict, NetworkRule
from .approval import ApprovalLog, ApprovalRecord, ApprovalDecision

__all__ = [
    "GRANTED_PERMISSIONS", "ALL_GRANTED", "SCOPED_GRANTED_ITEMS",
    "WITHHELD_ITEMS", "DENIAL_DESCRIPTIONS",
    "PolicyEnforcer", "EnforcementResult", "Denial",
    "get_enforcer", "is_allowed",
    "AuditLog",
    "SecretsManager", "SecretRef", "redact", "redact_dict",
    "NetworkPolicy", "NetworkVerdict", "NetworkRule",
    "ApprovalLog", "ApprovalRecord", "ApprovalDecision",
]
