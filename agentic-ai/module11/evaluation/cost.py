# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Cost projection before a run and measured cost after it. Pure arithmetic.

Unit prices are AgentCore Evaluations' published ones (built-in judge tokens, custom evaluators per
1,000) and Runtime v2 consumption prices. The model's own token price comes from `prices.yaml`; while that is
null (the AWS Price List has no entry for the model) the projection uses the conservative
planning price ($5 / $20 per 1M tokens) and says so. Trajectory evaluators are programmatic and cost nothing.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

JUDGE_IN_PER_1K = 0.0024
JUDGE_OUT_PER_1K = 0.012
CODE_EVAL_EACH = 1.50 / 1000
RUNTIME_PER_SECOND = (0.1276 + 2 * 0.0169) / 3600          # 1 vCPU, 2 GB
ASSUMED_MODEL_IN_PER_1K = 5.0 / 1000                        # $5 per 1M input tokens
ASSUMED_MODEL_OUT_PER_1K = 20.0 / 1000                      # $20 per 1M output tokens
ASSUMED_SESSION_IN, ASSUMED_SESSION_OUT = 8000, 1000        # tokens per session, companion and planner
ASSUMED_JUDGE_IN, ASSUMED_JUDGE_OUT = 6000, 300             # tokens per judge evaluation
ASSUMED_SESSION_SECONDS = 45.0
JUDGED = ("Builtin.GoalSuccessRate", "Builtin.Correctness", "Builtin.Helpfulness")


@dataclass
class Projection:
    label: str
    scenarios: int
    sessions: int
    invocations: int
    judge_evaluations: int
    code_evaluations: int
    model_usd: float
    judge_usd: float
    code_usd: float
    runtime_usd: float
    assumptions: list[str] = field(default_factory=list)

    @property
    def total_usd(self) -> float:
        return self.model_usd + self.judge_usd + self.code_usd + self.runtime_usd

    @property
    def per_session_usd(self) -> float:
        return self.total_usd / self.sessions if self.sessions else 0.0

    def lines(self) -> list[str]:
        out = [f"projection for {self.label}: {self.scenarios} scenarios, {self.sessions} sessions "
               f"({self.invocations} invocations), {self.judge_evaluations} judge evaluations, "
               f"{self.code_evaluations} code evaluations",
               f"  model ${self.model_usd:,.2f}  judges ${self.judge_usd:,.2f}  code evaluator ${self.code_usd:,.2f}  "
               f"runtime ${self.runtime_usd:,.2f}  total ${self.total_usd:,.2f}  (${self.per_session_usd:.3f} per session)"]
        out += [f"  assumption: {line}" for line in self.assumptions]
        return out


def model_price(prices: Mapping[str, Any] | None, model_id: str) -> tuple[float, float, bool]:
    """(input per 1k, output per 1k, from prices.yaml?)."""
    row = ((prices or {}).get("models") or {}).get(model_id) or {}
    if row.get("inputPer1kTokens") is not None and row.get("outputPer1kTokens") is not None:
        return float(row["inputPer1kTokens"]), float(row["outputPer1kTokens"]), True
    return ASSUMED_MODEL_IN_PER_1K, ASSUMED_MODEL_OUT_PER_1K, False


def project(label: str, scenarios: Sequence[Mapping[str, Any]], repeats: Mapping[str, int], *,
            evaluators: Sequence[str], prices: Mapping[str, Any] | None = None, model_id: str = "",
            measured: Mapping[str, float] | None = None) -> Projection:
    """Projected spend of one run. `measured` (a previous run's per-session averages, from `measure()`)
    replaces the planning assumptions when given."""
    counts = [(s, int(repeats.get(s["category"], repeats.get("default", 1)))) for s in scenarios]
    sessions = sum(r for _, r in counts)
    invocations = sum(r * len(s.get("turns") or [1]) for s, r in counts)
    judged = [e for e in evaluators if e in JUDGED]
    code = [e for e in evaluators if e.startswith("custom.")]
    in_price, out_price, priced = model_price(prices, model_id)
    assumptions = []
    if measured:
        s_in, s_out = measured["sessionInputTokens"], measured["sessionOutputTokens"]
        j_in, j_out = measured["judgeInputTokens"], measured["judgeOutputTokens"]
        seconds = measured.get("sessionSeconds", ASSUMED_SESSION_SECONDS)
        assumptions.append("token counts and session time are the averages measured on the last run")
    else:
        s_in, s_out, j_in, j_out, seconds = (ASSUMED_SESSION_IN, ASSUMED_SESSION_OUT, ASSUMED_JUDGE_IN,
                                             ASSUMED_JUDGE_OUT, ASSUMED_SESSION_SECONDS)
        assumptions.append(f"{s_in} input and {s_out} output model tokens per session, {j_in} and {j_out} per judge "
                           "evaluation (planning values, replaced after the first measured run)")
    if not priced:
        assumptions.append("model price is the planning price ($5 / $20 per 1M tokens): prices.yaml holds "
                           "no price for this model (AWS Price List has no entry)")
    judge_evals = sessions * len(judged)
    return Projection(
        label, len(counts), sessions, invocations, judge_evals, sessions * len(code),
        sessions * (s_in / 1000 * in_price + s_out / 1000 * out_price),
        judge_evals * (j_in / 1000 * JUDGE_IN_PER_1K + j_out / 1000 * JUDGE_OUT_PER_1K),
        sessions * len(code) * CODE_EVAL_EACH, sessions * seconds * RUNTIME_PER_SECOND, assumptions)


