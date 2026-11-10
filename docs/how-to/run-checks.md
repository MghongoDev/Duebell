# Run the checks

GitHub Actions runs these on every push and pull request, in mock mode. Run them yourself before you push, so a red check never reaches the branch. None needs credentials or a network.

```bash
.venv/bin/pip install -r requirements.txt -r requirements-dev.txt
.venv/bin/ruff check --select F app scripts tests
.venv/bin/pytest -q
.venv/bin/python scripts/smoke.py
.venv/bin/python scripts/evals.py
```

| Check | Passes when |
| --- | --- |
| `ruff check --select F` | No unused imports, undefined names or unused variables. |
| `pytest` | Every test passes. The suite covers validation, the gateways, payment sync, risk, the risk comparison, AI guardrails, reminders and channels, schedules, sign-in and ownership. |
| `smoke.py` | Three full demo runs from a fresh database finish, each printing one `Cycle` line, then `Smoke: pass`. |
| `evals.py` | The reminder and webhook checks pass. It skips the parsing cases unless live mode has credentials, and says so. |

To test parsing against a model, see [Parse free text with a live model](use-a-live-model.md).

## Update the dependency lock

`requirements.txt` and `requirements-dev.txt` pin every package to an exact version, so CI, Render and your machine install the same set. Edit the loose ranges in `requirements.in` or `requirements-dev.in`, never the lock files, then regenerate both with [uv](https://docs.astral.sh/uv/):

```bash
uv pip compile requirements.in --universal --python-version 3.12 -o requirements.txt
uv pip compile requirements-dev.in --universal --python-version 3.12 -o requirements-dev.txt
```

The lock targets Python 3.12, the version Render and CI run. To move every package to its newest allowed version, add `--upgrade` to both commands. Run the checks above before you commit the new locks.

## Compare models on synthetic data

```bash
.venv/bin/python scripts/train_risk.py
```

It prints held-out accuracy and AUC for the rule baseline and for logistic regression. The data is synthetic, so the numbers show the method, not real performance.

## Tests that need the toolkit

None do. The toolkit tests use a fake toolkit, so `pytest` passes without the optional install.
