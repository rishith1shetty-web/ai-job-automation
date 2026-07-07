from app.services.portals.base import PortalAdapter, PortalApplyResult, PortalJobResult
from app.services.portals.dice_portal import DicePortalAdapter
from app.services.portals.linkedin_portal import LinkedInPortalAdapter

__all__ = [
    "DicePortalAdapter",
    "LinkedInPortalAdapter",
    "PortalAdapter",
    "PortalApplyResult",
    "PortalJobResult",
]
