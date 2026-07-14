# Quantitative Research Upgrade

## Goal

Improve automatic candidate selection and position management without allowing an unvalidated model to alter live trading rules. The system must learn from protected local data, validate changes out of sample, and only then recommend configuration updates for review.

## Research Foundations

- Bailey et al., *The Probability of Backtest Overfitting*: parameter selection must be tested on unseen time periods, not just the best historical run.
- Lopez de Prado, *The Volume Clock: Insights into the High Frequency Paradigm*, DOI `10.3905/jpm.2012.39.1.019`: time alone is a poor proxy for market activity; volume and trade flow improve event timing.
- Hierarchical Risk Parity research: portfolio risk should consider correlation clusters instead of treating each token as independent.
- Binance USD-M Futures WebSocket market streams: mark price and book ticker streams can support low-latency execution-cost and stale-data checks.

## Data Contract

The protected `research_dataset` stores append-only JSONL partitions:

1. Decision records: market features, score components, warnings, strategy decision, cost gate, and timestamp.
2. Outcome records: entry, exit, realized PnL, holding time, direction, and strategy.
3. Future labels: maximum favorable excursion, maximum adverse excursion, triple-barrier result, realized fees, and slippage.

No API credential, secret, account identifier, or private configuration is stored in the training records.

## Candidate Selection Upgrades

### Meta-label Instead Of Direction Prediction

The existing trend/breakout signal remains the primary direction. A future classifier only answers whether the signal should be traded after cost:

`P(net outcome > 0 after fees and slippage | score, regime, ATR, volume, funding, structure)`.

This avoids training a model to guess every price move. It filters low-quality entries while retaining explainable direction logic.

### Triple-Barrier Labels

Each historical entry should be labeled by the first event among:

- ATR-based stop barrier;
- ATR-based profit barrier;
- time barrier.

The label must include fees and slippage. A nominally profitable trade that loses after costs is a negative training example.

### Correlation-Aware Risk Budget

Before a new entry, calculate correlation of recent hourly returns with open positions. Compare signed exposure, not only symbol names:

- correlated long altcoin positions consume one risk cluster;
- a short in a positively correlated token is an offset only when the signed PnL correlation is negative;
- cap the number and total notional of each cluster.

This addresses the failure mode where several different tokens are actually one BTC-beta bet.

### Microstructure and Cost Gate

Use book ticker, spread, mark-price freshness, volume ratio, taker-buy ratio, and expected round-trip cost. No automatic entry should be sent when expected reward after cost is below the configured threshold.

## Position Management Upgrades

1. Keep exchange-side stop protection as the non-negotiable fallback.
2. Replace fixed partial exits with state-aware exits:
   - trend continuation: ATR trailing stop and delayed scale-out;
   - volatility expansion against the position: reduce risk first;
   - loss of higher-timeframe alignment: reduce or exit;
   - liquidity sweep: wait once only if flow and structure recover.
3. Track MFE/MAE for every closed trade. This measures whether stops are too tight, targets are too close, or entries are late.
4. Add a time stop only when the trade has failed to achieve minimum R and volume/trend confirmation has decayed.

## Validation Before Any Live Parameter Change

1. Train and tune only on an earlier period.
2. Validate on a later untouched period using purged time splits to prevent overlap leakage.
3. Require sufficient samples per strategy and regime.
4. Evaluate net PnL after fees, slippage, drawdown, win rate, profit factor, and calibration.
5. Run the candidate model in shadow mode first: record its recommendation without changing live orders.
6. Present any proposed parameter change for review. The model must never self-deploy to true trading.

## Delivery Order

1. Continue collecting decision and outcome data.
2. Add triple-barrier/MFE/MAE labels and correlation clusters.
3. Build an offline walk-forward evaluator.
4. Run a shadow meta-labeler.
5. Only then expose validated recommendations in the desktop interface.
