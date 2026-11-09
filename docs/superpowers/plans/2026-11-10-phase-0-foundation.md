# Envoy Phase 0: Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Каркас репозитория с CI, доменными типами, схемой мандата, подписанным журналом с хэш-цепочкой и верификатором, портами, fake Telegram Bot API и адаптером с очередью и лимитами.

**Architecture:** Hexagonal: `domain` без зависимостей, чистая логика поверх него, адаптеры реализуют порты, конкретные классы создаются только в `app/bootstrap`. Реализации выбираются через реестр `name -> factory` и конфигурацию окружения. Всё, что есть в зрелых библиотеках (лимиты, повторы, логирование, конфигурация, миграции, криптография), берётся из них.

**Tech Stack:** Docker + Compose, Python (актуальная стабильная), Poetry, asyncio, Pydantic v2, pydantic-settings, cryptography, cbor2, SQLAlchemy 2 async + asyncpg + Alembic, aiogram, aiohttp, aiolimiter, tenacity, structlog, pytest, pytest-asyncio, pytest-mock, aioresponses, time-machine, syrupy, polyfactory, Hypothesis, testcontainers, black, ruff, mypy, import-linter.

**Spec:** `docs/superpowers/specs/2026-11-10-envoy-design.md` (§4, §5.1-5.2, §9.5, §10.3, §12.5, §13, §14 Фаза 0)

## Global Constraints

- Без комментариев в коде: ни inline, ни блочных, ни docstring, ни закомментированного кода; без debug-вывода, заглушек, «demo»-остатков (§15, CLAUDE.md).
- Только актуальные стабильные версии; зависимости добавляются через `poetry add <pkg>` / `poetry add --group dev <pkg>` без пина и без `--allow-prereleases`; после добавления проверить `poetry show --latest`, что версии стабильные и актуальные. Менеджер зависимостей — Poetry, форматирование — black (ruff не форматирует).
- Структура `src/envoy/{domain,mandate,engine,protocol,crypto,privacy,multilateral,execution,payments,security,journal,llm,telegram,miniapp_api,persistence,app}` (§4.3); в Фазе 0 создаются только нужные пакеты.
- Зависимости только внутрь (§4.2): `domain` ни от чего не зависит; `journal`, `crypto`, `mandate`, `engine`, `protocol`, `privacy`, `multilateral`, `execution` не импортируют `telegram`, `llm`, `persistence`, `sqlalchemy`, `aiogram`; конкретные реализации импортируются только в `envoy.app.bootstrap` (§4.4).
- Порты — `typing.Protocol`; узкие (`JournalWriter` отдельно от `JournalReader`, `MessageSender` отдельно от `UpdateSource`); `isinstance` на конкретных классах запрещён; иерархии глубже двух уровней запрещены.
- У каждого порта: контрактный набор тестов, in-memory/fake реализация, запись в реестре.
- `mypy --strict`, `ruff`, `import-linter` без замечаний; покрытие веток `journal`, `crypto` не ниже 90 %.
- Журнал: `entry_hash = H(prev_hash ‖ seq ‖ type ‖ H(payload_ciphertext) ‖ author ‖ ts ‖ mandate_commitment)`, каждая запись подписана Ed25519 ключом агента (§10.3).
- Сериализация канонических байтов: `cbor2` в детерминированной форме (§6.1).
- Telegram: исходящие через очередь с token bucket (глобально и на чат), повтор с экспоненциальной паузой и `retry_after` (§12.5).
- Тестовые инструменты (готовые, свои моки не пишутся): `pytest-mock` (`mocker`, `AsyncMock`/`create_autospec` с `spec`/`autospec` для портов — мок обязан повторять сигнатуру порта), `aioresponses` (HTTP-слой `aiohttp`/`aiogram` в юнит-тестах адаптера), `time-machine` (время), `pytest-recording` + `vcrpy` (записанные ответы внешних API, режим `none` в CI), `syrupy` (снапшоты схем и сериализации), `polyfactory` (фабрики Pydantic-моделей), `pytest-randomly` (случайный порядок), `Hypothesis`, `testcontainers`. Мокаются только границы (порты, сеть, время); внутренняя чистая логика не мокается. Порт-fake и мок не смешиваются: контрактные тесты идут на fake, поведение вызывающего кода — на `autospec`-моках.
- Запуск и тесты воспроизводимы через Docker Compose; compose-файлы разнесены по ролям (`compose.yaml` — общее, `compose.dev.yaml`, `compose.test.yaml`); образ `runtime` без dev-инструментов и без root.
- Все секреты и важные данные (токены, ключи, `database_url`, `master_key`) лежат только в `.env` (в `.gitignore` и `.dockerignore`); в репозитории только `.env.example` с пустыми значениями; в образ `.env` не копируется, в контейнеры попадает через `env_file`; в логах — только `SecretStr`/маскирование;
- Секреты только из окружения; в репозитории и логах их нет (§13).

## Review Focus

