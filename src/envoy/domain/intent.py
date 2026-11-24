from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from envoy.domain.offer import Offer


class _IntentModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Propose(_IntentModel):
    kind: Literal["propose"] = "propose"
    offer: Offer


class AcceptIntent(_IntentModel):
    kind: Literal["accept"] = "accept"


class RejectIntent(_IntentModel):
    kind: Literal["reject"] = "reject"


class Unclear(_IntentModel):
    kind: Literal["unclear"] = "unclear"
    missing: tuple[str, ...] = ()


type Intent = Annotated[
    Propose | AcceptIntent | RejectIntent | Unclear, Field(discriminator="kind")
]
