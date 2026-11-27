import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from envoy.widgets.resources import load_widget  # noqa: E402

STUB_MARKER = "envoy-preview-stub"
LONG_PLACE = "Кафе «<b>Пушкин</b>» & Co — Тверской бульвар, 26А, вход со двора 🙂 " * 3
THEMES = {
    "light": {
        "bg-color": "#ffffff",
        "text-color": "#000000",
        "hint-color": "#707579",
        "link-color": "#3390ec",
        "button-color": "#3390ec",
        "button-text-color": "#ffffff",
        "secondary-bg-color": "#f1f1f4",
        "destructive-text-color": "#e53935",
        "section-separator-color": "#e7e7ea",
        "accent-text-color": "#168acd",
    },
    "dark": {
        "bg-color": "#212121",
        "text-color": "#ffffff",
        "hint-color": "#aaaaaa",
        "link-color": "#8774e1",
        "button-color": "#8774e1",
        "button-text-color": "#ffffff",
        "secondary-bg-color": "#181818",
        "destructive-text-color": "#ff595a",
        "section-separator-color": "#100f10",
        "accent-text-color": "#8774e1",
    },
}
SAMPLES: dict[str, tuple[str, dict[str, object], str]] = {
    "dashboard-long": (
        "dashboard",
        {
            "r": 7,
            "round": 2,
            "max": 6,
            "status": "counterpart",
            "issues": [
                {"name": "место", "ours": LONG_PLACE, "theirs": "кафе"},
                {"name": "время", "ours": "03.12.2026 10:00", "theirs": "03.12.2026 10:00"},
            ],
            "proximity": 50,
            "verified": None,
        },
        "light",
    ),
    "escalation-pending": (
        "escalation",
        {
            "r": 9,
            "cause": 42,
            "expires_at": "2099-01-01T00:00:00Z",
            "remaining": 3600,
            "issues": [{"name": "место", "ours": LONG_PLACE, "theirs": None}],
            "outcome": "pending",
        },
        "dark",
    ),
    "escalation-stale": (
        "escalation",
        {
            "r": 3,
            "cause": 42,
            "expires_at": "2000-01-01T00:00:00Z",
            "remaining": 0,
            "issues": [{"name": "время", "ours": "03.12.2026 10:00", "theirs": None}],
            "outcome": "timeout",
        },
        "light",
    ),
    "summary-direct": (
        "summary",
        {
            "r": 1,
            "rows": [
                {
                    "label": f"Чат {index}",
                    "status": "owner",
                    "url": f"https://t.me/envoy_bot/{index}",
                }
                for index in range(10)
            ],
            "total": 12,
            "links": "direct",
        },
        "dark",
    ),
    "summary-request": (
        "summary",
        {
            "r": 1,
            "rows": [
                {"label": "Чат 2", "status": "counterpart", "url": "https://t.me/envoy_bot/5"},
                {"label": "Чат 3", "status": "agreed", "url": None},
            ],
            "total": 2,
            "links": "request",
        },
        "light",
    ),
}
STUB = """<style id="{marker}-theme">:root{{{variables}}}</style>
<script id="{marker}">
window.TelegramWidget = window.TelegramWidget || {{
  profile: 1, colorScheme: "{scheme}", isPreview: false, isForward: false, chatType: "private",
  viewer: {{id: 1, first_name: "Test"}}, publicData: null, chatData: {state},
  contentSafeAreaInset: {{top: 0, right: 0, bottom: 0, left: 0}},
  HapticFeedback: {{impactOccurred() {{ return this; }}, notificationOccurred() {{ return this; }},
    selectionChanged() {{ return this; }}}},
  _handlers: {{}},
  onEvent(type, handler) {{ (this._handlers[type] = this._handlers[type] || []).push(handler); }},
  offEvent() {{}}, ready() {{}},
  openLink(url) {{ console.log("openLink", url); }},
  openTelegramLink(url) {{ console.log("openTelegramLink", url); }},
  copyText(text) {{ console.log("copyText", text); }},
  request(data) {{
    console.log("request", data);
    const mode = (location.hash || "#ok").slice(1);
    if (mode === "BUSY" || mode === "TIMEOUT") {{
      const error = new Error(mode); error.name = "WidgetRequestError"; error.code = mode;
      return Promise.reject(error);
    }}
    return new Promise((resolve) => setTimeout(() => resolve({{result: mode}}), 300));
  }},
  push(state) {{
    this.chatData = JSON.stringify(state);
    (this._handlers.chatDataChanged || []).forEach((handler) => handler.call(this));
  }}
}};
</script>
"""


def preview(kind: str, state: str, scheme: str) -> str:
    variables = ";".join(f"--tg-theme-{key}:{value}" for key, value in THEMES[scheme].items())
    stub = STUB.format(
        marker=STUB_MARKER, variables=variables, scheme=scheme, state=json.dumps(state)
    )
    html = load_widget(kind)  # type: ignore[arg-type]
    return html.replace("<head>", "<head>\n" + stub, 1)


def write_previews(destination: Path) -> list[Path]:
    destination.mkdir(parents=True, exist_ok=True)
    written = []
    for name, (kind, state, scheme) in SAMPLES.items():
        path = destination / f"{name}.html"
        path.write_text(
            preview(kind, json.dumps(state, ensure_ascii=False), scheme), encoding="utf-8"
        )
        written.append(path)
    return written


if __name__ == "__main__":
    for path in write_previews(Path(sys.argv[1] if len(sys.argv) > 1 else "widget-previews")):
        print(path)
