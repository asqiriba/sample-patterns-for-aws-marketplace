# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""IAM role headroom reads (release/iam_budget.py holds the arithmetic). Read-only: IAM GetAccountSummary and
ListRoles, Service Quotas' requested-change history for L-FE177D64. IAM quotas live in us-east-1."""
from __future__ import annotations

from typing import Any, Iterable, Mapping

from module11.release import iam_budget as IB


def role_usage(iam: Any) -> tuple[int, int]:
    """(roles in the account, the account's Roles-per-account quota) from GetAccountSummary."""
    summary = iam.get_account_summary()["SummaryMap"]
    return int(summary["Roles"]), int(summary.get("RolesQuota") or IB.DEFAULT_QUOTA)


def role_names(iam: Any, prefix: str = "m11-") -> set[str]:
    """Names of the account's roles that start with `prefix`."""
    names: set[str] = set()
    for page in iam.get_paginator("list_roles").paginate():
        names.update(r["RoleName"] for r in page["Roles"] if r["RoleName"].startswith(prefix))
    return names


def pending_increase(quotas: Any) -> str | None:
    """'PENDING 1500' (or CASE_OPENED) when an increase of L-FE177D64 is in flight; None otherwise or unreadable."""
    try:
        history = quotas.list_requested_service_quota_change_history_by_quota(
            ServiceCode="iam", QuotaCode=IB.QUOTA_CODE)["RequestedQuotas"]
    except Exception:  # noqa: BLE001 - the request history is advisory; a missing permission must not block
        return None
    open_ = [r for r in history if r.get("Status") in {"PENDING", "CASE_OPENED"}]
    if not open_:
        return None
    latest = max(open_, key=lambda r: r.get("Created") or 0)
    return f"{latest['Status']} {int(latest['DesiredValue'])}"


def headroom(session: Any, planned: Mapping[str, Iterable[str]]) -> IB.Headroom:
    """The account's headroom for `planned` (see iam_budget.plan / stack_roles)."""
    iam = session.client("iam")
    used, quota = role_usage(iam)
    return IB.headroom(planned, used=used, quota=quota, existing=role_names(iam),
                       pending_request=pending_increase(session.client("service-quotas", region_name="us-east-1")))
