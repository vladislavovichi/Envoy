# Envoy Phase 2 — Агент ↔ агент и managed-боты Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal** — два агента на независимых операторах договариваются о встрече без LLM, закрепляют один оффер и открывают чек-листы. Владелец подписывает мандат в Mini App и может подключить собственного managed-бота.

**Architecture** — протокол передаёт подписанные сообщения через Telegram, а движок и `Authorizer` из Фазы 1 работают с теми же офферами независимо от транспорта. Каждая сторона хранит входящие доказательства, переходы автомата и очередь доставки в собственной БД. Mini App подписывает изменения от имени владельца, а виджет C создаёт черновики через тот же сервис мандатов.

**Tech Stack** — Poetry, Pydantic, cbor2, cryptography, PostgreSQL/SQLAlchemy/Alembic, aiogram и прямые Bot API вызовы, FastAPI, TypeScript/Vite, Telegram Mini Apps SDK, TLA+/TLC, pytest/Hypothesis, Docker Compose.

**Spec** — [спецификация Envoy](../specs/2026-11-10-envoy-design.md), §3–6, §7.6, §9.2–9.3, §10.2–10.5, §12, §13–15.

**Предпосылки** — [Фаза 1](2026-11-10-phase-1-meeting-with-human.md) нужна для движка, журнала сессии, топиков, `Authorizer` и чек-листа. [Фаза 1б](2026-11-21-phase-1b-owner-widgets.md) нужна только для задачи 8, которая переиспользует транспорт и обработку `widget_query`. В текущем дереве реализована Фаза 0. Интерфейсы двух предыдущих планов сверить с кодом перед началом соответствующей задачи.

## Global Constraints

- «Без комментариев в коде», Poetry, black, ruff, `mypy --strict`, import-linter. Использовать актуальные стабильные версии на момент реализации, без preview/beta/rc. Схема PostgreSQL меняется через Alembic.
- Секреты конфигурации приходят из `.env`. Приватный ключ владельца остаётся в `SecureStorage`. Ключи агентов и выданные Telegram токены managed-ботов шифруются при хранении. Токены, `initData` и подписываемые приватные данные исключены из логов.
- Каждое протокольное сообщение подписано Ed25519. Маршрут входящего проверяет транспорт, подписи, связь сессии со сторонами и лимиты; бот-сообщения обходят Translator. Исходящий оффер проходит `Authorizer` и проекцию до подписи. Ноль действий вне мандата и ноль утечек в тестовых сценариях.
- Подписанный мандат иммутабелен. Сессия закрепляет версию при старте; перевыпуск не меняет активные сессии. Приватные веса, резерв, ограничения и модель оппонента остаются локально.
- Текст протокола до 4096 символов. Данные виджета до 1024 байт UTF-8, запрос 1–64 байта, ответ в течение 15 секунд; порог теста ресурса после zlib уровня 9 — 3800 байт.
- Раскрытие «я агент» обязательно при открытии, включая приглашения и managed-ботов. Платежи, PSI, обработка споров арбитром и подписанный каталог ключей относятся к последующим фазам.
- Покрытие ядра по веткам не ниже 90 %. Движок сохраняет бюджет ≤ 2 с на типичный раунд без LLM. Проверки внешних сервисов в CI используют фикстуры и fake API.

## Решения плана

- Зафиксировать версию провода `1.0` и предел тела в 2048 байт. Для первой реализации среза встречи передаётся сам публичный оффер с его хэшем. Сообщения вне бюджета отклоняются до отправки, передача вложений по ссылке понадобится при добавлении сценариев с вложениями.
- Инициатор сессии координирует закрепление оффера. У обеих сторон сохранённые подтверждения готовности, решение координатора единственное и сохраняется до доставки. Транзиентные сообщения имеют срок годности; сохранённое решение можно повторно доставить в новом подписанном конверте по согласованному каналу восстановления.
- TLC проверяет безопасность при произвольных потерях и рестартах. Сходимость после сохранения готовности предполагает, что обе стороны восстановятся и хотя бы одна повторная доставка каждого нужного сообщения пройдёт. При постоянном разрыве ожидающая сторона фиксирует просроченное ожидание, уведомляет владельца и сохраняет неопределённость до получения решения; обязательства не создаются по локальному таймауту. Это уточняет требование §5.4 о завершении при любых потерях.
- Первый рукопожатие новой сессии требует действующего делегирования на мандат с подписью владельца. Сессии Фазы 1 продолжают работу с закреплённой версией. Для старой аттестации Mini App предлагает копию в новой версии и запрашивает явную подпись.
- Managed-боты имеют доступ для владельца и до 10 доверенных пользователей. Публичная карточка ведёт в основного бота, где начинается человеческий сценарий. В личке managed-бота входящий агент допускается через протокольную проверку.

## Review Focus

- Подписанное неизвестное необязательное поле должно пережить проверку подписи и хранение в журнале; неизвестное критическое поле завершает рукопожатие отказом. Задача 1.
- Истёкшая делегация, одновременная запись TOFU и смена ключа без доказательства не меняют доверенную привязку. Задача 2.
- Падение после записи готовности или решения и потеря ответа не создают второй чек-лист и не приводят к разным исходам. Задачи 3–4.
- Приглашение, открытое другим Telegram-пользователем или предъявленное другим агентом после использования, не перепривязывает сессию. Задача 7.
- Неподдерживаемый SecureStorage, устаревшая ревизия виджета и гонка подписей не активируют черновик. Задачи 5, 6 и 8.

## File Structure

| Путь | Ответственность |
|---|---|
| `src/envoy/protocol/{envelope,bodies,delegation,limits,machine}.py` | Кодек, сообщения, делегирование, приём и чистые переходы |
| `src/envoy/app/{agent_flow,invitation_flow,managed_bot_flow,miniapp}.py` | Use cases и API владельца |
| `src/envoy/mandate/{signatures,ports}.py` | Контракты подписи, версии, черновики |
| `src/envoy/persistence/{protocol_repo,key_registry,invitation_repo,managed_bot_repo}.py` | Персистентные порты и миграции |
| `src/envoy/telegram/`, `src/tg_fake_api/` | DTO, методы Telegram и контрактные фикстуры |
| `miniapp/`, `src/envoy/widgets/resources/mandate-v1.html` | Редактор и виджет C |
| `formal/`, `.github/workflows/ci.yml`, `compose.federation.yaml` | TLC, проверки и запуск двух операторов |

### Task 1 — Подписанный envelope

**Files** — `src/envoy/protocol/{envelope,bodies}.py`, `tests/unit/test_protocol_envelope.py`, `tests/property/test_protocol_wire.py`.

**Interfaces** — `Version(major: int, minor: int)`; `Envelope(v, sid: SessionId, seq: int, ack: int, type: MessageType, body, ts: int, exp: int, nonce: bytes, chain, crit: tuple[str, ...], sig: bytes)`. `encode_message(envelope: Envelope, signer: SigningKey) -> str`, `decode_message(text: str) -> DecodedEnvelope`, `DecodedEnvelope.verify(key: PublicKey) -> bool`. `DecodedEnvelope` хранит исходные канонические байты и отображение для интерпретации.

- [ ] Добавить тесты с векторами `envoy/1:`, `seq=1`, `ack=0`, UUIDv7, 16-байтным nonce и 64-байтной подписью. Перестановка ключей перед кодированием даёт те же байты; изменение дополнительного поля меняет результат проверки. Проверить 2048/2049 байт тела, 4096/4097 символов сообщения, обрезанный ввод, лишние байты и отклонение недетерминированного CBOR. Hypothesis подаёт произвольные строки и проверяет ограниченный тип результата: DTO или `InvalidEnvelope`.
- [ ] `poetry run pytest tests/unit/test_protocol_envelope.py tests/property/test_protocol_wire.py -q` → RED. Реализовать кодек через существующий `canonical.encode` и обёртки Ed25519. Ограничивать вход до декодирования и проверять единственную каноническую CBOR-карту до преобразования полей; подпись проверять по полной карте без `sig`, включая неизвестные поля. Типизацию тела применять после проверки подписи в принимающем потоке.
- [ ] Определить типы §6.2. Для `hello` — агент, владелец, диапазон версий, делегирование и опциональный токен. Для `capabilities` — имя возможности и версия. Для `propose/counter` — публичный оффер, его хэш, предыдущий хэш и раунд; для `accept` — хэш. Для `commit` — стадии `ready | decision`, хэш оффера и решение `commit | abort` только у `decision`. `revoke` ссылается на оффер; `dispute` переносит ссылки на журнал для будущего арбитра. `revoke_key` и доказательство ротации определяет задача 2.
- [ ] Повторить тесты → GREEN. Коммит `feat: add signed negotiation wire format`.