@dataclass
class ManifestCost:
    """Served-traffic cost of one manifest on one stage, from its adapter outcome items (B6 6c)."""
    manifest_version: str
    release: str
    sessions: int
    successful: int
    input_tokens: float          # means per session
    output_tokens: float
    tool_calls: float
    model_usd: float             # totals
    runtime_usd: float
    first_seen: float
    priced: bool

    @property
    def total_usd(self) -> float:
        return self.model_usd + self.runtime_usd

    @property
    def per_successful_usd(self) -> float | None:
        return self.total_usd / self.successful if self.successful else None

    @property
    def success_rate(self) -> float:
        return self.successful / self.sessions if self.sessions else 0.0


def by_manifest(items: Sequence[Mapping[str, Any]], prices: Mapping[str, Any] | None, model_id: str,
                *, include_eval: bool = False) -> list[ManifestCost]:
    """Group the stage's adapter outcome items (shadow copies excluded) by manifestVersion: tokens, tool calls and
    cost per SUCCESSFUL session (a session counts as successful when its status is `completed`). The model price is
    prices.yaml's or the planning price (flagged); runtime is consumption time at the v2 rates."""
    in_price, out_price, priced = model_price(prices, model_id)
    groups: dict[str, list[Mapping[str, Any]]] = {}
    for item in items:
        if item.get("shadow") or not item.get("manifestVersion"):
            continue
        groups.setdefault(str(item["manifestVersion"]), []).append(item)
    out = []
    for mv, rows in groups.items():
        n = len(rows)
        ins = sum(float(r.get("inputTokens") or 0) for r in rows)
        outs = sum(float(r.get("outputTokens") or 0) for r in rows)
        tools = sum(float(r.get("toolCalls") or 0) for r in rows)
        seconds = sum(float(r.get("latencyMs") or 0) for r in rows) / 1000
        first = min(float(str(r.get("sk", "0")).split("#", 1)[0] or 0) / 1000 for r in rows)
        release = next((str(r["release"]) for r in rows if r.get("release")), "-")
        out.append(ManifestCost(mv, release, n, sum(1 for r in rows if r.get("status") == "completed"),
                                ins / n, outs / n, tools / n, ins / 1000 * in_price + outs / 1000 * out_price,
                                seconds * RUNTIME_PER_SECOND, first, priced))
    return sorted(out, key=lambda c: c.first_seen)


def manifest_lines(rows: Sequence[ManifestCost], limit: int | None = None) -> list[str]:
    shown = list(rows)[-limit:] if limit else list(rows)
    out = [f"{'release':<14} {'manifest':<28} {'sessions':>8} {'ok':>5} {'in tok':>7} {'out tok':>7} {'tools':>5} "
           f"{'$ / ok session':>14}"]
    for r in shown:
        per = f"{r.per_successful_usd:.4f}" if r.per_successful_usd is not None else "-"
        out.append(f"{r.release:<14} {r.manifest_version:<28} {r.sessions:>8} {r.success_rate:>5.0%} "
                   f"{r.input_tokens:>7.0f} {r.output_tokens:>7.0f} {r.tool_calls:>5.1f} {per:>14}")
    if shown and not shown[0].priced:
        out.append("model price: planning price ($5 / $20 per 1M tokens); prices.yaml has none for this "
                   "model (run `module11 prices refresh`)")
    out.append("cost per successful session = (model tokens + runtime time) / sessions that completed; judge and "
               "online-evaluation spend are not included")
    return out


def measure(record: Mapping[str, Any], prices: Mapping[str, Any] | None = None, model_id: str = "") -> dict[str, Any]:
    """Measured spend of one RunRecord: token counts summed from the sessions' usage and the Evaluate results."""
    sessions = [s for s in record.get("sessions") or [] if s.get("status") != "invoke_error"]
    results = record.get("results") or []
    n = max(1, len(sessions))
    s_in = sum(int(s.get("inputTokens") or 0) for s in sessions)
    s_out = sum(int(s.get("outputTokens") or 0) for s in sessions)
    j_in = sum(int(r.get("inputTokens") or 0) for r in results)
    j_out = sum(int(r.get("outputTokens") or 0) for r in results)
    judge_calls = sum(1 for r in results if r.get("evaluatorId") in JUDGED and r.get("label") != "BLOCKED_BY_GUARDRAIL")
    code_calls = sum(1 for r in results if str(r.get("evaluatorId", "")).startswith("custom.") and r.get("value") is not None)
    seconds = sum(float(s.get("latencyMs") or 0) for s in sessions) / 1000
    in_price, out_price, priced = model_price(prices, model_id)
    model_usd = s_in / 1000 * in_price + s_out / 1000 * out_price
    judge_usd = j_in / 1000 * JUDGE_IN_PER_1K + j_out / 1000 * JUDGE_OUT_PER_1K
    code_usd = code_calls * CODE_EVAL_EACH
    runtime_usd = seconds * RUNTIME_PER_SECOND
    total = model_usd + judge_usd + code_usd + runtime_usd
    return {"sessions": len(sessions), "modelUsd": round(model_usd, 4), "judgeUsd": round(judge_usd, 4),
            "codeUsd": round(code_usd, 4), "runtimeUsd": round(runtime_usd, 4), "totalUsd": round(total, 4),
            "perSessionUsd": round(total / n, 5), "modelPriceFromPricesYaml": priced,
            "sessionInputTokens": s_in / n, "sessionOutputTokens": s_out / n,
            "judgeInputTokens": j_in / max(1, judge_calls), "judgeOutputTokens": j_out / max(1, judge_calls),
            "sessionSeconds": seconds / n, "judgeCalls": judge_calls, "codeCalls": code_calls}
