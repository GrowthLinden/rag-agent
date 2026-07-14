from __future__ import annotations

import asyncio
import json
import os

from fastapi import WebSocket


class RealtimeManager:
    def __init__(self) -> None:
        self.customer_connections: dict[tuple[str, str], set[WebSocket]] = {}
        self.admin_connections: set[WebSocket] = set()
        self.send_timeout_seconds = float(os.getenv("REALTIME_SEND_TIMEOUT_SECONDS", "2"))

    async def connect_customer(self, username: str, session_id: str, websocket: WebSocket) -> None:
        await websocket.accept()
        key = (username, session_id)
        self.customer_connections.setdefault(key, set()).add(websocket)

    async def connect_admin(self, websocket: WebSocket) -> None:
        await websocket.accept()
        self.admin_connections.add(websocket)

    def disconnect_customer(self, username: str, session_id: str, websocket: WebSocket) -> None:
        key = (username, session_id)
        connections = self.customer_connections.get(key)
        if not connections:
            return
        connections.discard(websocket)
        if not connections:
            self.customer_connections.pop(key, None)

    def disconnect_admin(self, websocket: WebSocket) -> None:
        self.admin_connections.discard(websocket)

    async def send_to_customer(self, username: str, session_id: str, event: dict) -> None:
        await self._broadcast(self.customer_connections.get((username, session_id), set()), event)

    async def send_to_admins(self, event: dict) -> None:
        await self._broadcast(self.admin_connections, event)

    async def _broadcast(self, connections: set[WebSocket], event: dict) -> None:
        if not connections:
            return
        payload = json.dumps(event, ensure_ascii=False)
        stale = []
        for websocket in list(connections):
            try:
                await asyncio.wait_for(websocket.send_text(payload), timeout=self.send_timeout_seconds)
            except Exception:
                stale.append(websocket)
        for websocket in stale:
            connections.discard(websocket)


realtime_manager = RealtimeManager()