### Task 2 — Делегирование, TOFU и рукопожатие

**Files** — `src/envoy/protocol/{delegation,handshake}.py`, `src/envoy/persistence/key_registry.py`, миграция `phase2_keys`, `tests/contract/test_peer_keys.py`, `tests/unit/test_handshake.py`.

**Interfaces** — `Delegation(owner_id: OwnerId, owner_key: bytes, agent_id: AgentId, bot_id: int, agent_key: bytes, mandate_id: MandateId, version: int, commitment: bytes, expires_at: datetime, signature: bytes)`. `delegation_bytes(delegation: Delegation) -> bytes`. `KeyRegistry` предоставляет асинхронные `get(bot_id: int) -> PeerPin | None`, `pin_first(pin: PeerPin) -> PeerPin`, `rotate(bot_id: int, proof: RotationProof) -> PeerPin`, `revoke(bot_id: int, proof: RevocationProof) -> None`. `negotiate(local: Capabilities, remote: Capabilities) -> NegotiatedCapabilities`.

- [ ] Добавить тесты: два конкурентных первых ключа дают один pin; `expires_at == now` недействителен; чужой `from.id` отвергается; смена ключа без подписи старого вызывает одну эскалацию; отзыв владельцем переживает рестарт. Пересечение `issue:datetime@1, @2` с `issue:datetime@1` даёт `@1`. Несовместимый major или неизвестное поле в `crit` даёт `reject(reason=unsupported, supported=…)` только подтверждённому отправителю в пределах лимита.
- [ ] `poetry run pytest tests/contract/test_peer_keys.py tests/unit/test_handshake.py -q` → RED. Связать сертификат с Telegram-ботом, публичным ключом, сроком и коммитментом мандата; подписывать канонические байты с отдельным контекстом для каждого вида доказательств. TOFU записывает ключи владельца и агента атомарно после проверки Telegram, сертификата и подписи hello. При ротации требовать подпись старого ключа и новое делегирование от закреплённого владельца; `revoke_key` проверять по ключу владельца и отклонять старые доказательства по ревизии. Сохранить историю ключей для проверки журнала и публиковать отпечаток ключа агента в описании бота.
- [ ] Повторить тесты → GREEN. Коммит `feat: verify agent delegations and pin peer keys`.

### Task 3 — Автомат переговоров и TLC

**Files** — `src/envoy/protocol/machine.py`, `formal/Negotiation.{tla,cfg}`, `formal/traces/`, `.github/workflows/ci.yml`, `tests/property/test_protocol_machine.py`.

**Interfaces** — `transition(state: NegotiationState, event: ProtocolEvent) -> Transition`. `Transition` содержит новое состояние и эффекты доставки, планирования либо открытия чек-листа. Эффекты исполняет задача 4.

