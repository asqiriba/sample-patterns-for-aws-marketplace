# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The control item `{pk: ROLLOUT, sk: CURRENT}` in `m11-<stage>-control`.

Every stage has the item: the adapter reads its `endpoints` map to build runtime session IDs and
to stamp each request's release identity. In dev, staging and prod the controller
Lambda is its only writer (`put_conditional`); `mr-*` stages have no controller, so there
`release deploy` writes the IDLE item itself (`put_idle`) with the same conditional write on `revision`.
Audit events `{pk: ROLLOUT#<id>, sk: EVENT#<ts>}` share the table; outcome items live in
`m11-<stage>-outcomes`.
"""
from __future__ import annotations

import time
from typing import Any, Mapping

KEY = {"pk": {"S": "ROLLOUT"}, "sk": {"S": "CURRENT"}}


class ConcurrentUpdate(RuntimeError):
    pass


def _ser():
    from boto3.dynamodb.types import TypeDeserializer, TypeSerializer
    return TypeSerializer(), TypeDeserializer()


def get(ddb: Any, table: str) -> dict | None:
    item = ddb.get_item(TableName=table, Key=KEY, ConsistentRead=True).get("Item")
    if not item:
        return None
    _, de = _ser()
    out = {k: de.deserialize(v) for k, v in item.items()}
    return _plain(out)


def _plain(value):
    from decimal import Decimal
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_plain(v) for v in value]
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    return value


def get_raw(ddb: Any, table: str) -> dict | None:
    """The item as DynamoDB returns it (numbers as Decimal): what `release/pins.py: ts` parses."""
    item = ddb.get_item(TableName=table, Key=KEY, ConsistentRead=True).get("Item")
    if not item:
        return None
    _, de = _ser()
    return {k: de.deserialize(v) for k, v in item.items()}


def to_dynamo(value: Any) -> Any:
    """Floats (the controller's epoch seconds) as Decimal; TypeSerializer refuses Python floats."""
    from decimal import Decimal
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            raise ValueError("non-finite number in a control item")
        return Decimal(repr(value))
    if isinstance(value, dict):
        return {k: to_dynamo(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_dynamo(v) for v in value]
    return value


def serialize(item: Mapping[str, Any]) -> dict:
    ser, _ = _ser()
    return {k: ser.serialize(to_dynamo(v)) for k, v in item.items()}


def put_conditional(ddb: Any, table: str, item: Mapping[str, Any], expected_revision: int | None) -> None:
    """The controller's write: PutItem with `revision = :expected` (or a first write)."""
    kwargs: dict[str, Any] = {"TableName": table, "Item": serialize({**item, "pk": "ROLLOUT", "sk": "CURRENT"})}
    if expected_revision is None:
        kwargs["ConditionExpression"] = "attribute_not_exists(pk)"
    else:
        kwargs["ConditionExpression"] = "revision = :r"
        kwargs["ExpressionAttributeValues"] = {":r": {"N": str(int(expected_revision))}}
    try:
        ddb.put_item(**kwargs)
    except Exception as exc:
        if str(getattr(exc, "response", {}).get("Error", {}).get("Code", "")) == "ConditionalCheckFailedException":
            raise ConcurrentUpdate(f"{table}: the control item changed since revision {expected_revision}") from exc
        raise


def put_event(ddb: Any, table: str, event: Mapping[str, Any]) -> None:
    """An audit event {pk: ROLLOUT#<id>, sk: EVENT#<ts>}."""
    ddb.put_item(TableName=table, Item=serialize({k: v for k, v in event.items() if v is not None}))


def events(ddb: Any, table: str, rollout_id: str) -> list[dict]:
    """The audit trail of one rollout, oldest first."""
    _, de = _ser()
    out, start = [], None
    while True:
        kwargs = {"TableName": table, "KeyConditionExpression": "pk = :pk AND begins_with(sk, :e)",
                  "ExpressionAttributeValues": {":pk": {"S": f"ROLLOUT#{rollout_id}"}, ":e": {"S": "EVENT#"}},
                  "ConsistentRead": True}
        if start:
            kwargs["ExclusiveStartKey"] = start
        page = ddb.query(**kwargs)
        out += [_plain({k: de.deserialize(v) for k, v in row.items()}) for row in page.get("Items", [])]
        start = page.get("LastEvaluatedKey")
        if not start:
            return out


def outcomes(ddb: Any, table: str, rollout_id: str, *, since_ms: int | None = None) -> list[dict]:
    """Outcome items of one rollout (`pk = ROLLOUT#<id>`), oldest first, as plain values."""
    _, de = _ser()
    values = {":pk": {"S": f"ROLLOUT#{rollout_id}"}}
    condition = "pk = :pk"
    if since_ms is not None:
        condition += " AND sk >= :since"
        values[":since"] = {"S": f"{int(since_ms)}"}
    out, start = [], None
    while True:
        kwargs = {"TableName": table, "KeyConditionExpression": condition, "ExpressionAttributeValues": values}
        if start:
            kwargs["ExclusiveStartKey"] = start
        page = ddb.query(**kwargs)
        out += [_plain({k: de.deserialize(v) for k, v in row.items()}) for row in page.get("Items", [])]
        start = page.get("LastEvaluatedKey")
        if not start:
            return out


def scan_outcomes(ddb: Any, table: str) -> list[dict]:
    """Every outcome item of a stage (the table is small: 30-day TTL), plain values."""
    _, de = _ser()
    out, kwargs = [], {"TableName": table}
    while True:
        page = ddb.scan(**kwargs)
        out += [_plain({k: de.deserialize(v) for k, v in item.items()}) for item in page.get("Items", [])]
        if not page.get("LastEvaluatedKey"):
            return out
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def endpoint_entry(version: str | int, manifest_version: str, behavior_digest: str, release: str) -> dict:
    return {"version": str(version), "manifestVersion": manifest_version, "behaviorDigest": behavior_digest,
            "release": release}


def put_idle(ddb: Any, table: str, endpoints: Mapping[str, Mapping], *, expected: Mapping | None,
             now: float | None = None) -> dict:
    """Write {state: IDLE, endpoints, revision+1}; conditional on the revision just read."""
    now = time.time() if now is None else now
    revision = int((expected or {}).get("revision", 0))
    item = {**(dict(expected) if expected else {}), "pk": "ROLLOUT", "sk": "CURRENT", "state": "IDLE",
            "revision": revision + 1, "endpoints": {k: dict(v) for k, v in endpoints.items()},
            "endpointsWrittenAt": int(now), "canaryPct": 0}
    ser, _ = _ser()
    condition = "attribute_not_exists(pk)" if expected is None else "revision = :r"
    kwargs = {"TableName": table, "Item": {k: ser.serialize(v) for k, v in item.items() if v is not None},
              "ConditionExpression": condition}
    if expected is not None:
        kwargs["ExpressionAttributeValues"] = {":r": {"N": str(revision)}}
    try:
        ddb.put_item(**kwargs)
    except Exception as exc:
        if str(getattr(exc, "response", {}).get("Error", {}).get("Code", "")) == "ConditionalCheckFailedException":
            raise ConcurrentUpdate(f"{table}: the control item changed since revision {revision}") from exc
        raise
    return item
