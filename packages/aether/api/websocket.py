"""
WebSocket endpoint for Aether realtime events.

Provides:
- Real-time event streaming for proposals, promotions, and head updates
- Per-repository subscription
- Event type filtering
"""

import asyncio
import hmac
import json
import os
from typing import Optional, Set
import structlog

from fastapi import WebSocket, WebSocketDisconnect

from ..models.events import AetherEvent, EventType
from ..services.event_bus import EventBus

logger = structlog.get_logger()

DEFAULT_WS_ALLOWED_ORIGINS = [
    "http://localhost:3000",
    "http://127.0.0.1:3000",
    "http://localhost:8100",
    "http://127.0.0.1:8100",
]


def _resolve_allowed_ws_origins() -> list[str]:
    """Resolve allowed browser Origin headers for WebSocket handshakes."""
    raw = os.getenv("AETHER_CORS_ORIGINS", "").strip()
    if not raw:
        return list(DEFAULT_WS_ALLOWED_ORIGINS)
    origins = [origin.strip() for origin in raw.split(",") if origin.strip()]
    allow_wildcard = os.getenv("AETHER_ALLOW_UNSAFE_CORS_WILDCARD", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "y",
        "on",
    }
    if "*" in origins and not allow_wildcard:
        return [o for o in origins if o != "*"] or list(DEFAULT_WS_ALLOWED_ORIGINS)
    return origins or list(DEFAULT_WS_ALLOWED_ORIGINS)


def _is_websocket_origin_allowed(websocket: WebSocket) -> bool:
    """Validate browser Origin header when present on a WebSocket handshake."""
    origin = websocket.headers.get("origin", "").strip()
    if not origin:
        return True
    allowed_origins = _resolve_allowed_ws_origins()
    if "*" in allowed_origins:
        return True
    return origin in allowed_origins


def _is_websocket_authenticated(websocket: WebSocket) -> bool:
    """Validate AETHER_API_TOKEN for WebSocket handshakes when configured."""
    configured_token = os.getenv("AETHER_API_TOKEN", "").strip()
    if not configured_token:
        return True

    auth_header = websocket.headers.get("authorization", "")
    bearer_token = (
        auth_header[7:].strip()
        if auth_header.lower().startswith("bearer ")
        else ""
    )
    header_token = websocket.headers.get("x-aether-token", "").strip()
    query_token = websocket.query_params.get("token", "").strip()
    candidate = bearer_token or header_token or query_token
    return bool(candidate) and hmac.compare_digest(candidate, configured_token)


class ConnectionManager:
    """Manages WebSocket connections."""

    def __init__(self, event_bus: EventBus):
        self.event_bus = event_bus
        self.active_connections: dict[str, WebSocket] = {}
        self.subscriptions: dict[str, dict] = {}  # connection_id -> {repo_id, event_types}

    async def connect(
        self,
        websocket: WebSocket,
        connection_id: str,
        repo_id: Optional[str] = None,
        event_types: Optional[list[str]] = None,
    ):
        """Accept a new WebSocket connection."""
        await websocket.accept()
        self.active_connections[connection_id] = websocket

        # Set up subscription
        type_set = None
        if event_types:
            type_set = {EventType(et) for et in event_types}

        self.subscriptions[connection_id] = {
            "repo_id": repo_id,
            "event_types": type_set,
        }

        # Subscribe to event bus
        self.event_bus.subscribe(
            callback=lambda event: asyncio.create_task(self._send_event(connection_id, event)),
            repo_id=repo_id,
            event_types=list(type_set) if type_set else None,
            is_async=False,  # We use create_task instead
        )

        logger.info(
            "websocket_connected",
            connection_id=connection_id,
            repo_id=repo_id,
            event_types=event_types,
        )

        # Send welcome message
        await websocket.send_json({
            "type": "connected",
            "connection_id": connection_id,
            "repo_id": repo_id,
            "subscribed_events": event_types or "all",
        })

    def disconnect(self, connection_id: str):
        """Remove a WebSocket connection."""
        if connection_id in self.active_connections:
            del self.active_connections[connection_id]

        if connection_id in self.subscriptions:
            del self.subscriptions[connection_id]

        logger.info("websocket_disconnected", connection_id=connection_id)

    async def _send_event(self, connection_id: str, event: AetherEvent):
        """Send an event to a specific connection."""
        if connection_id not in self.active_connections:
            return

        websocket = self.active_connections[connection_id]
        sub = self.subscriptions.get(connection_id, {})

        # Check repo filter
        if sub.get("repo_id") and sub["repo_id"] != event.repo_id:
            return

        # Check event type filter
        if sub.get("event_types") and event.event_type not in sub["event_types"]:
            return

        try:
            await websocket.send_json({
                "type": "event",
                "event_id": event.event_id,
                "event_type": event.event_type.value,
                "repo_id": event.repo_id,
                "timestamp": event.timestamp.isoformat(),
                "payload": event.payload,
            })
        except Exception as e:
            logger.warning(
                "websocket_send_failed",
                connection_id=connection_id,
                error=str(e),
            )

    async def broadcast(self, event: AetherEvent):
        """Broadcast an event to all matching connections."""
        for connection_id in list(self.active_connections.keys()):
            await self._send_event(connection_id, event)