- [ ] Добавить трассы и property-тесты: приемлем только последний неотвеченный оффер; для одного хэша `accept` и `counter` взаимоисключаются; при одновременном ходе выигрывает меньший SHA-256 от байтов UUID агента; `revoke` до готовности другой стороны может привести к откату, после неё фиксируется обращение `dispute`. Две стороны в `EXECUTING` имеют один хэш; `ready → crash → timeout → decision(commit)` даёт один эффект на сторону; `ready → timeout → decision(abort)` даёт ноль.
- [ ] `poetry run pytest tests/property/test_protocol_machine.py -q` → RED. Реализовать `HANDSHAKE → NEGOTIATING → AGREED → COMMITTING → EXECUTING`, отказ, отзыв и дедлайны по §5.4. До коммита согласование отклоняется при потере действующего делегирования. После сохранения коммита восстановление проверяет доказательство, привязанное к исходному ключу, хэшу и моменту принятия решения. На `accept` координатор и участник сохраняют готовность по одному хэшу под `Authorizer`. Координатор фиксирует единственное `commit` при обеих готовностях либо `abort` по отзыву/таймауту до решения. После отправки готовности участник по истечении таймера однократно эскалирует и запрашивает сохранённое решение с ограниченными по частоте повторами. Повтор решения идемпотентен; чек-лист создаётся лишь при подтверждённом коммите.
- [ ] Описать ту же таблицу переходов в TLA+ и прогнать общие трассы через Python и модель. Включить потерю, дублирование, переупорядочивание, падение и рестарт с сохранённым решением, таймауты и одновременные офферы. Проверить инварианты согласия и исключения двойного принятия, отсутствие дедлоков и сходимость с указанными допущениями. В отдельном job CI закрепить стабильный TLC и его checksum, запускать `java -jar tla2tools.jar -config formal/Negotiation.cfg formal/Negotiation.tla`; ожидается ноль нарушений. Команда соответствует [документации TLC](https://github.com/tlaplus/tlaplus/blob/master/USE.md).
- [ ] Повторить Python-тесты → GREEN. Коммит `feat: model and verify negotiation transitions`.

### Task 4 — Доставка, anti-loop и восстановление агента

**Files** — `src/envoy/app/{agent_flow,processor,recovery,bootstrap,settings}.py`, `src/envoy/protocol/limits.py`, `src/envoy/persistence/protocol_repo.py`, миграция `phase2_protocol`, Telegram-адаптер и fake, `tests/integration/test_agent_flow.py`, `tests/property/test_protocol_limits.py`.

**Interfaces** — `AgentFlow.open(peer_bot_id: int, owner: OwnerId) -> SessionId`, `receive(update: BotMessageUpdate) -> None`, `run_due(now: datetime) -> None`, все асинхронные. `BotMessageUpdate(bot_id: int, update_id: int, from_bot_id: int, chat_id: int, text: str, received_at: datetime)`. `ProtocolRepository` атомарно сохраняет переход, дедуп и очередь отправки на стороне одного оператора.

- [ ] Написать интеграционные тесты: два агента согласуют одну встречу, счётчик вызовов LLM равен нулю, оба журнала содержат оригинальные подписи и дают по одному чек-листу. Сбой после commit БД перед send и потерянный ack восстанавливаются. Апдейты разных ботов с `update_id=1` обрабатываются независимо. Тот же оффер под новым `seq` или 3 раунда без прогресса дают `stalled`. Четвёртая одновременная сессия с одной парой отклоняется. Повтор `seq` или nonce, `seq=3` до `seq=2`, устаревший `exp` и поток новых сессий не продвигают автомат вне правил.
- [ ] `poetry run pytest tests/integration/test_agent_flow.py tests/property/test_protocol_limits.py -q` → RED. Маршрутизировать личные bot-to-bot сообщения по `(bot_id, from_bot_id, sid)`; проверить подпись и handshake перед движком. Отправлять через существующий `OutboundQueue`. Применить token bucket на сессию, пару и отправителя, персистентный дедуп `(sender,sid,seq)` и nonce до истечения окна, потолки раундов, ожиданий и одновременных сессий. Начальные значения конфигурации: 60 сообщений в минуту на отправителя, 30 на пару, 12 на сессию, 3 активные сессии на пару, 20 раундов, ожидание ответа до 60 секунд и 3 раунда без прогресса. Всегда выбирать более строгую границу из мандата и платформы. `ack` подтверждает непрерывный префикс. Пропуск последовательности не применяет сообщение до повторной доставки, повторы `ack/ping/error` не порождают ответную петлю.
- [ ] В одной транзакции участника сохранять входящее доказательство в зашифрованном журнале, позицию автомата и исходящие задания. До дедлайна повторять тот же `seq`; при просрочке отсутствующего сообщения закрыть переговоры до готовности либо запрашивать решение через служебный режим восстановления. Использовать адресный подписанный запрос и ответ с собственным курсором, закреплёнными сторонами и хэшем принятого оффера. Этот режим переносит только сохранённое решение и не продвигает обычный курсор переговоров. На рестарте восстановить курсоры, таймеры и outbox по БД. Подключить существующие `Authorizer`, проекцию и чек-лист.
- [ ] Повторить тесты → GREEN. Проверить на тестовых ботах включённый bot-to-bot режим по [официальному руководству Telegram](https://core.telegram.org/bots/features#bot-to-bot-communication). Коммит `feat: connect peer negotiation to Telegram and recovery`.

### Task 5 — API владельца и подписанные версии мандата

**Files** — `src/envoy/app/miniapp.py`, `src/envoy/mandate/{ports,signatures}.py`, `src/envoy/persistence/mandate_store.py`, миграция `phase2_owner`, `tests/unit/test_miniapp_auth.py`, `tests/contract/test_signed_mandates.py`.

**Interfaces** — `GET /miniapp/mandates/schema`, `GET /miniapp/mandates/active`, `POST /miniapp/challenges`, `POST /miniapp/challenges/{id}/signature`. `SigningChallenge(id, purpose, bytes_to_sign, expires_at)`, назначение `enroll | mandate | delegate | escalation | revoke_key`. `MandateStore` расширяется транзакционным `activate_signed(owner: OwnerId, mandate_id: MandateId, expected_version: int | None, signature: bytes) -> StoredMandate`.

- [ ] Добавить тесты с валидной Telegram-фикстурой и явно просроченным `auth_date`; чужой бот и пользователь, повтор nonce, изменённые байты, устаревшая версия и два конкурентных подтверждения дают ноль лишних активаций. При перевыпуске `attestation=platform` старая сессия продолжает ссылаться на прежний `mandate_id`; новое делегирование строится на подписанной версии. Подпись эскалации с чужим `sid` или хэшем отвергается.
- [ ] `poetry run pytest tests/unit/test_miniapp_auth.py tests/contract/test_signed_mandates.py -q` → RED. Валидировать raw `initData` согласно Telegram, сверять свежесть и привязку к боту и владельцу. Challenge хранит назначение, одноразовый nonce и исходные данные. При первой регистрации проверить подпись challenge предлагаемым публичным ключом и атомарно закрепить его за владельцем. Повторный enroll с другим ключом отклоняется. Для публикации подписываются канонические байты мандата, владелец, ID, версия и challenge; для `delegate` формируются точные байты из задачи 2; для эскалации — `sid`, ревизия, хэш, выбор и срок. При подтверждении сверять ожидаемую версию, выполнять мутацию и погашать challenge атомарно; повтор авторизованного подтверждения возвращает сохранённый результат. Источник схемы — `Mandate.model_json_schema()`. Старые аттестации остаются проверяемы. Сверить [правила проверки Telegram initData](https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app).
- [ ] Повторить тесты → GREEN. Коммит `feat: add verified owner signing and mandate activation`.

### Task 6 — Mini App для встречи

**Files** — `miniapp/{package.json,package-lock.json,src/}`, `miniapp/tests/mandate.test.ts`, `docker/Dockerfile`.

**Interfaces** — `signChallenge(challenge: SigningChallenge): Promise<string>`; API задачи 5. `meeting` — единственный редактируемый шаблон с датой/временем, часовым поясом, местом, весами и жёсткими границами.

- [ ] Добавить тесты: форма соответствует сгенерированной схеме, шаблон проходит серверную валидацию, подпись байтов challenge проходит общий Python/TS вектор Ed25519; недоступное хранилище сохраняет режим просмотра/черновика; приватный ключ никогда не входит в запрос, черновик в DeviceStorage не содержит чувствительных полей в открытом виде; потеря соединения после подписи показывает фактический статус после повторного запроса.
- [ ] `npm --prefix miniapp test` → RED. Сделать интерфейс формы на TypeScript/Vite с Telegram WebApp SDK. Генерировать форму и типы из серверной JSON Schema, использовать готовую реализацию Ed25519 и распознавать поддержку клиента. Ключ создать на устройстве и сохранить в `SecureStorage` перед регистрацией. Сохранять в `DeviceStorage` только несекретное состояние редактирования. Перед подписью показать владельцу версию и изменения; неподписанные изменения остаются черновиком. Выдать действия подписания мандата, делегирования, эскалации и отзыва через один экран подтверждения. Сверить [документацию хранилищ Telegram](https://core.telegram.org/bots/webapps#securestorage).
- [ ] `npm --prefix miniapp ci`, затем `npm --prefix miniapp test` и `npm --prefix miniapp run build` → GREEN. Коммит `feat: add meeting mandate Mini App`.

### Task 7 — Приглашения и смена собеседника

**Files** — `src/envoy/app/{invitation_flow,processor}.py`, `src/envoy/persistence/invitation_repo.py`, миграция `phase2_invites`, адаптер и fake API, `tests/integration/test_invitation_flow.py`.

**Interfaces** — `issue(sid: SessionId, counterpart_user_id: int | None, expires_at: datetime) -> str`, `claim_human(token: str, user_id: int) -> SessionId`, `bind_agent(token: str, peer: VerifiedPeer) -> SessionId`, все асинхронные. Для перехода в агентский режим токен привязан к ожидаемому контрагенту; `VerifiedPeer` содержит подтверждённый Telegram ID представляемого пользователя.

- [ ] Проверить сценарии `guest_message → answerGuestQuery`, `inline_query → answerInlineQuery` и `/start <token>`: гостевой запрос получает один ответ, карточка раскрывает роль агента и ведёт в личку. При переходе остаются прежние `sid`, раунд, оффер, мандат и история. Чужой пользователь, повторный или истёкший токен, гонка второго `hello` и поздний `hello` после перехода не меняют маршрут. Без токена допустима только новая сессия.
- [ ] `poetry run pytest tests/integration/test_invitation_flow.py -q` → RED. Задействовать одноразовый случайный токен со сроком, хранить хэш, менять маршрут и погашать токен атомарно. Для уже известного человека связывание его агента требует подтверждённой Telegram-привязки: предъявитель проходит handshake, а сам человек авторизует этот bot ID в личке основного бота перед погашением. При тишине агента переключать на карточку человеку только до записи готовности к коммиту, по дедлайну ожидания, с атомарной инвалидизацией старого маршрута. Служебные апдейты не отправлять в Translator. Добавить контрактные фикстуры методов по [Telegram Bot API](https://core.telegram.org/bots/api#answerguestquery).
- [ ] Повторить тесты → GREEN. Коммит `feat: preserve negotiation across Telegram invitation routes`.

### Task 8 — Виджет C и черновик

**Files** — `src/envoy/widgets/resources/mandate-v1.html`, `src/envoy/widgets/{projection,owner_view}.py`, `src/envoy/app/widget_query_flow.py`, `tests/unit/test_widget_resources.py`, `tests/integration/test_mandate_widget.py`.

**Interfaces** — `WidgetMandateView(revision: int, slots: tuple[SlotOption, ...], adjustable_issues: tuple[str, ...])`; `MandateWidgetFlow.handle(update: WidgetQueryUpdate) -> None` (async). Переиспользует `OwnerTarget` и `WidgetTransport` из Фазы 1б.

- [ ] Проверить сохранение черновика, активная версия прежняя до подписи в Mini App. На границе транспорта 64-байтный запрос допустим, 65-байтный отклоняется. Чужая карточка, устаревшая ревизия и повтор после рестарта дают ответ и не пишут новый черновик. Канарейки весов, резерва и личного календаря отсутствуют в `chat_data`. Проверить порог ресурса и откат на Mini App при недоступном виджете.
- [ ] `poetry run pytest tests/unit/test_widget_resources.py tests/integration/test_mandate_widget.py -q` → RED. Показывать разрешённые к отображению варианты и локальные поправки весов через «−/+». Один запрос `m:<rev36>:<slots64>:<delta64>`: до 32 слотов в 4 байтах, до 8 поправок как signed bytes, base64url без паддинга. Проверять маску и длину поправок по ревизии карточки, применять к сохранённой версии черновика. Поправка равна одному процентному пункту на шаг, после применения нормировать веса. Отрицательный вес или нулевая сумма дают отказ. Повторно валидировать `Mandate`. Обновлять состояние только через edit; отказ и успех подтверждать через answer в течение 15 секунд. Передавать ссылку на Mini App в текстовом представлении.
- [ ] Повторить тесты → GREEN. Проверить на 320 и 430 px с заглушкой TelegramWidget и вручную в тестовом клиенте. Коммит `feat: create mandate drafts from owner widget`.

### Task 9 — Managed-бот и изоляция владельцев

**Files** — `src/envoy/app/{managed_bot_flow,bootstrap,runtime}.py`, `src/envoy/persistence/managed_bot_repo.py`, миграция `phase2_managed`, адаптер и fake API, `tests/contract/test_managed_bot_api.py`, `tests/integration/test_managed_bot_flow.py`.

**Interfaces** — `ManagedBotFlow.request(owner: OwnerId) -> None`, `reconcile(event: ManagedBotEvent) -> None`, `set_access(owner: OwnerId, bot_id: int, trusted: tuple[int, ...]) -> None` (async). Реестр хранит владельца, bot ID, зашифрованный токен и статус конфигурации.

- [ ] Добавить тесты: повтор создания запускает единственный рантайм; 10 доверенных допустимы, 11 отклоняются; чужой владелец, webhook с чужим секретом и апдейт другого бота не меняют профиль; сбой после получения токена восстанавливает настройку; при смене токена старый получатель апдейтов останавливается; токен отсутствует в логах.
- [ ] `poetry run pytest tests/contract/test_managed_bot_api.py tests/integration/test_managed_bot_flow.py -q` → RED. Использовать `KeyboardButtonRequestManagedBot` и суффикс `bot`. Сопоставить обновление владельца и managed-бота с сохранённым запросом создания и подтверждённым Telegram ID. Получить токен через метод платформы; настроить профиль, аватар, раскрытие, отпечаток ключа, ограничения доступа и обработчик обновлений с отдельным секретом. До подтверждения настроек агент остаётся неактивным. При смене владельца остановить обработку и потребовать отдельного подключения и делегирования новым владельцем. Сверить параметры с [описанием managed-ботов в Bot API](https://core.telegram.org/bots/api#getmanagedbottoken).
- [ ] Повторить тесты → GREEN. В тестовом Telegram проверить создание, ограничения и передачу bot-to-bot сообщения постороннего агента: если настройки Telegram блокируют его на уровне платформы, протокол ведёт основной бот. Коммит `feat: provision and isolate managed agents`.

### Task 10 — Приёмка двух операторов

**Files** — `compose.federation.yaml`, `Makefile`, `.github/workflows/ci.yml`, `scripts/check_coverage.py`, `tests/integration/test_federation_meeting.py`, `tests/property/test_protocol_faults.py`, `README.md`.

- [ ] Добавить тесты для двух независимых операторов в Compose. У каждого своя БД, мастер-ключ, токен бота и журнал. Переговоры идут через локальный fake Telegram. Оба проходят `hello → capabilities → propose/counter → accept → commit`, фиксируют один хэш и по одному чек-листу. Оператор B перезапускается посреди `COMMITTING`; восстановление доставки приводит к тому же результату. Property-тесты чередуют дубли, задержки и конечные потери; нет незавершённых таймеров после восстановления связи.
- [ ] Реализовать `make test-federation` поверх Compose и изолированных тестовых переменных. Запустить в CI сквозной тест, Mini App tests/build и TLC отдельными проверками. Добавить `protocol` к индивидуальным порогам покрытия ядра. В README описать подключение двух тестовых Telegram-ботов, проверку раскрытия, guest/inline, Mini App и виджета, а также допущение о восстановлении связи.
- [ ] `make test-federation`, `make test`, `make lint`, тесты и сборка Mini App, TLC → GREEN. Регрессия: сценарий человека из Фазы 1 и режимы `owner_view=text/widget` из Фазы 1б проходят. Ручной результат Telegram зафиксировать отдельным пунктом отчёта, с фактическими клиентами и версиями API. Коммит `test: verify federated meeting and phase two acceptance`.

## Самопроверка плана

| Требование Фазы 2 | Задачи |
|---|---|
| Envelope, подписи, версии, делегирование, TOFU и anti-loop | 1, 2, 4 |
| Согласованный оффер, закрепление, рестарт и TLA+/TLC | 3, 4, 10 |
| Mini App, ключ владельца, редактор встречи и перевыпуск | 5, 6 |
| Guest, inline и переход человек ↔ агент | 7, 10 |
| Виджет C и подпись черновика в Mini App | 5, 6, 8 |
| Managed-боты и ограничения доступа | 9, 10 |
| Два независимых оператора и фаззинг | 4, 10 |
