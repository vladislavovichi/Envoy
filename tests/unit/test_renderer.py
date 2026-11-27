from uuid import UUID

from envoy.app.renderer import TemplateRenderer
from envoy.domain.ids import MandateId
from envoy.domain.offer import Offer
from envoy.security.projection import OfferProjection, OwnerCard, OwnerCardProjection
from tests.support.mandates import RESERVATION, SLOTS, WEIGHT_TIME, meeting_mandate

RENDERER = TemplateRenderer("Europe/Moscow")
OFFER = Offer(values={"time": SLOTS[0], "place": "cafe"})


def test_offer_text_contains_only_offer_values() -> None:
    text = RENDERER.offer(OFFER)
    assert "01.12.2026 12:00" in text
    assert "cafe" in text
    for private in (str(RESERVATION), str(WEIGHT_TIME), "office", "station"):
        assert private not in text
    assert RENDERER.accept(OFFER).startswith("Договорились")
    assert "cafe" in RENDERER.deal_summary(OFFER)


def test_disclosure_names_agent_and_owner() -> None:
    text = RENDERER.disclosure("Анна", "Envoy")
    assert "Envoy" in text
    assert "Анна" in text
    assert "ИИ-агент" in text


def test_clarification_and_rejection_have_no_reasons() -> None:
    assert RENDERER.clarification(["time", "place"]) == "Уточните, пожалуйста: время, место."
    assert "round_limit" not in RENDERER.rejection("round_limit")


def test_owner_notice_with_and_without_link() -> None:
    assert "24 часов" in RENDERER.owner_notice("reply_window_closed")
    linked = RENDERER.owner_notice("translator_failed", "https://t.me/envoy_bot/5")
    assert linked.endswith("https://t.me/envoy_bot/5")
    assert RENDERER.owner_notice("custom") == "Envoy: custom."


def test_escalation_card_has_buttons_and_projected_terms() -> None:
    projection = OwnerCardProjection().project(OwnerCard(OFFER, "confirm_action"))
    card = RENDERER.escalation(projection, 42)
    assert "cafe" in card.text
    assert "01.12.2026 12:00" in card.text
    assert [(button.data, button.style) for button in card.buttons[0]] == [
        ("approve:42", "success"),
        ("decline:42", "danger"),
    ]
    empty = RENDERER.escalation({"offer": None, "reason": "x"}, 1)
    assert "—" in empty.text
    assert dict(OfferProjection().project(OFFER)) == {"place": "cafe", "time": SLOTS[0].isoformat()}


def test_mandate_draft_card_lists_values() -> None:
    card = RENDERER.mandate_draft(meeting_mandate(), MandateId(UUID(int=5)))
    assert "station" in card.text
    assert card.markup() is not None
    assert card.buttons[0][0].data.startswith("mandate_confirm:")
