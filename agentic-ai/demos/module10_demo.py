#!/usr/bin/env python3
"""
demos/module10_demo.py
======================
Module 10: Agent Exposure That Survives Change and Failure.

The cloud demo follows the DevOps Companion across its exposure boundary:

  1. API Gateway exposes the agent with Cognito identity, Lambda authorization,
     request limits, and a streaming Lambda adapter.
  2. LaunchDarkly exposes a recovery-planning capability to a selected team.
  3. Portkey demonstrates model routing and fallback after a controlled failure.
  4. Logz.io connects each caller to the behavior and models they received.

Local mode covers the partner exercises only, numbered 1–3.

USAGE
-----
  python demos/module10_demo.py --mode local
  python demos/module10_demo.py --section 1 --mode local
  python demos/module10_demo.py --section 2 --mode local
  MODULE10_MODE=mock python demos/module10_demo.py --section 2
  MODULE10_MODE=mock python demos/module10_demo.py --no-pause
  MODULE10_MODE=live python demos/module10_demo.py
"""

from __future__ import annotations

import argparse
from contextlib import ExitStack
import json
import os
import sys
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from textwrap import shorten

# Allow `python demos/module10_demo.py` when launched from `agentic-ai/`.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from module10.control_planes import MockPortkeyGateway
from module10.models import ReviewResponse
from module10.runtime import RuntimeBundle, build_runtime
from module10.server import start_server
from module10.urls import open_url

try:
    from rich.console import Console
    from rich.markdown import Markdown
    from rich.panel import Panel
    from rich.table import Table

    _console = Console()
    _RICH = True
except ImportError:
    _console = None
    _RICH = False


DEFAULT_CHANGE = json.loads(
    (Path(__file__).resolve().parents[1] / "module10/fixtures/deployment.json")
    .read_text(encoding="utf-8")
)["change_summary"]


def header(text: str, color: str = "cyan") -> None:
    if _RICH:
        _console.rule(f"[bold {color}]{text}[/bold {color}]", style=color)
    else:
        print(f"\n{'=' * 72}\n{text}\n{'=' * 72}")


def box(title: str, body: str, color: str = "cyan") -> None:
    if _RICH:
        _console.print(
            Panel(
                body,
                title=f"[bold]{title}[/bold]",
                border_style=color,
            )
        )
    else:
        print(f"\n--- {title} ---\n{body}")


def concept(text: str) -> None:
    text = " ".join(text.split())
    if _RICH:
        layout = Table.grid(padding=(0, 1), expand=True)
        layout.add_column(style="bold yellow", no_wrap=True)
        layout.add_column(style="yellow", ratio=1, overflow="fold")
        layout.add_row("💡 Module 10 Concept:", text)
        _console.print()
        _console.print(layout)
    else:
        print(f"\n💡 Module 10 Concept: {text}")


def user_says(text: str) -> None:
    if _RICH:
        _console.print(f"\n[bold green]USER ›[/bold green] [italic]{text}[/italic]")
    else:
        print(f"\nUSER › {text}")


def step_indicator(step: str, status: str = "completed", detail: str = "") -> None:
    """Show the current behavior as a short workflow event."""
    icons = {"completed": "✓", "running": "⟳", "paused": "⏸", "failed": "✗"}
    colors = {"completed": "green", "running": "yellow", "paused": "blue", "failed": "red"}
    icon = icons.get(status, "•")
    color = colors.get(status, "dim")
    message = f"  [{color}]{icon}[/{color}] [bold]{step}[/bold]"
    if detail:
        message += f" [dim]— {detail}[/dim]"
    if _RICH:
        _console.print(message)
    else:
        print(message.replace(f"[{color}]", "").replace(f"[/{color}]", "").replace("[bold]", "").replace("[/bold]", "").replace("[dim]", "").replace("[/dim]", ""))


def show_values(title: str, values: list[tuple[str, str]], color: str = "green") -> None:
    """Render a compact behavior summary rather than the full response payload."""
    if _RICH:
        table = Table(title=title, show_header=False, show_lines=False)
        table.add_column("Field", style=color, no_wrap=True)
        table.add_column("Value", overflow="fold")
        for label, value in values:
            table.add_row(label, value)
        _console.print(table)
    else:
        print(f"\n--- {title} ---")
        for label, value in values:
            print(f"  {label}: {value}")


def _short_message(response: ReviewResponse) -> str:
    message = str(response.body.get("message", ""))
    return shorten(message, width=96, placeholder="…") if message else "No agent summary returned."


