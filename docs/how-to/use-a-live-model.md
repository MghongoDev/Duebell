# Parse free text with a live model

Mock mode parses only one sentence pattern. To turn any short description into invoice fields, point Duebell at an Anthropic-compatible model.

1. Put these in `.env`:

   ```bash
   LLM_MODE=live
   ANTHROPIC_BASE_URL=https://api.anthropic.com
   ANTHROPIC_API_KEY=your-key
   ```

   A proxy that takes a bearer token uses `ANTHROPIC_AUTH_TOKEN` instead of the key. `ANTHROPIC_MODEL` picks the model. It defaults to `claude-haiku-5-5`.

2. Restart the app and use **Fill fields with AI**.

The model only fills the form. Duebell rejects any field it does not expect and any value of the wrong type, such as `true` for the terms. It trims and upper-cases the currency, then checks the values with the same rules as the manual form ([ADR 0004](../adr/0004-llm-as-form-filler.md)). A field the model leaves out stays empty and is marked on the form for you to fill in. Duebell does not guess it. You still click **Create invoice**.

## Check it works

```bash
.venv/bin/python scripts/evals.py --live
```

The script runs ten parsing cases against the model and reports each field it got wrong. It passes at 75 percent field accuracy or better. `--live` makes missing credentials an error rather than a skip.

## If it fails

| You see | Cause |
| --- | --- |
| "Mock parsing understands..." in live mode | The base URL or credential is missing, so parsing fell back to the mock. |
| "The AI service returned 401" | The key or token is wrong. |
| "The AI returned fields duebell does not accept" | The model added keys. Try again, or fill the form by hand. |
| "The AI gave a wrong type for..." | The model sent, say, text where a whole number belongs. Try again, or fill the form by hand. |
| "The AI left these out..." | The description did not give those fields. Fill in the marked fields. |

Each call costs whatever your model provider charges for one short request. Duebell tries a failed call up to three times in all.
