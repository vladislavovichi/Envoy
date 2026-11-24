from collections.abc import Mapping

from envoy.domain.events import ActionKind
from envoy.domain.session import SessionState
from envoy.security.authorizer import Action, Allow, AuthContext, Deny, Escalate, Verdict

ALLOWED_ACTIONS: Mapping[SessionState, frozenset[ActionKind]] = {
    SessionState.NEGOTIATING: frozenset(
        {"send_offer", "accept_offer", "reject_session", "ask_clarification"}
    ),
    SessionState.AGREED: frozenset({"open_checklist"}),
}
OFFER_ACTIONS: frozenset[ActionKind] = frozenset({"send_offer", "accept_offer"})


class SessionStateRule:
    def evaluate(self, action: Action, ctx: AuthContext) -> Verdict | None:
        state = ctx.view.state
        if state in (SessionState.INIT, SessionState.CLOSED):
            return Deny("session_not_active")
        if not ctx.view.disclosed:
            return Deny("not_disclosed")
        if action.kind not in ALLOWED_ACTIONS.get(state, frozenset()):
            return Deny("action_not_allowed_in_state")
        return None


class ConstraintsRule:
    def evaluate(self, action: Action, ctx: AuthContext) -> Verdict | None:
        if action.kind == "open_checklist":
            offer = ctx.view.agreed
        elif action.kind in OFFER_ACTIONS:
            offer = action.offer
        else:
            return Allow()
        if offer is None:
            return Deny("missing_offer")
        if not ctx.mandate.constraints.is_feasible(offer):
            return Deny("outside_constraints")
        if ctx.mandate.utility.value(offer) < ctx.mandate.reservation:
            return Deny("below_reservation")
        return Allow()


class LimitsRule:
    def evaluate(self, action: Action, ctx: AuthContext) -> Verdict | None:
        limits = ctx.mandate.limits
        if ctx.counterpart_id in limits.counterparts_deny:
            return Deny("counterpart_denied")
        if (
            limits.counterparts_allow is not None
            and ctx.counterpart_id not in limits.counterparts_allow
        ):
            return Deny("counterpart_not_allowed")
        opened = ctx.view.opened
        if opened is not None and action.kind != "open_checklist" and ctx.now > opened.deadline:
            return Deny("session_deadline")
        if action.kind == "send_offer" and ctx.view.round >= limits.max_rounds:
            return Deny("round_limit")
        if action.kind == "accept_offer" and ctx.agreed_today >= limits.max_deals_per_day:
            return Deny("daily_deal_limit")
        return None


class EscalationRule:
    def evaluate(self, action: Action, ctx: AuthContext) -> Verdict | None:
        if (
            action.kind == "accept_offer"
            and "accept_deal" in ctx.mandate.escalation.always
            and (action.offer is None or action.offer.hash() not in ctx.approved)
        ):
            return Escalate("confirm_action")
        return None


class ReplyWindowRule:
    def evaluate(self, action: Action, ctx: AuthContext) -> Verdict | None:
        return None if ctx.reply_window_open else Escalate("reply_window_closed")