def _release_values(response: ReviewResponse) -> list[tuple[str, str]]:
    release = response.body.get("release", {})
    route = response.body.get("route", {})
    agent_version = str(release.get("agent_version", "unknown"))
    status = response.status_code
    outcome = "available" if status < 400 else "rejected"
    if status == 503:
        outcome = "disabled by kill switch"
    behavior = {
        "reviewer-v1": "concise risk summary + recommendation",
        "reviewer-v2": "severity + evidence + next action",
        "none": "agent disabled",
    }.get(agent_version, "release behavior selected by control plane")
    return [
        ("HTTP outcome", f"{status} — {outcome}"),
        ("Release", f"{release.get('variation', 'unknown')} / {agent_version}"),
        ("Behavior contract", behavior),
        ("Model", str(route.get("actual_model", "not invoked"))),
        ("Degraded", str(route.get("degraded", False))),
        ("Agent says", _short_message(response)),
    ]


def pause(message: str, no_pause: bool) -> None:
    if no_pause:
        return
    try:
        input(message)
    except KeyboardInterrupt:
        raise SystemExit(0)


def clear_screen() -> None:
    print("\033[H\033[2J", end="")


@dataclass
class DemoContext:
    runtime: RuntimeBundle
    base_url: str
    no_pause: bool
    change_summary: str
    last_response: ReviewResponse | None = None
    full_demo: bool = False

    def request(self, tenant_id: str) -> ReviewResponse:
        payload = {
            "tenant_id": tenant_id,
            "change_summary": self.change_summary,
        }
        headers = {"Content-Type": "application/json"}
        if self.runtime.mode == "local":
            payload.pop("tenant_id")
            headers["X-Demo-Team"] = tenant_id
            headers["X-Demo-User"] = f"{tenant_id}-operator"
        encoded = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            f"{self.base_url}/invoke",
            data=encoded,
            headers=headers,
            method="POST",
        )
        try:
            with open_url(request, timeout=200) as response:
                body = json.loads(response.read().decode("utf-8"))
                result = ReviewResponse(response.status, body)
        except urllib.error.HTTPError as exc:
            body = json.loads(exc.read().decode("utf-8"))
            result = ReviewResponse(exc.code, body)
        self.last_response = result
        return result

    def set_recovery_planning_enabled(self, enabled: bool) -> None:
        from module10.flags import set_recovery_planning_enabled

        set_recovery_planning_enabled(self.runtime.launch_controller.path, enabled)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            with open_url(f"{self.base_url}/status", timeout=2) as response:
                teams = json.load(response)["teams"]
            if (
                teams["platform"]["recovery_planning_enabled"] is enabled
                and teams["payments"]["recovery_planning_enabled"] is False
                and all(team["reason"].get("kind") != "ERROR" for team in teams.values())
            ):
                return
            time.sleep(0.05)
        raise RuntimeError("The endpoint did not observe the expected local flag change")


def show_runtime_status(runtime: RuntimeBundle) -> None:
    if _RICH:
        table = Table(title="Integration Status", show_lines=True)
        table.add_column("Component", style="cyan")
        table.add_column("Mode", style="green")
        table.add_column("Detail", overflow="fold")
        for status in runtime.statuses:
            table.add_row(status.component, status.mode, status.detail)
        _console.print(table)
    else:
        for status in runtime.statuses:
            print(f"  {status.component}: {status.mode} — {status.detail}")


def show_response(title: str, response: ReviewResponse) -> None:
    show_values(
        title,
        _release_values(response),
        color="green" if response.status_code < 400 else "yellow",
    )


def show_route(response: ReviewResponse, title: str) -> None:
    route = response.body.get("route", {})
    attempts = route.get("attempts", [])
    if _RICH:
        table = Table(title=title, show_header=False, show_lines=False)
        table.add_column("Event", style="cyan", no_wrap=True)
        table.add_column("What happened", overflow="fold")
        for index, attempt in enumerate(attempts, start=1):
            target = str(attempt.get("target", "unknown"))
            model = str(attempt.get("model", "unknown"))
            status = str(attempt.get("status_code", "unknown"))
            error = str(attempt.get("error", ""))
            result = f"{model} returned HTTP {status}"
            if error:
                result += f" ({shorten(error, width=72, placeholder='…')})"
            table.add_row(f"Attempt {index}", f"{target} → {result}")
        if not attempts:
            table.add_row("Attempts", "No gateway attempts were recorded.")
        table.add_row(
            "Outcome",
            f"alias {route.get('requested_alias', 'unknown')} completed via "
            f"{route.get('final_target', 'unknown')}; "
            f"degraded={route.get('degraded', False)}",
        )
        _console.print(table)
        _console.print(f"  [bold]Trace ID:[/bold] {response.body.get('trace_id')}")
    else:
        print(f"\n{title}")
        for index, attempt in enumerate(attempts, start=1):
            error = attempt.get("error", "")
            detail = f" ({shorten(str(error), width=72, placeholder='…')})" if error else ""
            print(
                f"  Attempt {index}: {attempt.get('target')} -> "
                f"{attempt.get('model')} returned HTTP {attempt.get('status_code')}{detail}"
            )
        print(
            f"  Outcome: alias {route.get('requested_alias')} completed via "
            f"{route.get('final_target')}; degraded={route.get('degraded')}"
        )
        print(f"  Trace ID: {response.body.get('trace_id')}")