- Журнал из одной записи и пустой журнал: верификатор не падает, пустой журнал валиден.
- Запись с `payload` нулевой длины и с очень большой (1 МиБ): хэш и подпись считаются, шифрование работает.
- Крипто-удаление ключа записи: цепочка остаётся валидной, чтение содержимого даёт явную ошибку `ContentErased`, а не мусор.
- Поддельный `retry_after` от Telegram (ноль, отрицательное, очень большое): очередь не зацикливается и не спит бесконечно (потолок ожидания).
- Одновременная запись в журнал двумя корутинами: `seq` без дыр и дублей, вторая запись с тем же `seq` отклоняется хранилищем.
- Мандат с весами, дающими сумму `1 ± 1e-9`, и с отрицательным весом: первый принимается, второй отклоняется схемой.
- Повторный запуск `migrate` и рестарт `postgres`: миграции идемпотентны, сервисы ждут `service_healthy`/`service_completed_successfully`, а не стартуют вслепую.
- Ключ окружения с приватным полем в событии лога: поле маскируется на уровне логгера, а не вызывающего кода.

---

## File Structure

| Путь | Ответственность |
|---|---|
| `pyproject.toml`, `poetry.lock`, `.importlinter`, `.github/workflows/ci.yml` | Сборка, линтеры, контракты слоёв, CI |
| `src/envoy/domain/` | `ids.py`, `offer.py`, `events.py`, `journal.py` (типы записи), `errors.py` |
| `src/envoy/ports/` | Общие порты: `clock.py`, `rng.py`, `ids.py`, `registry.py` |
| `src/envoy/crypto/` | `keys.py` (Ed25519), `aead.py` (шифрование содержимого), `canonical.py` (CBOR) |
| `src/envoy/mandate/` | `schema.py` (Pydantic), `commitment.py` |
| `src/envoy/journal/` | `ports.py`, `chain.py`, `verifier.py`, `memory.py`, `contract.py` |
| `src/envoy/persistence/` | `models.py`, `journal_store.py`, `migrations/` |
| `src/envoy/telegram/` | `ports.py`, `fake/` (fake Bot API), `outbound.py` (очередь), `adapter.py`, `dependencies.py` |
| `src/envoy/observability/` | `logging.py` (structlog + маскирование) |
| `src/envoy/app/` | `settings.py`, `bootstrap.py` |
| `docker/`, `compose*.yaml`, `Makefile` | Образы (стадии base/deps/dev/runtime), стеки dev/test, команды |
| `tests/{unit,property,contract,integration}/` | Тесты по уровням §13 |

---

### Task 1: Каркас, линтеры, CI

**Files:**
- Create: `pyproject.toml`, `.importlinter`, `.github/workflows/ci.yml`, `src/envoy/__init__.py`, `src/envoy/domain/__init__.py`, `tests/unit/test_layers.py`
- Modify: `README.md`

**Interfaces:**
- Produces: рабочие команды `poetry run ruff check`, `poetry run mypy`, `poetry run lint-imports`, `poetry run pytest`.

- [ ] **Step 1: Инициализировать проект**

Run: `poetry init --name envoy --no-interaction`, `poetry config virtualenvs.in-project true --local`, `poetry env use <путь к актуальному стабильному python>` (системный Python 3.10 не подходит; установить актуальный через pyenv или пакетный менеджер ОС и указать в `[tool.poetry.dependencies] python`); затем `poetry add pydantic pydantic-settings cryptography cbor2 structlog aiolimiter tenacity aiohttp aiogram "sqlalchemy[asyncio]" asyncpg alembic` и `poetry add --group dev pytest pytest-asyncio hypothesis pytest-cov pytest-mock pytest-randomly pytest-recording aioresponses time-machine syrupy polyfactory "testcontainers[postgres]" black ruff mypy import-linter`.
Expected: `poetry.lock` создан, `poetry show` без pre-release версий.

- [ ] **Step 2: Настроить `pyproject.toml`**

`[tool.black] line-length = 100`, `target-version` — актуальная минорная версия Python; `[tool.mypy] strict = true`, `[tool.ruff.lint] select = ["E","F","I","UP","B","ASYNC","PT","ERA"]` (ERA ловит закомментированный код); `[tool.pytest.ini_options] asyncio_mode = "auto"`, `--cov=envoy --cov-branch`; `[tool.coverage.report]` с порогом 90 для `envoy/journal`, `envoy/crypto` через `fail_under` отдельным шагом CI.

- [ ] **Step 3: Написать падающий тест слоёв `tests/unit/test_layers.py::test_import_contracts_hold`**

```python
import subprocess
import sys


def test_import_contracts_hold() -> None:
    result = subprocess.run([sys.executable, "-m", "importlinter.cli"], capture_output=True, text=True)
    assert result.returncode == 0, result.stdout
```

- [ ] **Step 4: Написать `.importlinter`**

Контракты `layers` (forbidden): `envoy.domain` не импортирует ничего из `envoy.*`; чистые пакеты (`journal`, `crypto`, `mandate`, `engine`, `protocol`, `privacy`, `multilateral`, `execution`) не импортируют `envoy.telegram`, `envoy.llm`, `envoy.persistence`, `aiogram`, `sqlalchemy`; `envoy.app.bootstrap` — единственный модуль, которому разрешён импорт реализаций (контракт `forbidden` на `envoy.telegram.adapter`, `envoy.persistence.*` для всех модулей, кроме него).

- [ ] **Step 5: Запустить тест**

Run: `poetry run pytest tests/unit/test_layers.py -v`
Expected: PASS.

