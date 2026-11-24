# Проверка фазы 1 — 2026-11-24

Фаза 1 («встреча 1:1 с человеком») реализована в ветке `feat/phase-1-meeting` от `699b70d`. Коммитов нет: изменения оставлены в рабочем дереве, коммит выполняет пользователь.

## Результат

Выполнены все 13 задач плана: домен сессии и кодек событий, компилятор мандата и сетка кандидатов, движок (time-dependent + Парето), `Authorizer` с цепочкой правил и проекции, `InboundFilter` с вырезанием платёжных реквизитов, порт LLM с адаптером Claude и бюджетом, хранилища состояния (memory + PostgreSQL, миграция `phase1`), business/топики/чек-лист/колбэки в адаптере и fake Bot API, онбординг и мандат через чат, поток сессии с эскалациями и парковкой, исполнение по чек-листу, red-team каталог, рантайм с восстановлением и сервис `app` в Compose.

## Проверки

| Проверка | Результат |
|---|---|
| Полный pytest, Windows / Python 3.15.2 (включая PostgreSQL через testcontainers) | 434 passed, 21 deselected (`live_llm`) |
| То же в случайном порядке (`pytest-randomly`) | 434 passed |
| Полный набор в Compose (Linux): black, ruff, mypy, import-linter, pytest, гейты покрытия | всё зелёное, 434 passed |
| mypy strict | 93 исходных файла без ошибок |
| import-linter | 4 контракта соблюдены (адаптер Claude и `anthropic` — только из `bootstrap`) |
| Покрытие ветвей: engine / journal / crypto | 100 % / 90 % / ветвей нет |
| Миграции | `alembic check` против моделей, downgrade→upgrade, повторный upgrade идемпотентен; head `a3c71e0b9d42` |
| Compose | `app` стартует после `migrate` (`python -m envoy`), повторный migrate без изменений |
| Движок, 8 issues, 432 000 ячеек | решение раунда < 2 с (вместе с компиляцией) |
| Red-team | 24 атаки (по 3 на 8 категорий): 0 действий вне мандата, 0 утечек; самопроверка детектора на подделанных отчётах |

Приёмка: `test_meeting_scenario_reaches_checklist_and_fulfilled` (мандат из чата → `/envoy` → торг → эскалация → одобрение → чек-лист → обе отметки → `Closed(fulfilled)`, журналы сессии и владельца проходят `JournalVerifier`); `test_process_killed_after_decision_before_send_resumes_and_sends_once` и `test_process_killed_with_pending_escalation_keeps_timeout_job` на PostgreSQL.

## Решения и отклонения от плана

- **Структурированный вывод вместо forced tool.** На `claude-opus-5-5` `tool_choice` `any`/`tool` возвращает 400, поэтому `ClaudeLanguageModel` использует `output_config.format` (json_schema) с effort `low` и серверный `fallbacks: "default"`. LLM заполняет простые wire-схемы (`IntentWire`, `MandateDraftWire`); перевод в `Intent`/`Mandate` детерминированный, результат повторно валидируется Pydantic.
- **Кассеты Claude** — сохранённые ответы Messages API, которые отдаёт локальный HTTP-сервер реальному SDK (ключа API нет, SDK 2.x работает на `httpx2`). Живой прогон — ночной workflow `redteam-nightly.yml` с секретом `ANTHROPIC_API_KEY`; локально не запускался.
- **Кодек событий** кодирует `model_dump(mode="python")` каноническим CBOR и декодирует с восстановлением `timedelta`: JSON-режим терял типы значений оффера.
- **Схема мандата:** `datetime/place/enum` — явные списки `choices`, `money/integer/duration` — диапазон с шагом; шаблон `meeting` требует issues `time` и `place`; значения ограничений валидируются в схеме. `max_deposit` сохранён (спека §5.2). У мандата добавлен статус `discarded`.
- **Тело мандата в PostgreSQL шифруется** мастер-ключом (в нём резерв и веса).
- **Лимит частоты** во входном фильтре проверяется подменой `loop.time` (aiolimiter использует монотонные часы цикла, time-machine их не двигает).
- **Сборка конвейера** вынесена в `app/pipeline.py` и общая для `bootstrap` и тестов; общие шаги сессии — `app/steps.py` (`SessionSteps.once`, авторизация, закрытие).
- **Окно 24 часа:** если окно закрыто при `/envoy`, раскрытие и первый ход ждут первого входящего; парковка — `Decided(allow)` + `Escalated(reply_window_closed)`, доставка ровно один раз при следующем входящем или при восстановлении.
- Дедлайн сессии не блокирует открытие чек-листа уже согласованной сделки; лимит сделок в сутки — скользящие 24 часа.
- Таймаут и отклонение эскалации закрывают сессию даже если отказ контрагенту не удалось доставить.
- Схема business-методов сверена с официальной документацией Bot API 10.5 (22.11.2026), итог в `tests/fixtures/business_schema.json`.

## Известные ограничения

- В раскрытии владелец назван обобщённо («владельца аккаунта»): имя из `business_connection.user` в DTO пока не переносится.
- Реальный Telegram и живой LLM не прогонялись.
- Один прогон Compose-тестов до исправления e2e-хелпера записал тестовые строки в dev-базу `envoy_phase0` (том `postgres-data`); при необходимости удалить том.
