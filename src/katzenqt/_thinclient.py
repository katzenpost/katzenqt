from katzenpost_thinclient import ThinClient as BaseThinClient


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
            while response.get("session_token_reply") is None:
                await self.handle_response(response)
                if response.get("shutdown_event") is not None:
                    raise BrokenPipeError("Daemon shut down during session handshake")
                response = await super().recv(loop)
        if self._handshake_reads is not None:
            self._handshake_reads += 1
            if self._handshake_reads == 3:
                self._handshake_reads = None
        return response
