"""Shared test helpers.

The application is driven through :meth:`Application.handle` rather than over a
socket. That is deliberate: it keeps the suite fast and free of port conflicts,
and it exercises exactly the code path the HTTP adapter calls. Socket-level
behaviour is covered separately in ``test_server.py``.
"""

from __future__ import annotations

import json
from typing import Any, Dict, Optional, Tuple

from service.analytics import AnalyticsRecorder
from service.app import Application, Request, Response, parse_target
from service.config import Config
from service.shortener import ShortenerService
from service.storage import MemoryLinkStore

API_KEY = "test-key-0001"


def make_config(**overrides: Any) -> Config:
    defaults: Dict[str, Any] = {
        "db_path": ":memory:",
        "api_keys": frozenset({API_KEY}),
        "base_url": "http://short.test",
        "rate_limit_enabled": False,
        "analytics_enabled": True,
    }
    defaults.update(overrides)
    return Config(**defaults)


def make_app(config: Optional[Config] = None, store: Optional[MemoryLinkStore] = None,
             limiter=None) -> Tuple[Application, ShortenerService, MemoryLinkStore]:
    config = config or make_config()
    store = store or MemoryLinkStore()
    # Synchronous analytics in tests: an async writer would make click counts
    # arrive after the assertion that reads them.
    service = ShortenerService(store, config, AnalyticsRecorder(store, queue_size=0))
    app = Application(config, service, limiter=limiter)
    return app, service, store


def call(app: Application, method: str, target: str, body: Any = None,
         headers: Optional[Dict[str, str]] = None, api_key: Optional[str] = API_KEY,
         remote_addr: str = "203.0.113.7") -> Tuple[int, Any, Dict[str, str]]:
    """Issue a request and decode the response."""
    path, query = parse_target(target)
    merged = {}
    if api_key is not None:
        merged["x-api-key"] = api_key
    merged.update({k.lower(): v for k, v in (headers or {}).items()})
    payload = b"" if body is None else json.dumps(body).encode("utf-8")
    response: Response = app.handle(
        Request(method, path, query, merged, payload, remote_addr=remote_addr)
    )
    content_type = response.headers.get("Content-Type", "")
    if response.body and content_type.startswith("application/json"):
        decoded: Any = json.loads(response.body)
    elif response.body:
        decoded = response.body.decode("utf-8")
    else:
        decoded = None
    return response.status, decoded, response.headers


def create_link(app: Application, url: str = "https://example.com/page", **payload) -> Dict[str, Any]:
    body = {"url": url}
    body.update(payload)
    status, decoded, _ = call(app, "POST", "/api/v1/links", body)
    assert status in (200, 201), decoded
    return decoded
