from __future__ import annotations

import dataclasses
import json
import logging
import os
import urllib.error
import urllib.request
from email.message import Message
from typing import Any

import pytest

from pytools.errors import AppError, ConfigError
from pytools.notify import (
    DeliveryResult,
    Notification,
    Notifier,
    NotifyError,
    Severity,
    SlackWebhookNotifier,
)

SECRET = "SECRETSECRET"
URL = f"https://hooks.slack.com/services/T000/B000/{SECRET}"
OTHER_URL = "https://hooks.slack.com/services/T111/B111/OTHERSECRET"
NOTE = Notification("Disk", "90% full")


class FakeResponse:
    def __init__(self, status: int) -> None:
        self.status = status

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *exc_info: object) -> None:
        return None


class Recorder:
    """Stands in for ``urlopen``: records requests, then returns or raises ``outcome``."""

    def __init__(self, outcome: int | BaseException = 200) -> None:
        self.outcome = outcome
        self.requests: list[urllib.request.Request] = []
        self.timeouts: list[float | None] = []

    def __call__(self, request: urllib.request.Request, timeout: float | None = None) -> Any:
        self.requests.append(request)
        self.timeouts.append(timeout)
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return FakeResponse(self.outcome)


def install(monkeypatch: pytest.MonkeyPatch, outcome: int | BaseException = 200) -> Recorder:
    recorder = Recorder(outcome)
    monkeypatch.setattr(urllib.request, "urlopen", recorder)
    return recorder


def http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(URL, code, "Server Error", Message(), None)


def body_of(request: urllib.request.Request) -> dict[str, str]:
    data = request.data
    assert isinstance(data, bytes)
    parsed: dict[str, str] = json.loads(data)
    return parsed


def payload_text(recorder: Recorder, index: int = 0) -> str:
    return body_of(recorder.requests[index])["text"]


# ---- delivery success ---------------------------------------------------------------


@pytest.mark.parametrize("status", [200, 204])
def test_2xx_is_delivered(monkeypatch: pytest.MonkeyPatch, status: int) -> None:
    recorder = install(monkeypatch, status)
    result = SlackWebhookNotifier(URL).send(NOTE)
    assert result == DeliveryResult(delivered=True, status=status, reason=None)
    assert len(recorder.requests) == 1


def test_request_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    recorder = install(monkeypatch)
    SlackWebhookNotifier(URL, timeout=2.5).send(NOTE)
    request = recorder.requests[0]
    assert request.full_url == URL
    assert request.get_method() == "POST"
    assert request.get_header("Content-type") == "application/json"
    assert recorder.timeouts == [2.5]
    assert body_of(request) == {"text": "*Disk*\n90% full"}


def test_default_timeout_is_about_five_seconds(monkeypatch: pytest.MonkeyPatch) -> None:
    recorder = install(monkeypatch)
    SlackWebhookNotifier(URL).send(NOTE)
    assert recorder.timeouts == [5.0]


# ---- failure paths ------------------------------------------------------------------

FAILURES: list[tuple[str, int | BaseException]] = [
    ("status_500", 500),
    ("status_301", 301),
    ("status_199", 199),
    ("http_error", http_error(503)),
    ("url_error", urllib.error.URLError(f"cannot reach {URL}")),
    ("timeout", TimeoutError(f"timed out talking to {URL}")),
    ("socket_timeout", TimeoutError(URL)),
    ("connection_reset", ConnectionResetError(URL)),
]
FAILURE_OUTCOMES = [f[1] for f in FAILURES]
FAILURE_IDS = [f[0] for f in FAILURES]


@pytest.mark.parametrize("outcome", FAILURE_OUTCOMES, ids=FAILURE_IDS)
def test_failure_returns_not_delivered_and_warns(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, outcome: int | BaseException
) -> None:
    recorder = install(monkeypatch, outcome)
    notifier = SlackWebhookNotifier(URL, destination="chief-of-staff")
    with caplog.at_level(logging.DEBUG):
        result = notifier.send(NOTE)
    assert result.delivered is False
    assert result.reason
    assert len(recorder.requests) == 1  # exactly one attempt
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "chief-of-staff" in caplog.text
    assert SECRET not in caplog.text
    assert URL not in caplog.text
    assert SECRET not in repr(result)
    assert SECRET not in repr(notifier)
    assert SECRET not in str(notifier)


