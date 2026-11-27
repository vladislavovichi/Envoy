import asyncio
import random

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from envoy.protocol.machine import Phase
from tests.support.federation import two_operators

faults = st.lists(
    st.tuples(st.sampled_from(["drop", "duplicate", "swap", "tick"]), st.integers(0, 50)),
    max_size=25,
)


async def scenario(plan, seed: int) -> None:
    router, a, b = await two_operators()
    sid = await a.flow.open(b.bot_id, a.owner_id)
    shuffle = random.Random(seed)
    for action, index in plan:
        if router.queue:
            position = index % len(router.queue)
            match action:
                case "drop":
                    router.queue.pop(position)
                case "duplicate":
                    router.queue.append(router.queue[position])
                case "swap":
                    shuffle.shuffle(router.queue)
                case _:
                    pass
        if action == "tick":
            await router.tick()
        elif router.queue:
            update = router.queue.pop(0)
            await router.operators[update.bot_id].flow.receive(update)
    await router.settle(ticks=200)
    left, right = await a.repo.get(sid), await b.repo.get(sid)
    phases = {left.phase, right.phase}
    assert not phases & {Phase.HANDSHAKE, Phase.AGREED, Phase.COMMITTING}, (left.state, right.state)
    if Phase.EXECUTING in phases:
        assert phases == {Phase.EXECUTING}, (left.state, right.state)
        assert left.state.agreed == right.state.agreed
    for session in (left, right):
        assert session.outbox == ()
        if session.state.terminal:
            assert dict(session.timers) == {} or session.phase is Phase.EXECUTING


@settings(max_examples=60, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(plan=faults, seed=st.integers(0, 1000))
def test_finite_faults_converge_after_link_recovers(plan, seed) -> None:
    asyncio.run(scenario(plan, seed))
