import json

import pytest

from envoy.domain.ids import owner_for_user
from envoy.domain.telegram import MessageUpdate
from envoy.widgets.projection import (
    MandateRequest,
    MandateRequestRejected,
    encode_request,
    parse_request,
)
from tests.support.harness import OWNER_USER, build_harness
from tests.support.mandates import RESERVATION, WEIGHT_PLACE, WEIGHT_TIME

MARKER = "Черновик мандата"


async def started(**options):
    harness = build_harness(owner_view="widget", **options)
    await harness.activate()
    await harness.feed(MessageUpdate(harness.update_id(), OWNER_USER, OWNER_USER, text="/draft"))
    return harness


def card(harness):
    cards = harness.widgets_of(MARKER)
    assert len(cards) == 1
    return cards[0]


def drafts(harness) -> list:
    return [
        record
        for record in harness.pipeline.stores.mandates._records.values()
        if record.status == "draft"
    ]


async def test_card_state_has_no_private_mandate_fields() -> None:
    harness = await started()
    record = card(harness)
    state = json.loads(record.state)
    assert state["issues"] == ["place", "time"]
    assert len(state["slots"]) == 8
    assert all(slot["on"] for slot in state["slots"])
    for canary in (
        str(RESERVATION),
        str(WEIGHT_TIME),
        str(WEIGHT_PLACE),
        "station",
        "0.61",
        "beta",
    ):
        assert canary not in record.state
    assert record.allow_queries is True
    assert "Mini App" in record.caption


async def test_request_saves_new_draft_and_keeps_active_version() -> None:
    harness = await started()
    owner = owner_for_user(OWNER_USER)
    active = await harness.pipeline.stores.mandates.active(owner)
    record = card(harness)
    state = json.loads(record.state)
    request = MandateRequest(state["r"], tuple(range(1, 8)), (0, 5))
    await harness.widget_query(encode_request(request), record.message.message_id)
    assert harness.widget_answers() == ["ok"]
    assert await harness.pipeline.stores.mandates.active(owner) == active
    latest = max(drafts(harness), key=lambda item: item.version)
    time = next(issue for issue in latest.mandate.issues if issue.id == "time")
    assert len(time.choices) == 7
    weights = latest.mandate.utility.weights
    assert abs(sum(weights.values()) - 1) < 1e-9
    assert weights["time"] > WEIGHT_TIME
    edited = json.loads(harness.transport.widgets[record.message.message_id].state)
    assert edited["r"] == state["r"] + 1
    assert [slot["on"] for slot in edited["slots"]] == [False] + [True] * 7


async def test_stale_foreign_replayed_and_invalid_requests_write_nothing() -> None:
    harness = await started()
    record = card(harness)
    state = json.loads(record.state)
    good = encode_request(MandateRequest(state["r"], (0, 1), (1, -1)))
    await harness.widget_query(good, record.message.message_id)
    count = len(drafts(harness))
    await harness.widget_query(good, record.message.message_id)
    await harness.widget_query(good, record.message.message_id + 99)
    await harness.widget_query(good, record.message.message_id, user_id=77, chat_id=77)
    fresh = state["r"] + 1
    for request in (
        MandateRequest(fresh, (0, 31), (0, 0)),
        MandateRequest(fresh, (), (0, 0)),
        MandateRequest(fresh, (0,), (0,)),
        MandateRequest(fresh, (0,), (-100, 0)),
        MandateRequest(fresh, (0,), (-40, -60)),
    ):
        await harness.widget_query(encode_request(request), record.message.message_id)
    await harness.widget_query("m:zz:AAAA:", record.message.message_id)
    assert len(drafts(harness)) == count
    assert harness.widget_answers() == [
        "ok",
        "stale",
        "no",
        "no",
        "bad",
        "bad",
        "bad",
        "bad",
        "bad",
        "bad",
    ]


def test_request_codec_fits_transport_and_rejects_oversize() -> None:
    request = MandateRequest(36**8 - 1, tuple(range(32)), tuple([127, -128] * 4))
    encoded = encode_request(request)
    assert len(encoded.encode()) <= 64
    assert parse_request(encoded) == request
    with pytest.raises(MandateRequestRejected, match="too_long"):
        parse_request("m:" + "a" * 63)
    with pytest.raises(MandateRequestRejected, match="bad_format"):
        parse_request("m:1:AAAAAA:" + "A" * 12)


async def test_unavailable_widget_falls_back_to_mini_app_text() -> None:
    harness = build_harness(owner_view="widget")
    await harness.activate()
    harness.transport.widgets_available = False
    await harness.feed(MessageUpdate(harness.update_id(), OWNER_USER, OWNER_USER, text="/draft"))
    assert harness.widgets_of(MARKER) == []
    assert "Mini App" in harness.to_owner()[-1]
