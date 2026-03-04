# routes package for DDM Horse Dashboard

from routes.racing_routes import racing_bp, init_racing_service
from routes.guest import guest_ui

__all__ = [
    "racing_bp",
    "init_racing_service",
    "guest_ui",
]