def show_trace(runtime: RuntimeBundle, trace_id: str | None) -> None:
    trace = runtime.telemetry.get(trace_id)
    if not trace:
        box(
            "Trace Evidence Unavailable",
            "No in-memory trace was found for this request. "
            "Run the fallback scenario before investigating it.",
            color="yellow",
        )
        return

    if _RICH:
        table = Table(
            title=f"Trace Evidence — {trace['trace_id']}",
            show_lines=False,
        )
        table.add_column("Span", style="cyan")
        table.add_column("Status")
        table.add_column("Behavior observed", overflow="fold")
        for span in trace["spans"]:
            attributes = span.get("attributes", {})
            behavior = []
            for key in (
                "release.variation",
                "agent.version",
                "ai.gateway.fallback",
                "ai.gateway.primary_status_code",
                "gen_ai.response.model",
                "http.status_code",
                "response.degraded",
            ):
                if key in attributes:
                    behavior.append(f"{key}={attributes[key]}")
            table.add_row(
                span["name"],
                span["status"],
                ", ".join(behavior) or "Span recorded",
            )
        _console.print(table)
    else:
        print(f"\nTrace Evidence — {trace['trace_id']}")
        for span in trace["spans"]:
            attributes = span.get("attributes", {})
            behavior = ", ".join(
                f"{key}={attributes[key]}"
                for key in (
                    "release.variation",
                    "agent.version",
                    "ai.gateway.fallback",
                    "ai.gateway.primary_status_code",
                    "gen_ai.response.model",
                    "http.status_code",
                    "response.degraded",
                )
                if key in attributes
            )
            print(
                f"  {span['name']:<24} {span['status']:<6} "
                f"{behavior or 'Span recorded'}"
            )

    show_values(
        "Operator Evidence",
        [
            ("Search by", f"trace_id={trace['trace_id']}"),
            ("Release filter", 'tenant.id="platform-beta" + release.variation="candidate"'),
            ("Failure filter", "ai.gateway.fallback=true + primary_status_code=429"),
            (
                "Telemetry path",
                "OpenTelemetry → Logz.io"
                if runtime.telemetry.source == "opentelemetry"
                else runtime.telemetry.source,
            ),
        ],
        color="magenta",
    )


def show_intro(context: DemoContext) -> None:
    clear_screen()
    header("MODULE 10 — AGENT EXPOSURE AND INTEGRATION", "bold cyan")
    print(
        "\n  Agent exposure defines how applications reach an agent, what they can\n"
        "  ask it to do, and what they can expect in return.\n\n"
        "  The scenario: the DevOps Companion is deployed and running for multiple\n"
        "  teams. They want to make its deployment advice part of their tools\n"
        "  and release workflows.\n\n"
        "  Callers need a clear way to ask for a review, supply context,\n"
        "  and tell whether they received a complete answer or need to try again.\n"
        "  They should not need to understand how the agent is built.\n\n"
        "  Once teams depend on it, we need to keep that promise as the agent\n"
        "  evolves and the services behind it encounter problems.\n"
    )
    box(
        "Today's Scenario",
        "A release workflow asks for help with a failed staging deployment.\n"
        "Let's follow that request across the boundary from caller to agent,\n"
        "and see what it takes to make the service dependable.",
    )
    pause("  ↵  Press Enter to begin...", context.no_pause)