- [ ] **Step 6: CI `.github/workflows/ci.yml`**

Шаги: `actions/setup-python`, установка Poetry (`pipx install poetry`), `poetry check --lock`, `poetry install`, `poetry run black --check .`, `poetry run ruff check`, `poetry run mypy`, `poetry run lint-imports`, `poetry run pytest` (тесты с БД накатывают `alembic upgrade head`). Версии actions проверить на актуальные стабильные.

- [ ] **Step 7: Commit**

```bash
git add pyproject.toml poetry.lock .importlinter .github src tests README.md
git commit -m "chore: project scaffold with lint, typing and layer contracts"
```

---

### Task 2: Общие порты и реестр

**Files:**
- Create: `src/envoy/ports/{clock,rng,ids,registry}.py`, `tests/unit/test_registry.py`, `tests/unit/test_ports_defaults.py`

**Interfaces:**
- Produces:
  - `class Clock(Protocol): def now(self) -> datetime` (UTC aware); `SystemClock`, `FixedClock(start: datetime)` с `advance(delta: timedelta) -> None`.
  - `class Rng(Protocol): def token_bytes(self, n: int) -> bytes`; `SystemRng`, `SeededRng(seed: int)`.
  - `class IdGenerator(Protocol): def new_id(self) -> UUID` (UUIDv7); `UuidV7Generator(clock: Clock, rng: Rng)`, `SequentialIdGenerator`.
  - `class Registry(Generic[T]): def register(self, name: str, factory: Callable[..., T]) -> None; def create(self, name: str, **kwargs: Any) -> T`; повторная регистрация имени → `DuplicateRegistration`, неизвестное имя → `UnknownImplementation(name, known)`.

- [ ] **Step 1: Тесты** — `test_registry_creates_registered_factory`, `test_registry_rejects_duplicate_name`, `test_registry_unknown_name_lists_known`, `test_fixed_clock_advances_only_on_demand`, `test_uuid_v7_is_time_ordered` (два id при `advance(1s)` упорядочены), `test_seeded_rng_is_deterministic`.
- [ ] **Step 2:** Run `poetry run pytest tests/unit -k "registry or ports_defaults" -v` → FAIL (ModuleNotFoundError).
- [ ] **Step 3:** Реализовать сигнатуры выше. UUIDv7 — из стандартной библиотеки (`uuid.uuid7`), если доступна в актуальном Python, иначе пакет `uuid-utils`; свою битовую сборку не писать.
- [ ] **Step 4:** Run те же тесты → PASS; `poetry run mypy` чисто.
- [ ] **Step 5:** Commit `feat: clock, rng, id ports and implementation registry`.

---

### Task 3: Доменные типы

**Files:**
- Create: `src/envoy/domain/{ids,offer,events,journal,errors}.py`, `tests/unit/test_domain.py`

**Interfaces:**
- Consumes: ничего (домен изолирован).
- Produces:
  - `NewType`-идентификаторы `OwnerId`, `AgentId`, `SessionId`, `MandateId` (на UUID), `OfferHash` (bytes).
  - `class Offer(BaseModel, frozen=True)`: `values: Mapping[str, IssueValue]`; `IssueValue = Decimal | int | str | datetime | timedelta`; `def canonical_bytes(self) -> bytes`; `def hash(self) -> OfferHash` — SHA-256 от канонических байтов; порядок ключей не влияет.
  - `class JournalEntryType(StrEnum)` с `session_opened`, `mandate_attested`, `offer_sent`, `offer_received`, `escalated`, `decided`, `closed`.
  - `class JournalEntry(BaseModel, frozen=True)`: `seq: int, prev_hash: bytes, type: JournalEntryType, payload_ciphertext: bytes, author: AgentId, ts: datetime, mandate_commitment: bytes, entry_hash: bytes, signature: bytes`.
  - Ошибки: `ContentErased`, `ChainBroken(seq: int)`, `SignatureInvalid(seq: int)`, `SequenceConflict(seq: int)`.

- [ ] **Step 1: Тесты** — `test_offer_hash_ignores_key_order`, `test_offer_hash_differs_on_value_change`, `test_offer_is_immutable`, `test_offer_rejects_float`.
- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3:** Реализовать. `canonical_bytes` делегирует `envoy.crypto.canonical.encode` — зависимость домена от `crypto` запрещена, поэтому каноническая кодировка выносится в `domain/canonical.py` на `cbor2` (`canonical=True`), а `crypto` импортирует её оттуда.
- [ ] **Step 4:** Run → PASS.
- [ ] **Step 5:** Commit `feat: domain identifiers, offer and journal entry types`.

---

### Task 4: Ключи, подпись, шифрование содержимого

**Files:**
- Create: `src/envoy/crypto/{keys,aead}.py`, `tests/unit/test_crypto.py`, `tests/property/test_crypto_props.py`

