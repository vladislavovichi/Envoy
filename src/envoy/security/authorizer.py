from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from envoy.domain.events import ActionKind, EscalationReason
from envoy.domain.ids import OfferHash
from envoy.domain.offer import Offer
from envoy.domain.session import SessionView
from envoy.mandate.compiler import CompiledMandate


@dataclass(frozen=True)
class Action:
    kind: ActionKind
    offer: Offer | None = None
    reason: str | None = None
    missing: tuple[str, ...] = ()


@dataclass(frozen=True)
class Allow:
    pass


@dataclass(frozen=True)
class Escalate:
    reason: EscalationReason


@dataclass(frozen=True)
class Deny:
    reason: str


type Verdict = Allow | Escalate | Deny


@dataclass(frozen=True)
class AuthContext:
    view: SessionView
    mandate: CompiledMandate
    counterpart_id: int
    now: datetime
    agreed_today: int
    reply_window_open: bool
    approved: frozenset[OfferHash] = frozenset()


class PolicyRule(Protocol):
    def evaluate(self, action: Action, ctx: AuthContext) -> Verdict | None: ...


class Authorizer:
    def __init__(self, rules: Sequence[PolicyRule]) -> None:
        self._rules = tuple(rules)

    def authorize(self, action: Action, ctx: AuthContext) -> Verdict:
        escalation: Escalate | None = None
        allowed = False
        for rule in self._rules:
            verdict = rule.evaluate(action, ctx)
            match verdict:
                case Deny():
                    return verdict
                case Escalate() if escalation is None:
                    escalation = verdict
                case Allow():
                    allowed = True
        if escalation is not None:
            return escalation
        return Allow() if allowed else Deny("no_rule_allowed")
