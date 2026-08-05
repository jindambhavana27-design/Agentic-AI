"""HTTP application: routing, middleware, and request handlers.

Transport is deliberately framework-free. The application exposes a single
``handle(request) -> Response`` entry point, so the same object is driven by the
stdlib HTTP server in production and called directly in tests -- no sockets, no
fixtures, no flakiness.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Pattern, Tuple
from urllib.parse import parse_qs, urlsplit

from .analytics import AnalyticsRecorder, parse_window
from .config import Config
from .errors import (
    AppError,
    AuthenticationError,
    NotFoundError,
    PayloadTooLargeError,
    RateLimitedError,
    ValidationError,
)
from .observability import METRICS, get_logger, new_request_id, set_request_id
from .ratelimit import TokenBucketRateLimiter
from .shortener import ShortenerService
from .storage import build_store

_log = get_logger("http")

API_PREFIX = "/api/v1"


@dataclass
class Request:
    method: str
    path: str
    query: Dict[str, List[str]] = field(default_factory=dict)
    headers: Dict[str, str] = field(default_factory=dict)
    body: bytes = b""
    remote_addr: str = "127.0.0.1"
    path_params: Dict[str, str] = field(default_factory=dict)
    principal: Optional[str] = None

    def header(self, name: str, default: Optional[str] = None) -> Optional[str]:
        return self.headers.get(name.lower(), default)

    def query_one(self, name: str, default: Optional[str] = None) -> Optional[str]:
        values = self.query.get(name)
        return values[0] if values else default

    def json(self, max_bytes: int) -> Dict[str, Any]:
        if len(self.body) > max_bytes:
            raise PayloadTooLargeError("request body exceeds %d bytes" % max_bytes)
        if not self.body:
            raise ValidationError("request body must be a JSON object")
        try:
            parsed = json.loads(self.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValidationError("request body is not valid JSON: %s" % exc) from exc
        if not isinstance(parsed, dict):
            raise ValidationError("request body must be a JSON object")
        return parsed


@dataclass
class Response:
    status: int = 200
    body: bytes = b""
    headers: Dict[str, str] = field(default_factory=dict)

    @classmethod
    def json(cls, status: int, payload: Any, headers: Optional[Dict[str, str]] = None) -> "Response":
        data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        merged = {"Content-Type": "application/json; charset=utf-8"}
        merged.update(headers or {})
        return cls(status=status, body=data, headers=merged)

    @classmethod
    def redirect(cls, status: int, location: str, headers: Optional[Dict[str, str]] = None) -> "Response":
        merged = {
            "Location": location,
            # Referrer would otherwise leak the short code to the destination,
            # and downstream caches must not pin a link we may retire.
            "Referrer-Policy": "no-referrer",
            "Cache-Control": "private, max-age=0, no-store",
        }
        merged.update(headers or {})
        return cls(status=status, body=b"", headers=merged)

    @classmethod
    def text(cls, status: int, text: str, content_type: str = "text/plain; charset=utf-8") -> "Response":
        return cls(status=status, body=text.encode("utf-8"), headers={"Content-Type": content_type})


Handler = Callable[[Request], Response]


@dataclass
class Route:
    method: str
    pattern: Pattern
    handler: Handler
    name: str
    auth_required: bool = False
    rate_limit_cost: float = 1.0


SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'",
}


class Application:
    def __init__(
        self,
        config: Config,
        service: Optional[ShortenerService] = None,
        limiter: Optional[TokenBucketRateLimiter] = None,
    ) -> None:
        self.config = config
        if service is None:
            store = build_store(config)
            recorder = AnalyticsRecorder(store, enabled=config.analytics_enabled)
            service = ShortenerService(store, config, recorder)
        self.service = service
        self.limiter = limiter or (
            TokenBucketRateLimiter(config.rate_limit_capacity, config.rate_limit_refill_per_sec)
            if config.rate_limit_enabled
            else None
        )
        self.routes: List[Route] = []
        self._register_routes()

    # -- routing --------------------------------------------------------------

    def route(self, method: str, pattern: str, name: str, auth_required: bool = False,
              rate_limit_cost: float = 1.0):
        def decorator(fn: Handler) -> Handler:
            self.routes.append(
                Route(method, re.compile(pattern), fn, name, auth_required, rate_limit_cost)
            )
            return fn

        return decorator

    def _register_routes(self) -> None:
        r = self.route
        r("POST", r"^/api/v1/links$", "create_link", auth_required=True, rate_limit_cost=2.0)(self.handle_create)
        r("GET", r"^/api/v1/links$", "list_links", auth_required=True)(self.handle_list)
        r("GET", r"^/api/v1/links/(?P<code>[A-Za-z0-9_-]{1,64})/stats$", "link_stats", auth_required=True)(self.handle_stats)
        r("GET", r"^/api/v1/links/(?P<code>[A-Za-z0-9_-]{1,64})$", "get_link", auth_required=True)(self.handle_get)
        r("DELETE", r"^/api/v1/links/(?P<code>[A-Za-z0-9_-]{1,64})$", "delete_link", auth_required=True)(self.handle_delete)
        r("GET", r"^/healthz$", "healthz")(self.handle_healthz)
        r("GET", r"^/readyz$", "readyz")(self.handle_readyz)
        r("GET", r"^/metrics$", "metrics")(self.handle_metrics)
        # Catch-all redirect must be registered last so it never shadows a
        # real API route.
        r("GET", r"^/(?P<code>[A-Za-z0-9_-]{1,64})$", "redirect")(self.handle_redirect)

    def _match(self, method: str, path: str) -> Tuple[Optional[Route], Dict[str, str], bool]:
        """Return (route, params, path_matched_other_method)."""
        path_matched = False
        for route in self.routes:
            m = route.pattern.match(path)
            if not m:
                continue
            if route.method != method:
                path_matched = True
                continue
            return route, m.groupdict(), path_matched
        return None, {}, path_matched

    # -- middleware chain -----------------------------------------------------

    def handle(self, request: Request) -> Response:
        request_id = request.header("x-request-id") or new_request_id()
        # Untrusted header: cap length and character set before echoing it back.
        request_id = re.sub(r"[^A-Za-z0-9._-]", "", request_id)[:64] or new_request_id()
        set_request_id(request_id)
        started = time.perf_counter()
        route_name = "unmatched"
        try:
            route, params, other_method = self._match(request.method, request.path)
            if route is None:
                if other_method:
                    response = _error_response(
                        AppError("method not allowed"), request_id, status=405, code="method_not_allowed"
                    )
                else:
                    response = _error_response(NotFoundError("no route for %s" % request.path), request_id)
            else:
                route_name = route.name
                request.path_params = params
                response = self._dispatch(route, request)
        except AppError as exc:
            response = _error_response(exc, request_id)
        except Exception as exc:  # unexpected: log with stack, return opaque 500
            _log.exception("unhandled error: %s" % exc)
            METRICS.increment("unhandled_errors_total")
            response = _error_response(AppError("internal server error"), request_id)

        elapsed = time.perf_counter() - started
        response.headers.setdefault("X-Request-Id", request_id)
        for key, value in SECURITY_HEADERS.items():
            response.headers.setdefault(key, value)

        labels = {"route": route_name, "method": request.method, "status": str(response.status)}
        METRICS.increment("http_requests_total", labels=labels)
        METRICS.observe("http_request_duration_seconds", elapsed, labels={"route": route_name})
        _log.info(
            "request",
            extra={
                "method": request.method,
                "path": request.path,
                "route": route_name,
                "status": response.status,
                "duration_ms": round(elapsed * 1000, 3),
            },
        )
        set_request_id(None)
        return response

    def _dispatch(self, route: Route, request: Request) -> Response:
        if self.limiter is not None:
            identity = self._rate_limit_identity(request)
            allowed, retry_after = self.limiter.allow(identity, route.rate_limit_cost)
            if not allowed:
                METRICS.increment("rate_limited_total", labels={"route": route.name})
                raise RateLimitedError("too many requests", retry_after=retry_after)
        if route.auth_required:
            request.principal = self._authenticate(request)
        return route.handler(request)

    def _rate_limit_identity(self, request: Request) -> str:
        key = request.header("x-api-key")
        if key:
            # Never key the limiter on the raw secret; it ends up in memory
            # dumps and, worse, in logs the moment someone adds debugging.
            import hashlib

            return "key:" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]
        return "ip:" + request.remote_addr

    def _authenticate(self, request: Request) -> Optional[str]:
        if not self.config.require_auth:
            return "anonymous"
        key = request.header("x-api-key")
        if not key:
            raise AuthenticationError("X-API-Key header is required")
        # Compare against every configured key in constant time so a timing
        # side channel cannot confirm a partial guess.
        import hmac

        for candidate in self.config.api_keys:
            if hmac.compare_digest(key, candidate):
                return _principal_for(candidate)
        raise AuthenticationError("invalid API key")

    # -- handlers -------------------------------------------------------------

    def handle_create(self, request: Request) -> Response:
        payload = request.json(self.config.max_body_bytes)
        idem = request.header("idempotency-key")
        if idem is not None and (len(idem) > 128 or not idem.strip()):
            raise ValidationError("Idempotency-Key must be 1-128 characters")

        link, created = self.service.create_link(
            url=payload.get("url"),
            alias=payload.get("alias"),
            ttl_seconds=payload.get("ttl_seconds"),
            metadata=payload.get("metadata"),
            owner=request.principal,
            idempotency_key=idem,
        )
        body = link.to_public_dict(self.config.public_base)
        headers = {"Location": body["short_url"]}
        return Response.json(201 if created else 200, body, headers)

    def handle_get(self, request: Request) -> Response:
        link = self.service.get_link(request.path_params["code"])
        return Response.json(200, link.to_public_dict(self.config.public_base))

    def handle_list(self, request: Request) -> Response:
        limit_raw = request.query_one("limit", "50")
        try:
            limit = int(limit_raw)
        except (TypeError, ValueError):
            raise ValidationError("limit must be an integer", {"limit": limit_raw})
        links, cursor = self.service.list_links(limit, request.query_one("cursor"))
        return Response.json(
            200,
            {
                "items": [l.to_public_dict(self.config.public_base) for l in links],
                "next_cursor": cursor,
            },
        )

    def handle_stats(self, request: Request) -> Response:
        days = parse_window(request.query_one("window"))
        stats = self.service.stats(request.path_params["code"], days)
        payload = stats.to_dict()
        payload["window_days"] = days
        return Response.json(200, payload)

    def handle_delete(self, request: Request) -> Response:
        self.service.delete_link(request.path_params["code"])
        return Response(status=204)

    def handle_redirect(self, request: Request) -> Response:
        link = self.service.resolve(
            request.path_params["code"],
            referrer=request.header("referer"),
            user_agent=request.header("user-agent"),
        )
        return Response.redirect(self.config.redirect_status, link.target_url)

    def handle_healthz(self, request: Request) -> Response:
        return Response.json(200, {"status": "ok", "service": self.config.service_name})

    def handle_readyz(self, request: Request) -> Response:
        healthy = self.service.store.health()
        return Response.json(
            200 if healthy else 503,
            {"status": "ready" if healthy else "degraded", "storage": healthy},
        )

    def handle_metrics(self, request: Request) -> Response:
        return Response.text(200, METRICS.render_prometheus(), "text/plain; version=0.0.4")

    def close(self) -> None:
        self.service.close()


def _principal_for(api_key: str) -> str:
    """Stable, non-reversible principal id derived from the API key."""
    import hashlib

    return "key-" + hashlib.sha256(api_key.encode("utf-8")).hexdigest()[:12]


def _error_response(exc: AppError, request_id: str, status: Optional[int] = None,
                    code: Optional[str] = None) -> Response:
    status = status or exc.status
    payload = exc.to_payload(request_id)
    if code:
        payload["error"]["code"] = code
    headers = {}
    if isinstance(exc, RateLimitedError):
        headers["Retry-After"] = str(max(1, int(exc.retry_after + 0.999)))
    METRICS.increment("errors_total", labels={"code": payload["error"]["code"]})
    return Response.json(status, payload, headers)


def parse_target(raw_path: str) -> Tuple[str, Dict[str, List[str]]]:
    """Split a request target into path and parsed query string."""
    parts = urlsplit(raw_path)
    return parts.path or "/", parse_qs(parts.query, keep_blank_values=True)