def section_1(context: DemoContext) -> None:
    clear_screen()
    number = 2 if context.runtime.mode == "cloud" else 1
    header(f"SECTION {number} — CAN WE SAFELY GIVE AN AGENT MORE CAPABILITY?", "green")
    box(
        "Background",
        "The DevOps Companion is running and investigating deployment\n"
        "problems. A new Recovery Planner can check service dependencies and\n"
        "rollback prerequisites, then prepare a plan for the release owner.\n\n"
        "We'll make that capability available to the platform team and confirm\n"
        "that other teams keep investigating through the same endpoint.",
    )
    box(
        "Release the planner safely",
        "We need to control which callers receive the new planner and be able\n"
        "to withdraw it while investigation stays available.\n\n"
        "LaunchDarkly provides runtime control: target changes to specific users\n"
        "or teams and switch capabilities on or off without redeploying.\n"
        "Here, a feature flag decides whether the Companion can delegate to\n"
        "the Recovery Planner for the caller's team.",
    )
    box(
        "What LaunchDarkly Controls",
        "Recovery planning OFF → investigate and hand planning to a human\n"
        "Recovery planning ON  → platform can delegate to the Recovery Planner\n"
        "                        other teams keep investigation only\n\n"
        "The application uses the flag to expose or remove the planner handoff.",
    )
    print(
        ("\n  In this demo, the agents run on AgentCore and use Amazon Bedrock\n"
         if context.runtime.mode == "cloud" else
         "\n  In this demo, the endpoint runs locally, and the agents use Amazon Bedrock\n")
        +
        "  to investigate a fictional deployment incident. Any recovery plan they\n"
        "  produce is a draft for an operator to review; the agents do not carry\n"
        "  out the proposed changes.\n"
    )
    context.set_recovery_planning_enabled(False)
    pause("  Press Enter to investigate with the existing capability...", context.no_pause)
    clear_screen()
    header("INVESTIGATE — RECOVERY PLANNING IS UNAVAILABLE", "green")
    show_investigation(context, planning_enabled=False)

    pause("  Press Enter to enable recovery planning...", context.no_pause)
    clear_screen()
    header("EXPOSE THE RECOVERY PLANNER", "green")
    context.set_recovery_planning_enabled(True)
    box(
        "LaunchDarkly flag changed",
        "Recovery planning: OFF → ON\n\n"
        "For the platform team, the Companion can now delegate to a specialist\n"
        "that checks dependencies and rollback readiness, then drafts a plan.\n"
        "Other teams continue with investigation only.\n\n"
        "The endpoint has loaded the change. No restart is needed.",
        color="green",
    )

    pause("  Press Enter to send the same request with planning available...", context.no_pause)
    clear_screen()
    header("INVESTIGATE AND DELEGATE — PRODUCE A RECOVERY PLAN", "green")
    show_investigation(context, planning_enabled=True)

    pause("  Press Enter to send the same request as the payments team...", context.no_pause)
    print("\n  Planning is still enabled for platform. Payments is outside the pilot.")
    show_investigation(context, team="payments", planning_enabled=False)
    concept(
        "A feature release changes what an agent can do for a particular caller. "
        "Targeting lets one team use the Recovery Planner while other teams keep "
        "their existing behavior through the same endpoint."
    )
    pause(
        "  Press Enter to explore model resilience..." if context.full_demo
        else "  Press Enter to finish this section...",
        context.no_pause,
    )


def show_investigation(
    context: DemoContext, *, planning_enabled: bool, team: str = "platform"
) -> ReviewResponse:
    identity = f"authenticated team: {team}" if context.runtime.mode == "cloud" else f"X-Demo-Team: {team}"
    print(
        f"\n  POST {context.base_url}/invoke | {identity}\n"
        "  The Companion is investigating...",
        flush=True,
    )
    response = context.request(team)
    if response.status_code != 200 or response.body.get("status") != "completed":
        raise RuntimeError(
            f"Investigation failed with HTTP {response.status_code}: {json.dumps(response.body)}"
        )
    release = response.body["release"]
    if (
        release["recovery_planning_enabled"] is not planning_enabled
        or release["reason"].get("kind") == "ERROR"
    ):
        raise RuntimeError(f"Unexpected flag evaluation: {release}")
    plan = response.body["recovery_plan"]
    if bool(plan) is not planning_enabled:
        raise RuntimeError("The agent did not complete the expected capability demonstration")
    print("  HTTP 200\n")
    for event in response.body["events"]:
        label = "handoff to Recovery Planner" if event["tool"] == "delegate_recovery_planning" else event["tool"]
        print(f"  {event['agent']} → {label} [{event['status']}]")
        if event.get("summary"):
            print(f"    {event['summary']}")
    print()
    if _RICH:
        _console.print(Panel(Markdown(str(response.body["review"])), title=f"DevOps Companion — {team}", border_style="green"))
        if plan:
            _console.print(Panel(Markdown(str(plan["text"])), title="Recovery Planner — draft for human review", border_style="cyan"))
    else:
        print(f"--- DevOps Companion — {team} ---\n{response.body['review']}")
        if plan:
            print(f"--- Recovery Planner — draft for human review ---\n{plan['text']}")
    if plan:
        location = (f"./artifacts/{Path(plan['path']).name}" if "path" in plan
                    else f"{context.base_url}/requests/{response.body['artifact_id']} (authenticated access)")
        print(f"\n  Saved plan: {location}\n  No changes executed.")
    return response