**Interfaces:**
- Consumes: `Rng` (Task 2).
- Produces:
  - `class SigningKey`: `@classmethod generate(cls) -> SigningKey`; `public(self) -> PublicKey`; `sign(self, data: bytes) -> bytes`; `to_bytes()/from_bytes()`.
  - `class PublicKey`: `verify(self, data: bytes, signature: bytes) -> bool`; `fingerprint(self) -> str` (SHA-256 hex, 16 первых символов); `to_bytes()/from_bytes()`.
  - `class ContentCipher`: `generate_key() -> bytes`; `encrypt(key: bytes, plaintext: bytes, aad: bytes) -> bytes` (nonce внутри шифртекста); `decrypt(key: bytes, ciphertext: bytes, aad: bytes) -> bytes`, при неверном ключе/AAD — `ContentErased`-совместимое `DecryptionFailed`.
  - Реализация: `cryptography` (`Ed25519PrivateKey`, `ChaCha20Poly1305`); собственной арифметики нет.

- [ ] **Step 1: Тесты** — `test_sign_verify_roundtrip`, `test_verify_rejects_modified_data`, `test_verify_rejects_other_key`, `test_fingerprint_stable_and_16_chars`, `test_cipher_roundtrip`, `test_cipher_rejects_wrong_aad`, `test_cipher_empty_plaintext`; property: `test_cipher_roundtrip_any_bytes` (`binary(max_size=1_048_576)`), `test_signature_fails_on_any_single_byte_flip`.
- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3:** Реализовать на `cryptography`.
- [ ] **Step 4:** Run → PASS.
- [ ] **Step 5:** Commit `feat: ed25519 signing and chacha20poly1305 content cipher`.

---

### Task 5: Схема мандата

**Files:**
- Create: `src/envoy/mandate/{schema,commitment}.py`, `tests/unit/test_mandate.py`, `tests/property/test_mandate_props.py`

**Interfaces:**
- Consumes: `canonical` (Task 3), `Rng` (Task 2).
- Produces:
  - Pydantic-модели (frozen, `extra="forbid"`): `Mandate`, `Issue` (дискриминатор `kind`: `money|datetime|place|enum|integer|duration`), `PiecewiseLinear`, `UtilitySpec(weights: Mapping[str, float], value_functions: Mapping[str, PiecewiseLinear])`, `Predicate` — размеченное объединение `Cmp(op in {<=,>=,in,between,not_in}) | And | Or | Not | Requires`, `Reservation`, `StrategySpec(kind, params)`, `Limits`, `Escalation`, `template: Literal[meeting,purchase,advertisement,exchange,join,freelance]`.
  - Валидаторы модели: все `w_i >= 0`, `abs(sum(w) - 1) <= 1e-9`, ключи `weights` и `value_functions` совпадают с id issues, `Cmp` ссылается только на объявленные issues.
  - `Mandate.json_schema() -> dict` (из `model_json_schema`), `Mandate.canonical_bytes() -> bytes`.
  - `def commit(mandate: Mandate, rng: Rng) -> MandateCommitment` где `MandateCommitment(salt: bytes, digest: bytes)`; `def verify_reveal(commitment_digest: bytes, mandate: Mandate, salt: bytes) -> bool`. `digest = SHA-256(salt ‖ canonical_bytes)`.

- [ ] **Step 1: Тесты** — `test_valid_meeting_mandate_parses`, `test_weights_must_sum_to_one`, `test_negative_weight_rejected`, `test_weights_sum_tolerance_1e_9_accepted`, `test_unknown_issue_in_constraint_rejected`, `test_extra_field_rejected`, `test_mandate_is_immutable`, `test_json_schema_contains_all_issue_kinds`, `test_commit_reveal_roundtrip`, `test_reveal_with_modified_mandate_fails`; property: `test_canonical_bytes_independent_of_construction_order`.
- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3:** Реализовать на Pydantic v2 (`model_validator`, `Field(discriminator="kind")`); `eval`/выражения не используются.
- [ ] **Step 4:** Run → PASS.
- [ ] **Step 5:** Commit `feat: mandate schema with commitment`.

---

### Task 6: Журнал — цепочка, подписи, верификатор

**Files:**
- Create: `src/envoy/journal/{ports,chain,verifier,memory,contract}.py`, `tests/contract/test_journal_store.py`, `tests/unit/test_journal_chain.py`, `tests/property/test_journal_props.py`

**Interfaces:**
- Consumes: `SigningKey`, `PublicKey`, `ContentCipher` (Task 4), `JournalEntry` и ошибки (Task 3), `Clock` (Task 2).
- Produces:
  - `class JournalWriter(Protocol): async def append(self, entry: JournalEntry) -> None` (при занятом `seq` → `SequenceConflict`).
  - `class JournalReader(Protocol): async def read(self, session: SessionId, start: int = 0) -> list[JournalEntry]; async def head(self, session: SessionId) -> JournalEntry | None`.
  - `class KeyVault(Protocol): async def entry_key(self, session: SessionId, seq: int) -> bytes` ; `async def erase(self, session: SessionId, seq: int) -> None`; при стёртом ключе `entry_key` бросает `ContentErased`.
  - `class JournalAuthor`: `__init__(self, session: SessionId, agent: AgentId, signing_key: SigningKey, writer: JournalWriter, reader: JournalReader, vault: KeyVault, cipher: ContentCipher, clock: Clock)`; `async def record(self, type: JournalEntryType, payload: bytes, mandate_commitment: bytes) -> JournalEntry` — берёт `head`, считает `seq`/`prev_hash`, шифрует payload ключом записи (AAD = `session ‖ seq`), считает `entry_hash` по формуле из Global Constraints, подписывает.
  - `def entry_hash(entry_fields...) -> bytes` — единственное место формулы.
  - `class JournalVerifier`: `def verify(self, entries: Sequence[JournalEntry], keys: Mapping[AgentId, PublicKey]) -> None` — бросает `ChainBroken(seq)` или `SignatureInvalid(seq)`; пустой список валиден; `async def read_payload(entry, vault, cipher) -> bytes` с `ContentErased` при стёртом ключе.
  - `InMemoryJournalStore` и `InMemoryKeyVault` реализуют все порты; `contract.JournalStoreContract` — набор тестов, параметризуемый фабрикой хранилища.

