"""
TODO: Run the client without the Qt GUI
"""
import network

if "__main__" == __name__:
    client = asyncio.run_until_complete(network.reconnect())
    asyncio.run_until_complete(network.start_background_threads(client))
