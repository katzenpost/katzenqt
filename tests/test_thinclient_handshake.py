import asyncio
import struct

import cbor2
import pytest
from katzenpost_thinclient import Config

from katzenqt._thinclient import ThinClient


@pytest.mark.asyncio
@pytest.mark.parametrize("interleaved, shutdown", [(False, False), (True, False), (True, True)])
async def test_session_handshake_preserves_events_on_start_and_reconnect(
    tmp_path, interleaved, shutdown,
):
    statuses = []
    epochs = []
    tokens = []
    handlers = []

    async def status(event):
        statuses.append(event["is_connected"])

    async def pki(event):
        epochs.append(cbor2.loads(event["payload"])["Epoch"])

    async def serve(reader, writer):
        handlers.append(asyncio.current_task())

        async def send(response):
            payload = cbor2.dumps(response)
            writer.write(struct.pack('>I', len(payload)) + payload)
            await writer.drain()

        try:
            await send({"connection_status_event": {"is_connected": True}})
            await send({"new_pki_document_event": {"payload": cbor2.dumps({"Epoch": 1})}})
            size = struct.unpack('>I', await reader.readexactly(4))[0]
            request = cbor2.loads(await reader.readexactly(size))
            tokens.append(request["session_token"]["client_instance_token"])
            if shutdown:
                await send({"shutdown_event": {"reason": "test"}})
                await reader.read()
                return
            if interleaved:
                await send({"encrypt_read_reply": {"query_id": b"pending", "reply": "ready"}})
                await send({"new_pki_document_event": {"payload": cbor2.dumps({"Epoch": 2})}})
                await send({"connection_status_event": {"is_connected": False}})
                await send({"connection_status_event": {"is_connected": True}})
            await send({"session_token_reply": {"resumed": len(tokens) > 1}})
            await reader.read()
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(serve, '127.0.0.1', 0)
    port = server.sockets[0].getsockname()[1]
    config = tmp_path / 'thinclient.toml'
    config.write_text(f'[Dial.Tcp]\nAddress = "127.0.0.1:{port}"\nNetwork = "tcp"\n')
    client = ThinClient(Config(str(config), on_connection_status=status, on_new_pki_document=pki))
    client.response_queues[b"pending"] = asyncio.Queue()
    loop = asyncio.get_running_loop()
    try:
        if shutdown:
            with pytest.raises(BrokenPipeError, match="Daemon shut down"):
                await asyncio.wait_for(client.start(loop), 2)
            assert client._received_shutdown
            return
        await asyncio.wait_for(client.start(loop), 2)
        client.task.cancel()
        await asyncio.gather(client.task, return_exceptions=True)
        client.socket.close()
        await asyncio.wait_for(client._reconnect(loop), 2)
        assert tokens == [client.instance_token, client.instance_token]
        assert epochs == ([1, 2, 1, 2] if interleaved else [1, 1])
        assert statuses == ([True, False, True] * 2 if interleaved else [True, True])
        if interleaved:
            for _ in range(2):
                reply = await asyncio.wait_for(client.response_queues[b"pending"].get(), 1)
                assert reply["reply"] == "ready"
        assert client._handshake_reads is None
    finally:
        client._stopping = True
        client.socket.close()
        if hasattr(client, 'task'):
            client.task.cancel()
            await asyncio.gather(client.task, return_exceptions=True)
        server.close()
        await server.wait_closed()
        await asyncio.gather(*handlers)
