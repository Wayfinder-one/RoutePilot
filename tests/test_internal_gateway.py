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
