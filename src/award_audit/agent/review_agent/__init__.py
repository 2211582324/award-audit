"""Case-level review-agent contracts.

This package is intentionally independent from the legacy M4/M5 runners.  It
defines the bounded case packet and validated semantic decisions that the new
M5 path will use.
"""

from award_audit.agent.review_agent.models import (
    AssetAssessment,
    ParsedAsset,
    ReviewCasePacket,
    ReviewOutcome,
    ScopeCandidate,
    SourceCandidate,
    SubmissionSummary,
    validate_review_outcome,
)
from award_audit.agent.review_agent.packet import build_review_case_packet
from award_audit.agent.review_agent.runner import SemanticReviewRunner
from award_audit.agent.review_agent.service import ReviewAgent, ReviewAgentRun

__all__ = [
    "AssetAssessment",
    "ParsedAsset",
    "ReviewCasePacket",
    "ReviewAgent",
    "ReviewAgentRun",
    "ReviewOutcome",
    "SemanticReviewRunner",
    "ScopeCandidate",
    "SourceCandidate",
    "SubmissionSummary",
    "build_review_case_packet",
    "validate_review_outcome",
]
