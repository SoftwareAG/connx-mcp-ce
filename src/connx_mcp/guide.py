#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""guide.py - `guide`: where this machine stands and the one command to run next.

Description:
    `guide` asks the same questions as `start` and `status`, with their own checks: Docker
    (check_docker), the environment's completeness (lifecycle.check_complete), its stored image
    (image.choice, which refuses one naming no registry, as that environment's step), the demo binding's
    refusals that `start` would raise (bind.refusals, which pairs each with its fix), its image
    (image.inspect, before the first start pins it), then the StatusReport of STATUS_CHECKS
    (container, `/health`, certificate, drift, demo binding) and whether the skill is installed
    (skill.installed; a record saying uninstall-skill removed it on purpose skips that step). Each
    environment lands on the first stage of STAGES it has not passed; the most urgent stage over all
    guided environments names exactly one next step, as a runnable command whose `<...>` parts are
    listed in `fill_in`. Once every guided environment is healthy and has its skill, `done` is true
    and there is no next step. The status notices and drift of every environment are warnings, as is
    a hint for a recorded skill instructions section that has gone (skill.instructions_hints), which
    never blocks `done`. `guide` never prompts and never
    changes anything; it always succeeds, since its answer is the result.

Copyright:
    Copyright 2026 Software GmbH
    Licensed under the Apache License, Version 2.0 (the "License"); you may not use this file except in compliance
    with the License. You may obtain a copy of the License at https://www.apache.org/licenses/LICENSE-2.0
    Unless required by applicable law or agreed to in writing, software distributed under the License is distributed
    on an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the License
    for the specific language governing permissions and limitations under the License.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from connx_mcp import CLI_NAME, environment, exposure, image, lifecycle, login, skill, tls
from connx_mcp.demo import bind
from connx_mcp.docker import CONTAINER_UP, check_docker
from connx_mcp.environment import Environment
from connx_mcp.lifecycle import StatusReport
from connx_mcp.output import CliError, Output
from connx_mcp.ports import Ports
from connx_mcp.start_inputs import StartInputs

STAGES = (
    "docker",
    "no_environment",
    "not_configured",
    "no_image",
    "not_started",
    "unhealthy",
    "certificate",
    "demo_binding",
    "no_skill",
    "healthy",
)
_PLACEHOLDER = re.compile(r"<[^<>]+>")


@dataclass
class Step:
    """Where one environment (or the machine) stands and the command that moves it on.

    Attributes:
        stage (str): One of STAGES.
        environment (str | None): The environment; None for a machine-wide step.
        reason (str): What was found, in a sentence or two.
        command (str | None): The one command to run next; None once the environment is healthy.
        warnings (list[str]): The environment's status notices and drift.
    """

    stage: str
    environment: str | None
    reason: str
    command: str | None
    warnings: list[str] = field(default_factory=list)

    @property
    def rank(self) -> int:
        """The stage's place in STAGES: lower is more urgent."""
        return STAGES.index(self.stage)

    def to_json(self) -> dict[str, Any]:
        """Return the step as one line of the environments list."""
        return {"environment": self.environment, "stage": self.stage, "reason": self.reason, "next": self.command}


def guide(ports: Ports, out: Output, given_name: str | None) -> None:
    """Run `guide`: inspect this machine and name the next step.

    Args:
        ports (Ports): The ports.
        out (Output): Where the result goes.
        given_name (str | None): The environment to guide; without it CONNX_MCP_ENVIRONMENT, else every environment.
            An invalid name, or Docker failing while inspecting, ends in that CliError.
    """
    try:
        check_docker(ports.docker)
    except CliError as error:
        _report(out, Step("docker", None, error.message, error.next_command or "docker version"), [])
        return
    names = _names(given_name)
    steps = [_environment_step(name, ports) for name in names]
    if (unbound := _orphaned_binding(steps, environment.names())) is not None:
        steps.append(unbound)
    chosen = min(steps, key=lambda step: step.rank)
    _report(out, chosen, [s for s in steps if s.environment is not None])


def _names(given_name: str | None) -> list[str]:
    """Return the environments to guide: the named or selected one, else every one, else `default`."""
    if given_name is not None or os.environ.get(environment.ENVIRONMENT_VARIABLE):
        return [environment.resolve_name(given_name, "guide")]
    return environment.names() or [environment.DEFAULT_NAME]


def _configure_command(name: str) -> str:
    """Return the configure command for an environment; `default` needs no name."""
    return f"{CLI_NAME} configure" if name == environment.DEFAULT_NAME else f"{CLI_NAME} configure {name}"


def _environment_step(name: str, ports: Ports) -> Step:
    """Return the first stage an environment has not passed, with its command."""
    try:
        env = environment.load(name)
    except CliError as error:
        return Step("not_configured", name, error.message, error.next_command or _configure_command(name))
    if not env.exists:
        return Step(
            "no_environment", name, environment.EnvironmentNotFound(env).message, _configure_command(name),
        )  # fmt: skip
    try:
        lifecycle.check_complete(env, StartInputs())
    except CliError as error:
        return Step("not_configured", name, error.message, error.next_command or _configure_command(name))
    # `start` refuses a stored image that names no registry right after completeness.
    if (refusal := image.refused_reference(env)) is not None:
        return Step("not_configured", name, refusal.message, refusal.next_command)
    step = _configured_step(env, ports)
    # A warning, never the next step; a running environment's status report already carries it.
    if (exposed := exposure.warning(env)) is not None and exposed not in step.warnings:
        step.warnings.insert(0, exposed)
    return step


def _configured_step(env: Environment, ports: Ports) -> Step:
    """Return the first stage a complete environment has not passed: binding, image, container, health, skill."""
    name = env.name
    binding = bind.follow(env)
    # Before the image and the container: `start` refuses these right after the completeness check.
    if refusals := bind.refusals(env, binding):
        return Step("demo_binding", name, refusals[0].message, refusals[0].next_command)
    if (missing := _missing_image(env, ports)) is not None:
        return missing
    report = lifecycle.collect_status(env, ports)
    step = _running_step(env, report, binding, ports)
    step.warnings = [*report.notices, *(f"Drift: {d}" for d in report.drift), *step.warnings]
    return step


def _missing_image(env: Environment, ports: Ports) -> Step | None:
    """Return the sign-in or download step when the image the first start pins is not on this machine."""
    if env.host.image_digest:
        return None
    chosen = image.choice(env)
    ref = chosen.reference
    if image.inspect(ports.docker, ref) is not None:
        return None
    registry = image.registry(ref)
    warnings: list[str] = []
    try:
        has_sign_in = login.signed_in(registry)
    except CliError as error:
        has_sign_in, warnings = False, [error.message]
    if not has_sign_in:
        # Plain `login` signs in to the default image's registry, which an environment's own image never uses.
        plain = not chosen.environment and registry == image.default_registry().value
        return Step(
            "no_image",
            env.name,
            f"The image {ref} is not on this machine, and Docker holds no sign-in for {registry}. "
            + f"{image.REGISTRY_HINT}\n{chosen.line}",
            f"{CLI_NAME} login" if plain else f"{CLI_NAME} login --registry {registry}",
            warnings,
        )
    return Step("no_image", env.name, f"The image {ref} is not on this machine.", f"{CLI_NAME} download {env.name}")


def _running_step(env: Environment, report: StatusReport, binding: bind.Binding | None, ports: Ports) -> Step:
    """Return the step of a configured environment with its image: started, health, certificate, binding, skill."""
    name = env.name
    if report.state == "paused":
        return Step("unhealthy", name, f"Environment {name!r} is paused, so it answers nothing.",
                    f"docker unpause {lifecycle.container_name(env)}")  # fmt: skip
    if report.state not in CONTAINER_UP:
        if report.state == "exited" and report.exit_code:
            return Step(
                "not_started",
                name,
                f"Environment {name!r} exited with exit code {report.exit_code}. Read its log, fix the cause, "
                f"then run {CLI_NAME} start {name}.",
                f"{CLI_NAME} log {name} --tail 100",
            )
        return Step("not_started", name, f"Environment {name!r} is not running ({report.state}).",
                    f"{CLI_NAME} start {name}")  # fmt: skip
    if not report.serving:
        answer = f"answers {report.http_status}" if report.http_status is not None else f"is {report.health_error}"
        reason = f"Environment {name!r} is {report.state} but not serving: /health {answer}."
        if report.docker_health == "starting":
            return Step("unhealthy", name, f"{reason} It is still starting.", f"{CLI_NAME} status {name}")
        return Step("unhealthy", name, reason, f"{CLI_NAME} log {name} --tail 100")
    if (certificate := _certificate_step(env, report)) is not None:
        return certificate
    if binding is not None and binding.notes:
        return Step("demo_binding", name, binding.notes[0], f"{CLI_NAME} start-demo")
    return _skill_step(env, report, ports)


def _certificate_step(env: Environment, report: StatusReport) -> Step | None:
    """Return the renew step for a served certificate near expiry, or a renewed one not yet served."""
    facts = report.certificate
    if facts is None:
        return None
    served = facts.get("served") or {}
    level = served.get("level", "ok")
    restart = f"{CLI_NAME} restart {env.name} --if-cert-changed"
    if facts.get("renewed_not_served"):
        return Step("certificate", env.name, f"The certificate in {facts['directory']} is not served yet.", restart)
    if level == "ok":
        return None
    reason = f"The certificate {env.name!r} serves is {level}: {facts['summary']}."
    if facts.get("directory") is None and served.get("self_signed"):
        return Step(
            "certificate",
            env.name,
            f"{reason} The step deletes the volume {tls.certificate_volume(env)} that holds the generated "
            "certificate, so the next start generates a new one; run it when a short outage is acceptable.",
            tls.purge_command(env),
        )
    return Step("certificate", env.name, f"{reason} Renew it in {facts['directory']}.", restart)


def _skill_step(env: Environment, report: StatusReport, ports: Ports) -> Step:
    """Return the install-skill step until the skill is installed, then the healthy step, which is done.

    A recorded skill instructions section that has gone is a hint among the warnings, never a step. A record
    saying uninstall-skill removed everything skips the step until the next install-skill.
    """
    serving = f"Environment {env.name!r} is serving at {report.health_url.removesuffix('/health')}."
    warnings: list[str] = []
    try:
        has_skill = skill.installed(env)
    except CliError as error:
        has_skill, warnings = False, [error.message]
    if has_skill:
        hints = skill.instructions_hints(env)
        return Step("healthy", env.name, f"{serving} The connx-sql-expert skill is installed.", None, hints)
    if not warnings and (uninstalled := skill.uninstalled_on(env)) is not None:
        reason = f"{serving} You uninstalled the connx-sql-expert skill on {uninstalled}, so guide skips its step."
        return Step("healthy", env.name, reason, None)
    home = Path(skill.home_directory(os.name, os.environ))
    detected = skill.detect(ports.docker.which, home)
    command = skill.InstallRequest([], None, False, None).command(env, detected)
    reason = f"{serving} Give your AI harness the connx-sql-expert skill; this is the last step."
    return Step("no_skill", env.name, reason, command, warnings)


def _orphaned_binding(steps: list[Step], names: list[str]) -> Step | None:
    """Return the rebind step when the demo stack is bound to an environment that no longer exists."""
    bound = bind.bound_environment()
    if bound is None or bound in names:
        return None
    target = next((s.environment for s in steps if s.stage != "no_environment" and s.environment), None)
    if target is None:
        return None
    return Step(
        "demo_binding",
        None,
        f"The demo stack is bound to environment {bound!r}, which no longer exists.",
        f"{CLI_NAME} configure-demo --for {target} --rebind",
    )


def _report(out: Output, chosen: Step, steps: list[Step]) -> None:
    """Print the next step (or that there is none), the warnings and, with several environments, each one's stage."""
    many = len(steps) > 1
    warnings = [f"{s.environment}: {w}" if many else w for s in steps for w in s.warnings]
    reason = chosen.reason
    if many and chosen.command is not None:
        waiting = sum(1 for s in steps if s.command is not None)
        reason += f" ({waiting} of {len(steps)} environments need attention; the most urgent is named.)"
    done = chosen.command is None
    data = {
        "stage": chosen.stage,
        "environment": chosen.environment,
        "reason": reason,
        "done": done,
        "next": chosen.command,
        "fill_in": _PLACEHOLDER.findall(chosen.command or ""),
        "warnings": warnings,
        "environments": [s.to_json() for s in steps],
    }
    lines = [reason, "Nothing to do: every step is done." if done else f"Next: {chosen.command}"]
    if many:
        lines += ["", "Environments:", *(f"  {s.environment}: {s.stage} ({s.command or 'done'})" for s in steps)]
    lines += [f"Warning: {w}" for w in warnings]
    out.result(data, "\n".join(lines))