# Global connection manager (will be initialized by server)
_manager: Optional[ConnectionManager] = None


def get_connection_manager() -> Optional[ConnectionManager]:
    """Get the connection manager."""
    return _manager


def set_connection_manager(manager: ConnectionManager):
    """Set the connection manager."""
    global _manager
    _manager = manager


async def websocket_endpoint(
    websocket: WebSocket,
    repo_id: Optional[str] = None,
):
    """
    WebSocket endpoint for realtime events.

    Query params:
    - repo_id: Filter to specific repository (optional)
    - events: Comma-separated list of event types to subscribe to (optional)
    """
    if not _is_websocket_origin_allowed(websocket):
        await websocket.close(code=1008, reason="Forbidden: Origin not allowed")
        return

    if not _is_websocket_authenticated(websocket):
        await websocket.close(code=1008, reason="Unauthorized: valid AETHER_API_TOKEN required")
        return

    manager = get_connection_manager()
    if not manager:
        await websocket.close(code=1011, reason="Service not available")
        return

    # Parse event types from query params
    event_types = None
    if "events" in websocket.query_params:
        event_types = websocket.query_params["events"].split(",")

    # Generate connection ID
    from ..models.entities import generate_id
    connection_id = generate_id("ws")

    try:
        await manager.connect(
            websocket,
            connection_id,
            repo_id=repo_id,
            event_types=event_types,
        )

        # Keep connection alive and handle incoming messages
        while True:
            try:
                # Wait for messages (primarily for ping/pong and graceful close)
                data = await websocket.receive_text()

                # Parse message
                try:
                    message = json.loads(data)
                except json.JSONDecodeError:
                    await websocket.send_json({
                        "type": "error",
                        "message": "Invalid JSON",
                    })
                    continue

                # Handle message types
                if message.get("type") == "ping":
                    await websocket.send_json({"type": "pong"})

                elif message.get("type") == "subscribe":
                    # Update subscription
                    new_repo_id = message.get("repo_id")
                    new_event_types = message.get("event_types")

                    manager.subscriptions[connection_id] = {
                        "repo_id": new_repo_id,
                        "event_types": {EventType(et) for et in new_event_types} if new_event_types else None,
                    }

                    await websocket.send_json({
                        "type": "subscribed",
                        "repo_id": new_repo_id,
                        "event_types": new_event_types or "all",
                    })

                elif message.get("type") == "get_recent":
                    # Send recent events
                    recent = manager.event_bus.get_recent_events(
                        repo_id=message.get("repo_id"),
                        limit=message.get("limit", 50),
                    )
                    await websocket.send_json({
                        "type": "recent_events",
                        "events": [
                            {
                                "event_id": e.event_id,
                                "event_type": e.event_type.value,
                                "repo_id": e.repo_id,
                                "timestamp": e.timestamp.isoformat(),
                                "payload": e.payload,
                            }
                            for e in recent
                        ],
                    })

            except WebSocketDisconnect:
                break

    finally:
        manager.disconnect(connection_id)


# Event types that clients can subscribe to
SUBSCRIBABLE_EVENTS = [
    EventType.PROPOSAL_SUBMITTED.value,
    EventType.PROPOSAL_EVALUATING.value,
    EventType.PROPOSAL_PROMOTED.value,
    EventType.PROPOSAL_REJECTED.value,
    EventType.PROPOSAL_QUARANTINED.value,
    EventType.HEAD_UPDATED.value,
    EventType.HEAD_ROLLBACK.value,
    EventType.CONFLICT_DETECTED.value,
    EventType.CONFLICT_RESOLVED.value,
    EventType.GATE_STARTED.value,
    EventType.GATE_COMPLETED.value,
    EventType.GATE_FAILED.value,
    EventType.SNAPSHOT_CREATED.value,
    EventType.PROMOTION_STARTED.value,
    EventType.PROMOTION_COMPLETED.value,
    EventType.PROMOTION_FAILED.value,
]
