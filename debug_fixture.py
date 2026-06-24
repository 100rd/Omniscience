import asyncio
import logging

from tests.test_store_contract import _full_store_factory

logging.basicConfig(level=logging.DEBUG)


async def main() -> None:
    try:
        gen = _full_store_factory()()
        _ = await anext(gen)
        print("Full store created successfully.")  # noqa: T201
        await anext(gen)
    except Exception:
        import traceback

        traceback.print_exc()


asyncio.run(main())
