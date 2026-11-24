import asyncio

import pytest

from envoy.domain.telegram import (
    BusinessConnection,
    BusinessConnectionUpdate,
    BusinessMessageUpdate,
    CallbackUpdate,
    MessageUpdate,
    TextEntity,
    UnsupportedUpdate,
)
from envoy.security.inbound import Dropped, InboundFilter, OwnerDirectory, Passed
from envoy.security.payment import redact_payment_details, utf16_to_index

OWNER = 1
COUNTERPART = 2
TON = "EQ" + "A" * 20 + "b-_" + "c" * 23
CARD = "4111 1111 1111 1111"


@pytest.fixture
def owners(mocker):
    directory = mocker.create_autospec(OwnerDirectory, instance=True)
    directory.is_owner.side_effect = lambda user_id: user_id == OWNER
    directory.connection_owner.side_effect = lambda connection_id: (
        OWNER if connection_id == "conn" else None
    )
    return directory


@pytest.fixture
def inbound(owners) -> InboundFilter:
    return InboundFilter(owners, max_text_length=200, rate_per_minute=3)


def business(text: str | None = "привет", **overrides: object) -> BusinessMessageUpdate:
    values: dict = {
        "update_id": 1,
        "connection_id": "conn",
        "chat_id": COUNTERPART,
        "user_id": COUNTERPART,
        "message_id": 5,
        "text": text,
    }
    values.update(overrides)
    return BusinessMessageUpdate(**values)


async def test_text_from_counterpart_passes(inbound) -> None:
    result = await inbound.check(business("давай в пятницу"))
    assert result == Passed(business("давай в пятницу"), "counterpart", False)


@pytest.mark.parametrize("kind", ["other", "service"])
async def test_voice_photo_sticker_dropped_as_not_text(inbound, kind) -> None:
    assert await inbound.check(business(None, content_kind=kind)) == Dropped("not_text")
    assert await inbound.check(business("caption", content_kind="other")) == Dropped("not_text")


async def test_service_message_dropped_as_not_text(inbound) -> None:
    assert await inbound.check(business(None, content_kind="service")) == Dropped("not_text")
    assert await inbound.check(UnsupportedUpdate(9)) == Dropped("not_text")


async def test_text_over_limit_dropped(inbound) -> None:
    assert await inbound.check(business("x" * 201)) == Dropped("too_long")
    assert isinstance(await inbound.check(business("x" * 200)), Passed)


async def test_bot_sender_dropped(inbound) -> None:
    assert await inbound.check(business(is_bot=True)) == Dropped("bot_sender")


async def test_edited_business_message_dropped(inbound) -> None:
    assert await inbound.check(business(edited=True)) == Dropped("edited")
    assert await inbound.check(business(edited=True, done_task_ids=(1,))) == Dropped("edited")


async def test_owner_message_in_business_chat_classified(inbound) -> None:
    result = await inbound.check(business("/envoy", user_id=OWNER))
    assert isinstance(result, Passed)
    assert result.sender == "owner_in_business_chat"


async def test_checklist_done_service_message_passes(inbound) -> None:
    update = business(None, content_kind="service", done_task_ids=(1, 2))
    assert await inbound.check(update) == Passed(update, "counterpart")


async def test_unknown_business_connection_dropped(inbound) -> None:
    assert await inbound.check(business(connection_id="other")) == Dropped("unknown_sender")


async def test_private_message_from_stranger_dropped(inbound) -> None:
    stranger = MessageUpdate(update_id=1, chat_id=7, user_id=7, text="hi")
    assert await inbound.check(stranger) == Dropped("unknown_sender")
    owner = MessageUpdate(update_id=2, chat_id=OWNER, user_id=OWNER, text="hi")
    assert await inbound.check(owner) == Passed(owner, "owner")