def section_2(context: DemoContext) -> None:
    if context.runtime.mode not in {"local", "cloud"}:
        section_2_legacy(context)
        return
    gateway = context.runtime.model_gateway.gateway
    context.set_recovery_planning_enabled(True)
    gateway.set_drill(False)
    clear_screen()
    number = 3 if context.runtime.mode == "cloud" else 2
    header(f"SECTION {number} — GIVE EACH AGENT THE RIGHT MODEL", "cyan")
    box(
        "Keep recovery planning available",
        "Agent exposure gives release workflows a dependable way to ask for help.\n"
        "The DevOps Companion is running; its Recovery Planner is available\n"
        "to the platform team. Portkey selects the model for each agent's work.\n\n"
        "Frontier model providers often offer variants with different tradeoffs\n"
        "in accuracy, latency, and cost. The right choice depends on the task.\n\n"
        "For this demo, we'll use Portkey to route between two OpenAI models:\n"
        "GPT-5.6 Luna for investigation and coordination, and GPT-5.6 Terra as\n"
        "the Recovery Planner's preferred model.\n\n"
        "A rate limit can interrupt a model request. We'll first run the preferred\n"
        "route, then simulate HTTP 429 so Portkey falls back to GPT-5.6 Luna.",
    )
    box(
        "How Portkey AI helps",
        "Portkey AI provides a gateway between our agents and their models.\n"
        "Here, it routes by agent role and handles temporary model failures.\n"
        "Conditional routing can also use metadata supplied by the application:\n"
        "a user's plan could select a model, or their region could select a\n"
        "regional provider. Each route can have its own fallback policy.\n\n"
        "Before using GPT-5.6 Luna as the Recovery Planner's fallback, we need to\n"
        "evaluate its tool use, incident reasoning, and recovery-plan quality\n"
        "against the same task requirements as the preferred model. Completing\n"
        "a request alone does not establish that a fallback is suitable.",
    )
    print(f"\n  Portkey console: {gateway.console_url}\n")
    pause("  Press Enter to investigate and prepare a recovery plan...", context.no_pause)
    show_routed_investigation(context, expect_fallback=False)

    pause("  Press Enter to prepare the model-interruption drill...", context.no_pause)
    clear_screen()
    header("SIMULATE A MODEL INTERRUPTION", "yellow")
    gateway.set_drill(True)
    box(
        "Failure drill armed",
        "The planner's next GPT-5.6 Terra request after gathering evidence will\n"
        "receive a simulated 429.\n\n"
        "The interruption is injected inside the demo gateway. Portkey handles\n"
        "it using the configured fallback policy; successful model calls are real.",
        color="yellow",
    )

    pause("  Press Enter to send the same request through the interruption...", context.no_pause)
    clear_screen()
    header("FINISH THE RECOVERY PLAN", "cyan")
    print(
        "\n  The planner gathers evidence using GPT-5.6 Terra. When its next model\n"
        "  request fails, Portkey forwards that turn to GPT-5.6 Luna with the\n"
        "  evidence already collected.\n"
    )
    show_routed_investigation(context, expect_fallback=True)
    box(
        "Recovery plan created using fallback",
        "The planner continued with its existing tool results. The caller received\n"
        "a completed draft and an explicit fallback indicator.\n"
        "The release owner still needs to review and approve the proposed recovery.",
        color="yellow",
    )

    concept(
        "Model routing is part of keeping an agent available. A tested fallback "
        "lets the workflow continue with its existing evidence when a model request "
        "fails, so the caller can still receive a complete plan."
    )
    pause(
        "  Press Enter to understand the caller's experience..." if context.full_demo
        else "  Press Enter to finish this section...",
        context.no_pause,
    )


