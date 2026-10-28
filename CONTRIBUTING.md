# Contributing

Thanks for helping. Bug reports, fixes, tests and docs are all welcome.

## Before you start

- For a security problem, don't open an issue: see [SECURITY.md](SECURITY.md).
- For anything bigger than a small fix, open an issue first to talk it through.
- Changes to trading, risk limits, the safety review (`ct_agent/safety.py`) or live execution get a careful
  review. They decide whether real money moves, so please explain the reasoning and add tests.

## Setting up

```bash
python -m venv .venv
.venv/bin/pip install -e ".[dev]"     # Windows: .venv\Scripts\pip install -e ".[dev]"
pip install pre-commit && pre-commit install   # secret scan and checks before each commit
cp .env.example .env                  # paper mode; no keys needed to run the tests
.venv/bin/pytest
```

The tests run offline against fakes of the chain, X and Grok (`tests/fakes.py`). They never need a real key,
and they must never make a real network call or trade.

## Pull requests

- Keep each pull request to one change, with tests for new behaviour.
- `pytest` and the CI secret scan must pass.
- Never commit a real key, wallet address you control, `.env` file, database or anything from `data/`.
  Use obviously fake values in tests (`"0x" + "11" * 32`, `"https://site.example"`).
- New settings go in `ct_agent/config.py` with a safe default (off, paper, empty) and in `.env.example`
  with a comment saying what they do and whether they're a secret. `tests/test_safety.py` checks that every
  setting is listed there.
- Anything that spends money or posts publicly must default to off.
- Match the surrounding code: short functions, plain names, comments that explain why rather than what.

## Licence

By contributing you agree that your contribution is released under this repository's [licence](LICENSE).
