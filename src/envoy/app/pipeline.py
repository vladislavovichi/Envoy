from dataclasses import dataclass
from typing import Protocol

from envoy.app.execution_flow import ExecutionFlow
from envoy.app.journal import SessionJournal
from envoy.app.owner_flow import OwnerFlow
from envoy.app.ports import (
    BusinessConnectionRepository,
    Scheduler,
    SessionRepository,
    UpdateLedger,
)
from envoy.app.processor import ConfiguredOwnerDirectory, SessionLocks, UpdateProcessor
from envoy.app.renderer import TemplateRenderer
from envoy.app.session_flow import SessionFlow
from envoy.app.steps import SessionSteps
from envoy.app.window import ReplyWindow
from envoy.crypto.keys import SigningKey
from envoy.engine.engine import NegotiationEngine
from envoy.engine.opponent import LastOfferProximity
from envoy.engine.strategy import strategy_from_spec
from envoy.llm.budget import BudgetedLanguageModel
from envoy.llm.ports import LanguageModel, UsageLedger
from envoy.llm.translator import LlmTranslator
from envoy.mandate.compiler import CompiledMandate
from envoy.mandate.ports import MandateStore
from envoy.ports.clock import Clock
from envoy.ports.rng import Rng
from envoy.security.authorizer import Authorizer
from envoy.security.inbound import InboundFilter
from envoy.security.rules import (
    ConstraintsRule,
    EscalationRule,
    LimitsRule,
    ReplyWindowRule,
    SessionStateRule,
)
from envoy.telegram.ports import (
    BotIdentity,
    CallbackAnswerer,
    ChecklistSender,
    MessageSender,
    TopicCreator,
)


class TelegramSurface(TopicCreator, ChecklistSender, CallbackAnswerer, BotIdentity, Protocol):
    pass


@dataclass(frozen=True)
class PipelineConfig:
    owner_ids: frozenset[int]
    timezone: str
    agent_name: str = "Envoy"
    inbound_max_text_length: int = 2000
    inbound_rate_per_minute: int = 20
    llm_daily_token_budget: int = 200_000


@dataclass(frozen=True)
class Stores:
    journal: SessionJournal
    mandates: MandateStore
    sessions: SessionRepository
    connections: BusinessConnectionRepository
    ledger: UpdateLedger
    scheduler: Scheduler
    usage: UsageLedger


@dataclass(frozen=True)
class Pipeline:
    processor: UpdateProcessor
    session_flow: SessionFlow
    owner_flow: OwnerFlow
    execution: ExecutionFlow
    steps: SessionSteps
    stores: Stores
    window: ReplyWindow
    authorizer: Authorizer


def default_authorizer() -> Authorizer:
    return Authorizer(
        [SessionStateRule(), ConstraintsRule(), LimitsRule(), EscalationRule(), ReplyWindowRule()]
    )


def default_engine(compiled: CompiledMandate) -> NegotiationEngine:
    return NegotiationEngine(compiled, strategy_from_spec(compiled.strategy), LastOfferProximity())


def assemble_pipeline(
    config: PipelineConfig,
    stores: Stores,
    sender: MessageSender,
    surface: TelegramSurface,
    model: LanguageModel,
    signing_key: SigningKey,
    clock: Clock,
    rng: Rng,
) -> Pipeline:
    renderer = TemplateRenderer(config.timezone)
    window = ReplyWindow(stores.connections, clock)
    authorizer = default_authorizer()
    translator = LlmTranslator(
        BudgetedLanguageModel(model, stores.usage, config.llm_daily_token_budget, clock)
    )
    steps = SessionSteps(
        stores.journal,
        stores.sessions,
        stores.mandates,
        stores.scheduler,
        sender,
        clock,
        authorizer,
        window,
    )
    execution = ExecutionFlow(steps, surface, renderer, config.timezone)
    session_flow = SessionFlow(
        steps,
        stores.connections,
        execution,
        translator,
        default_engine,
        surface,
        surface,
        surface,
        renderer,
        renderer,
        config.timezone,
        config.agent_name,
    )
    owner_flow = OwnerFlow(
        translator,
        stores.mandates,
        sender,
        surface,
        stores.journal,
        signing_key,
        renderer,
        clock,
        config.timezone,
        rng,
    )
    inbound = InboundFilter(
        ConfiguredOwnerDirectory(config.owner_ids, stores.connections),
        config.inbound_max_text_length,
        config.inbound_rate_per_minute,
    )
    processor = UpdateProcessor(
        stores.ledger,
        inbound,
        session_flow,
        owner_flow,
        execution,
        stores.connections,
        stores.sessions,
        window,
        SessionLocks(),
    )
    return Pipeline(
        processor, session_flow, owner_flow, execution, steps, stores, window, authorizer
    )