def show_routed_investigation(context: DemoContext, *, expect_fallback: bool) -> None:
    response = show_investigation(context, planning_enabled=True)
    actual_fallback = response.body["routing"]["fallback_used"]
    if actual_fallback is not expect_fallback:
        raise RuntimeError(
            f"Expected fallback={expect_fallback}, observed {actual_fallback}. "
            "The live route did not match this stage of the drill."
        )
    usage = response.body.get("usage")
    if usage:
        print(f"  Completed responses: {usage['total_tokens']} tokens | Fallback used: {actual_fallback}")


def section_2_legacy(context: DemoContext) -> None:
    clear_screen()
    header("SECTION 2 — WHAT HAPPENS WHEN THE PRIMARY MODEL FAILS?", "yellow")
    box(
        "Background",
        "A release flag can select the right agent behavior, but it cannot make\n"
        "the model call reliable. The application should depend on a logical\n"
        "deployment alias and remain available when one physical route fails.",
    )
    box(
        "What Portkey Controls",
        "The application asks for `deployment-reviewer`.\n"
        "Portkey owns the physical route behind that alias:\n"
        "  primary  → GPT-5.6 Terra\n"
        "  fallback → GPT-5.6 Luna\n\n"
        "We will make one primary request fail so the behavior is visible.",
    )
    user_says("Run the candidate review, inject one primary-route failure, and recover.")

    first = context.request("platform-beta")
    step_indicator(
        "1. Candidate behavior selected",
        detail="platform-beta is still running reviewer-v2",
    )
    show_route(first, "Candidate Request — Primary Route")

    if isinstance(context.runtime.model_gateway, MockPortkeyGateway):
        context.runtime.model_gateway.arm_primary_throttle("platform-beta")
        step_indicator(
            "2. Primary failure prepared",
            status="failed",
            detail="the next GPT-5.6 Terra attempt will return HTTP 429",
        )
    else:
        box(
            "Live Portkey Action",
            "Trigger or confirm the prepared primary-route failure in Portkey, "
            "then continue so the next request exercises the configured fallback.",
            color="yellow",
        )
        pause(
            "  ↵  Press Enter after the model-interruption drill is ready...",
            context.no_pause,
        )

    response = context.request("platform-beta")
    step_indicator(
        "3. Fallback request completed",
        detail="Portkey recovered the request through the configured fallback",
    )
    show_route(response, "Portkey Fallback Evidence")
    show_response("Behavior after failover", response)
    concept(
        "The caller received a useful response, but this was degraded execution. "
        "The route evidence makes the tradeoff visible: the primary failed, the "
        "fallback completed, and the request remained available."
    )
    pause("  ↵  Press Enter to investigate the request...", context.no_pause)


def section_3(context: DemoContext) -> None:
    clear_screen()
    header("SECTION 3 — CAN AN OPERATOR EXPLAIN AND ROLL BACK THE CHANGE?", "magenta")
    response = context.last_response
    if response is None or not response.body.get("route", {}).get("fallback_used"):
        if isinstance(context.runtime.model_gateway, MockPortkeyGateway):
            context.runtime.model_gateway.arm_primary_throttle("platform-beta")
            response = context.request("platform-beta")

    box(
        "Background",
        "The request returned successfully after failover, but “HTTP 200” alone\n"
        "does not tell an operator whether the candidate behavior is healthy.\n"
        "The operator needs one connected story: release decision → model attempt\n"
        "→ fallback → final response.",
    )
    box(
        "What Logz.io / OpenTelemetry Contributes",
        "The trace carries the release variation, agent version, model attempts,\n"
        "429 evidence, and degraded outcome together. That lets the operator\n"
        "investigate the behavior before changing the release flag.",
    )
    user_says("Find the degraded request, explain what happened, then roll it back.")
    trace_id = response.body.get("trace_id") if response else None
    step_indicator(
        "Trace located",
        detail=f"{trace_id or 'no trace id'} links the release and gateway events",
    )
    show_trace(context.runtime, trace_id)

    if hasattr(context.runtime.launch_controller, "set_candidate_enabled"):
        step_indicator(
            "Candidate targeting disabled",
            detail="the next beta request should return to reviewer-v1",
        )
        context.runtime.launch_controller.set_candidate_enabled(False)
        rolled_back = context.request("platform-beta")
        show_response("Candidate Rolled Back to Stable", rolled_back)
        step_indicator(
            "Kill switch exercised",
            detail="temporarily disable the agent, then restore it",
        )
        context.runtime.launch_controller.set_agent_enabled(False)
        disabled = context.request("platform-beta")
        show_response("Kill Switch Response", disabled)
        context.runtime.launch_controller.set_agent_enabled(True)
    else:
        box(
            "Live LaunchDarkly Action",
            "Change the candidate targeting back to stable, or turn off the "
            "reviewer-enabled flag. Then invoke the same endpoint again.",
            color="magenta",
        )
        pause("  ↵  Press Enter after changing the feature flag...", context.no_pause)
        rolled_back = context.request("platform-beta")
        show_response("Post-Rollback Response", rolled_back)

    concept(
        "Logz.io supplies the evidence, LaunchDarkly supplies the rollback "
        "control, and Portkey remains the model-routing boundary. The useful "
        "integration is the handoff between those responsibilities."
    )
    pause("  ↵  Press Enter for the module summary...", context.no_pause)


