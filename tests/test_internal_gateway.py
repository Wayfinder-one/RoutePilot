import httpx
import pytest
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from router.api.app import create_app
from tests.routing_fixtures import test_settings


def make_client(tmp_path, token=''):
    app = create_app(test_settings(
        tmp_path, gateway_token=token,
        jev_url='', jev_model='',
    ), transport=httpx.MockTransport(lambda request: httpx.Response(503)))
    return TestClient(app)


def test_internal_route_fails_closed_when_token_not_configured(tmp_path):
    client = make_client(tmp_path)
    response = client.post('/internal/v1/chat/completions', json={
        'model': 'auto', 'messages': [{'role': 'user', 'content': '你好'}],
    })
    assert response.status_code == 503
    assert response.json() == {'error': {'message': 'gateway_auth_not_configured'}}


def test_internal_route_rejects_missing_and_wrong_token(tmp_path):
    client = make_client(tmp_path, token='expected')
    for supplied in (None, 'wrong'):
        headers = {} if supplied is None else {'X-SR-Gateway-Token': supplied}
        response = client.post('/internal/v1/chat/completions', headers=headers, json={
            'model': 'auto', 'messages': [{'role': 'user', 'content': '你好'}],
        })
        assert response.status_code == 401
        assert response.json() == {'error': {'message': 'gateway_unauthorized'}}


def test_internal_route_forwards_to_shared_executor(tmp_path, monkeypatch):
    client = make_client(tmp_path, token='expected')
    called = {}

    async def spy(request, **kwargs):
        called.update(kwargs)
        return JSONResponse({'ok': True})

    monkeypatch.setattr('router.api.routes_internal.run_chat_completion', spy)
    response = client.post('/internal/v1/chat/completions',
        headers={'X-SR-Gateway-Token': 'expected'},
        json={'model': 'auto', 'messages': [{'role': 'user', 'content': '你好'}]})
    assert response.status_code == 200
    assert called['allow_department_header'] is True
    assert 'use_requested_model' not in called


def test_internal_route_requires_gateway_token_even_when_authorization_present(tmp_path):
    """A user Bearer token must never authenticate the internal endpoint."""
    client = make_client(tmp_path, token='expected')
    response = client.post(
        '/internal/v1/chat/completions',
        headers={'Authorization': 'Bearer user-key', 'X-SR-Gateway-Token': 'wrong'},
        json={'model': 'auto', 'messages': [{'role': 'user', 'content': '你好'}]})
    assert response.status_code == 401


def test_internal_route_decodes_department_header(tmp_path, monkeypatch):
    """The gateway-authenticated endpoint accepts the department header."""
    client = make_client(tmp_path, token='expected')
    seen = {}

    async def spy(request, **kwargs):
        seen['department'] = await request.json()
        return JSONResponse({'ok': True})

    monkeypatch.setattr('router.api.routes_internal.run_chat_completion', spy)
    response = client.post(
        '/internal/v1/chat/completions',
        headers={'X-SR-Gateway-Token': 'expected', 'X-SR-Department': '%E8%B4%A2%E5%8A%A1%E9%83%A8'},
        json={'model': 'auto', 'messages': [{'role': 'user', 'content': '你好'}]})
    assert response.status_code == 200
    assert seen['department']['model'] == 'auto'


def test_public_v1_rejects_forged_department_header(tmp_path):
    """The public /v1 entry must keep refusing X-SR-Department."""
    client = make_client(tmp_path, token='expected')
    response = client.post(
        '/v1/chat/completions',
        headers={'X-SR-Department': '%E8%B4%A2%E5%8A%A1%E9%83%A8'},
        json={'model': 'auto', 'messages': [{'role': 'user', 'content': '你好'}]})
    assert response.status_code == 400
    assert response.json() == {'error': {'message': 'department_header_not_allowed'}}


def test_internal_route_rejects_invalid_department_before_executor(tmp_path):
    """Malformed percent-encoding is rejected before any routing work happens."""
    client = make_client(tmp_path, token="expected")
    response = client.post(
        "/internal/v1/chat/completions",
        headers={"X-SR-Gateway-Token": "expected", "X-SR-Department": "%zz"},
        json={"model": "auto", "messages": [{"role": "user", "content": "你好"}]},
    )
    assert response.status_code == 400
    assert response.json() == {"error": {"message": "invalid_department"}}


def test_internal_route_forwards_valid_department_without_monkeypatch(tmp_path):
    """A valid header reaches the real executor path (not just the DTO echo)."""
    client = make_client(tmp_path, token="expected")
    # Empty route set in this test workspace: the shared executor answers 503
    # no_active_tree, which proves the request passed auth and department
    # decoding rather than being rejected before routing.
    response = client.post(
        "/internal/v1/chat/completions",
        headers={"X-SR-Gateway-Token": "expected",
                 "X-SR-Department": "%E8%B4%A2%E5%8A%A1%E9%83%A8"},
        json={"model": "auto", "messages": [{"role": "user", "content": "你好"}]},
    )
    assert response.status_code == 503
    assert response.json() == {"error": {"message": "no_active_tree"}}
