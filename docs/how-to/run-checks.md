# Run the checks

Run these before you commit. None needs credentials or a network.

```bash
.venv/bin/pytest -q
.venv/bin/python scripts/smoke.py
.venv/bin/python scripts/evals.py
```

| Check | Passes when |
| --- | --- |
| `pytest` | Every test passes. The suite covers validation, the gateways, payment sync, risk, the risk comparison, AI guardrails, reminders and channels, schedules, sign-in and ownership. |
| `smoke.py` | Three full demo runs from a fresh database finish, each printing one `Cycle` line, then `Smoke: pass`. |
| `evals.py` | The reminder and webhook checks pass. It skips the parsing cases unless live mode has credentials, and says so. |

To test parsing against a model, see [Parse free text with a live model](use-a-live-model.md).

## Compare models on synthetic data

```bash
.venv/bin/python scripts/train_risk.py
```

It prints held-out accuracy and AUC for the rule baseline and for logistic regression. The data is synthetic, so the numbers show the method, not real performance.

## Tests that need the toolkit

None do. The toolkit tests use a fake toolkit, so `pytest` passes without the optional install.
