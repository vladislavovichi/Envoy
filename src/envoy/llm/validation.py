from envoy.domain.intent import Intent, Propose, Unclear
from envoy.domain.offer import Offer
from envoy.mandate.grid import CandidateGrid


def validate_intent(intent: Intent, grid: CandidateGrid) -> Intent:
    if not isinstance(intent, Propose):
        return intent
    values = intent.offer.values
    missing = tuple(
        issue_id
        for axis, issue_id in enumerate(grid.ids)
        if issue_id not in values or grid.position(axis, values[issue_id]) is None
    )
    if missing:
        return Unclear(missing=missing)
    return Propose(offer=Offer(values={issue_id: values[issue_id] for issue_id in grid.ids}))
