from __future__ import annotations

import logging
import random

import httpx
import pytest

from pytools.http import (
    HttpStatusError,
    HttpTransportError,
    RetryPolicy,
    RetryTransport,
    raise_for_status,
)

SECRETS = ("sekret-header-value", "sekret-query-value", "sekret-body-value")
URL = "https://api.example.test/v1/orders?token=sekret-query-value"
HEADERS = {"Authorization": "Bearer sekret-header-value"}
BODY = b'{"signature": "sekret-body-value"}'


def _client(handler: httpx.MockTransport) -> httpx.Client:
    transport = RetryTransport(
        handler,
        policy=RetryPolicy(max_attempts=3),
        rng=random.Random(0),
        sleep=lambda _s: None,
    )
    return httpx.Client(transport=transport)


def _assert_clean(text: str) -> None:
    for secret in SECRETS:
        assert secret not in text


def _all_text(caplog: pytest.LogCaptureFixture, exc: BaseException | None) -> str:
    # httpx's own "httpx" logger prints full URLs at INFO (consumer config); ours is checked.
    parts = [
        record.getMessage() + " " + repr(record.__dict__)
        for record in caplog.records
        if record.name.startswith("pytools")
    ]
    while exc is not None:
        # only our own typed errors are inspected; the httpx cause is test-authored
        if isinstance(exc, HttpStatusError | HttpTransportError):
            parts.append(str(exc) + repr(exc.args) + repr(exc.__dict__))
        exc = exc.__cause__
    return "\n".join(parts)


def test_no_secrets_in_logs_or_errors_on_status_failure(
    caplog: pytest.LogCaptureFixture,
) -> None:
    transport = httpx.MockTransport(lambda request: httpx.Response(503))
    caplog.set_level(logging.DEBUG)
    with _client(transport) as client:
        response = client.post(URL, headers=HEADERS, content=BODY)  # 503 not retried for POST
        response = client.request(
            "GET", URL, headers=HEADERS, content=BODY
        )  # retried to exhaustion
        with pytest.raises(HttpStatusError) as info:
            raise_for_status(response)
    assert any("retrying GET https://api.example.test/v1/orders" in m for m in caplog.messages)
    assert all(r.levelno == logging.WARNING for r in caplog.records if r.name.startswith("pytools"))
    _assert_clean(_all_text(caplog, info.value))
    _assert_clean(str(info.value))


def test_no_secrets_on_429_post_and_transport_failure(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    with _client(httpx.MockTransport(lambda request: httpx.Response(429))) as client:
        client.post(URL, headers=HEADERS, content=BODY)

    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    with _client(httpx.MockTransport(boom)) as client, pytest.raises(HttpTransportError) as info:
        client.post(URL, headers=HEADERS, content=BODY)
    assert caplog.records
    _assert_clean(_all_text(caplog, info.value))
    assert "token=" not in str(info.value)
    assert str(info.value) == "POST https://api.example.test/v1/orders failed: ConnectError"