- [ ] **Step 1: Тесты**
  - Unit: `test_empty_journal_is_valid`, `test_single_entry_verifies`, `test_three_entries_chain_verifies`, `test_modified_payload_ciphertext_detected` (`ChainBroken`), `test_modified_ts_detected`, `test_reordered_entries_detected`, `test_removed_middle_entry_detected`, `test_foreign_signature_detected` (`SignatureInvalid`), `test_zero_length_payload_ok`, `test_one_mib_payload_ok`, `test_erased_key_keeps_chain_valid_and_read_raises_content_erased`, `test_concurrent_appends_produce_gapless_seq` (`asyncio.gather` из двух `JournalAuthor` на одно хранилище: итоговые `seq` — 0..n-1 без дублей, проигравший получает `SequenceConflict` и повторяет).
  - Property (`Hypothesis`): `test_any_single_byte_mutation_of_any_entry_is_detected` — генерируется журнал 1–20 записей, мутируется один байт любого поля, верификатор бросает `ChainBroken | SignatureInvalid`.
  - Contract: `JournalStoreContract` — `test_append_then_read_returns_in_order`, `test_duplicate_seq_rejected`, `test_head_of_empty_is_none`, `test_read_from_offset`.
- [ ] **Step 2:** Run `poetry run pytest tests/unit/test_journal_chain.py tests/property/test_journal_props.py tests/contract/test_journal_store.py -v` → FAIL.
- [ ] **Step 3:** Реализовать по сигнатурам. `entry_hash` — `hashlib.sha256` над `canonical.encode([...])`; `JournalAuthor.record` при `SequenceConflict` повторяет не более 3 раз, затем пробрасывает.
- [ ] **Step 4:** Run → PASS; `poetry run pytest --cov=envoy.journal --cov-branch --cov-fail-under=90`.
- [ ] **Step 5:** Commit `feat: signed hash-chain journal with verifier and crypto-shredding`.

---

### Task 7: PostgreSQL-хранилище журнала

**Files:**
- Create: `src/envoy/persistence/{models,journal_store,key_vault}.py`, `src/envoy/persistence/migrations/` (Alembic), `alembic.ini`, `tests/contract/test_pg_journal_store.py`, `tests/conftest.py`
- Modify: `.importlinter` (разрешить `persistence` только `app.bootstrap`)

**Interfaces:**
- Consumes: `JournalWriter`, `JournalReader`, `KeyVault`, `JournalStoreContract` (Task 6).
- Produces: `PgJournalStore(session_factory: async_sessionmaker[AsyncSession])` реализует `JournalWriter` и `JournalReader`; `PgKeyVault(session_factory, master_key: bytes, cipher: ContentCipher)` реализует `KeyVault` (ключи записей хранятся зашифрованными мастер-ключом; `erase` удаляет строку). Таблицы `journal_entries(session_id, seq, ...)` с `PRIMARY KEY (session_id, seq)` — уникальность даёт `SequenceConflict` на уровне БД; `entry_keys(session_id, seq, wrapped_key)`.
- Фикстура `pg_session_factory` поднимает PostgreSQL через `testcontainers` и накатывает `alembic upgrade head`.

- [ ] **Step 1: Тест** — `tests/contract/test_pg_journal_store.py` подклассом `JournalStoreContract` с фабрикой `PgJournalStore`, плюс `test_erased_key_row_removed`, `test_unique_violation_maps_to_sequence_conflict`, `test_migrations_upgrade_head_creates_schema`, `test_models_match_migrations` (`alembic check` через `alembic.command.check` не находит расхождений), `test_downgrade_base_then_upgrade_head_roundtrip`.
- [ ] **Step 2:** Run `poetry run pytest tests/contract/test_pg_journal_store.py -v` → FAIL.
- [ ] **Step 3:** `poetry run alembic init -t async src/envoy/persistence/migrations`; `env.py` берёт URL из `Settings.database_url`; первая миграция — `poetry run alembic revision --autogenerate -m "journal"`, затем проверить и поправить вручную; схема БД меняется только миграциями (`create_all` не используется нигде, включая тесты); реализовать `PgJournalStore`/`PgKeyVault`; `IntegrityError` по PK → `SequenceConflict`.
- [ ] **Step 4:** Run → PASS (Docker доступен).
- [ ] **Step 5:** Commit `feat: postgres journal store and key vault with migrations`.

---

### Task 8: Telegram-порты и fake Bot API

**Files:**
- Create: `src/envoy/telegram/ports.py`, `src/tg_fake_api/{server,state,errors}.py`, `src/tg_fake_api/__init__.py`, `tests/contract/test_fake_bot_api.py`
- Modify: `.importlinter`, `pyproject.toml` (добавить пакет `tg_fake_api` в `packages`)

