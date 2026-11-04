"""The surrender API's shared secret, which the frontend proxy sends in
X-API-Secret, is compared in constant time."""

from __future__ import annotations

import hmac

import pytest
from fastapi.testclient import TestClient

import services.surrender_api as api

SECRET = "s" * 43


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(api, "API_SECRET", SECRET)
    return TestClient(api.app)


@pytest.mark.parametrize("headers", [{}, {"X-API-Secret": "wrong"}, {"X-API-Secret": SECRET + "x"}])
def test_requests_without_the_secret_are_forbidden(client, headers):
    assert client.post("/build-surrender", json={}, headers=headers).status_code == 403


def test_the_secret_is_compared_in_constant_time(client, monkeypatch):
    calls = []
    real = hmac.compare_digest

    def spy(a, b):
        calls.append((a, b))
        return real(a, b)

    monkeypatch.setattr(api.hmac, "compare_digest", spy)
    client.post("/build-surrender", json={}, headers={"X-API-Secret": "wrong"})
    assert calls == [(b"wrong", SECRET.encode())]


def test_the_right_secret_passes_the_check(client):
    """It reaches the route, which refuses the empty request itself."""
    assert client.post("/build-surrender", json={}, headers={"X-API-Secret": SECRET}).status_code == 422
