from __future__ import annotations

import logging
import random
import socket
import traceback

import httpx
import pytest

from pytools.http import (
    HttpStatusError,
    HttpTransportError,
    RetryPolicy,
    RetryTransport,
    raise_for_status,
)
from pytools.http._errors import safe_url

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


# ---- review fixes: __cause__ leak, safe_url, one-shot bodies ---------------------------


def _chain(exc: BaseException | None) -> list[BaseException]:
    seen: list[BaseException] = []
    while exc is not None and exc not in seen:
        seen.append(exc)
        exc = exc.__cause__ or exc.__context__
    return seen


def test_protocol_error_cause_does_not_leak_header_value() -> None:
    secret = "SECRETSIG"
    transport = RetryTransport(httpx.HTTPTransport(), sleep=lambda _s: None)
    # httpx connects before h11 validates headers, so give it a local listener to connect to.
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        url = f"http://127.0.0.1:{listener.getsockname()[1]}/x"
        with httpx.Client(transport=transport) as client, pytest.raises(HttpTransportError) as info:
            client.get(url, headers={"X-Signature": f"{secret}\nabc"})
    err = info.value
    assert err.error_type == "LocalProtocolError"
    assert err.__cause__ is None
    assert err.__context__ is None
    for link in _chain(err):
        assert secret not in str(link)
        assert secret not in repr(link)
    rendered = "".join(traceback.format_exception(err))
    assert secret not in rendered


def test_safe_cause_classes_are_chained() -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out")

    with (
        httpx.Client(transport=RetryTransport(httpx.MockTransport(boom))) as client,
        pytest.raises(HttpTransportError) as info,
    ):
        client.post("https://api.example.test/x", content=b"x")
    assert isinstance(info.value.__cause__, httpx.ReadTimeout)


def test_safe_url_keeps_path_encoded_and_blocks_log_forgery() -> None:
    url = httpx.URL("https://h.test/a%0AFAKE%20LOG%20LINE?x=1")
    text = safe_url(url)
    assert text == "https://h.test/a%0AFAKE%20LOG%20LINE"
    assert "\n" not in text
    assert "x=1" not in text


def test_safe_url_brackets_ipv6_and_drops_userinfo() -> None:
    text = safe_url(httpx.URL("http://user:pw@[::1]:8080/p?q=secret"))
    assert text == "http://[::1]:8080/p"


def test_forged_path_not_in_retry_log(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    transport = httpx.MockTransport(lambda request: httpx.Response(503))
    with _client(transport) as client:
        client.get("https://h.test/a%0AFAKE%20LOG%20LINE?x=1")
    ours = [r.getMessage() for r in caplog.records if r.name.startswith("pytools")]
    assert ours
    assert all("\n" not in m for m in ours)