@pytest.mark.parametrize("outcome", FAILURE_OUTCOMES, ids=FAILURE_IDS)
def test_strict_path_raises_notify_error_without_url(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, outcome: int | BaseException
) -> None:
    recorder = install(monkeypatch, outcome)
    notifier = SlackWebhookNotifier(URL)
    with caplog.at_level(logging.DEBUG), pytest.raises(NotifyError) as excinfo:
        notifier.send_or_raise(NOTE)
    assert isinstance(excinfo.value, AppError)
    assert len(recorder.requests) == 1
    assert SECRET not in str(excinfo.value)
    assert SECRET not in repr(excinfo.value)
    assert SECRET not in caplog.text
    assert SECRET not in repr(notifier)
    if isinstance(outcome, BaseException):
        assert excinfo.value.__cause__ is outcome


def test_failure_reason_uses_status_and_class_name(monkeypatch: pytest.MonkeyPatch) -> None:
    install(monkeypatch, http_error(503))
    result = SlackWebhookNotifier(URL).send(NOTE)
    assert result.status == 503
    assert result.reason == "HTTPError"
    install(monkeypatch, urllib.error.URLError("boom"))
    result = SlackWebhookNotifier(URL).send(NOTE)
    assert result.status is None
    assert result.reason == "URLError"
    install(monkeypatch, 500)
    result = SlackWebhookNotifier(URL).send(NOTE)
    assert result.status == 500
    assert result.reason == "HTTP 500"


def test_strict_path_returns_result_on_success(monkeypatch: pytest.MonkeyPatch) -> None:
    install(monkeypatch, 204)
    assert SlackWebhookNotifier(URL).send_or_raise(NOTE).delivered is True


def test_send_does_not_swallow_programming_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    install(monkeypatch, KeyError("bug"))
    with pytest.raises(KeyError):
        SlackWebhookNotifier(URL).send(NOTE)


# ---- construction and env -----------------------------------------------------------


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "   ",
        "http://hooks.slack.com/services/T0/B0/SECRETSECRET",
        "ftp://hooks.slack.com/services/T0/B0/SECRETSECRET",
        "hooks.slack.com/services/T0/B0/SECRETSECRET",
        "https:///services/T0/B0/SECRETSECRET",
    ],
)
def test_malformed_url_raises_config_error_without_leaking(bad: str) -> None:
    with pytest.raises(ConfigError) as excinfo:
        SlackWebhookNotifier(bad)
    assert SECRET not in str(excinfo.value)


def test_non_positive_timeout_is_config_error() -> None:
    with pytest.raises(ConfigError):
        SlackWebhookNotifier(URL, timeout=0)


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    for name in list(os.environ):
        if name.startswith("SLACK_WEBHOOK"):
            monkeypatch.delenv(name)
    return monkeypatch


def test_from_env_destination_wins_over_default(clean_env: pytest.MonkeyPatch) -> None:
    clean_env.setenv("SLACK_WEBHOOK_URL", OTHER_URL)
    clean_env.setenv("SLACK_WEBHOOK_CHIEF_OF_STAFF", URL)
    recorder = install(clean_env)
    SlackWebhookNotifier.from_env("chief-of-staff").send(NOTE)
    assert recorder.requests[0].full_url == URL


def test_from_env_falls_back_to_default(clean_env: pytest.MonkeyPatch) -> None:
    clean_env.setenv("SLACK_WEBHOOK_URL", OTHER_URL)
    recorder = install(clean_env)
    SlackWebhookNotifier.from_env("ops").send(NOTE)
    assert recorder.requests[0].full_url == OTHER_URL


def test_from_env_without_destination_uses_default(clean_env: pytest.MonkeyPatch) -> None:
    clean_env.setenv("SLACK_WEBHOOK_URL", URL)
    recorder = install(clean_env)
    SlackWebhookNotifier.from_env().send(NOTE)
    assert recorder.requests[0].full_url == URL


