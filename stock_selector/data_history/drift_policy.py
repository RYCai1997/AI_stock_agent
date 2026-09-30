"""Predeclared, fail-closed decision for source-equivalence audit rows."""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd


def evaluate_equivalence(summary: pd.DataFrame, policy_path: Path,
                         verified_gates: dict[str, bool] | None = None) -> dict:
    policy = json.loads(policy_path.read_text(encoding="utf-8"))
    issues = []
    if len(summary) != policy["required_reference_nodes"] or summary.signal_date.duplicated().any():
        issues.append("nine unique frozen reference dates are required")
    if "status" not in summary or not summary.status.eq("numeric_comparison_only").all():
        issues.append("candidate data missing or numeric comparison incomplete")
    if issues:
        return {"equivalent": False, "status": "blocked_by_missing_data_credentials",
                "reference_data_provider_version": policy["reference_data_provider_version"],
                "candidate_data_provider_version": policy["candidate_data_provider_version"],
                "issues": issues}
    for row in summary.to_dict("records"):
        date = row["signal_date"]
        if row["reference_member_count"] != policy["member_count"] or row["candidate_member_count"] != policy["member_count"]:
            issues.append(f"{date}: member count differs from frozen target")
        if row["member_overlap_count"] < policy["member_count"] - policy["maximum_missing_or_extra_members"]:
            issues.append(f"{date}: historical universe drift")
        if not row.get("candidate_membership_not_future"):
            issues.append(f"{date}: candidate membership is missing or future")
        for field, limit in policy["maximum_absolute_difference"].items():
            if (pd.isna(row.get(f"{field}_max_abs_diff")) or
                    row[f"{field}_max_abs_diff"] > limit or
                    row.get(f"{field}_paired_count", 0) < policy["member_count"] * policy["minimum_paired_fraction"]):
                issues.append(f"{date}: {field} price-derived drift or missing pairs")
        for field, minimum in policy["minimum_spearman"].items():
            value = row.get(f"{field}_spearman")
            if pd.isna(value) or value < minimum:
                issues.append(f"{date}: {field} score rank drift")
        for field, minimum in policy["minimum_pass_agreement"].items():
            value = row.get(f"{field}_agreement")
            if pd.isna(value) or value < minimum:
                issues.append(f"{date}: {field} pass/fail drift")
        if row.get("top5_overlap_count") != policy["required_top5_overlap"]:
            issues.append(f"{date}: Top 5 drift")
    gates = verified_gates or {}
    for gate in policy["mandatory_non_numeric_gates"]:
        if gates.get(gate) is not True:
            issues.append(f"unverified semantic gate: {gate}")
    return {"equivalent": not issues,
            "status": "equivalent" if not issues else "data_provider_drift_or_unverified",
            "reference_data_provider_version": policy["reference_data_provider_version"],
            "candidate_data_provider_version": policy["candidate_data_provider_version"],
            "issues": issues}
