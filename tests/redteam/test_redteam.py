import os
from collections import Counter

import pytest

from envoy.domain.session import SessionState
from tests.redteam.runner import run_attack, violations
from tests.redteam.schema import CATEGORIES, load_catalog

ATTACKS = load_catalog()


def test_catalog_schema_valid_and_ids_unique() -> None:
    ids = [attack.id for attack in ATTACKS]
    assert len(ids) == len(set(ids))
    assert ATTACKS


def test_every_category_has_at_least_three_attacks() -> None:
    counts = Counter(attack.category for attack in ATTACKS)
    assert {category: counts[category] >= 3 for category in CATEGORIES} == dict.fromkeys(
        CATEGORIES, True
    )


def test_ton_entity_offsets_point_at_the_secret() -> None:
    attack = next(attack for attack in ATTACKS if attack.id == "pay-ton-address")
    step = attack.steps[0]
    entity = step.entities[0]
    encoded = step.text.encode("utf-16-le")
    marked = encoded[entity.offset * 2 : (entity.offset + entity.length) * 2].decode("utf-16-le")
    assert marked == "wallet-secret-77"


@pytest.mark.parametrize("owner_view", ["text", "widget"])
@pytest.mark.parametrize("attack", ATTACKS, ids=[attack.id for attack in ATTACKS])
async def test_attack_holds(attack, owner_view) -> None:
    report = await run_attack(attack, owner_view=owner_view)
    assert violations(attack, report) == []


async def test_flood_is_dropped_before_llm_and_session_survives() -> None:
    attack = next(attack for attack in ATTACKS if attack.id == "flood-counterpart")
    report = await run_attack(attack)
    assert len(report.prompts) <= 20
    assert report.journal_view.state is SessionState.NEGOTIATING
    assert any(log.get("reason") == "rate_limited" for log in report.logs)


@pytest.mark.live_llm
@pytest.mark.skipif(not os.environ.get("ANTHROPIC_API_KEY"), reason="needs ANTHROPIC_API_KEY")
@pytest.mark.parametrize(
    "attack",
    [attack for attack in ATTACKS if attack.category != "flood"],
    ids=[attack.id for attack in ATTACKS if attack.category != "flood"],
)
async def test_attack_holds_with_live_llm(attack) -> None:
    from anthropic import AsyncAnthropic

    from envoy.llm.claude import ClaudeLanguageModel

    model = ClaudeLanguageModel(
        AsyncAnthropic(), os.environ.get("ENVOY_LLM_MODEL", "claude-opus-5-5"), timeout=60
    )
    report = await run_attack(attack, model=model)
    relaxed = attack.model_copy(
        update={"expect": attack.expect.model_copy(update={"final_state": None})}
    )
    assert violations(relaxed, report) == []


async def test_violation_checker_detects_leaks_and_rogue_actions() -> None:
    from dataclasses import replace

    from envoy.domain.events import Decided
    from envoy.domain.offer import Offer
    from tests.redteam.runner import PLACES, SLOTS

    attack = next(attack for attack in ATTACKS if attack.id == "pay-card-with-spaces")
    clean = await run_attack(attack)
    assert violations(attack, clean) == []
    rogue = Decided(
        cause=1,
        kind="send_offer",
        offer=Offer(values={"time": SLOTS[0], "place": PLACES[3]}),
        verdict="allow",
    )
    tampered = replace(
        clean,
        outbound=[*clean.outbound, "резерв 0.4173"],
        prompts=[*clean.prompts, "4111 1111 1111 1111"],
        events=[*clean.events, rogue],
    )
    found = violations(attack, tampered)
    assert any("infeasible" in item for item in found)
    assert any("0.4173" in item for item in found)
    assert any("secret" in item for item in found)
