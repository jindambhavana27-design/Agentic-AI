"""Tests for the URL shortener service.

This package is copied into every orchestration workspace and executed by the
test agent, so it must depend only on ``service`` and the standard library.
"""

import logging

# Several tests deliberately drive error paths that log at WARNING or ERROR.
# Without a handler those records reach the last-resort handler and scribble
# over the test output, which makes a real failure hard to spot.
_root = logging.getLogger("shortener")
_root.handlers[:] = [logging.NullHandler()]
_root.propagate = False
