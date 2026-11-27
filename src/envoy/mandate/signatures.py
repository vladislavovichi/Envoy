from datetime import datetime
from typing import Literal

from envoy.domain.canonical import encode
from envoy.domain.ids import MandateId, OfferHash, OwnerId, SessionId
from envoy.mandate.schema import Mandate

type Purpose = Literal["enroll", "mandate", "delegate", "escalation", "revoke_key"]
type Choice = Literal["approve", "decline"]

PURPOSES: tuple[Purpose, ...] = ("enroll", "mandate", "delegate", "escalation", "revoke_key")
ENROLL_CONTEXT = "envoy/enroll/v1"
MANDATE_CONTEXT = "envoy/mandate/v1"
ESCALATION_CONTEXT = "envoy/escalation/v1"
REVOKE_CONTEXT = "envoy/revoke-own-key/v1"


def enroll_bytes(owner: OwnerId, public_key: bytes, challenge: str, nonce: bytes) -> bytes:
    return encode(
        {
            "ctx": ENROLL_CONTEXT,
            "owner_id": owner,
            "public_key": public_key,
            "challenge": challenge,
            "nonce": nonce,
        }
    )


def mandate_bytes(
    owner: OwnerId,
    mandate_id: MandateId,
    version: int,
    mandate: Mandate,
    challenge: str,
    nonce: bytes,
) -> bytes:
    return encode(
        {
            "ctx": MANDATE_CONTEXT,
            "owner_id": owner,
            "mandate_id": mandate_id,
            "version": version,
            "mandate": mandate.canonical_bytes(),
            "challenge": challenge,
            "nonce": nonce,
        }
    )


def escalation_bytes(
    owner: OwnerId,
    sid: SessionId,
    revision: int,
    offer_hash: OfferHash,
    choice: Choice,
    expires_at: datetime,
    challenge: str,
    nonce: bytes,
) -> bytes:
    return encode(
        {
            "ctx": ESCALATION_CONTEXT,
            "owner_id": owner,
            "sid": sid,
            "revision": revision,
            "offer_hash": offer_hash,
            "choice": choice,
            "expires_at": expires_at,
            "challenge": challenge,
            "nonce": nonce,
        }
    )


def revoke_key_bytes(
    owner: OwnerId, agent_key: bytes, revision: int, challenge: str, nonce: bytes
) -> bytes:
    return encode(
        {
            "ctx": REVOKE_CONTEXT,
            "owner_id": owner,
            "agent_key": agent_key,
            "revision": revision,
            "challenge": challenge,
            "nonce": nonce,
        }
    )
