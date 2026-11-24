from uuid import UUID

from envoy.app.journal import SessionJournal
from envoy.app.renderer import CardRenderer
from envoy.crypto.keys import SigningKey
from envoy.domain.events import MandateAttested
from envoy.domain.ids import MandateId, OwnerId, owner_stream
from envoy.domain.telegram import CallbackUpdate
from envoy.llm.translator import TranslationFailed, Translator, UnsupportedTemplate
from envoy.mandate.commitment import attestation_bytes, commitment_digest
from envoy.mandate.ports import MandateNotFound, MandateStateError, MandateStore
from envoy.ports.clock import Clock
from envoy.ports.rng import Rng
from envoy.telegram.ports import CallbackAnswerer, MessageSender

ONBOARDING = (
    "Я Envoy — агент, который договаривается о встречах от вашего имени.\n\n"
    "Как это работает:\n"
    "1. Опишите здесь, когда и где вам удобно встретиться. Я покажу черновик мандата — "
    "проверьте значения и подтвердите кнопкой. Без подтверждения я ничего не делаю.\n"
    "2. Подключите меня как чат-бота: Настройки → Telegram для бизнеса → Чат-боты.\n"
    "3. В чате с собеседником напишите /envoy — я представлюсь как ваш ИИ-агент и начну "
    "переговоры. /stop там же останавливает сессию.\n\n"
    "Модель доверия: собеседник видит только предложения, ваши границы и приоритеты "
    "остаются приватными. Оператор платформы технически хранит данные обеих сторон; "
    "решения и эскалации приходят только сюда, в личный чат."
)
UNSUPPORTED = "Пока я умею договариваться только о встречах (время и место)."
NOT_PARSED = "Не получилось разобрать описание. Перечислите удобные время и места."


class OwnerFlow:
    def __init__(
        self,
        translator: Translator,
        mandates: MandateStore,
        sender: MessageSender,
        answerer: CallbackAnswerer,
        journal: SessionJournal,
        signing_key: SigningKey,
        renderer: CardRenderer,
        clock: Clock,
        timezone: str,
        rng: Rng,
    ) -> None:
        self._translator = translator
        self._mandates = mandates
        self._sender = sender
        self._answerer = answerer
        self._journal = journal
        self._signing_key = signing_key
        self._renderer = renderer
        self._clock = clock
        self._timezone = timezone
        self._rng = rng

    async def on_start(self, chat_id: int) -> None:
        await self._sender.send_message(chat_id, ONBOARDING)

    async def on_text(self, owner: OwnerId, chat_id: int, text: str) -> None:
        try:
            mandate = await self._translator.draft_mandate(text, self._clock.now(), self._timezone)
        except UnsupportedTemplate:
            await self._sender.send_message(chat_id, UNSUPPORTED)
            return
        except TranslationFailed:
            await self._sender.send_message(chat_id, NOT_PARSED)
            return
        draft = await self._mandates.save_draft(owner, mandate, self._rng.token_bytes(32))
        card = self._renderer.mandate_draft(draft.mandate, draft.id)
        await self._sender.send_message(chat_id, card.text, reply_markup=card.markup())

    async def on_callback(self, owner: OwnerId, callback: CallbackUpdate) -> None:
        action, _, raw_id = callback.data.partition(":")
        try:
            draft = await self._mandates.get(MandateId(UUID(raw_id)))
        except ValueError, MandateNotFound:
            await self._answerer.answer_callback(callback.callback_id, "Черновик не найден")
            return
        if draft.owner != owner:
            await self._answerer.answer_callback(callback.callback_id, "Черновик не найден")
            return
        try:
            if action == "mandate_cancel":
                await self._mandates.discard(draft.id)
                await self._answerer.answer_callback(callback.callback_id, "Черновик отменён")
                return
            signature = self._signing_key.sign(attestation_bytes(draft.mandate))
            active = await self._mandates.activate(draft.id, signature)
        except MandateStateError:
            await self._answerer.answer_callback(callback.callback_id, "Уже обработано")
            return
        await self._journal.append(
            owner_stream(owner),
            MandateAttested(mandate_id=active.id, version=active.version),
            commitment_digest(active.mandate, active.salt),
        )
        await self._answerer.answer_callback(callback.callback_id, "Мандат активирован")
        if callback.chat_id is not None:
            await self._sender.send_message(
                callback.chat_id,
                f"Мандат версии {active.version} активирован. Новые сессии будут "
                "использовать его; уже начатые продолжат со своей версией.",
            )
