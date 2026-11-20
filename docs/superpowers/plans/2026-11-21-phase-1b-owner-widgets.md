# Envoy Phase 1б — Виджеты владельца Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal** — показать владельцу ход сделки, дать ему возможность ответить на эскалацию и присылать дневную сводку в личном чате с ботом.

**Architecture** — оркестрация Фазы 1 вызывает порт `OwnerView`. Текст и HTML используют одну модель представления, собранную из журнала и проекции офферов. Оба способа принять решение вызывают один обработчик эскалаций с `Authorizer`, а доставка и актуализация карточек восстанавливаются отдельно от решения.

**Tech Stack** — Python, Poetry, PostgreSQL/Alembic, существующий адаптер aiogram и `tg_fake_api`, самодостаточные ресурсы HTML/CSS/JavaScript, pytest и Docker Compose.

**Spec** — [спецификация Envoy](../specs/2026-11-10-envoy-design.md), §3, §9.2–9.3, §10.4, §12.3, §12.5–12.7, §13, §14 (Фаза 1б), §15.

**Предпосылка** — Фаза 1 из [плана встречи 1:1](2026-11-10-phase-1-meeting-with-human.md) реализована и проходит сквозной сценарий. Сейчас в `main` реализована Фаза 0, поэтому `SessionFlow`, `SessionRecord`, `CardRenderer`, `InboundFilter`, `Scheduler` и `Recovery` ниже обозначают интерфейсы из плана Фазы 1. Перед реализацией сверить их с фактическими интерфейсами.

## Global Constraints

- Виджеты A, B и D доставляются только в личный чат владельца с ботом. C относится к Фазе 2. Shared, inline, группы, TON и виджеты контрагента находятся за рамками среза.
- HTML — 1–32768 байт UTF-8 без NUL, максимум 4096 байт при zlib level 9. Внутренний порог каждого ресурса — 3800 байт при том же сжатии. Без сети, внешних ресурсов и полей ввода.
- `chat_data` и `public_data` — до 1024 байт UTF-8 каждое. Envoy передаёт состояние в `chat_data` и оставляет `public_data` пустым. `caption` содержит текстовую карточку и ограничен 1024 символами после обработки entities.
- `request` — 1–64 байта UTF-8, синхронно из обработчика нажатия, один запрос в полёте. Лимит Telegram — 5 запросов за 5 секунд на пользователя и бота. Ответ — до 1024 байт в течение 15 секунд, в том числе при отказе. Состояние меняется через `editMessageWidgetData`.
- Код отправленного виджета фиксирован. При смене версии HTML создаётся новое сообщение, прежние интерактивные привязки теряют силу. Клиент игнорирует входящее состояние с `r` меньше показанной ревизии.
- Журнал и `Authorizer` определяют решения. Только `InboundFilter` → проверка владельца, личного чата, карточки, текущего `cause` и серверного дедлайна → общий обработчик эскалации. При таймауте действует отказ.
- В HTML, состоянии, подписях, ответах и логах используются разрешённые поля. Резерв, веса, приватные границы, модель оппонента и данные другого владельца исключены.
- Порты на `typing.Protocol`, реализации выбираются в `app/bootstrap.py`, миграции через Alembic. Код без комментариев, `mypy --strict`, black, ruff и import-linter. Офлайн-проверки через существующие цели `make test` и `make lint`.

