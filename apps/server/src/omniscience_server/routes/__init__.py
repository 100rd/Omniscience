"""Route package — each module registers one logical group of endpoints."""

from omniscience_server.routes.health import router as health_router

__all__ = ["health_router"]