**Interfaces:**
- Produces:
  - `class UpdateSource(Protocol): def updates(self) -> AsyncIterator[Update]`.
  - `class MessageSender(Protocol): async def send_message(self, chat_id: int, text: str, *, reply_markup: ReplyMarkup | None = None) -> SentMessage`.
  - `class ChatSurface(Protocol): capabilities: SurfaceCapabilities` (`private_context: bool`, `ephemeral: bool`, `checklist_native: bool`). Флаг `ephemeral` означает только «временный просмотр» (окно 15 с, доставка не гарантирована, §12.6): на ephemeral не строятся решения и таймауты. Порт ответа на callback ephemeral-сообщением вводится в Фазе 4 отдельным классом без правок ядра.
  - Доменные DTO `Update`, `SentMessage`, `ReplyMarkup` — собственные, не из `aiogram`.
  - `tg_fake_api.FakeBotApiServer` — самостоятельный пакет в репозитории, не импортирует ничего из `envoy` (контракт import-linter `independent`), знает только формат Bot API; при необходимости выносится в отдельный пакет переносом каталога и своим `pyproject.toml`. Если появится поддерживаемый fake с лицензией и поддержкой актуального Bot API, он подставляется вместо него, а проходит тот же `tests/contract/test_fake_bot_api.py`. Реализован на `aiohttp`: `sendMessage`, `getUpdates`, `setWebhook`, `getMe`; методы `inject_update(...)`, `fail_next(method, error_code, retry_after=None)`, `calls: list[RecordedCall]`; формат ответов — как в Bot API (`{"ok": ..., "result": ...}` / `{"ok": false, "error_code": 429, "parameters": {"retry_after": n}}`).

- [ ] **Step 1: Тесты** (через `aiohttp.ClientSession`, как это делает реальный клиент): `test_get_me_returns_bot`, `test_send_message_records_call_and_returns_message`, `test_get_updates_returns_injected_in_order_and_acks_offset`, `test_fail_next_returns_429_with_retry_after`, `test_unknown_method_returns_404_envelope`.
- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3:** Реализовать fake на `aiohttp.web`; фикстура `fake_bot_api` в `tests/conftest.py`.
- [ ] **Step 4:** Run → PASS.
- [ ] **Step 5:** Commit `feat: telegram ports and fake bot api server`.

---

### Task 9: Исходящая очередь с лимитами и повторами

**Files:**
- Create: `src/envoy/telegram/outbound.py`, `tests/unit/test_outbound.py`, `tests/property/test_outbound_props.py`

**Interfaces:**
- Consumes: `MessageSender` (Task 8), `Clock` (Task 2).
- Produces: `OutboundQueue(sender: MessageSender, global_rate: float, per_chat_rate: float, max_retry_after: float, clock: Clock)`; `async def submit(self, chat_id: int, text: str, *, reply_markup=None) -> SentMessage` — порядок отправки в рамках одного чата сохраняется; `async def run(self) -> None` / `async def close(self) -> None`; `class RetryAfter(Exception)` с полем `seconds: float`, который бросает адаптер при 429.
- Реализация: `aiolimiter.AsyncLimiter` на глобальный лимит и на каждый чат (словарь по `chat_id`), `tenacity.AsyncRetrying` с `wait_exponential` и кастомным `wait`, уважающим `RetryAfter.seconds`, потолок `min(seconds, max_retry_after)`, `stop_after_attempt(5)`; собственный token bucket не пишется.

- [ ] **Step 1: Тесты** — `test_messages_to_one_chat_keep_order`, `test_global_rate_not_exceeded` (`FixedClock`/виртуальное время), `test_per_chat_rate_not_exceeded`, `test_retry_after_is_respected_then_succeeds`, `test_retry_after_zero_does_not_spin`, `test_retry_after_negative_treated_as_zero`, `test_retry_after_huge_is_capped_by_max`, `test_gives_up_after_five_attempts_and_raises`, `test_sender_called_once_per_message` (`create_autospec(MessageSender)`, `assert_awaited_once_with`); property: `test_order_preserved_for_any_interleaving_of_chats`.
- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3:** Реализовать на `aiolimiter` + `tenacity`.
- [ ] **Step 4:** Run → PASS.
- [ ] **Step 5:** Commit `feat: outbound queue with rate limits and retry_after`.

---

### Task 10: Адаптер aiogram и реестр Telegram-зависимостей

**Files:**
- Create: `src/envoy/telegram/{adapter,dependencies}.py`, `tests/fixtures/bot_api_methods.json`, `tests/contract/test_adapter_vs_fake.py`, `tests/unit/test_dependency_registry.py`
- Modify: `.importlinter`

