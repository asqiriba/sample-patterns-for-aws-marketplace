# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The release ledger: one S3 object per entry under `ledger/<stage>/` and `ledger/shared/`.
Entries are never rewritten or deleted; teardown and `shadow report` page
every prefix. A failed put is retried 3 times, then logged as `ledger_write_failed` and counted in
`m11/LedgerWriteFailures`; it never blocks the operation that wrote it."""
from __future__ import annotations

import json
import logging
import time
from typing import Any, Iterator

from module11.release import audit

LOG = logging.getLogger("module11.ledger")


def put(s3: Any, bucket: str, stage: str, kind: str, *, by: str, cloudwatch: Any = None,
        sleep=time.sleep, **fields: Any) -> tuple[str, dict]:
    at = time.time()
    key = audit.ledger_key(stage, at)
    entry = audit.ledger_entry(kind, stage, at, by, **fields)

    def _put(k: str, body: bytes) -> None:
        s3.put_object(Bucket=bucket, Key=k, Body=body, ContentType="application/json")

    def _log(level: str, event: str, **kw: Any) -> None:
        LOG.log(logging.ERROR if level == "ERROR" else logging.INFO, json.dumps({"event": event, **kw}, default=str))

    def _metric(namespace: str, name: str, value: float) -> None:
        if cloudwatch is not None:
            cloudwatch.put_metric_data(Namespace=namespace, MetricData=[
                {"MetricName": name, "Value": value, "Dimensions": [{"Name": "Stage", "Value": stage}]}])

    audit.put_with_retry(_put, key, entry, sleep=sleep, log=_log, metric=_metric)
    return key, entry


def entries(s3: Any, bucket: str, stage: str) -> Iterator[tuple[str, dict]]:
    """(key, entry) in time order (ListObjectsV2 is lexicographic and keys start with the stamp)."""
    token = None
    while True:
        kwargs = {"Bucket": bucket, "Prefix": f"ledger/{stage}/", **({"ContinuationToken": token} if token else {})}
        page = s3.list_objects_v2(**kwargs)
        for obj in page.get("Contents", []):
            body = s3.get_object(Bucket=bucket, Key=obj["Key"])["Body"].read()
            yield obj["Key"], json.loads(body)
        if not page.get("IsTruncated"):
            return
        token = page.get("NextContinuationToken")
