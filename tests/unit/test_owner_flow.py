from uuid import UUID

import pytest

from envoy.app.owner_flow import NOT_PARSED, UNSUPPORTED, OwnerFlow
from envoy.app.renderer import TemplateRenderer
from envoy.domain.events import MandateAttested
from envoy.domain.ids import OwnerId, owner_stream
from envoy.domain.telegram import CallbackUpdate
from envoy.journal.verifier import JournalVerifier
from envoy.llm.translator import TranslationFailed, Translator, UnsupportedTemplate
from envoy.mandate.commitment import attestation_bytes, commitment_digest
from envoy.mandate.ports import InMemoryMandateStore
from envoy.ports.clock import FixedClock
from envoy.ports.ids import SequentialIdGenerator
from envoy.ports.rng import SeededRng
from envoy.telegram.memory import FakeTransport
from tests.support.journal import AGENT, build_journal
from tests.support.mandates import meeting_mandate
from tests.support.sessions import T0

OWNER = OwnerId(UUID(int=1))
STRANGER = OwnerId(UUID(int=2))


@pytest.fixture
def parts(mocker):
    translator = mocker.create_autospec(Translator, instance=True)
    translator.draft_mandate.return_value = meeting_mandate()
    clock = FixedClock(T0)
    journal, store, key = build_journal(clock)
    transport = FakeTransport()
    mandates = InMemoryMandateStore(SequentialIdGenerator())
    flow = OwnerFlow(
        translator,
        mandates,
        transport,
        transport,
        journal,
        key,
        TemplateRenderer("Europe/Moscow"),
        clock,
        "Europe/Moscow",
        SeededRng(1),
    )
    return flow, translator, transport, mandates, journal, store, key


def callback(data: str, user_id: int = 1) -> CallbackUpdate:
    return CallbackUpdate(9, "cb", user_id, 1, 3, data)


async def test_start_mentions_trust_model_and_business_setup(parts) -> None:
    flow, _, transport, *_ = parts
    await flow.on_start(1)
    text = transport.messages[0].text
    for phrase in ("Модель доверия", "Telegram для бизнеса", "/envoy", "/stop", "Оператор"):
        assert phrase in text


async def test_text_produces_draft_card_with_parsed_values_and_buttons(parts) -> None:
    flow, translator, transport, mandates, *_ = parts
    await flow.on_text(OWNER, 1, "завтра с 9 до 16, офис или кафе")
    translator.draft_mandate.assert_awaited_once()
    record = transport.sent[0]
    assert "12:00" in record.message.text
    assert "office" in record.message.text
    assert "station" in record.message.text
    buttons = record.reply_markup.buttons[0]
    assert [button.style for button in buttons] == ["success", "danger"]
    draft_id = buttons[0].data.split(":")[1]
    assert (await mandates.get(UUID(draft_id))).status == "draft"


async def draft_id(flow, transport) -> str:
    await flow.on_text(OWNER, 1, "встреча")
    return transport.sent[-1].reply_markup.buttons[0][0].data.split(":")[1]


async def test_confirm_activates_and_journals_platform_attestation(parts) -> None:
    flow, _, transport, mandates, journal, store, key = parts
    identity = await draft_id(flow, transport)
    await flow.on_callback(OWNER, callback(f"mandate_confirm:{identity}"))
    active = await mandates.active(OWNER)
    assert active is not None
    assert key.public().verify(attestation_bytes(active.mandate), active.signature)
    events = await journal.events(owner_stream(OWNER))
    assert events == [MandateAttested(mandate_id=active.id, version=1, attestation="platform")]
    entries = await store.read(owner_stream(OWNER))
    assert entries[0].mandate_commitment == commitment_digest(active.mandate, active.salt)
    JournalVerifier().verify(entries, {AGENT: key.public()})
    assert transport.callback_answers[-1] == ("cb", "Мандат активирован")
    await flow.on_callback(OWNER, callback(f"mandate_confirm:{identity}"))
    assert transport.callback_answers[-1] == ("cb", "Уже обработано")
    assert len(await journal.events(owner_stream(OWNER))) == 1


async def test_cancel_discards_draft(parts) -> None:
    flow, _, transport, mandates, *_ = parts
    identity = await draft_id(flow, transport)
    await flow.on_callback(OWNER, callback(f"mandate_cancel:{identity}"))
    assert (await mandates.get(UUID(identity))).status == "discarded"
    assert await mandates.active(OWNER) is None


async def test_confirm_of_foreign_draft_ignored(parts) -> None:
    flow, _, transport, mandates, journal, *_ = parts
    identity = await draft_id(flow, transport)
    await flow.on_callback(STRANGER, callback(f"mandate_confirm:{identity}", user_id=2))
    assert await mandates.active(OWNER) is None
    assert transport.callback_answers[-1] == ("cb", "Черновик не найден")
    await flow.on_callback(OWNER, callback("mandate_confirm:not-a-uuid"))
    await flow.on_callback(OWNER, callback(f"mandate_confirm:{UUID(int=999)}"))
    assert transport.callback_answers[-1] == ("cb", "Черновик не найден")
    assert await journal.events(owner_stream(OWNER)) == []


async def test_unsupported_template_reported(parts) -> None:
    flow, translator, transport, mandates, *_ = parts
    translator.draft_mandate.side_effect = UnsupportedTemplate("purchase")
    await flow.on_text(OWNER, 1, "купи велосипед")
    assert transport.messages[-1].text == UNSUPPORTED


async def test_translation_failure_reports_without_saving(parts) -> None:
    flow, translator, transport, mandates, *_ = parts
    translator.draft_mandate.side_effect = TranslationFailed("x")
    await flow.on_text(OWNER, 1, "???")
    assert transport.messages[-1].text == NOT_PARSED
    assert transport.sent[-1].reply_markup is None
    assert await mandates.active(OWNER) is None


async def test_new_version_supersedes_old_for_new_sessions_only(parts) -> None:
    flow, _, transport, mandates, *_ = parts
    first = await draft_id(flow, transport)
    await flow.on_callback(OWNER, callback(f"mandate_confirm:{first}"))
    second = await draft_id(flow, transport)
    await flow.on_callback(OWNER, callback(f"mandate_confirm:{second}"))
    old = await mandates.get(UUID(first))
    active = await mandates.active(OWNER)
    assert old.status == "superseded"
    assert old.mandate.version == 1
    assert active.version == 2
    assert "версии 2" in transport.messages[-1].text