**Interfaces:**
- Consumes: порты и `FakeBotApiServer` (Task 8), `OutboundQueue`, `RetryAfter` (Task 9).
- Produces:
  - `AiogramAdapter(token: str, api_base: str, queue: OutboundQueue)` реализует `UpdateSource` и `MessageSender`; весь `aiogram` импортируется только здесь; ошибка `TelegramRetryAfter` → `RetryAfter`; метод, которого нет в `aiogram`, вызывается через `async def call_raw(self, method: str, **params) -> Any` (aiogram `TelegramMethod` с произвольным именем либо `Bot.session.make_request`).
  - `TelegramDependency(method: str, since_api: str, used_in: str)`; `REGISTRY: tuple[TelegramDependency, ...]` (`sendMessage`, `getUpdates`, `setWebhook`, `getMe`, `sendChecklist`, `answerGuestQuery`, `refundStarPayment`, `createForumTopic`); `def missing_in(schema_methods: set[str]) -> list[str]`.
  - Неизвестные апдейты и служебные сообщения (например, `paid_messages_waived` из 10.4) адаптер не роняет: тест `test_adapter_maps_unknown_update_to_ignored_dto`; их обработка в `InboundFilter` описана в Фазе 1 (Task 5, Task 8).
  - Фикстура `bot_api_methods.json` — список методов из официального описания API; обновляется отдельной командой `poetry run python -m envoy.telegram.dependencies --refresh`, CI только читает фикстуру.

- [ ] **Step 1: Тесты** — `test_registry_methods_exist_in_fixture`, `test_registry_has_no_duplicates`, `test_adapter_sends_message_via_fake`, `test_adapter_yields_injected_update`, `test_adapter_maps_429_to_retry_after`, `test_adapter_call_raw_unknown_method_hits_fake_with_method_name`, `test_adapter_maps_unknown_update_to_ignored_dto`, `test_secret_token_header_sent_for_webhook_mode`; юнит-уровень без сервера на `aioresponses`: `test_adapter_builds_send_message_request_exactly`, `test_adapter_parses_error_envelope`.
- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3:** Реализовать адаптер на `aiogram` (`Bot` с `TelegramAPIServer.from_base(api_base)`); команду `--refresh` реализовать скачиванием официальной страницы API и разбором имён методов `lxml`/`beautifulsoup4` (добавить через `poetry add --group dev`).
- [ ] **Step 4:** Run → PASS.
- [ ] **Step 5:** Commit `feat: aiogram adapter isolated behind ports with dependency registry`.

---

### Task 11: Конфигурация, логи, корень композиции

**Files:**
- Create: `src/envoy/app/{settings,bootstrap}.py`, `src/envoy/observability/logging.py`, `tests/unit/test_logging_masking.py`, `tests/integration/test_bootstrap_swap.py`, `.env.example`

**Interfaces:**
- Consumes: все реестры и реализации Tasks 2, 6, 7, 10.
- Produces:
  - `Settings(BaseSettings)` (`pydantic-settings`, префикс `ENVOY_`): `journal_store: str`, `telegram_transport: str`, `database_url: SecretStr | None`, `bot_token: SecretStr | None`, `agent_key: SecretStr`, `master_key: SecretStr`, лимиты очереди.
  - `configure_logging(private_fields: frozenset[str]) -> None` — `structlog` с процессором, заменяющим значения `private_fields` и любых `SecretStr` на `"***"`; приватные имена по умолчанию: `reservation`, `weights`, `opponent_model`, `mandate`, `utility`, `private_key`, `token`. Тот же процессор заменяет в любых строковых значениях номера банковских карт (13–19 цифр с допустимыми пробелами и дефисами, проверка Луна через `python-stdnum`, `stdnum.luhn`) на `"[card]"` (§9.2); собственная реализация алгоритма Луна не пишется.
  - `build_application(settings: Settings) -> Application` — единственное место, где импортируются конкретные классы; выбор реализаций по `settings` через `Registry` (`journal_store`: `memory|postgres`; `telegram_transport`: `fake|aiogram`).
  - `SettingsConfigDict(env_file=".env", env_prefix="ENVOY_")`; обязательные секреты без значений по умолчанию, отсутствие любого → понятная ошибка запуска; `.env.example` содержит все ключи с пустыми значениями и без реальных данных; тест `test_env_file_is_gitignored` (`git check-ignore .env` возвращает 0) и `test_env_example_keys_match_settings` (ключи `.env.example` совпадают с полями `Settings`).

- [ ] **Step 1: Тесты**
  - `test_private_field_masked_in_log_event`, `test_secret_str_never_rendered`, `test_nested_private_field_masked`, `test_non_private_field_kept`, `test_card_number_in_text_masked_in_log` (с пробелами и дефисами), `test_digits_failing_luhn_kept`, `test_card_number_inside_nested_value_masked`.
  - `test_bootstrap_swaps_journal_store_by_config` — `journal_store=memory` и `journal_store=postgres` дают приложения с одинаковым наблюдаемым поведением (запись, чтение, верификация) без правок в `journal`/`domain`.
  - `test_bootstrap_swaps_telegram_transport_by_config` (`fake` ↔ `aiogram` против `FakeBotApiServer`).
  - `test_unknown_implementation_name_fails_fast_with_known_list`.
  - `test_composition_root_wires_ports_with_autospec_mocks` — `build_application` с подменёнными через конфигурацию реализациями на `create_autospec`-моках портов; `test_settings_snapshot` (`syrupy`) фиксирует набор ключей `Settings`.
- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3:** Реализовать; логгер `structlog` с `JSONRenderer`, процессор маскирования рекурсивно обходит dict/list.
- [ ] **Step 4:** Run `poetry run pytest -v && poetry run black --check . && poetry run ruff check && poetry run mypy && poetry run lint-imports` → всё зелёное.
- [ ] **Step 5:** Commit `feat: settings, masked structured logging and composition root`.

---

### Task 12: Docker и Compose

**Files:**
- Create: `docker/Dockerfile`, `.dockerignore`, `compose.yaml`, `compose.dev.yaml`, `compose.test.yaml`, `docker/postgres/` (только при необходимости init-параметров), `.env.example` (дополнить), `Makefile`
- Modify: `tests/conftest.py`, `.github/workflows/ci.yml`

**Interfaces:**
- Consumes: `Settings` и `alembic upgrade head` (Tasks 7, 11).
- Produces: команды `make up`, `make down`, `make migrate`, `make test`, `make lint`, каждая — тонкая обёртка над `docker compose`.

Разделение файлов (каждый отвечает за одно):
- `compose.yaml` — только то, что одинаково везде: `postgres` (официальный образ актуальной стабильной major-версии, volume, `healthcheck` через `pg_isready`) и `migrate` (одноразовый сервис, `depends_on: postgres: condition: service_healthy`, команда `alembic upgrade head`, `restart: "no"`). Сервис приложения появится в Фазе 1, когда у процесса будет рантайм; пустой «заглушечный» сервис в Фазе 0 не добавляется.
- `compose.dev.yaml` — оверрайд для разработки: проброс порта PostgreSQL на localhost, монтирование `src/` и `tests/`, `ENVOY_TELEGRAM_TRANSPORT=fake`.
- `compose.test.yaml` — сервис `tests` (профиль `test`): образ стадии `dev`, `depends_on: migrate: condition: service_completed_successfully`, команда `black --check . && ruff check && mypy && lint-imports && pytest`; `ENVOY_TEST_DATABASE_URL` указывает на сервис `postgres`.
- `docker/Dockerfile` — многостадийный: `base` (актуальный стабильный `python:*-slim`, Poetry установлен через `pipx`/официальный установщик с фиксацией версии из lock), `deps` (`poetry install --no-root --only main`, слой кэшируется по `pyproject.toml` + `poetry.lock`), `dev` (+ группа `dev`), `runtime` (только main-зависимости и `src/`, непривилегированный пользователь, без Poetry в образе).
- Конфигурация только через переменные окружения; во всех compose-файлах `env_file: .env` (`required: true`), значения секретов в compose-файлы не пишутся, `docker compose config` в CI запускается с `.env`, созданным из `.env.example`; секреты не копируются в образ (`.dockerignore` исключает `.env`, `.git`, `.venv`, `tests/fixtures` не нужен в runtime).

- [ ] **Step 1: Падающая проверка** — `make test`
Expected: FAIL (`make: *** No rule to make target 'test'`).

- [ ] **Step 2: Фикстура БД** — в `tests/conftest.py` `pg_session_factory` использует `ENVOY_TEST_DATABASE_URL`, если он задан (контейнерный запуск), иначе поднимает PostgreSQL через `testcontainers`; миграции накатывает в обоих случаях только если схема пуста.

- [ ] **Step 3: Реализовать** `Dockerfile`, три compose-файла, `.dockerignore`, `Makefile`; `docker compose config` для каждого сочетания файлов проходит валидацию.

- [ ] **Step 4: Проверка** — Run `make up && make migrate && make test && make down`
Expected: `postgres` healthy, `migrate` завершается с кодом 0, повторный `make migrate` тоже с кодом 0 (идемпотентность), все проверки в сервисе `tests` зелёные.

- [ ] **Step 5: CI** — добавить в `ci.yml` отдельную задачу `docker`: `docker compose -f compose.yaml -f compose.test.yaml --profile test run --rm tests`, плюс `docker build --target runtime` и проверку, что в runtime-образе нет `poetry`, `pytest`, `black`.

- [ ] **Step 6: Commit** `chore: docker image and compose stacks for dev and tests`.

---

## Self-Review

- **Покрытие спеки (Фаза 0, §14):** каркас и CI — Task 1; доменные типы — Task 3; мандат-схема — Task 5; журнал с хэш-цепочкой и верификатором — Task 6, хранилище — Task 7; порты — Tasks 2, 6, 8; fake Telegram — Task 8; адаптер с очередью и лимитами — Tasks 9–10; реестр зависимостей Telegram — Task 10; крипто-шреддинг §9.5 — Tasks 4, 6, 7; маскирование логов §9.2 и конфигурация §13 — Task 11; запуск в контейнерах §15 (Docker Compose) — Task 12; критерий модульности §4.4 — Task 11.
- **Приёмка Фазы 0:** «журнал обнаруживает правки» — property-тест Task 6; «зависимости слоёв проверяются в CI» — Task 1; «fake Bot API проходит контрактные тесты» — Tasks 8, 10.
- **Согласованность типов:** `JournalEntry`, `SequenceConflict`, `ContentErased` определены в Task 3 и используются в 6–7 без переименований; `RetryAfter` определён в Task 9 и используется в Task 10; `Registry` из Task 2 — в Task 11.
- **Не входит в Фазу 0:** `engine`, `protocol`, `security`, `llm`, `payments`, Mini App, TLA+ — планы Фаз 1–3.
