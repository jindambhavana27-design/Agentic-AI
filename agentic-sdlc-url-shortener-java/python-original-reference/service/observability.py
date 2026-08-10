"""Structured logging, request correlation, and an in-process metrics registry.

Logs are emitted as single-line JSON so they are greppable by machine without a
parsing sidecar. Metrics are exposed in Prometheus text format at ``/metrics``.
The registry is deliberately tiny -- counters and fixed-bucket histograms cover
what this service needs, and pulling in a client library for that is not worth
the dependency.
"""

from __future__ import annotations

import json
import logging
import sys
import threading
import time
import uuid
from contextlib import contextmanager
from typing import Any, Dict, Iterable, Optional, Tuple

_request_id: "threading.local" = threading.local()

# Fields the logging module puts on every record; anything else on the record is
# treated as caller-supplied structured context.
_STANDARD_FIELDS = frozenset(vars(logging.LogRecord("", 0, "", 0, "", (), None)).keys()) | {
    "message", "asctime", "taskName",
}


def new_request_id() -> str:
    return uuid.uuid4().hex[:16]


def set_request_id(value: Optional[str]) -> None:
    _request_id.value = value


def get_request_id() -> Optional[str]:
    return getattr(_request_id, "value", None)


class JsonFormatter(logging.Formatter):
    def __init__(self, service: str = "url-shortener") -> None:
        super().__init__()
        self.service = service

    def format(self, record: logging.LogRecord) -> str:
        payload: Dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(record.created)),
            "level": record.levelname,
            "service": self.service,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        rid = get_request_id()
        if rid:
            payload["request_id"] = rid
        for key, value in record.__dict__.items():
            if key not in _STANDARD_FIELDS and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str, separators=(",", ":"))


def configure_logging(level: str = "INFO", service: str = "url-shortener") -> None:
    root = logging.getLogger("shortener")
    root.handlers.clear()
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter(service))
    root.addHandler(handler)
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    root.propagate = False


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger("shortener.%s" % name)


# -- metrics ------------------------------------------------------------------

_LATENCY_BUCKETS = (0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0)

Labels = Tuple[Tuple[str, str], ...]


def _labels(mapping: Optional[Dict[str, str]]) -> Labels:
    if not mapping:
        return ()
    return tuple(sorted(mapping.items()))


class MetricsRegistry:
    """Thread-safe counters and histograms."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters: Dict[Tuple[str, Labels], float] = {}
        self._histograms: Dict[Tuple[str, Labels], Dict[str, Any]] = {}
        self._gauges: Dict[Tuple[str, Labels], float] = {}

    def increment(self, name: str, value: float = 1.0, labels: Optional[Dict[str, str]] = None) -> None:
        key = (name, _labels(labels))
        with self._lock:
            self._counters[key] = self._counters.get(key, 0.0) + value

    def set_gauge(self, name: str, value: float, labels: Optional[Dict[str, str]] = None) -> None:
        with self._lock:
            self._gauges[(name, _labels(labels))] = value

    def observe(self, name: str, value: float, labels: Optional[Dict[str, str]] = None) -> None:
        key = (name, _labels(labels))
        with self._lock:
            hist = self._histograms.get(key)
            if hist is None:
                hist = {"buckets": {b: 0 for b in _LATENCY_BUCKETS}, "count": 0, "sum": 0.0}
                self._histograms[key] = hist
            hist["count"] += 1
            hist["sum"] += value
            for bound in _LATENCY_BUCKETS:
                if value <= bound:
                    hist["buckets"][bound] += 1

    @contextmanager
    def timer(self, name: str, labels: Optional[Dict[str, str]] = None):
        start = time.perf_counter()
        try:
            yield
        finally:
            self.observe(name, time.perf_counter() - start, labels)

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "counters": {_fmt(k): v for k, v in self._counters.items()},
                "gauges": {_fmt(k): v for k, v in self._gauges.items()},
                "histograms": {
                    _fmt(k): {"count": v["count"], "sum": v["sum"]}
                    for k, v in self._histograms.items()
                },
            }

    def reset(self) -> None:
        with self._lock:
            self._counters.clear()
            self._histograms.clear()
            self._gauges.clear()

    def render_prometheus(self) -> str:
        lines: list = []
        with self._lock:
            counters = dict(self._counters)
            gauges = dict(self._gauges)
            histograms = {k: {"buckets": dict(v["buckets"]), "count": v["count"], "sum": v["sum"]}
                          for k, v in self._histograms.items()}
        for (name, labels), value in sorted(counters.items()):
            lines.append("# TYPE %s counter" % name)
            lines.append("%s%s %g" % (name, _render_labels(labels), value))
        for (name, labels), value in sorted(gauges.items()):
            lines.append("# TYPE %s gauge" % name)
            lines.append("%s%s %g" % (name, _render_labels(labels), value))
        for (name, labels), hist in sorted(histograms.items()):
            lines.append("# TYPE %s histogram" % name)
            for bound, count in sorted(hist["buckets"].items()):
                extra = labels + (("le", str(bound)),)
                lines.append("%s_bucket%s %d" % (name, _render_labels(extra), count))
            inf = labels + (("le", "+Inf"),)
            lines.append("%s_bucket%s %d" % (name, _render_labels(inf), hist["count"]))
            lines.append("%s_sum%s %g" % (name, _render_labels(labels), hist["sum"]))
            lines.append("%s_count%s %d" % (name, _render_labels(labels), hist["count"]))
        return "\n".join(lines) + "\n"


def _render_labels(labels: Iterable[Tuple[str, str]]) -> str:
    items = list(labels)
    if not items:
        return ""
    inner = ",".join('%s="%s"' % (k, str(v).replace('"', '\\"')) for k, v in items)
    return "{%s}" % inner


def _fmt(key: Tuple[str, Labels]) -> str:
    name, labels = key
    return name + _render_labels(labels)


METRICS = MetricsRegistry()
