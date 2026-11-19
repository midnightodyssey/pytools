# pytools

## House style

Python here follows the house style in the `dev-standards` repo: read `STYLE.md` before
writing code (section 0 is the short version). `DELTAS.md` there lists where existing code
does not yet comply, so don't treat non-compliant code as precedent.

`scripts/check.sh` is the gate (ruff, mypy, pytest, UTF-8); it uses `.venv` when present.

## Agent skills

### Issue tracker

**Before any `gh` call**, read `docs/agents/issue-tracker.md`: issues live in this repo's GitHub Issues.

### Triage labels

Default vocabulary: needs-triage, needs-info, ready-for-agent, ready-for-human, wontfix. See `docs/agents/triage-labels.md`.

### Domain docs

Single-context: one `GLOSSARY.md` and `docs/adr/` at the repo root, created only when a term or decision gets settled. See `docs/agents/domain.md`.
