from hypothesis import strategies as st

from envoy.mandate.schema import Mandate

ISSUE_IDS = ("a", "b", "c", "d")


@st.composite
def issues(draw: st.DrawFn, issue_id: str) -> dict:
    kind = draw(st.sampled_from(["integer", "enum", "place", "money", "duration"]))
    if kind in ("enum", "place"):
        size = draw(st.integers(1, 5))
        return {"id": issue_id, "kind": kind, "choices": [f"{issue_id}{i}" for i in range(size)]}
    low = draw(st.integers(0, 50))
    count = draw(st.integers(0, 5))
    step = draw(st.integers(1, 7))
    if kind == "money":
        return {
            "id": issue_id,
            "kind": "money",
            "minimum": str(low),
            "maximum": str(low + step * count),
            "step": str(step),
        }
    if kind == "duration":
        return {
            "id": issue_id,
            "kind": "duration",
            "minimum": low * 60,
            "maximum": (low + step * count) * 60,
            "step": step * 60,
        }
    return {"id": issue_id, "kind": "integer", "minimum": low, "maximum": low + step * count}


def _value(issue: dict, draw: st.DrawFn) -> object:
    if issue["kind"] in ("enum", "place"):
        return draw(st.sampled_from([*issue["choices"], "unknown"]))
    if issue["kind"] == "money":
        return str(draw(st.integers(0, 90)))
    if issue["kind"] == "duration":
        return draw(st.integers(0, 90)) * 60
    return draw(st.integers(0, 90))


@st.composite
def predicates(draw: st.DrawFn, issue_list: list[dict], depth: int = 0) -> dict:
    kind = draw(st.sampled_from(["cmp"] if depth >= 2 else ["cmp", "and", "or", "not", "requires"]))
    if kind == "cmp":
        issue = draw(st.sampled_from(issue_list))
        ops = ["in", "not_in"]
        if issue["kind"] not in ("enum", "place"):
            ops += ["<=", ">=", "between"]
        op = draw(st.sampled_from(ops))
        if op in ("in", "not_in"):
            value: object = [_value(issue, draw) for _ in range(draw(st.integers(0, 3)))]
            if issue["kind"] not in ("enum", "place"):
                value = [item for item in value if item != "unknown"]
        elif op == "between":
            value = sorted([_value(issue, draw), _value(issue, draw)], key=float)
        else:
            value = _value(issue, draw)
        return {"kind": "cmp", "issue": issue["id"], "op": op, "value": value}
    if kind in ("and", "or"):
        return {
            "kind": kind,
            "predicates": [
                draw(predicates(issue_list, depth + 1)) for _ in range(draw(st.integers(1, 3)))
            ],
        }
    if kind == "not":
        return {"kind": "not", "predicate": draw(predicates(issue_list, depth + 1))}
    return {
        "kind": "requires",
        "condition": draw(predicates(issue_list, depth + 1)),
        "consequence": draw(predicates(issue_list, depth + 1)),
    }


@st.composite
def mandates(draw: st.DrawFn) -> Mandate:
    count = draw(st.integers(1, len(ISSUE_IDS)))
    issue_list = [draw(issues(issue_id)) for issue_id in ISSUE_IDS[:count]]
    raw_weights = [draw(st.integers(0, 10)) for _ in issue_list]
    if sum(raw_weights) == 0:
        raw_weights[0] = 1
    total = sum(raw_weights)
    weights = {issue["id"]: raw / total for issue, raw in zip(issue_list, raw_weights, strict=True)}
    value_functions = {}
    for issue in issue_list:
        xs = sorted(draw(st.sets(st.integers(0, 6000), min_size=2, max_size=4)))
        ys = [draw(st.floats(0, 1)) for _ in xs]
        value_functions[issue["id"]] = {"points": [[x, y] for x, y in zip(xs, ys, strict=True)]}
    return Mandate.model_validate(
        {
            "template": "purchase",
            "issues": issue_list,
            "utility": {"weights": weights, "value_functions": value_functions},
            "hard_constraints": [
                draw(predicates(issue_list)) for _ in range(draw(st.integers(0, 3)))
            ],
            "reservation": {"minimum_utility": draw(st.floats(0, 1))},
            "strategy": {"kind": "time_dependent", "params": {"beta": draw(st.floats(0.1, 5))}},
            "limits": {"max_rounds": draw(st.integers(1, 20))},
            "escalation": {"always": []},
        }
    )
