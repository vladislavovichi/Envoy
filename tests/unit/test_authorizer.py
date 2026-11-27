from dataclasses import replace
from datetime import timedelta

import pytest

from envoy.domain.events import Closed, Decided, Disclosed, Outcome
from envoy.domain.offer import Offer
from envoy.domain.session import SessionView, apply, fold
from envoy.mandate.compiler import CompiledMandate, compile_mandate
from envoy.security.authorizer import (
    Action,
    Allow,
    AuthContext,
    Authorizer,
    Deny,
    Escalate,
    Verdict,
)
from envoy.security.rules import (
    ConstraintsRule,
    EscalationRule,
    LimitsRule,
    ReplyWindowRule,
    SessionStateRule,
)
from tests.support.mandates import SLOTS, meeting_mandate
from tests.support.sessions import T0, opened

GOOD = Offer(values={"time": SLOTS[0], "place": "office"})
BELOW = Offer(values={"time": SLOTS[7], "place": "park"})
OUTSIDE = Offer(values={"time": SLOTS[0], "place": "station"})


def chain() -> Authorizer:
    return Authorizer(
        [SessionStateRule(), ConstraintsRule(), LimitsRule(), EscalationRule(), ReplyWindowRule()]
    )


def negotiating() -> SessionView:
    return fold([opened(), Disclosed()])


def context(
    view: SessionView | None = None,
    mandate: CompiledMandate | None = None,
    **overrides: object,
) -> AuthContext:
    values: dict = {
        "view": view or negotiating(),
        "mandate": mandate or compile_mandate(meeting_mandate()),
        "counterpart_id": 10,
        "now": T0,
        "agreed_today": 0,
        "reply_window_open": True,
        "approved": frozenset(),
    }
    values.update(overrides)
    return AuthContext(**values)


class Fixed:
    def __init__(self, verdict: Verdict | None) -> None:
        self.verdict = verdict

    def evaluate(self, action: Action, ctx: AuthContext) -> Verdict | None:
        return self.verdict


def test_default_is_deny_without_allow() -> None:
    assert Authorizer([]).authorize(Action("send_offer", GOOD), context()) == Deny(
        "no_rule_allowed"
    )
    assert Authorizer([Fixed(None)]).authorize(Action("send_offer", GOOD), context()) == Deny(
        "no_rule_allowed"
    )


def test_deny_beats_escalate_beats_allow() -> None:
    action, ctx = Action("send_offer", GOOD), context()
    allow, escalate, deny = Fixed(Allow()), Fixed(Escalate("confirm_action")), Fixed(Deny("x"))
    assert Authorizer([allow, escalate, deny]).authorize(action, ctx) == Deny("x")
    assert Authorizer([allow, escalate]).authorize(action, ctx) == Escalate("confirm_action")
    assert Authorizer([escalate, Fixed(Escalate("reply_window_closed"))]).authorize(
        action, ctx
    ) == Escalate("confirm_action")
    assert Authorizer([allow, Fixed(None)]).authorize(action, ctx) == Allow()


def test_good_offer_allowed() -> None:
    assert chain().authorize(Action("send_offer", GOOD), context()) == Allow()


def test_offer_outside_constraints_denied() -> None:
    assert chain().authorize(Action("send_offer", OUTSIDE), context()) == Deny(
        "outside_constraints"
    )
    unknown = Offer(values={"time": SLOTS[0], "place": "moon"})
    assert chain().authorize(Action("accept_offer", unknown), context()) == Deny(
        "outside_constraints"
    )
    assert chain().authorize(Action("send_offer", None), context()) == Deny("missing_offer")


def test_offer_below_reservation_denied() -> None:
    assert chain().authorize(Action("accept_offer", BELOW), context()) == Deny("below_reservation")


def test_offer_before_disclosure_denied() -> None:
    view = fold([opened()])
    assert chain().authorize(Action("send_offer", GOOD), context(view)) == Deny("not_disclosed")


@pytest.mark.parametrize("kind", ["send_offer", "reject_session", "ask_clarification"])
def test_action_in_terminal_state_denied(kind: str) -> None:
    view = apply(negotiating(), Closed(outcome=Outcome.rejected, reason="x"))
    assert chain().authorize(Action(kind, GOOD), context(view)) == Deny("session_not_active")
    assert chain().authorize(Action(kind, GOOD), context(SessionView())) == Deny(
        "session_not_active"
    )


