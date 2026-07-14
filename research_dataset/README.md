# Binance Trade Assistant Research Dataset

This directory is a protected, append-only local dataset for future model training and strategy research.

It is intentionally outside `release`, `build`, `reports`, and runtime database folders. Never delete, move,
or overwrite it while cleaning old executable versions or generated files.

Current records:

- `decisions/YYYY-MM.jsonl`: market features, signal scores, warnings, decisions, and later outcome fields.
- `outcomes/YYYY-MM.jsonl`: closed-trade entry/exit, realized PnL, holding time, and strategy labels.
- `dataset_manifest.json`: schema and retention policy.

The dataset stores no API key, API secret, or account credential. It is local only by default.
