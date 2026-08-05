"""URL shortener service.

Layering (dependencies point downward only)::

    server.py        stdlib HTTP adapter
    app.py           routing, middleware, request/response mapping
    shortener.py     domain rules
    storage/         persistence port + adapters
    models.py        entities
    config/errors/validation/observability/ratelimit/analytics   cross-cutting

Nothing below ``app.py`` imports anything HTTP-related, which is what allows the
domain to be tested without a transport.
"""

__version__ = "1.1.0"
