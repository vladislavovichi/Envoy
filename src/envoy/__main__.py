import asyncio

from envoy.app.bootstrap import build_application
from envoy.app.settings import Settings


async def main() -> None:
    application = build_application(Settings())
    try:
        await application.runtime.run()
    finally:
        await application.close()


if __name__ == "__main__":
    asyncio.run(main())