def show_summary(context: DemoContext) -> None:
    clear_screen()
    header("MODULE 10 COMPLETE", "bold green")
    print(
        "\n  What we covered:\n\n"
        "   • LaunchDarkly\n"
        "     Targeted reviewer-v2 to platform-beta and provided the rollback switch.\n\n"
        "   • Portkey\n"
        "     Routed the logical deployment-reviewer request through the prepared\n"
        "     fallback path from GPT-5.6 Terra to GPT-5.6 Luna.\n\n"
        "   • Logz.io\n"
        "     Connected release, model attempts, degraded status, and trace evidence\n"
        "     so the operator could make a controlled rollback decision.\n"
    )
    print(f"  Telemetry: {context.runtime.telemetry.status()}")
    pause("  ↵  Demo complete.", context.no_pause)


def show_wrap_up(no_pause: bool, *, cloud: bool = False) -> None:
    clear_screen()
    header("MODULE 10 COMPLETE", "bold green")
    print("\n  What we covered:\n")
    if cloud:
        print(
            "   • Agent exposure through API Gateway\n"
            "     Cognito and a Lambda authorizer established caller identity and\n"
            "     permissions. API Gateway applied request controls, while our Lambda\n"
            "     adapter managed ownership, safe retries, and the response stream.\n"
        )
    print(
        "   • Controlled capability releases\n"
        "     Introduce new agent capabilities to selected callers through the same\n"
        "     endpoint. LaunchDarkly enabled the Recovery Planner for platform,\n"
        "     while payments continued with investigation only.\n\n"
        "   • Model selection and resilience\n"
        "     Match models to each agent's work and recover from temporary failures.\n"
        "     Portkey switched models after an error while preserving the agent's\n"
        "     context. Routing can also select models by task or user attributes,\n"
        "     such as a user's plan or region.\n\n"
        "   • Visibility into each caller's experience\n"
        "     Connect a request to its feature decisions, agent actions, and models.\n"
        "     Logz.io correlated that evidence so we could confirm which capability\n"
        "     the caller received and whether a model fallback occurred.\n\n"
        "  The caller kept using one endpoint as capabilities and models changed.\n"
    )
    pause("  Press Enter to finish...", no_pause)


