"""REST API v1 module for Omniscience.

Exposes the aggregated v1 router and the error handler registration function.

Usage::

    from omniscience_server.rest import api_v1_router, register_error_handlers

    app.include_router(api_v1_router)
    register_error_handlers(app)
"""

from __future__ import annotations

from omniscience_server.rest.errors import register_error_handlers
from omniscience_server.rest.router import api_v1_router

__all__ = ["api_v1_router", "register_error_handlers"]
