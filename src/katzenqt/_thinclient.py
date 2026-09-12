import asyncio

from katzenpost_thinclient import ThinClient as BaseThinClient

# Matches network.py's _DAEMON_RPC_TIMEOUT_SECONDS. asyncio.TimeoutError is a
# builtins.TimeoutError subclass (Python 3.11+), which is itself an OSError
# subclass -- the base client's own _reconnect() already catches OSError and
# backs off, so raising this lets its existing retry loop regain control
# instead of hanging on a daemon that never completes the handshake.
_HANDSHAKE_TIMEOUT_SECONDS = 30.0


class ThinClient(BaseThinClient):
    """Handle interleaved events during the 0.0.24 session handshake."""

    _handshake_reads = None

    async def start(self, loop):
        self._handshake_reads = 0
        try:
            return await super().start(loop)
        finally:
            self._handshake_reads = None

    def _create_socket(self):
        self._handshake_reads = 0
        return super()._create_socket()

    async def recv(self, loop):
        response = await super().recv(loop)
        if self._handshake_reads == 2:
            response = await asyncio.wait_for(
                self._drain_until_session_token_reply(loop, response),
                timeout=_HANDSHAKE_TIMEOUT_SECONDS,
            )
        if self._handshake_reads is not None:
            self._handshake_reads += 1
            if self._handshake_reads == 3:
                self._handshake_reads = None
        return response

    async def _drain_until_session_token_reply(self, loop, response):
        """Handle interleaved events (connection_status, pki_doc, ...) sent
        before session_token_reply during the handshake's third read."""
        while response.get("session_token_reply") is None:
            await self.handle_response(response)
            if response.get("shutdown_event") is not None:
                raise BrokenPipeError("Daemon shut down during session handshake")
            response = await super().recv(loop)
        return response
