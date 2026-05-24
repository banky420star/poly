import asyncio
import json
from pathlib import Path
from datetime import datetime, timezone


class Recorder:
    """Writes events to JSONL and optionally broadcasts via WebSocket."""

    def __init__(self, path: str = "logs/snapshots.jsonl"):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._ws_clients: set = set()

    def attach_ws(self, clients: set):
        """Share a set of websocket connections for live broadcast."""
        self._ws_clients = clients

    def write(self, event: str, payload: dict):
        row = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "event": event,
            **payload,
        }

        line = json.dumps(row, default=str)

        with self.path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")

        if self._ws_clients:
            stale = set()
            for ws in list(self._ws_clients):
                try:
                    asyncio.ensure_future(ws.send(line))
                except Exception:
                    stale.add(ws)
            self._ws_clients.difference_update(stale)
