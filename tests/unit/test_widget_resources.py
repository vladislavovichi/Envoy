import re
import subprocess
import sys
import zlib
from html.parser import HTMLParser
from pathlib import Path

import pytest

from envoy.widgets.resources import CURRENT_VERSION, WIDGET_KINDS, load_widget

SOURCE = Path("src/envoy/widgets/resources")
FORBIDDEN_TAGS = {"form", "input", "textarea", "select", "iframe", "img", "link", "object", "embed"}
VOID = {"meta", "br", "hr", "img", "input", "link"}


class Checker(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.stack: list[str] = []
        self.tags: set[str] = set()
        self.ids: set[str] = set()
        self.urls: list[str] = []
        self.errors: list[str] = []

    def handle_starttag(self, tag, attrs) -> None:
        self.tags.add(tag)
        for name, value in attrs:
            if name == "id" and value:
                self.ids.add(value)
            if name in {"src", "href", "action", "srcset"} and value:
                self.urls.append(value)
        if tag not in VOID:
            self.stack.append(tag)

    def handle_endtag(self, tag) -> None:
        if not self.stack or self.stack[-1] != tag:
            self.errors.append(f"unbalanced </{tag}>")
            return
        self.stack.pop()


@pytest.fixture(params=WIDGET_KINDS)
def kind(request):
    return request.param


def test_packaged_resource_matches_source_tree(kind) -> None:
    assert load_widget(kind) == (SOURCE / f"{kind}-v{CURRENT_VERSION}.html").read_text("utf-8")


def test_resource_limits(kind) -> None:
    raw = load_widget(kind).encode("utf-8")
    assert 1 <= len(raw) <= 32768
    assert b"\x00" not in raw
    assert len(zlib.compress(raw, 9)) <= 3800


def test_resource_is_valid_offline_html_without_inputs(kind) -> None:
    html = load_widget(kind)
    checker = Checker()
    checker.feed(html)
    checker.close()
    assert checker.errors == []
    assert checker.stack == []
    assert not checker.tags & FORBIDDEN_TAGS
    assert checker.urls == []
    assert not re.search(r"https?://", html)
    for api in ("fetch(", "XMLHttpRequest", "WebSocket", "innerHTML", "eval(", "localStorage"):
        assert api not in html
    for identifier in re.findall(r'\$\("([a-z]+)"\)', html):
        assert identifier in checker.ids


def test_resource_ignores_older_revision_and_renders_text_safely(kind) -> None:
    html = load_widget(kind)
    assert "d.r<R" in html
    assert 'onEvent("chatDataChanged"' in html
    assert "textContent" in html


def test_escalation_requests_and_summary_link_modes() -> None:
    escalation = load_widget("escalation")
    assert 'send("a")' in escalation
    assert 'send("d")' in escalation
    assert 'k+":"+D.cause' in escalation
    summary = load_widget("summary")
    assert '"l:"+D.r+":"+i' in summary
    assert "openTelegramLink" in summary
    assert 'links==="direct"' in summary


def test_browser_previews_include_stub_and_samples(tmp_path) -> None:
    subprocess.run(
        [sys.executable, "scripts/widget_preview.py", str(tmp_path)],
        check=True,
        capture_output=True,
    )
    written = {path.name: path.read_text("utf-8") for path in tmp_path.glob("*.html")}
    assert set(written) == {
        "dashboard-long.html",
        "escalation-pending.html",
        "escalation-stale.html",
        "summary-direct.html",
        "summary-request.html",
    }
    for kind in WIDGET_KINDS:
        assert "envoy-preview-stub" not in load_widget(kind)
    for text in written.values():
        assert "envoy-preview-stub" in text
        assert text.index("window.TelegramWidget") < text.index("(function(){")