def test_from_env_nothing_set_is_config_error(clean_env: pytest.MonkeyPatch) -> None:
    with pytest.raises(ConfigError, match="SLACK_WEBHOOK_OPS"):
        SlackWebhookNotifier.from_env("ops")
    with pytest.raises(ConfigError, match="SLACK_WEBHOOK_URL"):
        SlackWebhookNotifier.from_env()


def test_from_env_empty_value_counts_as_unset(clean_env: pytest.MonkeyPatch) -> None:
    clean_env.setenv("SLACK_WEBHOOK_OPS", "")
    clean_env.setenv("SLACK_WEBHOOK_URL", URL)
    recorder = install(clean_env)
    SlackWebhookNotifier.from_env("ops").send(NOTE)
    assert recorder.requests[0].full_url == URL


def test_from_env_malformed_is_config_error_without_leaking(clean_env: pytest.MonkeyPatch) -> None:
    clean_env.setenv("SLACK_WEBHOOK_OPS", f"http://hooks.slack.com/services/T0/B0/{SECRET}")
    with pytest.raises(ConfigError) as excinfo:
        SlackWebhookNotifier.from_env("ops")
    assert SECRET not in str(excinfo.value)
    assert "SLACK_WEBHOOK_OPS" in str(excinfo.value)


def test_repr_hides_url_and_names_destination() -> None:
    text = repr(SlackWebhookNotifier(URL, destination="ops"))
    assert SECRET not in text
    assert "ops" in text
    assert SECRET not in repr(SlackWebhookNotifier(URL))


def test_notifier_satisfies_protocol() -> None:
    assert isinstance(SlackWebhookNotifier(URL), Notifier)


# ---- formatting ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("severity", "prefix"),
    [(Severity.INFO, ""), (Severity.WARN, "*[WARN]* "), (Severity.CRITICAL, "*[CRITICAL]* ")],
)
def test_severity_prefix(monkeypatch: pytest.MonkeyPatch, severity: Severity, prefix: str) -> None:
    recorder = install(monkeypatch)
    SlackWebhookNotifier(URL).send(Notification("T", "B", severity))
    assert payload_text(recorder) == f"{prefix}*T*\nB"


def test_default_severity_is_info() -> None:
    assert Notification("T", "B").severity is Severity.INFO


def test_text_is_not_escaped(monkeypatch: pytest.MonkeyPatch) -> None:
    recorder = install(monkeypatch)
    body = "see <https://example.com|dashboard> & <!here> a>b"
    SlackWebhookNotifier(URL).send(Notification("T", body))
    assert payload_text(recorder) == f"*T*\n{body}"


def test_truncation_boundary(monkeypatch: pytest.MonkeyPatch) -> None:
    marker = "…(truncated)"
    recorder = install(monkeypatch)
    notifier = SlackWebhookNotifier(URL)
    head = "*T*\n"
    notifier.send(Notification("T", "x" * (38_000 - len(head))))
    exact = payload_text(recorder, 0)
    assert len(exact) == 38_000
    assert not exact.endswith(marker)
    notifier.send(Notification("T", "x" * (38_000 - len(head) + 1)))
    cut = payload_text(recorder, 1)
    assert len(cut) == 38_000
    assert cut.endswith(marker)
    assert cut.startswith(head)


def test_prefix_counts_toward_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    recorder = install(monkeypatch)
    SlackWebhookNotifier(URL).send(Notification("T", "x" * 50_000, Severity.CRITICAL))
    text = payload_text(recorder)
    assert len(text) == 38_000
    assert text.startswith("*[CRITICAL]* *T*\n")


# ---- types --------------------------------------------------------------------------


def test_severity_ordering() -> None:
    assert Severity.INFO < Severity.WARN < Severity.CRITICAL
    assert Severity.CRITICAL >= Severity.WARN
    assert sorted([Severity.CRITICAL, Severity.INFO, Severity.WARN]) == [
        Severity.INFO,
        Severity.WARN,
        Severity.CRITICAL,
    ]


def test_value_types_are_frozen() -> None:
    with pytest.raises(dataclasses.FrozenInstanceError):
        NOTE.title = "x"  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        DeliveryResult(delivered=True).delivered = False  # type: ignore[misc]
