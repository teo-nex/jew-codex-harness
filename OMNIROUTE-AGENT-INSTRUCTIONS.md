# OmniRoute for local agents

OmniRoute is an external service dependency. Its default local endpoint is `http://127.0.0.1:20128/v1`; confirm the endpoint and model catalog live before using it. Obtain the gateway credential only from an existing protected file referenced by `JEV_OMNIROUTE_AUTH_FILE`. The JSON field is `omniroute.key`. Never print, commit or pass the key on a command line.

Use exact model IDs from the live `GET /v1/models` catalog and a small real request to verify each route. A catalog entry alone does not prove availability or model identity. Do not infer account quota or cost from this repository. Each provider may have separate limits and data handling terms.

For a text-only request, use `scripts/omniroute_text.py` with `--input-file`, `--task` and `--external-data-ok` only when sending that input to the selected provider is authorized. The helper reads the gateway key from its protected file, bounds output and saves its result outside the repository. Codex worker requests use `jev-workers.execute` when delegation is useful; the provider ladder keeps one worker dialog and handles provider fallback.
