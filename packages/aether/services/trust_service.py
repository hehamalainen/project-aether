"""
Agent Trust Service for Aether.

Uses a Bayesian Beta distribution (alpha/beta) to track agent reliability
across different scopes (languages, repositories).

Combined Score = logit(confidence) + λ * logit(trust)
"""

import math
from datetime import UTC, datetime
from typing import Optional, Tuple

import structlog
from sqlmodel import Session, select

from ..models.entities import AgentMetric

logger = structlog.get_logger()


def logit(x: float) -> float:
    """Compute the logit of x: ln(x / (1 - x))."""
    # Clamp to avoid division by zero or log of zero
    x = max(min(x, 0.999), 0.001)
    return math.log(x / (1.0 - x))


def sigmoid(x: float) -> float:
    """Compute the sigmoid of x (inverse logit)."""
    return 1.0 / (1.0 + math.exp(-x))


class TrustService:
    """Service for managing agent trust metrics and combined scoring."""

    def __init__(self, session: Session):
        self.session = session
        # Default prior for Beta distribution (alpha=3, beta=3 -> 0.5 trust with some weight)
        self.alpha0 = 3.0
        self.beta0 = 3.0
        # Trust weight for combined scoring
        self.lambda_trust = 0.5

    def get_metrics(self, agent_id: str, scope: str = "global") -> AgentMetric:
        """Get or initialize metrics for an agent in a given scope."""
        statement = select(AgentMetric).where(
            AgentMetric.agent_id == agent_id,
            AgentMetric.scope == scope
        )
        metric = self.session.exec(statement).first()

        if not metric:
            metric = AgentMetric(
                agent_id=agent_id,
                scope=scope,
                trust_alpha=self.alpha0,
                trust_beta=self.beta0
            )
            self.session.add(metric)
            self.session.commit()
            self.session.refresh(metric)

        return metric

    def calculate_trust_score(self, metric: AgentMetric) -> float:
        """Calculate the trust score from alpha and beta."""
        return metric.trust_alpha / (metric.trust_alpha + metric.trust_beta)

    def compute_combined_score(self, confidence: float, trust: float) -> float:
        """Combine agent confidence and system trust using logit-space addition."""
        l_conf = logit(confidence)
        l_trust = logit(trust)
        
        # Combine: score = L_conf + lambda * L_trust
        l_combined = l_conf + self.lambda_trust * l_trust
        
        return sigmoid(l_combined)

    def record_submission(self, agent_id: str, scope: str = "global", commit: bool = True):
        """Record a new proposal submission."""
        metric = self.get_metrics(agent_id, scope)
        metric.total_proposals += 1
        metric.last_seen = datetime.now(UTC)
        metric.updated_at = datetime.now(UTC)
        self.session.add(metric)
        if commit:
            self.session.commit()
        else:
            self.session.flush()

    def record_promotion(
        self,
        agent_id: str,
        scope: str = "global",
        weight: float = 1.0,
        commit: bool = True,
    ):
        """Record a successful promotion (increases alpha)."""
        metric = self.get_metrics(agent_id, scope)
        metric.promoted_count += 1
        metric.trust_alpha += weight
        metric.updated_at = datetime.now(UTC)
        self.session.add(metric)
        if commit:
            self.session.commit()
        else:
            self.session.flush()

    def record_hard_fail(
        self,
        agent_id: str,
        scope: str = "global",
        weight: float = 3.0,
        commit: bool = True,
    ):
        """Record a hard gate failure (increases beta)."""
        metric = self.get_metrics(agent_id, scope)
        metric.hard_fail_count += 1
        metric.trust_beta += weight
        metric.updated_at = datetime.now(UTC)
        self.session.add(metric)
        if commit:
            self.session.commit()
        else:
            self.session.flush()

    def record_rejection(
        self,
        agent_id: str,
        scope: str = "global",
        weight: float = 3.0,
        commit: bool = True,
    ):
        """Record a rejection (rejection due to gate failure or invalid diff)."""
        self.record_hard_fail(agent_id, scope, weight, commit)


    def record_neutral_loss(self, agent_id: str, scope: str = "global", commit: bool = True):
        """Record a valid proposal that wasn't promoted due to competition (no trust penalty)."""
        metric = self.get_metrics(agent_id, scope)
        metric.neutral_loss_count += 1
        metric.updated_at = datetime.now(UTC)
        self.session.add(metric)
        if commit:
            self.session.commit()
        else:
            self.session.flush()

    def get_combined_agent_score(self, agent_id: str, raw_confidence: float, scope: str = "global") -> float:
        """Get the trust-adjusted confidence score for an agent."""
        metric = self.get_metrics(agent_id, scope)
        trust = self.calculate_trust_score(metric)
        combined = self.compute_combined_score(raw_confidence, trust)
        
        logger.debug(
            "agent_trust_calculated",
            agent_id=agent_id,
            scope=scope,
            raw_conf=raw_confidence,
            trust=trust,
            combined=combined
        )
        
        return combined