def test_action_not_allowed_in_state_denied() -> None:
    agreed = apply(
        negotiating(), Decided(cause=1, kind="accept_offer", offer=GOOD, verdict="allow")
    )
    assert chain().authorize(Action("send_offer", GOOD), context(agreed)) == Deny(
        "action_not_allowed_in_state"
    )
    assert chain().authorize(Action("open_checklist"), context(agreed)) == Allow()
    assert chain().authorize(Action("open_checklist"), context()) == Deny(
        "action_not_allowed_in_state"
    )


def test_open_checklist_requires_feasible_agreement() -> None:
    agreed = replace(
        apply(negotiating(), Decided(cause=1, kind="accept_offer", offer=GOOD, verdict="allow")),
        agreed=OUTSIDE,
    )
    assert chain().authorize(Action("open_checklist"), context(agreed)) == Deny(
        "outside_constraints"
    )


def test_round_limit_and_deadline_denied() -> None:
    view = replace(negotiating(), round=6)
    assert chain().authorize(Action("send_offer", GOOD), context(view)) == Deny("round_limit")
    assert chain().authorize(Action("accept_offer", GOOD), context(view)) == Allow()
    late = context(now=T0 + timedelta(days=1, seconds=1))
    assert chain().authorize(Action("send_offer", GOOD), late) == Deny("session_deadline")
    assert chain().authorize(Action("reject_session"), late) == Deny("session_deadline")


def test_daily_deal_limit_denied() -> None:
    ctx = context(agreed_today=1)
    assert chain().authorize(Action("accept_offer", GOOD), ctx) == Deny("daily_deal_limit")
    assert chain().authorize(Action("send_offer", GOOD), ctx) == Allow()


def test_deny_list_beats_allow_list() -> None:
    both = compile_mandate(
        meeting_mandate(
            limits={"max_rounds": 6, "counterparts_allow": [10], "counterparts_deny": [10]}
        )
    )
    assert chain().authorize(Action("send_offer", GOOD), context(mandate=both)) == Deny(
        "counterpart_denied"
    )
    allow_only = compile_mandate(
        meeting_mandate(limits={"max_rounds": 6, "counterparts_allow": [11]})
    )
    assert chain().authorize(Action("send_offer", GOOD), context(mandate=allow_only)) == Deny(
        "counterpart_not_allowed"
    )


def test_accept_escalates_when_mandate_requires_confirmation() -> None:
    confirming = compile_mandate(meeting_mandate(escalation={"always": ["accept_deal"]}))
    ctx = context(mandate=confirming)
    assert chain().authorize(Action("accept_offer", GOOD), ctx) == Escalate("confirm_action")
    assert chain().authorize(Action("send_offer", GOOD), ctx) == Allow()


def test_approved_offer_skips_escalation() -> None:
    confirming = compile_mandate(meeting_mandate(escalation={"always": ["accept_deal"]}))
    ctx = context(mandate=confirming, approved=frozenset({GOOD.hash()}))
    assert chain().authorize(Action("accept_offer", GOOD), ctx) == Allow()
    other = Offer(values={"time": SLOTS[1], "place": "office"})
    assert chain().authorize(Action("accept_offer", other), ctx) == Escalate("confirm_action")


def test_closed_reply_window_escalates() -> None:
    ctx = context(reply_window_open=False)
    assert chain().authorize(Action("send_offer", GOOD), ctx) == Escalate("reply_window_closed")
    assert chain().authorize(Action("send_offer", OUTSIDE), ctx) == Deny("outside_constraints")


def test_extra_rule_added_without_core_change() -> None:
    class NoCafe:
        def evaluate(self, action: Action, ctx: AuthContext) -> Verdict | None:
            if action.offer is not None and action.offer.values.get("place") == "cafe":
                return Deny("no_cafe")
            return None

    authorizer = Authorizer([SessionStateRule(), ConstraintsRule(), NoCafe()])
    cafe = Offer(values={"time": SLOTS[0], "place": "cafe"})
    assert authorizer.authorize(Action("send_offer", cafe), context()) == Deny("no_cafe")
    assert authorizer.authorize(Action("send_offer", GOOD), context()) == Allow()