Поля API и ограничения сверены с [Bot API 10.4](https://core.telegram.org/bots/api#sendwidget), [WidgetQuery](https://core.telegram.org/bots/api#widgetquery) и [руководством Telegram Widgets](https://core.telegram.org/bots/widgets#limits).

## Решения плана

- `ENVOY_OWNER_VIEW=text|widget`, по умолчанию `text`. Если виджет отвергнут, состояние или подпись не умещаются либо доставка истекла, используется текстовое представление в личке владельца. Поддержку клиента через Bot API определить нельзя. Для старого клиента оставляем подпись и штатные inline-кнопки эскалации. Если проверка кнопок на тестовом сервере не проходит, рядом с B отправляется текстовая карточка с кнопками.
- Дашборд показывает долю совпавших значений среди issues, представленных в обоих офферах. При отсутствии общих issues показатель скрыт. Статус проверки журнала выводится только при наличии результата серверного верификатора.
- Сводка отправляется с 20:00 по `ENVOY_DEFAULT_TIMEZONE`, один раз за локальный день с активными сделками. Отдельный топик создаётся лениво, при недоступности топиков сводка доставляется в основную личку. До 10 строк в виджете при соблюдении лимитов, оставшиеся строки идут текстом. Порядок — по ближайшему дедлайну, затем по `SessionId`.

## Review Focus

- Чужой пользователь с валидным идентификатором запроса, устаревшая карточка и чужой топик получают нейтральный отказ без доступа к данным сделки (Task 5).
- Одновременно нажаты кнопки в тексте и в виджете либо наступил дедлайн на границе обработки. Журнал хранит один итог (Task 5).
- Кириллица, эмодзи, HTML в названии места и очень длинный оффер. Лимиты считаются в UTF-8, данные отображаются как текст, при переполнении решение доступно в текстовой карточке (Tasks 2, 4).
- Процесс падает после фиксации решения и до редактирования виджета. Восстановление дочитывает журнал и применяет актуальную ревизию без нового решения (Task 4).
- Навигация из сводки на клиенте, который не открывает topic-link, и сводка с более чем 10 активными сделками. Владелец получает ссылку в личке и все строки (Task 6).

## File Structure

| Путь | Ответственность |
|---|---|
| `src/envoy/app/{owner_view,escalation,widget_queries,daily_summary}.py` | Порт, общая обработка эскалаций, маршрутизация запросов и сводки |
| `src/envoy/app/{renderer,session_flow,processor,recovery,runtime,settings,bootstrap}.py` | Текстовый вывод и точки подключения к Фазе 1 |
| `src/envoy/security/widget_projection.py` | Белый список и DTO для представления |
| `src/envoy/widgets/{owner_view,resources}.py`, `src/envoy/widgets/resources/{dashboard-v1,escalation-v1,summary-v1}.html` | Реализация Telegram-виджетов и ресурсы |
| `src/envoy/domain/telegram.py`, `src/envoy/telegram/{ports,adapter,outbound,dependencies}.py` | DTO, порты, Bot API и доставка с лимитами |
| `src/envoy/persistence/{models,session_repo,owner_views}.py`, `src/envoy/persistence/migrations/versions/*_phase1b.py` | Ссылки на карточки, привязки действий, отметки отправки сводок |
| `src/tg_fake_api/{server,state,errors}.py`, `tests/fixtures/widget_schema.json` | Контракт методов и апдейтов Telegram 10.4 |
| `docs/superpowers/reports/2026-11-21-phase-1b-widget-checklist.md` | Фактические результаты предварительной и финальной ручных проверок |

### Task 1 — Подтвердить поведение Telegram и добавить контракт транспорта

**Files** — `src/envoy/domain/telegram.py`, `src/envoy/telegram/{ports,adapter,outbound,dependencies}.py`, `src/tg_fake_api/{server,state,errors}.py`, `tests/fixtures/{widget_schema,bot_api_methods}.json`, `tests/contract/test_widget_transport.py` и отчёт из таблицы.

**Interfaces** — `WidgetQueryUpdate(update_id: int, query_id: str, user_id: int, chat_id: int | None, message_id: int | None, data: str | None, received_at: datetime)` в `Update`. `data=None` для неподдерживаемой формы запроса с валидным ID. `WidgetTransport.send_widget(chat_id: int, html: str, state: str, caption: str, *, thread_id: int | None, allow_queries: bool, reply_markup: ReplyMarkup | None = None) -> int` возвращает ID сообщения. `edit_widget(chat_id: int, message_id: int, state: str) -> None`. `answer_widget(query_id: str, data: str, *, deadline: datetime) -> None`.

- [ ] На выделенном тестовом боте проверить `Date`, открытие topic-link через `openTelegramLink`, `sendWidget(message_thread_id=…)`, `reply_markup` на поддерживаемом клиенте и текстовый путь клиента без поддержки. Зафиксировать клиент, версию и поведение. При недоступном `Date` использовать оставшиеся секунды и локальный таймер. При недоступной ссылке использовать запрос на отправку ссылки. При отказе отправки в топик применить текстовый путь для владельца.
- [ ] Добавить контрактные тесты для точной HTTP-формы и обоих способов получения `widget_query`. Зафиксировать `assert response_id == sent_message_id`, `assert mapped.data == "a:42"` при вложенном `type.type == "data"`, `assert mapped_wallet.data is None`, `assert edits == 1` при повторе тех же данных. Fake проверяет 1024/1025 байт, 32768/32769 сырых байт, 4096/4097 сжатых байт, NUL, 64/65 байт запроса, 5/6 запросов в окне, принятие ответа до 15 секунд и отказ с 15 секунд. Сверить ошибки с руководством.
- [ ] `poetry run pytest tests/contract/test_widget_transport.py -q` → RED, виджетных портов и методов ещё нет.
- [ ] Реализовать через `AiogramAdapter.call_raw`, разбирать внешний объект запроса по официальному `WidgetQuery`, добавлять `widget_query` в `allowed_updates` polling и webhook. Задействовать существующие глобальный и per-chat лимитеры `OutboundQueue` для send/edit. `answerWidgetQuery` ограничить дедлайном и отправлять отдельно от очереди чата. Нормализовать только ошибку edit «message is not modified» в успех. Фикстура сверяет форму трёх методов и апдейта, реестр отмечает версию 10.4.
- [ ] Повторить тест → GREEN. Commit `feat: add Telegram owner widget transport`.

### Task 2 — Общая проекция и текстовое представление

**Files** — `src/envoy/security/widget_projection.py`, `src/envoy/app/{owner_view,renderer}.py`, `tests/unit/{test_widget_projection,test_text_owner_view}.py`.

**Interfaces** — `OwnerTarget(owner_id: OwnerId, user_id: int, chat_id: int, thread_id: int | None)`. `Dashboard(revision: int, round: int, max_rounds: int, status: str, issues: tuple[IssueRow, ...], proximity: int | None, verified: bool | None)`, `IssueRow(name: str, ours: str | None, theirs: str | None)`. `EscalationCard(revision: int, cause: int, expires_at: datetime, issues: tuple[IssueRow, ...], outcome: Literal["pending", "approved", "declined", "timeout"])`. `SummaryRow(label: str, status: str, topic_url: str | None)`. Порт `OwnerView` предоставляет `dashboard(target: OwnerTarget, session: SessionId, card: Dashboard) -> None`, `escalation(target: OwnerTarget, session: SessionId, card: EscalationCard) -> None` и `summary(target: OwnerTarget, day: date, rows: tuple[SummaryRow, ...]) -> None`, все методы асинхронные. `WidgetProjection` строит эти модели из явно переданных полей, используя `OfferProjection` из Фазы 1.

- [ ] Добавить тесты. Два из трёх одинаковых общих значений дают `assert card.proximity == 67`, без сравниваемых значений `assert card.proximity is None`. Канарейки резерва, веса и модели отсутствуют в сериализации, тексте и логах. Эскалация `cause=42` выдаёт существующие кнопки `approve:42`/`decline:42` только при `pending`; текстовая сводка сохраняет каждую строку и topic-link.
- [ ] `poetry run pytest tests/unit/test_widget_projection.py tests/unit/test_text_owner_view.py -q` → RED, порт и проекция отсутствуют.
- [ ] Выделить `TextOwnerView` из `CardRenderer` и существующей отправки владельцу. Использовать `OwnerTarget` и топик сессии с откатом в основную личку при его недоступности. Преобразовывать типизированные значения через существующий рендерер Фазы 1. Округлять процент до ближайшего целого. Длинный текст разбивать средствами текстовой доставки с сохранением кнопок эскалации.
- [ ] Повторить тесты → GREEN. Commit `feat: extract owner view and safe projection`.

### Task 3 — Три версионируемых HTML-ресурса

**Files** — `src/envoy/widgets/resources.py`, `src/envoy/widgets/resources/{dashboard-v1,escalation-v1,summary-v1}.html`, `pyproject.toml` при необходимости включения HTML в сборку, `tests/unit/test_widget_resources.py`.

**Interfaces** — `load_widget(kind: Literal["dashboard", "escalation", "summary"], version: int = 1) -> str`. Общий ключ состояния `r: int`. Dashboard добавляет `round, max, status, issues, proximity, verified`. Escalation — `cause, expires_at` (UTC ISO 8601), `remaining` (секунды), `issues, outcome`. Summary — `rows` с `label, status, url` и `links: "direct" | "request"`. `issues` — строки с `name, ours, theirs`.

- [ ] Добавить тесты для трёх ресурсов из установленного пакета и исходного дерева. Для каждого `assert 1 <= len(raw) <= 32768`, `assert b"\x00" not in raw`, `assert len(zlib.compress(raw, 9)) <= 3800`; HTML валиден, без форм и внешних URL ресурсов. Подготовить примеры для браузера с заглушкой `TelegramWidget` из [официального руководства](https://core.telegram.org/bots/widgets#testing).
- [ ] `poetry run pytest tests/unit/test_widget_resources.py -q` → RED, ресурсы ещё отсутствуют.
- [ ] A показывает номер раунда, значения «вы / они», статус, сравнение и известный результат проверки журнала. B показывает оффер и оставшееся время, отправляет `a:<cause>` / `d:<cause>`, блокирует кнопки на время запроса и ждёт итогового `chatDataChanged`. При клиентском нуле блокирует действия; исход определяет сервер. D показывает строки со статусом. В режиме `direct` строка открывает URL синхронно на клик, а ручная кнопка «Прислать ссылку» отправляет `l:<r>:<index>` отдельным нажатием. В режиме `request` действие строки сразу отправляет тот же запрос. Все ресурсы обновляются из `chatDataChanged`, используют `textContent` для входных значений и игнорируют меньшую ревизию.
- [ ] Повторить тест → GREEN. Проверить в браузере ширины 320 и 430 px, светлую и тёмную темы, длинное место с HTML-символами, устаревшую ревизию, отсчёт, поведение при `BUSY` и `TIMEOUT`. Проверить wheel через `poetry build -f wheel` и запуск теста ресурсов в окружении с установленным wheel. Commit `feat: add versioned owner widget resources`.

### Task 4 — Доставка, сохранение карточек и восстановление

**Files** — `src/envoy/widgets/owner_view.py`, `src/envoy/persistence/{models,session_repo,owner_views}.py` и миграция `phase1b`, `src/envoy/app/{session_flow,recovery,settings,bootstrap}.py`, `.env.example`, `.importlinter`, `tests/contract/test_owner_view_store.py`, `tests/integration/test_owner_widget_delivery.py`.

**Interfaces** — `WidgetRef(chat_id: int, message_id: int, version: int, revision: int)`. `SessionRecord.dashboard_widget: WidgetRef | None` и `SessionRepository.set_dashboard_widget(session: SessionId, ref: WidgetRef) -> None` (async). `OwnerViewStore` предоставляет асинхронные `put_escalation(session: SessionId, cause: int, ref: WidgetRef) -> None` и `find_escalation(chat_id: int, message_id: int) -> tuple[SessionId, int] | None` для текущих привязок. `WidgetOwnerView` реализует порт из Task 2 поверх `WidgetTransport` и `TextOwnerView`; метод `summary` до Task 6 делегирует в `TextOwnerView`.

- [ ] Добавить тесты. После открытия и двух раундов `assert widget_messages == 1`, `assert dashboard_revisions == sorted(set(dashboard_revisions))`. Если правка оказалась применена перед падением, повтор считается доставленным. При 1025 байтах UTF-8 в состоянии или отказе `sendWidget` эскалация доставляется текстом с кнопками. При падении после `OwnerResolved` и до edit восстановление обновляет B и A без повторного решения. Существующая запись сессии читается с `dashboard_widget=None`.
- [ ] `poetry run pytest tests/contract/test_owner_view_store.py tests/integration/test_owner_widget_delivery.py -q` → RED, отсутствуют ссылки и реализация.
- [ ] Привязать A к записи сессии и B к паре `(session, cause)` с уникальным индексом на `(chat_id, message_id)`. Состояние строится из журнала, `r` берётся из номера последней учтённой записи. Отправлять карточку один раз на версию ресурса, после этого править данные при росте ревизии. Записывать ID после подтверждения Telegram, а ревизию после успешного send/edit. Длительную доставку запускать после записи событий, использовать порядок очереди Фазы 1 и повторную свёртку в `Recovery`. При неопределённом исходе первоначального send возможна дублирующая карточка, к решению допускается только сохранённая актуальная привязка.
- [ ] Собрать `OwnerView` через существующий `Registry` в `bootstrap`, подключить A при открытии, после раунда, эскалации, согласования и закрытия. Для B подключить `confirm_action`, решение и таймаут. Использовать одинаковую текстовую проекцию для подписи и отката. Документировать выбор режима в `.env.example`.
- [ ] Повторить тесты → GREEN, проверить `poetry run lint-imports`. Commit `feat: publish and recover owner widget updates`.

### Task 5 — Один обработчик решений для текста и виджета

**Files** — `src/envoy/app/{escalation,widget_queries,processor,session_flow}.py`, `src/envoy/security/inbound.py`, `tests/integration/test_widget_escalation.py`.

**Interfaces** — `EscalationResolver.resolve(session: SessionId, user_id: int, cause: int, choice: Literal["approve", "decline"]) -> Literal["recorded", "stale", "expired", "forbidden"]` (async). `WidgetQueryFlow.handle(update: WidgetQueryUpdate) -> None` (async). `SessionFlow.on_callback` остаётся входом для кнопок Фазы 1 и делегирует в тот же `EscalationResolver`.

- [ ] Добавить тесты. Для двух update ID с одним `a:42`, пары callback/widget и гонки `a:42`/`d:42` проверять `assert count(OwnerResolved, cause=42) == 1` и максимум одно разрешённое действие. При `now == expires_at`, чужом владельце из allowlist, перепутанном чате, устаревшем сообщении, неподдерживаемом типе или невалидных данных — ни одного нового действия, ответ на запрос с ID попытки. При задержанном outbound очередь сообщения не блокирует ответ виджету. Если `Authorizer` вернул `Deny`, подтверждение не отправляет действие контрагенту.
- [ ] `poetry run pytest tests/integration/test_widget_escalation.py -q` → RED, общего входа нет.
- [ ] Принимать только `a:<cause>` или `d:<cause>` с каноническим неотрицательным ASCII-числом для B. `InboundFilter` фильтрует владельца и допустимый вид апдейта, обработчик сверяет точную пару `(chat_id, message_id)` и владельца с записью сессии. Под тем же замком сессии, что у текстового callback и timeout job, перечитать pending эскалацию и серверное время, проверить `Clock.now() < expires_at`, записать один `OwnerResolved` по `(session, cause)`. Продолжение действия использует тот же повторный `Authorizer` и восстановление, что в Фазе 1.
- [ ] Разделить запись решения и сетевые эффекты. В течение 10 секунд от получения попытаться записать решение и ответить через `answerWidgetQuery`. Ограничить ожидание замка и начать запись только до истечения этого бюджета. Если запись уже началась и итог к этому сроку неизвестен, отправить нейтральный ответ и сверить журнал перед любым повтором. Bot API не передаёт время нажатия в `WidgetQuery`, поэтому `received_at + 15 s` — локальная верхняя граница бюджета, а Telegram может отклонить ответ раньше. Повтор получает отказ или сохранённый результат. Если Telegram сообщает об истечении ID, прекратить попытки ответа и восстановить только уже записанное решение.
- [ ] Повторить тест → GREEN. Commit `feat: authorize widget and callback decisions through one flow`.

### Task 6 — Дневная сводка и ссылка на топик

**Files** — `src/envoy/app/{daily_summary,widget_queries,runtime}.py`, `src/envoy/widgets/owner_view.py`, `src/envoy/persistence/owner_views.py` и миграция при необходимости, `tests/integration/test_daily_owner_summary.py`.

**Interfaces** — `DailySummary.run_due(now: datetime) -> None` (async) вызывается тем же воркером, что и `Scheduler.due`. `OwnerViewStore` дополняется состоянием доставки по `(owner_id, local_date)` и записью `SummaryWidgetRef(target: OwnerTarget, day: date, ref: WidgetRef, rows: tuple[SummaryRow, ...])`, доступной по `(chat_id, message_id)`. `WidgetQueryFlow` принимает `l:<r>:<index>` только для совпавшей сводки.

- [ ] Добавить тесты. Два запуска после 20:00 с рестартом дают одну завершённую сводку на владельца и дату. До 20:00 и без активных сделок отправок нет. При 12 сделках `assert len(widget_rows) <= 10`, `assert len(chat_data.encode("utf-8")) <= 1024`, `assert total_rows_rendered == 12`. Запрос совпавшей строки даёт одну ссылку владельцу. Чужой запрос, старый `r`, строка без топика и индекс за пределами списка ссылок не отправляют.
- [ ] `poetry run pytest tests/integration/test_daily_owner_summary.py -q` → RED, отсутствует поставка сводки.
- [ ] Использовать часы и часовой пояс Фазы 1, `SessionRepository.list_active()` и проекции журнала. Группировать сессии по владельцу, сохранять ID топика сводки и отмечать доставку каждой части. При рестарте в текущие сутки дослать неподтверждённые части; для доставки с неизвестным результатом действует ограничение Telegram из Task 4. Выбирать префикс из максимум 10 строк, пока целиком помещаются JSON и caption. Остальные строки отправлять штатным текстом; при пустом префиксе делегировать всю сводку текстовому представлению.
- [ ] Ссылку строить существующим `topic_link` из сохранённого топика сессии. Связать `l:<r>:<index>` с сохранённым порядком строк и проверенным владельцем, подтвердить запрос по дедлайну Task 5 и доставить ссылку в ту же личку с дедупликацией по query ID. Выбирать прямое открытие или запрос по результату Task 1.
- [ ] Повторить тест → GREEN, проверить обновление PostgreSQL до `head` на данных Фазы 1. Commit `feat: deliver daily owner summaries and topic links`.

### Task 7 — Приёмка Фазы 1б

**Files** — `tests/integration/test_meeting_e2e.py`, `tests/redteam/` (существующий раннер Фазы 1), отчёт о ручной проверке из Task 1.

- [ ] Расширить сквозной тест параметром `owner_view=text|widget`. Ожидать равные события решения, одно принятие, чек-лист и `Closed(fulfilled)`. Проверить, что контрагент получает только сообщения Фазы 1. Добавить `test_widget_state_and_caption_exclude_mandate_canaries` и сценарий, где текст и виджет кликают одновременно. Тест отслеживает реальные аргументы Bot API, код HTML, ответы на запросы и лог приложения.
- [ ] `make up && make migrate && make migrate && make test && make lint` → все команды успешны. Выполнить существующую проверку покрытия ядра из Фазы 1 с порогом 90 % по веткам.
- [ ] На тестовом сервере пройти сценарий A → B → A с одобрением, отказом, таймаутом и повторным нажатием. Проверить изменение `chat_data`, текстовую подпись и кнопки на клиенте без поддержки, D с открытием топика и запросом ссылки, откат при ошибке отправки. Зафиксировать результаты и версии клиентов в отчёте. Фаза закрывается после ручной проверки.
- [ ] Commit `test: verify phase 1b owner widget acceptance`.

## Самопроверка плана

| Требование спецификации | Задачи |
|---|---|
| `OwnerView`, две реализации, конфигурация, откат | 2, 4, 7 |
| A, B, D и версии HTML, порог 3800 байт | 3, 4, 6, 7 |
| `WidgetProjection`, канарейки, только личка владельца | 2, 4, 5, 7 |
| Fake Bot API, размеры, ошибки, 15 секунд | 1, 5 |
| Повторы, старые ревизии, таймаут, восстановление из журнала | 3, 4, 5 |
| Сценарий Фазы 1 в двух режимах и ручной чек-лист | 1, 7 |
