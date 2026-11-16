# Проверка фазы 0 — 2026-11-16

Фаза 0 реализована в ветке `feat/phase-0-foundation`. Коммитов, слияний и отправки в remote не было: по указанию пользователя коммит выполняет он сам. Исходный HEAD — `686898d`.

## Результат

Реализованы все 12 задач: каркас Poetry/CI, порты и реестры, доменные типы и канонический CBOR, Ed25519 и ChaCha20Poly1305, схема мандата и коммитменты, подписанный журнал и криптоудаление, PostgreSQL и Alembic, независимый HTTP fake Bot API, очередь с лимитами/повторами, aiogram-адаптер и реестр методов, настройки/маскирование/композиция, Docker/Compose/Make.

## Выполненные проверки

| Проверка | Результат |
|---|---|
| Полный pytest, Windows / Python 3.15.2 | 151 passed, 13.69 с |
| Полный pytest, Linux Compose / Python 3.15.2 | 151 passed, 9.44 с |
| Снимки Settings и JSON Schema мандата | 2 passed |
| Black | 81 файл без изменений |
| Ruff | без замечаний |
| mypy strict | 50 исходных файлов без ошибок |
| import-linter | 4 контракта соблюдены |
| Poetry lock | `poetry check --lock` успешно |
| Покрытие journal: ветви | 90% — 18/20 |
| Покрытие crypto: строки | 100%; условных ветвей нет |
| journal + crypto, совокупное покрытие строк и ветвей | 98% |
| Весь envoy, совокупное покрытие строк и ветвей | 92% |
| Compose base/dev/test | конфигурации валидны |
| PostgreSQL | healthcheck, рестарт и повторные миграции успешны |
| Миграции | upgrade, downgrade/upgrade, повторный upgrade, соответствие моделям проверены |
| Runtime | без root; нет Poetry, pytest, Black и `/app/.env`; импорт приложения успешен |
| Зависимости | проверены через `poetry show --latest`, добавлены стабильные версии без prerelease |

Установка Make в Windows отменена пользователем и не повторялась. Все пять рецептов проверены через `make -n` в Linux-образе; соответствующие команды Compose выполнены напрямую. CI содержит запуск Make и проверки runtime; GitHub Actions удалённо не запускался, поскольку push не выполнялся.

## Итоговое ревью и исправления

Независимый reviewer проверил весь исходный код и каждый пункт Review Focus. Найдено шесть существенных замечаний и одно замечание о timezone, переклассифицированное в существенное: валидная модель не должна ломаться при создании коммитмента. Все замечания исправлены; регрессионные тесты сначала воспроизвели ошибки, затем прошли вместе с полными наборами Windows и Linux.

1. Запись и ключ публикуются атомарно через `JournalCommitter.commit(entry, key)`. PostgreSQL использует одну транзакцию; отмена операции не оставляет запись без ключа. In-memory не передаёт управление между изменениями.
2. Тестовый Compose использует отдельный `postgres-test` в tmpfs. Фикстуры блокируют разрушительные проверки на БД с адресом и именем рабочей БД.
3. Оба лимитера проверяются перед списанием любого токена, поэтому ожидание глобального лимита не нарушает интервал внутри чата.
4. Polling повторяет временные ошибки с ограниченной экспоненциальной задержкой; закрытие прерывает ожидание. Лимит исходящих повторов остаётся равным пяти.
5. Маскируются все секретные поля настроек, в том числе вложенные plain-string значения `ENVOY_TEST_DATABASE_URL`, пароль и пользователь PostgreSQL.
6. Номер карты с соседними цифровыми ссылками обнаруживается и маскируется; Luhn проверяет python-stdnum.
7. Naive datetime отвергается при валидации Offer, временных границ issue и дедлайна мандата.

Отложенных замечаний нет. Повторный reviewer не запускался: исправления подтверждены регрессионными тестами и полными наборами.

## Решения и отклонения от плана

Ниже сохранён полный журнал решений исполнителя. Более позднее прямое указание пользователя запрещает коммиты и отменяет первоначальный процессный план консолидированного коммита. Конструктор UUIDv7 использует стандартную библиотеку без неработающих параметров clock/rng; детерминированные идентификаторы предоставляет отдельная реализация порта.

- Ruling: Use current checkout and a feature branch after user rejected the setup stall; no worktree created.
- Ruling: Skill shell helpers cannot run under Windows sandbox (signal pipe denied); maintain equivalent briefs and ledger with native tools.
- Ruling: UuidV7Generator uses stdlib uuid7() without unused clock/rng parameters. Standard library owns secure time/entropy, and deterministic IDs use SequentialIdGenerator. Cost: constructor differs from the plan.
- Ruling: AiogramAdapter is a raw MessageSender; bootstrap wraps it in OutboundQueue and exposes only QueuedSender through Application. This removes the cyclic Adapter(queue)/Queue(adapter) constructor dependency. Cost: direct adapter use bypasses the queue, as contract tests intentionally do.
- Ruling: aioresponses 0.7.9 omits aiohttp 3.14 stream_writer. Tests supply the real ClientResponse constructor via functools.partial and an autospec AbstractStreamWriter, without changing production HTTP or pinning old dependencies. Cost: test compatibility helper may become unnecessary after upstream updates.
- Ruling: GNU Make installer was canceled by the user; do not retry it. Run the same Compose commands directly on Windows and verify Make recipes in CI/Linux. Cost: host make commands cannot be executed on this Windows installation.
- Ruling: Runtime image starts from a clean Python slim stage and copies only main venv/src/alembic config, excluding Poetry/build tools. Cost: separate runtime layer base, required for tool isolation.
- Ruling: git commits are consolidated after final review rather than creating intermediate commits with partially assembled fixtures. Cost: coarser commit history; test evidence is retained per task in this ledger until finishing.
- Final: Ruling: Add JournalCommitter.commit(entry,key) for atomic entry+key publication; JournalWriter.append remains the raw store contract. Memory commits without yielding; PostgreSQL uses one transaction. Cost: JournalAuthor's writer parameter now requires atomic committer capability; composition root binds vault/store.
- Final: Ruling: Test Compose uses a separate tmpfs PostgreSQL instance and test migration service. Fixtures reject the application database identity. Cost: an extra temporary PostgreSQL container during tests.
- Final: Ruling: Both aiolimiter buckets must have capacity before either token is consumed; bounded cooperative admission polling avoids stale reservations. Cost: at most one small admission interval of extra scheduling delay; no custom token bucket.
- Final: Ruling: Polling retries recoverable errors indefinitely with capped exponential waits, interrupted by close/cancellation. Outbound messages retain their separate five-attempt bound. Cost: intake waits for recovery during outages instead of terminating silently.
- Final: Ruling: Reject naive datetimes at model validation, consistently for Offer, datetime issues and deadlines. Cost: callers must provide timezone-aware dates, matching canonical serialization.
- Final: Ruling: Live Docker/restart behavior and lock latest/stability checks were outside the reviewer's assignment; parent verifies them directly before completion. Cost: none; no unreviewed behavior is waived.
