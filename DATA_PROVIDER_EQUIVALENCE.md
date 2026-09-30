# Data Provider Equivalence Audit

**Status (2026-09-30): `blocked_by_missing_data_credentials`.** The nine frozen Baostock reference snapshots are present, each with 300 rows. No Tushare token, authorized endpoint access, or Tushare candidate snapshot is available in this environment. The accompanying `provider_equivalence_summary.csv` records nine blocked comparisons; `provider_equivalence_by_ticker.csv` has a header and zero observations. Blank comparison cells are **not** agreement.

The audit command is:

```powershell
cd D:\CodexWorkSpace\Stock\AI_stock_agent
$env:PYTHONPATH = (Resolve-Path stock_selector).Path
python -m data_history.provider_equivalence `
  --reference stock_selector/outputs/research_data `
  --candidate stock_selector/outputs/tushare_snapshots `
  --output .
```

The reference dates are 2020-03-16, 2020-07-15, 2021-05-17, 2021-12-15, 2022-05-16, 2022-10-17, 2023-03-15, 2024-05-15, and 2025-07-15. The candidate membership snapshot and each constituent update must be dated on or before its signal date. A same-month `index_weight` record dated later than the signal is rejected. Special interim CSI300 changes still need independent official-announcement checks.

When candidate snapshots exist, the offline audit compares member counts, missing and extra members, price, EMA200, return20d, 6-1M and 12-1M momentum, the six factor values, three scores, pass/fail agreement, actionable overlap, and Top 5 overlap. Spearman correlation is reported only when enough nonconstant paired observations exist. Numeric proximity alone never establishes semantic equivalence.

## Factor-definition crosswalk requiring verification

| Formal V1 concept | Current Baostock source and definition | Tushare candidate | Gate before equivalence |
| --- | --- | --- | --- |
| ROE | `query_profit_data.roeAvg`; numeric ratio in current implementation | `fina_indicator.roe_avg` candidate | Confirm denominator, averaging, percentage versus ratio, consolidated scope, `ann_date` and period. |
| CFO / revenue | `query_cash_flow_data.CFOToOR`; current ratio | `fina_indicator.ocf_to_or` candidate, or same-period cashflow/income reconstruction | Verify operating cash-flow definition, revenue basis, unit, period and disclosure timing. |
| EPS growth stability | Sample standard deviation of clipped annual `epsTTM` growth, using up to six published annual values; minimum four growth observations | Reconstruct from dated Tushare annual EPS records | Confirm TTM EPS meaning, restatements, split effects, annual coverage and publication dates. |
| Earnings yield | Inverse of `peTTM` from Baostock adjusted K data | `daily_basic.pe_ttm` inverse candidate | Confirm quote date, price basis, TTM earnings denominator and negative/zero treatment. |
| Net cashflow yield | Inverse of `pcfNcfTTM`; **net cash flow**, not operating cash flow | Tushare `daily_basic` cashflow valuation field or reconstructed formula | Verify the exact net-cash-flow numerator and TTM/market-cap timing; similar field names are insufficient. |
| Book-to-price | Inverse of Baostock `pbMRQ` | `daily_basic.pb` inverse candidate | Confirm MRQ versus most-recent-known book value and disclosure timing. |

All mappings above are **unverified**. For source fields whose Tushare availability or meaning differs by endpoint permission, the builder must mark the factor unavailable and fail closed. `statDate`/`end_date` alone never makes a report PIT; `pubDate`/`ann_date <= signal_date` is required. Price and adjusted-factor conventions must be checked against Baostock `adjustflag=2` before comparing EMA or momentum.

The current Value ranking uses Baostock `query_stock_industry(date=signal_date)` industry names and the project's `INDUSTRY_SECTIONS` grouping. It must not silently switch to Shenwan/Tushare industry groups. If historical Baostock taxonomy cannot be reproduced, mark `industry_source_drift` and assign an explicit, distinct `data_provider_version` to Tushare results; do not label those snapshots semantically identical to Baostock V1.

Acceptance tolerances are frozen separately in `DATA_PROVIDER_DRIFT_POLICY.json` before any full 65-month candidate result is inspected. Until the nine-node audit, field semantics and industry taxonomy all pass, this data-source change cannot be called provider-equivalent.
