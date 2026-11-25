"""Internal endpoints of the decision kernel (``/internal/v1``).

The gateway-token handshake and the old chat forwarding are gone: APISIX and
the peripheral authenticate the user *before* the decision sub-request, the
kernel receives no user credential and never executes a model request.  This
module keeps the single internal mount point by aggregating the decision
router.
"""
from fastapi import APIRouter

from router.api.routes_decision import router as decision_router

router = APIRouter()
router.include_router(decision_router)
