import hmac

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from router.api.routes_v1 import run_chat_completion

router = APIRouter()


def _error(message, status_code):
    return JSONResponse(
        status_code=status_code,
        content={'error': {'message': message}},
    )


@router.post('/chat/completions')
async def internal_chat_completions(request: Request):
    expected = request.app.state.settings.gateway_token
    if not expected:
        return _error('gateway_auth_not_configured', 503)
    supplied = request.headers.get('x-sr-gateway-token', '')
    if not supplied or not hmac.compare_digest(supplied, expected):
        return _error('gateway_unauthorized', 401)
    return await run_chat_completion(request, allow_department_header=True)
