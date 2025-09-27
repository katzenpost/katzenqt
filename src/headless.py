"""
TODO: Run the client without the Qt GUI
"""
import network
import asyncio
import traceback

async def amain():
    client = None
    while not client:
        try:
            client = await network.reconnect()
        except Exception as e:
            print("Exception", e)
            traceback.print_exc()
            await asyncio.sleep(2)
    await network.start_background_threads(client)

if "__main__" == __name__:
    loop = asyncio.new_event_loop()
    loop.run_until_complete(amain())
    loop.run_forever()
