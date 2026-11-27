import argparse
import asyncio
import json
import re
from dataclasses import dataclass
from pathlib import Path

import aiohttp


@dataclass(frozen=True)
class TelegramDependency:
    method: str
    since_api: str
    used_in: str


REGISTRY = (
    TelegramDependency("sendMessage", "1.0", "outbound"),
    TelegramDependency("getUpdates", "1.0", "adapter"),
    TelegramDependency("setWebhook", "1.0", "adapter"),
    TelegramDependency("getMe", "1.0", "adapter bot username for topic links"),
    TelegramDependency("sendChecklist", "9.1", "phase 1 execution"),
    TelegramDependency("answerCallbackQuery", "1.0", "phase 1 escalation callbacks"),
    TelegramDependency("answerGuestQuery", "10.0", "phase 2 guest surface"),
    TelegramDependency("refundStarPayment", "7.4", "phase 3 payments"),
    TelegramDependency("createForumTopic", "9.4", "phase 1 session topics in private chat"),
    TelegramDependency("sendWidget", "10.4", "phase 1b owner widgets"),
    TelegramDependency("editMessageWidgetData", "10.4", "phase 1b owner widget state"),
    TelegramDependency("answerWidgetQuery", "10.4", "phase 1b owner widget queries"),
    TelegramDependency("setMyDescription", "6.6", "phase 2 agent key fingerprint"),
    TelegramDependency("answerInlineQuery", "1.0", "phase 2 invitation cards inline"),
    TelegramDependency("getManagedBotToken", "9.6", "phase 2 managed agents"),
    TelegramDependency("replaceManagedBotToken", "9.6", "phase 2 managed agents"),
    TelegramDependency("setManagedBotAccessSettings", "10.0", "phase 2 managed agent access"),
    TelegramDependency("setMyName", "6.7", "phase 2 managed agent profile"),
)


def missing_in(schema_methods: set[str]) -> list[str]:
    return sorted(item.method for item in REGISTRY if item.method not in schema_methods)


async def refresh(destination: Path) -> None:
    from bs4 import BeautifulSoup

    async with aiohttp.ClientSession() as session:
        async with session.get("https://core.telegram.org/bots/api") as response:
            response.raise_for_status()
            soup = BeautifulSoup(await response.text(), "html.parser")
    methods = sorted(
        {
            node.get_text(strip=True)
            for node in soup.select("h4")
            if re.fullmatch(r"[a-z][a-zA-Z0-9]+", node.get_text(strip=True))
        }
    )
    missing = missing_in(set(methods))
    if not methods or missing:
        raise ValueError(f"Official Bot API method extraction incomplete: {missing}")
    await asyncio.to_thread(destination.parent.mkdir, parents=True, exist_ok=True)
    await asyncio.to_thread(
        destination.write_text, json.dumps(methods, indent=2) + "\n", encoding="utf-8"
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--refresh", action="store_true", required=True)
    parser.parse_args()
    asyncio.run(refresh(Path("tests/fixtures/bot_api_methods.json")))


if __name__ == "__main__":
    main()
