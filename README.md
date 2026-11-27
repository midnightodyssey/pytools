# pytools

House-style Python plumbing shared across midnightodyssey projects: strict Decimal money
parsing, typed errors, and standard logging setup. Used by our projects.

**Plumbing only — analytics go to [quant-utils](https://github.com/midnightodyssey/quant-utils).**

## Install

```
pip install "pytools @ git+https://github.com/midnightodyssey/pytools.git@v0.4.0"
```

Optional extras: `pytools[pydantic]`, `pytools[click]`, `pytools[http]`, `pytools[crypto]`.
No runtime dependencies otherwise.
Requires Python >= 3.12. CI tests 3.12 and 3.14.

## Modules

- `pytools.money` — `parse_decimal` / `money`: a `str` goes straight to `Decimal(str)`
  (`"0.1"` is exactly `Decimal("0.1")`); a `float` uses its shortest repr; `bool`, NaN,
  Infinity and garbage raise `MoneyParseError`.
  - `pytools.money.pydantic.MoneyDecimal` — `Annotated` pydantic v2 type (`pytools[pydantic]`).
  - `pytools.money.click.DECIMAL` / `DecimalParamType` — click option type (`pytools[click]`).
- `pytools.errors` — `AppError`, `ConfigError`, and the `wrap_errors` context manager
  (wrap and re-raise as a typed error `from exc`).
- `pytools.logging` — `configure_logging(app_name, ...)`: rotating file handler plus console,
  house format, never blocks startup.
- `pytools.http` (`pytools[http]`) — httpx `RetryTransport` / `AsyncRetryTransport`:
  exponential backoff with full jitter on 429 and transient 5xx, honouring `Retry-After`
  (capped by `RetryPolicy.max_retry_after`), plus `RateLimiter` / `AsyncRateLimiter` and
  typed `HttpError`s. 5xx and ambiguous transport failures are retried **only for idempotent
  methods** unless `RetryPolicy(retry_non_idempotent=True)`, so by default a POST is
  re-sent only when the server cannot have processed it (429, or the connection was never
  made). Narrow `RetryPolicy(idempotent_methods=...)` if a venue uses PUT/DELETE for order
  amends. One-shot (iterator/generator) bodies are sent once and never retried. Retries
  replay the identical request, so a signed request reuses its timestamp and nonce. Logs and errors never carry bodies, header
  values or query strings, but httpx's own `httpx` logger prints full URLs at INFO: raise it
  to WARNING if you put secrets in query strings.

  ```python
  client = httpx.Client(transport=RetryTransport(policy=RetryPolicy(), rate_limiter=RateLimiter(10, 1.0)))
  response = raise_for_status(client.get("https://api.example.com/v1/markets"))
  ```
- `pytools.crypto` (`pytools[crypto]`) — `load_private_key(Path | bytes, password=None)` for
  Ed25519 or RSA PEM keys (any other type raises `UnsupportedKeyTypeError` naming it; a bare
  `str` is rejected as ambiguous), `sign(key, message)` (Ed25519, or RSA-PSS SHA-256 with
  MGF1-SHA-256 and a 32-byte salt by default), and strict standard / URL-safe base64 helpers.
  Callers build their own pre-sign strings and headers; nothing venue-specific lives here.
- `pytools.killswitch` — `KillSwitch(path)`: file-backed kill switch, fail-closed (any doubt is
  armed), `os.stat` on every `status()` call (no caching), atomic `arm()`, and a hand `touch`
  arms it. File contents never decide armed; JSON `{armed_at, reason, actor}` is compatible with
  predarb's format. `raise_if_armed()` raises `KillSwitchArmedError`.

- `pytools.notify` — best-effort Slack incoming-webhook notifications, stdlib only. The
  webhook URL is a secret and never reaches a log line, `repr` or exception message.
  `SlackWebhookNotifier.from_env("chief-of-staff")` reads `SLACK_WEBHOOK_CHIEF_OF_STAFF`, then
  `SLACK_WEBHOOK_URL`. `send()` never raises on delivery failure (WARNING + `delivered=False`);
  `send_or_raise()` raises `NotifyError`. One attempt, ~5s timeout, no retry. The URL host is
  pinned to `hooks.slack.com` (port 443 only); pass `allowed_hosts=("host",)` to use a
  Slack-compatible endpoint.

  ```python
  notifier = SlackWebhookNotifier.from_env("chief-of-staff")
  notifier.send(Notification("Kill switch armed", "feed down", Severity.CRITICAL))
  ```

## Versioning

Semver tags (`vX.Y.Z`). Consumers pin an exact tag, never a branch.

## Development

```
pip install -e ".[dev]"
scripts/check.sh   # ruff, mypy (strict), pytest — same gates as release CI
```

CI runs only on published releases and manual dispatch, not on PRs.
