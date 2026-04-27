from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import datetime, timezone
from uuid import uuid4

from fastapi import APIRouter
from fastapi.responses import StreamingResponse

router = APIRouter()


QUEUE_MAXSIZE = 500
HEARTBEAT_SECONDS = 15.0


@dataclass
class EventStream:
    subscribers: set[asyncio.Queue[str]] = field(default_factory=set)
    dropped_messages: int = 0

    async def publish(self, event: str, data: dict) -> None:
        enriched = {
            "event_id": data.get("event_id") or str(uuid4()),
            "event_type": data.get("event_type") or event,
            "emitted_at": data.get("emitted_at") or datetime.now(timezone.utc).isoformat(),
            **data,
        }
        payload = f"event: {event}\ndata: {json.dumps(enriched, separators=(',', ':'))}\n\n"
        for queue in list(self.subscribers):
            try:
                queue.put_nowait(payload)
            except asyncio.QueueFull:
                # Backpressure: drop the oldest message to make room so the client
                # keeps the newer state. Never drop the subscriber itself — UI
                # clients can't easily reconnect mid-run.
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
                try:
                    queue.put_nowait(payload)
                except asyncio.QueueFull:
                    self.dropped_messages += 1
                    pass

    async def subscribe(self) -> AsyncIterator[str]:
        queue: asyncio.Queue[str] = asyncio.Queue(maxsize=QUEUE_MAXSIZE)
        self.subscribers.add(queue)
        try:
            yield ": connected\n\n"
            while True:
                try:
                    # Wait for an event up to HEARTBEAT_SECONDS, then emit a comment
                    # to keep proxies / clients from timing the connection out.
                    yield await asyncio.wait_for(queue.get(), timeout=HEARTBEAT_SECONDS)
                except asyncio.TimeoutError:
                    yield ": heartbeat\n\n"
        finally:
            self.subscribers.discard(queue)


stream = EventStream()


@router.get("/stream")
async def sse_stream() -> StreamingResponse:
    return StreamingResponse(
        stream.subscribe(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )
