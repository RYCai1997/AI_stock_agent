"""PUBLIC_V1 source hierarchy and core-data gate, separate from strategy rules."""

from __future__ import annotations

from . import DATA_PROVIDER_VERSION


SOURCE_HIERARCHY = {
    "universe": ("csi_official", "baostock", "eastmoney"),
    "financial": ("cninfo", "sse", "szse", "baostock"),
    "execution_prices": ("eastmoney", "baostock"),
    "indicator_prices": ("baostock", "eastmoney"),
    "security_state": ("sse", "szse", "baostock"),
    "corporate_actions": ("cninfo", "sse", "szse", "baostock"),
    "industry": ("baostock",),
}

CORE_FIELDS = (
    "historical_csi300_membership", "qvm_factors", "financial_publication_dates",
    "momentum_history", "ema200", "latest_price", "security_eligibility",
    "historical_industry",
)

WARNING_FIELDS = ("nonheld_security_corporate_action", "secondary_source_availability",
                  "unused_relative_strength")


def assess_quality(coverage: dict[str, bool], fallback_count: int,
                   warnings: list[str] | None = None) -> dict:
    if fallback_count < 0:
        raise ValueError("fallback_count cannot be negative")
    missing = [name for name in CORE_FIELDS if not bool(coverage.get(name, False))]
    details = {key: coverage.get(key, False) for key in (
        "universe_complete", "fundamental_complete", "price_complete",
        "security_state_complete", "corporate_action_complete",
        "industry_complete", "cross_source_checks")}
    return {"data_provider_version": DATA_PROVIDER_VERSION,
            "primary_eligible": not missing,
            "missing_core_fields": missing, "details": details,
            "fallback_count": fallback_count, "warnings": warnings or [],
            "data_quality_score": sum(bool(value) for value in details.values()) / len(details)}
