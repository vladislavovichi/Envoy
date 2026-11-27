from functools import cache
from importlib.resources import files
from typing import Literal

type WidgetKind = Literal["dashboard", "escalation", "summary"]

CURRENT_VERSION = 1
WIDGET_KINDS: tuple[WidgetKind, ...] = ("dashboard", "escalation", "summary")


@cache
def load_widget(kind: WidgetKind, version: int = CURRENT_VERSION) -> str:
    resource = files("envoy.widgets").joinpath("resources", f"{kind}-v{version}.html")
    return resource.read_text("utf-8")