async def test_business_connection_from_owner_only(inbound) -> None:
    own = BusinessConnectionUpdate(1, BusinessConnection("conn", OWNER, OWNER))
    assert await inbound.check(own) == Passed(own, "owner")
    foreign = BusinessConnectionUpdate(2, BusinessConnection("x", COUNTERPART, COUNTERPART))
    assert await inbound.check(foreign) == Dropped("unknown_sender")


async def test_callback_from_non_owner_dropped(inbound) -> None:
    foreign = CallbackUpdate(1, "cb", COUNTERPART, COUNTERPART, 3, "approve:1")
    assert await inbound.check(foreign) == Dropped("unknown_sender")
    own = CallbackUpdate(2, "cb", OWNER, OWNER, 3, "approve:1")
    assert await inbound.check(own) == Passed(own, "owner")


async def test_rate_limit_drops_after_budget_and_recovers(inbound, mocker) -> None:
    loop = asyncio.get_running_loop()
    now = loop.time()
    mocker.patch.object(loop, "time", side_effect=lambda: now)
    for _ in range(3):
        assert isinstance(await inbound.check(business()), Passed)
    assert await inbound.check(business()) == Dropped("rate_limited")
    assert isinstance(await inbound.check(business(user_id=OWNER, text="/envoy")), Passed)
    now += 61
    assert isinstance(await inbound.check(business()), Passed)


async def test_card_number_redacted_before_passing(inbound) -> None:
    for card in (CARD, "4111-1111-1111-1111", "4111111111111111"):
        result = await inbound.check(business(f"переведи залог на {card} сегодня"))
        assert isinstance(result, Passed)
        assert result.update.text == "переведи залог на [карта] сегодня"
        assert result.payment_details_redacted
        assert "4111" not in str(result)


async def test_digits_failing_luhn_kept(inbound) -> None:
    result = await inbound.check(business("встреча 1234 5678 9012 3456 в 10"))
    assert isinstance(result, Passed)
    assert result.update.text == "встреча 1234 5678 9012 3456 в 10"
    assert not result.payment_details_redacted


async def test_ton_address_entity_redacted(inbound) -> None:
    prefix = "😀 кошелёк "
    text = prefix + "wallet-name-x тут"
    offset = len(prefix.encode("utf-16-le")) // 2
    entity = TextEntity("ton_address", offset, len("wallet-name-x"))
    result = await inbound.check(business(text, entities=(entity,)))
    assert isinstance(result, Passed)
    assert result.update.text == "😀 кошелёк [TON-адрес] тут"
    assert result.update.entities == ()


async def test_ton_address_in_plain_text_redacted(inbound) -> None:
    raw = "0:" + "ab" * 32
    result = await inbound.check(business(f"шли на {TON} или {raw}"))
    assert isinstance(result, Passed)
    assert result.update.text == "шли на [TON-адрес] или [TON-адрес]"


async def test_bank_card_entity_redacted_even_without_luhn_match(inbound) -> None:
    text = "карта 1234 5678"
    entity = TextEntity("bank_card_number", 6, 9)
    result = await inbound.check(business(text, entities=(entity,)))
    assert isinstance(result, Passed)
    assert result.update.text == "карта [карта]"


async def test_payment_details_flag_set_only_when_redacted(inbound) -> None:
    clean = await inbound.check(business("обычный текст", entities=(TextEntity("bold", 0, 3),)))
    assert isinstance(clean, Passed)
    assert not clean.payment_details_redacted


def test_overlapping_spans_merge_and_utf16_offsets() -> None:
    text = f"{CARD}"
    redacted, flag = redact_payment_details(text, (TextEntity("bank_card_number", 0, 4),))
    assert (redacted, flag) == ("[карта]", True)
    assert utf16_to_index("😀a", 2) == 1
    assert utf16_to_index("ab", 10) == 2
    empty = redact_payment_details("abc", (TextEntity("ton_address", 1, 0),))
    assert empty == ("abc", False)


def test_invalid_limits_rejected(owners) -> None:
    with pytest.raises(ValueError, match="positive"):
        InboundFilter(owners, max_text_length=0, rate_per_minute=1)
