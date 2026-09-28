from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd


QUALITY = ["roic", "fcf_margin", "eps_growth_std"]
VALUE = ["earnings_yield", "fcf_yield", "book_to_price"]
MOMENTUM = ["mom_6_1", "mom_12_1"]


def _load_ticker_map(path: Path) -> dict[str, int]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    result = {item["ticker"]: int(item["cik_str"]) for item in payload.values()}
    result.update({ticker.replace("-", "."): cik for ticker, cik in list(result.items()) if "-" in ticker})
    return result


def _industry(ticker: str, ticker_map: dict[str, int], submission_cache: Path) -> tuple[str, str, bool]:
    cik = ticker_map.get(ticker)
    if cik is None:
        return "", "", False
    path = submission_cache / f"{cik:010d}.json"
    if not path.exists():
        return "", "", False
    payload = json.loads(path.read_text(encoding="utf-8"))
    sic = str(payload.get("sic") or "").zfill(4)
    return sic, str(payload.get("sic_description") or ""), sic.startswith("6")


def main() -> None:
    parser = argparse.ArgumentParser(description="Audit one US selector run at ticker grain")
    parser.add_argument("--run-dir", required=True, type=Path)
    parser.add_argument("--ticker-map", required=True, type=Path)
    parser.add_argument("--submission-cache", required=True, type=Path)
    args = parser.parse_args()

    metadata = json.loads((args.run_dir / "run_metadata.json").read_text(encoding="utf-8"))["provider"]
    metrics = pd.read_csv(args.run_dir / "raw_metrics.csv").set_index("ticker", drop=False)
    ticker_map = _load_ticker_map(args.ticker_map)
    rows = []

    for ticker, row in metrics.iterrows():
        missing_quality = [column for column in QUALITY if pd.isna(row[column])]
        missing_value = [column for column in VALUE if pd.isna(row[column])]
        missing_momentum = [column for column in MOMENTUM if pd.isna(row[column])]
        financial = "financial" in str(row["industry_l1"]).lower()
        if financial:
            category = "financial_model_unsupported"
        elif missing_quality:
            category = "quality_incomplete"
        elif missing_value:
            category = "value_incomplete"
        elif missing_momentum:
            category = "momentum_incomplete"
        else:
            category = "score_ready"
        rows.append({
            "ticker": ticker,
            "category": category,
            "provider_status": "built",
            "sic": row.get("sic", ""),
            "industry": row.get("industry_description", ""),
            "missing_metrics": ";".join(missing_quality + missing_value + missing_momentum),
            "detail": "",
        })

    for ticker, detail in metadata["errors"].items():
        sic, industry, financial = _industry(ticker, ticker_map, args.submission_cache)
        if "missing one or more local" in detail:
            category = "cache_missing"
        elif financial:
            category = "financial_model_unsupported"
        elif "no common annual fiscal period" in detail:
            category = "annual_fact_gap"
        else:
            category = "provider_error"
        rows.append({
            "ticker": ticker,
            "category": category,
            "provider_status": "failed",
            "sic": sic,
            "industry": industry,
            "missing_metrics": "",
            "detail": detail,
        })

    for ticker in metadata["ignored_unmapped_symbols"]:
        rows.append({
            "ticker": ticker,
            "category": "ticker_unmapped",
            "provider_status": "not_mapped",
            "sic": "",
            "industry": "",
            "missing_metrics": "",
            "detail": "not present in current SEC ticker map",
        })

    audit = pd.DataFrame(rows).sort_values(["category", "ticker"])
    if audit["ticker"].duplicated().any():
        raise RuntimeError("coverage audit contains duplicate ticker rows")
    expected = int(metadata["original_members"])
    if len(audit) != expected:
        raise RuntimeError(f"coverage reconciliation failed: expected {expected}, got {len(audit)}")
    summary = (
        audit.groupby("category", as_index=False)
        .agg(tickers=("ticker", "size"))
        .sort_values("tickers", ascending=False)
    )
    summary["share"] = summary["tickers"] / expected
    audit.to_csv(args.run_dir / "coverage_detail.csv", index=False)
    summary.to_csv(args.run_dir / "coverage_summary.csv", index=False)
    result = {
        "status": "PASS",
        "as_of": metadata["as_of"],
        "expected_tickers": expected,
        "reconciled_tickers": len(audit),
        "categories": {
            row.category: {"tickers": int(row.tickers), "share": float(row.share)}
            for row in summary.itertuples(index=False)
        },
    }
    (args.run_dir / "coverage_audit.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

