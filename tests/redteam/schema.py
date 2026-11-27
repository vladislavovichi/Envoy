from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

from envoy.domain.session import SessionState
from envoy.llm.translator import IntentWire

type Category = Literal[
    "injection_direct",
    "injection_in_offer",
    "authority_claim",
    "leak_probe",
    "identity_spoof",
    "flood",
    "compromised_llm",
    "payment_details",
]
CATEGORIES: tuple[str, ...] = (
    "injection_direct",
    "injection_in_offer",
    "authority_claim",
    "leak_probe",
    "identity_spoof",
    "flood",
    "compromised_llm",
    "payment_details",
)


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Entity(_Model):
    type: str
    offset: int
    length: int


class Step(_Model):
    actor: Literal["counterpart", "bot", "stranger"]
    text: str
    entities: tuple[Entity, ...] = ()
    repeat: int = Field(default=1, ge=1, le=500)


class ScriptedReply(_Model):
    match: str
    intent: IntentWire


class Expectation(_Model):
    no_action_outside_mandate: bool = True
    no_leak: bool = True
    final_state: SessionState | None = None
    max_llm_calls: int | None = None
    secrets: tuple[str, ...] = ()


class Attack(_Model):
    id: str
    category: Category
    description: str
    steps: tuple[Step, ...] = Field(min_length=1)
    llm_script: tuple[ScriptedReply, ...] = ()
    expect: Expectation = Expectation()


CATALOG = Path(__file__).parent / "catalog"


def load_catalog(directory: Path = CATALOG) -> list[Attack]:
    attacks: list[Attack] = []
    for path in sorted(directory.glob("*.yaml")):
        for raw in yaml.safe_load(path.read_text(encoding="utf-8")) or []:
            attacks.append(Attack.model_validate(raw))
    return attacks
