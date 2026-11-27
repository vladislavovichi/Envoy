from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol

from envoy.app.daily_summary import SUMMARY_HOUR, DailySummary
from envoy.app.execution_flow import ExecutionFlow
from envoy.app.journal import SessionJournal
from envoy.app.locks import SessionLocks
from envoy.app.memory import InMemoryOwnerViewStore
from envoy.app.owner_flow import OwnerFlow
from envoy.app.owner_view import OwnerPresenter, OwnerView, TextOwnerView
from envoy.app.ports import (
    BusinessConnectionRepository,
    OwnerViewStore,
    Scheduler,
    SessionRepository,
    UpdateLedger,
)
from envoy.app.processor import ConfiguredOwnerDirectory, UpdateProcessor
from envoy.app.renderer import TemplateRenderer
from envoy.app.session_flow import SessionFlow
from envoy.app.steps import SessionSteps
from envoy.app.widget_queries import WidgetQueryFlow
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
    WidgetTransport,
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
    summary_hour: int = SUMMARY_HOUR


@dataclass(frozen=True)
class Stores:
    journal: SessionJournal
    mandates: MandateStore
    sessions: SessionRepository
    connections: BusinessConnectionRepository
    ledger: UpdateLedger
    scheduler: Scheduler
    usage: UsageLedger
    owner_views: OwnerViewStore = field(default_factory=InMemoryOwnerViewStore)


type OwnerViewFactory = Callable[[TextOwnerView], OwnerView]


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
    owner_view: OwnerView
    text_view: TextOwnerView
    presenter: OwnerPresenter
    widget_queries: WidgetQueryFlow | None
    daily_summary: DailySummary


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
    owner_views: OwnerViewFactory | None = None,
    widgets: WidgetTransport | None = None,
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
    text_view = TextOwnerView(sender, renderer, stores.owner_views)
    owner_view: OwnerView = text_view if owner_views is None else owner_views(text_view)
    presenter = OwnerPresenter(owner_view, renderer)
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
        presenter,
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
    daily_summary = DailySummary(
        steps,
        owner_view,
        stores.owner_views,
        surface,
        surface,
        config.timezone,
        config.summary_hour,
    )
    locks = SessionLocks()
    widget_queries = (
        None
        if widgets is None
        else WidgetQueryFlow(
            widgets,
            stores.sessions,
            stores.owner_views,
            session_flow.resolver,
            locks,
            clock,
            text_view,
        )
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
        locks,
        widget_queries,
    )
    return Pipeline(
        processor,
        session_flow,
        owner_flow,
        execution,
        steps,
        stores,
        window,
        authorizer,
        owner_view,
        text_view,
        presenter,
        widget_queries,
        daily_summary,
    )