def main() -> None:
    from module10.observability import load_environment
    load_environment()
    parser = argparse.ArgumentParser(
        description="Module 10: Agent Exposure That Survives Change and Failure"
    )
    parser.add_argument(
        "--section",
        type=int,
        choices=range(1, 5),
        metavar="1-4",
        help="cloud: exposure, flags, routing, tracing; local: flags, routing, tracing (1–3)",
    )
    parser.add_argument(
        "--mode",
        choices=("mock", "local", "live", "cloud"),
        default=None,
        help="defaults to MODULE10_MODE or local; cloud starts with API Gateway; local starts with flags",
    )
    parser.add_argument(
        "--with-ui",
        action="store_true",
        help="Use browser actions with terminal commentary.",
    )
    parser.add_argument(
        "--no-pause",
        action="store_true",
        help="run without interactive pauses",
    )
    parser.add_argument(
        "--change-summary",
        default=DEFAULT_CHANGE,
        help="deployment request sent to the exposed endpoint",
    )
    from module10.observability import validate_session
    parser.add_argument("--session", type=validate_session, default=os.getenv("MODULE10_DEMO_SESSION"))
    parser.add_argument("--seed-observability", action="store_true",
                        help="run Sections 1–2 without pauses to capture a real learning session")
    parser.add_argument("--outputs", type=Path, help="external Pulumi output JSON for cloud mode")
    parser.add_argument("--credentials-file", type=Path, help="external demo identity file for cloud mode")
    args = parser.parse_args()

    if args.seed_observability and args.section:
        parser.error("--seed-observability cannot be combined with --section")
    mode = args.mode or os.getenv("MODULE10_MODE") or "local"
    if args.with_ui and (mode != "cloud" or args.no_pause):
        parser.error("--with-ui requires --mode cloud and cannot be combined with --no-pause.")
    if mode == "cloud":
        if not args.outputs or not args.credentials_file:
            parser.error("Cloud mode requires --outputs and --credentials-file.")
        if args.seed_observability:
            parser.error("Run cloud Sections 2–3 with the same --session to capture a learning session.")
        if args.section == 4 and not args.session and not args.with_ui:
            parser.error("Cloud Section 4 requires the --session used for Sections 2–3.")
        from module10.cloud_demo import run
        try:
            run(args, sys.modules[__name__])
        except (ValueError, RuntimeError) as exc:
            parser.error(str(exc))
        return
    if args.section == 4:
        parser.error("Section 4 is available in cloud mode.")
    if args.section == 3:
        if not args.session:
            parser.error("Section 3 requires --session with captured Sections 1–2.")
        from module10.inspection import show
        try:
            show(args.session, no_pause=args.no_pause, clear=clear_screen,
                 header=header, box=box, pause=pause, concept=concept)
        except ValueError as exc:
            parser.error(str(exc))
        return
    if args.section == 1 and mode != "local":
        parser.error("Section 1 uses --mode local with real Bedrock Responses calls")
    if mode == "local" and args.section is None and not args.seed_observability:
        from uuid import uuid4
        args.session = args.session or "module10-" + uuid4().hex[:12]
    if args.seed_observability and mode != "local":
        parser.error("Seeding needs --mode local.")
    try:
        with ExitStack() as resources:
            if mode == "local":
                from module10.collector import managed_collector
                resources.enter_context(managed_collector(required=args.section is None))
            if args.seed_observability:
                from uuid import uuid4
                session_was_generated = not args.session
                args.session = args.session or uuid4().hex[:12]
                args.no_pause = True
                for section in (1, 2):
                    args.section = section
                    _run_demo(args, mode)
                print(f"\nCaptured learning session: {args.session}" if session_was_generated
                      else "\nLearning session captured.")
            else:
                _run_demo(args, mode)
    except ValueError as exc:
        parser.error(str(exc))


def _run_demo(args, mode):
    full_local = mode == "local" and args.section is None
    with ExitStack() as cleanup:
        flags_path = None
        if mode == "local":
            from module10.flags import flag_path

            directory = cleanup.enter_context(tempfile.TemporaryDirectory(prefix="module10-"))
            flags_path = Path(directory) / "flags.json"
            flags_path.write_text(flag_path().read_text(encoding="utf-8"), encoding="utf-8")
        runtimes = cleanup.enter_context(ExitStack())
        runtime = build_runtime(
            mode, flags_path=flags_path,
            model_backend="portkey" if args.section == 2 and mode == "local" else "bedrock",
            session=args.session, section=args.section or 1,
        )
        runtimes.callback(runtime.close)
        server, _thread = start_server(
            runtime.service,
            runtime_mode=runtime.mode,
            runtime_statuses=runtime.statuses,
        )
        cleanup.callback(server.server_close)
        cleanup.callback(server.shutdown)
        host, port = server.server_address
        context = DemoContext(
            runtime=runtime,
            base_url=f"http://{host}:{port}",
            no_pause=args.no_pause,
            change_summary=args.change_summary,
            full_demo=full_local,
        )
        if args.section is None:
            show_intro(context)
        sections = {
            1: section_1,
            2: section_2,
            3: section_3,
        }
        selected = [args.section] if args.section else ([1, 2, 3] if full_local else [2, 3])
        for number in selected:
            if full_local and number == 2:
                runtime = build_runtime(
                    "local", flags_path=flags_path, model_backend="portkey",
                    session=args.session, section=2,
                )
                runtimes.callback(runtime.close)
                # Requests are sequential in the demo. Rebind the next request's
                # service between sections while retaining the listening endpoint.
                server.RequestHandlerClass.service = runtime.service
                server.RequestHandlerClass.runtime_statuses = runtime.statuses
                context.runtime = runtime
                context.last_response = None
            if full_local and number == 3:
                from module10.inspection import show
                show(args.session, no_pause=args.no_pause, clear=clear_screen,
                     header=header, box=box, pause=pause, concept=concept,
                     final_prompt="  Press Enter for the module wrap-up...")
                continue
            sections[number](context)
            if hasattr(runtime.telemetry, "flush"):
                runtime.telemetry.flush()
        if full_local:
            show_wrap_up(args.no_pause)
        elif mode != "local":
            show_summary(context)


if __name__ == "__main__":
    main()
