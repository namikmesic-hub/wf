"""A local HTTPS CONNECT proxy that can go silent, as a sleeping laptop's network does: for the
opt-in test of Cursor's own retries (tests/test_cursor.py). While paused, it reads nothing and
sends nothing in either direction, and keeps every connection open. `held` is the longest time it
really held bytes that it had read, so a test can tell that a stream went through it and stalled."""

import asyncio
import threading
import time


class Blackhole:
    def __init__(self):
        self.connects = 0
        self.held = 0.0
        self._until = 0.0
        self._loop = asyncio.new_event_loop()
        self._ready = threading.Event()
        self.url = ""

    def start(self) -> "Blackhole":
        threading.Thread(target=self._run, daemon=True).start()
        self._ready.wait(10)
        return self

    def pause(self, seconds: float) -> None:
        self._until = time.monotonic() + seconds

    def stop(self) -> None:
        self._loop.call_soon_threadsafe(self._loop.stop)

    def _run(self) -> None:
        asyncio.set_event_loop(self._loop)
        server = self._loop.run_until_complete(asyncio.start_server(self._client, "127.0.0.1", 0))
        self.url = f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}"
        self._ready.set()
        self._loop.run_forever()

    async def _held(self) -> None:
        while time.monotonic() < self._until:
            await asyncio.sleep(0.2)

    async def _client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            request = await reader.readuntil(b"\r\n\r\n")
            method, target = request.split(b" ", 2)[:2]
            if method != b"CONNECT":
                writer.close()
                return
            host, port = target.decode().rsplit(":", 1)
            up_reader, up_writer = await asyncio.open_connection(host, int(port))
            self.connects += 1
            writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            await writer.drain()
            await asyncio.gather(self._pipe(reader, up_writer), self._pipe(up_reader, writer))
        except (OSError, asyncio.IncompleteReadError, ValueError):
            writer.close()

    async def _pipe(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while True:
                await self._held()
                data = await reader.read(65536)
                if not data:
                    break
                began = time.monotonic()
                await self._held()  # held: never delivered while paused
                self.held = max(self.held, time.monotonic() - began)
                writer.write(data)
                await writer.drain()
        except OSError:
            pass
        finally:
            writer.close()
