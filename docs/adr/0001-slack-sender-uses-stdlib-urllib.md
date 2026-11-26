# Slack sender uses stdlib urllib, not pytools.http

A Slack incoming-webhook URL carries its secret in the path
(`https://hooks.slack.com/services/T…/B…/<secret>`). `pytools.http` redacts URLs with
`safe_url`, which strips userinfo, query and fragment but deliberately keeps the path, so
its `HttpStatusError` / `HttpTransportError` messages, its retry warnings and httpx's own
INFO logging would all print the webhook secret. We therefore built `SlackWebhookNotifier`
on stdlib `urllib.request`, with its own rule that the URL never reaches a log line or an
exception message. That also keeps `pytools.notify` in the dependency-free core rather than
behind the `[http]` extra.

## Considered options

- **Reuse `pytools.http` and redact the path in `safe_url`.** Rejected: path redaction would
  make every other caller's errors far less useful to debug, and httpx's own logger would
  still leak the URL unless every consumer remembered to silence it.
- **Reuse `pytools.http` and catch-and-rewrap every error in the notifier.** Rejected: the
  retry transport logs the URL before the notifier sees anything. The notifier also makes
  exactly one attempt, so the retry machinery buys nothing.

## Consequences

Do not "tidy" the notifier onto `pytools.http` without first solving path redaction there.
Any test of the notifier's failure paths must assert the webhook URL is absent from logs and
exception text.
