import asyncio

from dotenv import load_dotenv

from vibekeeper.app import run

if __name__ == "__main__":
    load_dotenv()
    asyncio.run(run())
