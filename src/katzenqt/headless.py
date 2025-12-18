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

# await network.conversation_update_queue.get() # network.py writes here when a ConversationLog is updated, the queue items are 'int' PK for persistent.Conversation
# await network.check_for_new() # when we have written a new SendOperation to PlaintextWAL
# await network.signal_readables_to_mixwal() # when we have written a new ConversationPeer/ReadCapWAL.active==True

if "__main__" == __name__:
    loop = asyncio.new_event_loop()
    loop.run_until_complete(amain())
    loop.run_forever()
