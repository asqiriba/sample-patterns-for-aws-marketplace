# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""`module11 prices refresh`: Bedrock on-demand token prices for the models.yaml IDs, from the AWS Price
List API (the cost signal; `harness/evaluation/prices.yaml`).

Matches the base model name (the inference profile ID without its geography prefix, for example
`gpt-5.6-luna`) against each AmazonBedrock product's `model` and `usagetype` attributes, keeps the
standard on-demand input, cached-input and output token prices (not batch, priority or flex), and
converts every unit to USD per 1,000 tokens. With `--write` it updates prices.yaml, which is a
release artifact, so the change goes through review like any other.
"""
from __future__ import annotations

import json
import re
from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml

from module11.release import artifacts as A

PRICES = f"{A.HARNESS}/evaluation/prices.yaml"
KINDS = {"input tokens": "inputPer1kTokens", "output tokens": "outputPer1kTokens",
         "cached input tokens": "cachedInputPer1kTokens", "cache read input tokens": "cachedInputPer1kTokens"}


def _products(pricing: Any, region: str):
    token = None
    while True:
        kwargs = {"ServiceCode": "AmazonBedrock", "MaxResults": 100,
                  "Filters": [{"Type": "TERM_MATCH", "Field": "regionCode", "Value": region}],
                  **({"NextToken": token} if token else {})}
        page = pricing.get_products(**kwargs)
        for raw in page.get("PriceList", []):
            yield json.loads(raw)
        token = page.get("NextToken")
        if not token:
            return


def _per_1k(dimension: dict) -> Decimal | None:
    usd = Decimal(dimension.get("pricePerUnit", {}).get("USD", "0") or "0")
    unit = dimension.get("unit", "").lower()
    match = re.search(r"(\d+)\s*([km]?)", unit.replace(",", ""))
    if "token" not in unit and not match:
        return None
    scale = Decimal(1)
    if match:
        scale = Decimal(match.group(1)) * {"": 1, "k": 1000, "m": 1_000_000}[match.group(2)]
    elif "1k" in unit:
        scale = Decimal(1000)
    elif "1m" in unit or "million" in unit:
        scale = Decimal(1_000_000)
    return usd / scale * 1000


def lookup(pricing: Any, model_id: str, region: str) -> dict:
    base = model_id.split(".", 1)[-1].split(".", 1)[-1].lower()       # us.openai.gpt-5.6-luna -> gpt-5.6-luna
    out: dict[str, float | None] = {v: None for v in set(KINDS.values())}
    for product in _products(pricing, region):
        attrs = product["product"]["attributes"]
        name = f"{attrs.get('model', '')} {attrs.get('usagetype', '')}".lower()
        kind = KINDS.get(attrs.get("inferenceType", "").lower())
        if base not in name or kind is None or attrs.get("feature", "On-demand Inference") != "On-demand Inference":
            continue
        if any(word in attrs.get("usagetype", "").lower() for word in ("batch", "priority", "flex", "provisioned")):
            continue
        for term in (product.get("terms", {}).get("OnDemand") or {}).values():
            for dimension in term.get("priceDimensions", {}).values():
                price = _per_1k(dimension)
                if price is not None:
                    out[kind] = float(round(price, 8))
    return out


def refresh(pricing: Any, root: Path, region: str, *, write: bool = False) -> dict:
    path = Path(root) / PRICES
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    rows = {model: lookup(pricing, model, region) for model in doc.get("models", {})}
    if write:
        text = path.read_text(encoding="utf-8")
        for model, row in rows.items():
            line = (f"  {model}: {{inputPer1kTokens: {row['inputPer1kTokens']}, cachedInputPer1kTokens: "
                    f"{row['cachedInputPer1kTokens']}, outputPer1kTokens: {row['outputPer1kTokens']}}}")
            text = re.sub(rf"^  {re.escape(model)}:.*$", line.replace("None", "null"), text, flags=re.M)
        path.write_text(text, encoding="utf-8")
    return rows
