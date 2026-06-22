import asyncio
import logging
from tests.test_store_contract import _full_store_factory, _postgres_factory

logging.basicConfig(level=logging.DEBUG)

async def main():
    try:
        gen = _full_store_factory()()
        store = await anext(gen)
        print("Full store created successfully.")
        await anext(gen)
    except Exception as e:
        import traceback
        traceback.print_exc()

asyncio.run(main())
