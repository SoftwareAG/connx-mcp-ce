#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""lifecycle.py - The server lifecycle of one environment: start, stop, restart, log and status.

Description:
    `start` is a pipeline of steps (START_STEPS) over one StartRun: check the environment, pin the
    image on first start, settle the timeout, build and render the compose file, bring it up and
    wait until the container is healthy. A later slice adds a check, an override or a warning by
    inserting a step; the steps share the run's render input (`run.spec`) and its warnings. The
    wait ends at once when the container exits, and on timeout leaves it running; both failures
    print the log tail and the timeout also the `/health` answer. A mounted CDD or Vault CA file
    other users cannot read is refused before compose up, and a server log line naming either file
    ends the wait at once with that cause. `status` builds a StatusReport
    from STATUS_CHECKS (container state, Docker health, `/health`), which later slices extend
    with fields and checks; `start` ends with the same report.

Copyright:
    Copyright 2026 Software GmbH
    Licensed under the Apache License, Version 2.0 (the "License"); you may not use this file except in compliance
    with the License. You may obtain a copy of the License at https://www.apache.org/licenses/LICENSE-2.0
    Unless required by applicable law or agreed to in writing, software distributed under the License is distributed
    on an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the License
    for the specific language governing permissions and limitations under the License.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from connx_mcp import (
    CLI_NAME,
    compose,
    drift,
    environment,
    exposure,
    identity,
    image,
    port_check,
    settings,
    start_inputs,
    tls,
)
from connx_mcp.compose import ComposeSpec
from connx_mcp.demo import bind
from connx_mcp.docker import query
from connx_mcp.environment import Environment, EnvironmentNotFound
from connx_mcp.image import ImageStatus
from connx_mcp.output import CliError, ExitCode, Output, shell_argument
from connx_mcp.ports import Ports, Terminal
from connx_mcp.start_inputs import StartInputs

DEFAULT_START_TIMEOUT = 180
DEFAULT_STOP_TIMEOUT = 10
POLL_SECONDS = 2.0
LOG_TAIL_LINES = 40
STATE_FORMAT = (
    '{"status":{{json .State.Status}},'
    '"health":{{if .State.Health}}{{json .State.Health.Status}}{{else}}null{{end}},'
    '"exit_code":{{.State.ExitCode}},"restarts":{{.RestartCount}}}'
)
_ENDED = {"exited", "dead", "restarting"}


# --- Shared facts ---------------------------------------------------------------------------------------


def load_configured(given_name: str | None, command: str) -> Environment:
    """Load an environment that must exist.

    Args:
        given_name (str | None): The name on the command line.
        command (str): The command, for the suggested next command.

    Returns:
        Environment: The stored environment.

    Raises:
        EnvironmentNotFound: There is no such environment; an invalid name raises resolve_name's CliError.
    """
    env = environment.load(environment.resolve_name(given_name, command))
    if not env.exists:
        raise EnvironmentNotFound(env)
    return env


def container_name(env: Environment) -> str:
    """Return the server container's name.

    Args:
        env (Environment): The environment.

    Returns:
        str: `connx-mcp-ce-<name>`.
    """
    return compose.project_name(env.name)


def base_url(env: Environment) -> str:
    """Return the server's URL on this machine: https when the server settings turn TLS on.

    Args:
        env (Environment): The environment.

    Returns:
        str: `http(s)://localhost:<published port>`.
    """
    scheme = "https" if settings.tls_on(env.server) else "http"
    return f"{scheme}://localhost:{compose.published_port(env)}"


@dataclass(frozen=True)
class ContainerState:
    """What Docker says about the server container.

    Attributes:
        status (str): Docker's state: created, running, restarting, exited, paused or dead.
        health (str | None): Docker health (starting, healthy, unhealthy); None without a health check.
        exit_code (int): The last exit code.
        restarts (int): How often Docker restarted it.
    """

    status: str
    health: str | None
    exit_code: int
    restarts: int


def container_state(ports: Ports, container: str) -> ContainerState | None:
    """Inspect a container.

    Args:
        ports (Ports): The ports.
        container (str): The container name.

    Returns:
        ContainerState | None: Its state, or None when there is no such container.

    Raises:
        CliError: Docker failed for another reason.
    """
    result = query(ports.docker, ["container", "inspect", "--format", STATE_FORMAT, container])
    if result.returncode != 0:
        if "no such" in result.stderr.lower():
            return None
        raise CliError(ExitCode.DOCKER_FAILED, f"docker container inspect {container} failed: {result.stderr.strip()}")
    facts = json.loads(result.stdout)
    return ContainerState(
        status=facts["status"], health=facts["health"], exit_code=facts["exit_code"], restarts=facts["restarts"]
    )


def log_tail(ports: Ports, container: str, lines: int = LOG_TAIL_LINES, since: float | None = None) -> str:
    """Return the last log lines of a container, stdout and stderr merged in time order.

    Args:
        ports (Ports): The ports.
        container (str): The container name.
        lines (int): How many lines.
        since (float | None): Only lines logged from then on (seconds since the epoch); None for any.

    Returns:
        str: The lines without Docker's timestamps, or why there are none.
    """
    after = ["--since", str(int(since))] if since is not None else []
    result = query(ports.docker, ["logs", *after, "--tail", str(lines), "--timestamps", container])
    if result.returncode != 0:
        return f"(no log: {result.stderr.strip()})"
    stamped = sorted(result.stdout.splitlines() + result.stderr.splitlines(), key=lambda line: line.split(" ", 1)[0])
    return "\n".join(line.split(" ", 1)[1] if " " in line else line for line in stamped) or "(the log is empty)"


# --- status ---------------------------------------------------------------------------------------------


@dataclass
class StatusReport:
    """One environment's status; later slices add fields and STATUS_CHECKS entries that fill them.

    Attributes:
        environment (str): The environment name.
        container (str): The container name.
        state (str): Docker's container state, or `not created`.
        docker_health (str | None): Docker health; None without a container or health check.
        exit_code (int | None): The container's last exit code.
        health_url (str): The `/health` URL probed.
        http_status (int | None): The `/health` HTTP status; None when not answered.
        health_body (str): The `/health` body.
        health_error (str | None): Why `/health` was not answered.
        notices (list[str]): Warnings for the administrator.
        image (ImageStatus | None): The pinned image, a newer pulled one and the one running.
        drift (list[str]): How the running container differs from the stored environment.
        certificate (dict[str, Any] | None): The served and on-disk certificates with TLS on (tls.certificate_facts).
        demo_bound_environment (str | None): The environment the demo stack is bound to (bind.status_check).
        demo_stack (dict[str, Any] | None): The remote demo stack the environment is bound to (remote.describe).
        network (dict[str, Any] | None): Where the port listens, whether callers sign in with OAuth and whether
            the open network is accepted (exposure.status_check).
    """

    environment: str
    container: str
    state: str = "not created"
    docker_health: str | None = None
    exit_code: int | None = None
    health_url: str = ""
    http_status: int | None = None
    health_body: str = ""
    health_error: str | None = None
    notices: list[str] = field(default_factory=list)
    image: ImageStatus | None = None
    drift: list[str] = field(default_factory=list)
    certificate: dict[str, Any] | None = None
    demo_bound_environment: str | None = None
    demo_stack: dict[str, Any] | None = None
    network: dict[str, Any] | None = None

    @property
    def serving(self) -> bool:
        """Whether the server answers `/health` with 200."""
        return self.http_status == 200

    def to_json(self) -> dict[str, Any]:
        """Return the report as a JSON object."""
        try:
            body: Any = json.loads(self.health_body) if self.health_body else None
        except json.JSONDecodeError:
            body = self.health_body
        return {
            "environment": self.environment,
            "container": self.container,
            "state": self.state,
            "docker_health": self.docker_health,
            "exit_code": self.exit_code,
            "serving": self.serving,
            "health": {
                "url": self.health_url,
                "http_status": self.http_status,
                "body": body,
                "error": self.health_error,
            },
            "image": self.image.to_json() if self.image else None,
            "certificate": self.certificate,
            "notices": self.notices,
            "drift": self.drift,
            "demo_bound_environment": self.demo_bound_environment,
            "demo_stack": self.demo_stack,
            "network": self.network,
        }

    def human(self) -> str:
        """Return the report as text."""
        answer = (
            f"{self.http_status} {self.health_body.strip()}"
            if self.http_status is not None
            else f"no answer ({self.health_error})"
        )
        lines = [
            f"Environment {self.environment!r} ({self.container})",
            f"  Container:     {self.state}" + (f" (exit code {self.exit_code})" if self.state == "exited" else ""),
            f"  Docker health: {self.docker_health or 'none'}",
            f"  /health:       {answer}",
            f"                 {self.health_url}",
            *(self.image.human() if self.image else []),
            *([f"  Certificate:   {self.certificate['summary']}"] if self.certificate else []),
        ]
        lines += [f"  Drift:         {d}" for d in self.drift]
        if bound := self.demo_bound_environment:
            lines.append(
                f"  Demo stack:    bound to {'this environment' if bound == self.environment else repr(bound)}"
            )
        if far := self.demo_stack:
            ports = ", ".join(f"{service} {port}" for service, port in far["ports"].items())
            lines.append(f"  Demo stack:    remote at {far['stack_host']} ({ports})")
        return "\n".join([*lines, *(f"Warning: {n}" for n in self.notices)])


StatusCheck = Callable[[Environment, Ports, StatusReport], None]


def _container_facts(env: Environment, ports: Ports, report: StatusReport) -> None:
    """Fill in the container's state and Docker health."""
    state = container_state(ports, report.container)
    if state is not None:
        report.state, report.docker_health, report.exit_code = state.status, state.health, state.exit_code


def _health_facts(env: Environment, ports: Ports, report: StatusReport) -> None:
    """Ask the running server's `/health`; TLS verification is off for this probe only."""
    report.health_url = f"{base_url(env)}/health"
    if report.state != "running":
        report.health_error = f"not asked: the container is {report.state}"
        return
    reply = ports.probe.health(report.health_url)
    report.http_status, report.health_body, report.health_error = reply.status, reply.body, reply.error


STATUS_CHECKS: list[StatusCheck] = [
    image.status_reference,
    _container_facts,
    _health_facts,
    image.status_check,
    drift.since_start,
    tls.certificate_facts,
    bind.status_check,
    exposure.status_check,
]


def collect_status(env: Environment, ports: Ports) -> StatusReport:
    """Build an environment's status report.

    Args:
        env (Environment): The environment.
        ports (Ports): The ports.

    Returns:
        StatusReport: Every STATUS_CHECKS entry applied in order.
    """
    report = StatusReport(environment=env.name, container=container_name(env))
    for check in STATUS_CHECKS:
        check(env, ports, report)
    return report


def status(ports: Ports, out: Output, given_name: str | None) -> None:
    """Run `status <env>`: container state, Docker health and the `/health` answer.

    Args:
        ports (Ports): The ports.
        out (Output): Where the result goes.
        given_name (str | None): The environment name on the command line.
    """
    report = collect_status(load_configured(given_name, "status"), ports)
    out.result(report.to_json(), report.human())


# --- start ----------------------------------------------------------------------------------------------


@dataclass
class StartOptions:
    """What one `start` call asks for.

    Attributes:
        timeout (int | None): Seconds to wait for healthy; remembered in host settings when given.
        inputs (StartInputs): Ask-at-start secret sources and one-run overrides.
    """

    timeout: int | None = None
    inputs: StartInputs = field(default_factory=StartInputs)


@dataclass
class StartRun:
    """The state one start shares between its steps.

    Attributes:
        env (Environment): The environment being started.
        ports (Ports): The ports.
        out (Output): Where progress goes.
        options (StartOptions): The command line's options.
        timeout (int): Seconds to wait for healthy, settled by a step.
        spec (ComposeSpec | None): The render input, built by a step and adjustable by later ones.
        pinned (bool): This start pinned the image.
        warnings (list[str]): Warnings for the result.
        report (StatusReport | None): The status once healthy.
        terminal (Terminal | None): Where to prompt; None when prompting is off (--no-input, --json, no terminal).
        passed (dict[str, str]): Server settings given for this run only; they reach the container through
            docker's process environment, never a file.
        since (float | None): When compose up ran (seconds since the epoch): a container compose keeps holds
            the log of earlier starts, which never names this start's cause.
    """

    env: Environment
    ports: Ports
    out: Output
    options: StartOptions
    timeout: int = DEFAULT_START_TIMEOUT
    spec: ComposeSpec | None = None
    pinned: bool = False
    warnings: list[str] = field(default_factory=list)
    report: StatusReport | None = None
    terminal: Terminal | None = None
    passed: dict[str, str] = field(default_factory=dict)
    since: float | None = None

    @property
    def container(self) -> str:
        """The container name."""
        return container_name(self.env)

    def require_spec(self) -> ComposeSpec:
        """Return the render input, which the build step must have made.

        Returns:
            ComposeSpec: The render input.

        Raises:
            RuntimeError: A step that needs the render input runs before it is built.
        """
        if self.spec is None:
            raise RuntimeError("the start pipeline used the render input before building it")
        return self.spec


StartStep = Callable[[StartRun], None]


def default_start_timeout(env: Environment) -> int:
    """Return the start timeout when neither `--timeout` nor host settings give one.

    Args:
        env (Environment): The environment.

    Returns:
        int: Seconds; longer with OAuth or Vault on.
    """
    return identity.default_start_timeout(env.server, DEFAULT_START_TIMEOUT)


def require_complete(run: StartRun) -> None:
    """Start step: refuse an environment without its required host settings or whose CDD file is gone."""
    check_complete(run.env, run.options.inputs)


def check_complete(env: Environment, inputs: StartInputs) -> None:
    """Refuse an environment without its required host settings or whose CDD file is gone.

    Args:
        env (Environment): The environment.
        inputs (StartInputs): The run's inputs (a `--cdd` override counts).

    Raises:
        CliError: A required host setting is missing, or the CDD file does not exist; the next command names
            each value to fill in as `<flag name>`. A missing `--cdd` override names `start` again, never
            `configure`, which would change the stored environment.
    """
    if missing := env.missing_host():
        raise CliError(
            ExitCode.INPUT_REQUIRED,
            f"Environment {env.name!r} is missing {', '.join(missing)}.",
            f"{CLI_NAME} configure {env.name} " + " ".join(f"{flag} <{flag.lstrip('-')}>" for flag in missing),
        )
    cdd = start_inputs.cdd_file(env, inputs) or ""
    if inputs.cdd is not None and not Path(cdd).is_file():
        # A one-run override: the stored environment is fine, so the fix is the path given to this start.
        raise CliError(
            ExitCode.START_OVERRIDE_INVALID,
            f"--cdd {shell_argument(str(inputs.cdd))} (for this run only) names no file: {cdd} does not exist. The environment's "
            "stored CDD file is unchanged; give the path of an existing .cdd file.",
            f"{CLI_NAME} start {env.name} --cdd <path of the .cdd file>",
        )
    if not Path(cdd).is_file():
        raise CliError(
            ExitCode.CDD_NOT_FOUND,
            f"The CDD file {cdd} of environment {env.name!r} does not exist.",
            f"{CLI_NAME} configure {env.name} --cdd <path of the .cdd file>",
        )


# The files the server reads through a mount, by mount target, as a message names them.
_READ_FILES = {compose.CDD_TARGET: "CDD file", identity.VAULT_CA_TARGET: "Vault CA file"}


def refuse_unreadable_mounts(run: StartRun) -> None:
    """Start step: refuse a mounted CDD or Vault CA file other users cannot read, before compose up.

    The server runs as a user of the image, not the administrator, so a file only its owner or group can read
    fails inside the container (the CDD after the whole wait). Only Linux hosts are checked: Docker on Windows
    and macOS reaches the file through its VM's file sharing (under Lima's reverse-sshfs as the host user), so
    the mode bits do not say what the container can read; the server's own log line still names the cause there.

    Args:
        run (StartRun): The start in progress; its render input is built and overridden.

    Raises:
        CliError: One or more mounted files lack read permission for others; names each and the chmod.
    """
    if sys.platform != "linux":
        return
    mounted = {m.target: m.source for m in run.require_spec().mounts if m.target in _READ_FILES}
    modes = {target: Path(source).stat().st_mode & 0o777 for target, source in mounted.items()}
    unreadable = [(_READ_FILES[t], mounted[t], mode) for t, mode in modes.items() if not mode & 0o004]
    if not unreadable:
        return
    named = "\n".join(f"  {what} {path} (mode {mode:04o})" for what, path, mode in unreadable)
    paths = " ".join(shell_argument(path) for _, path, _ in unreadable)
    raise CliError(
        ExitCode.MOUNTED_FILE_UNREADABLE,
        f"The server of environment {run.env.name!r} runs as another user and cannot read:\n{named}\n"
        f"Let other users read it: chmod 644 {paths}",
        f"chmod 644 {paths}",
    )


def _pin_image(run: StartRun) -> None:
    """Pin the image digest on the first start."""
    run.pinned = image.pin_if_unpinned(run.ports.docker, run.env)


def _settle_timeout(run: StartRun) -> None:
    """Take `--timeout`, remembering it, else the stored timeout, else the default."""
    host = run.env.host
    if run.options.timeout is not None:
        host.start_timeout = run.options.timeout
    run.timeout = host.start_timeout or default_start_timeout(run.env)


def _save_host_settings(run: StartRun) -> None:
    """Keep the pinned digest and the timeout, even if the start fails later."""
    environment.save_host(run.env)


def _build_spec(run: StartRun) -> None:
    """Build the render input from the environment."""
    run.spec = compose.spec_for(run.env)


def _render(run: StartRun) -> None:
    """Write the compose file."""
    compose.write(run.env, run.require_spec())


def _compose_up(run: StartRun) -> None:
    """Create or update the container from the compose file; never pull, the digest is pinned."""
    run.out.progress(f"Starting {run.container} ...")
    run.since = run.ports.clock.time()
    args = [*compose.compose_args(run.env), "up", "--detach", "--pull", "never", "--no-build"]
    compose_command(run.ports, run.out, args, run.env, compose.process_environment(run.require_spec()))


def compose_command(
    ports: Ports, out: Output, args: list[str], env: Environment, variables: dict[str, str] | None = None
) -> None:
    """Run a compose command on the terminal, so its progress shows; in JSON mode capture it instead.

    Args:
        ports (Ports): The ports.
        out (Output): Where the result goes; its mode decides whether compose's output is shown or captured.
        args (list[str]): Arguments after `docker`, starting with `compose`.
        env (Environment): The environment.
        variables (dict[str, str] | None): Variables for docker's process environment only.

    Raises:
        CliError: The command failed; compose's own message is above it, or inside it in JSON mode.
    """
    if out.json_mode:
        result = ports.docker.run(args, env=variables)
        code, said = result.returncode, f": {result.stderr.strip()}"
    else:
        code = ports.docker.stream(args, env=variables)
        said = f" with exit code {code}; its output is above"
    if code != 0:
        what = next((a for a in args[1:] if a in ("up", "stop", "down")), "")
        raise CliError(
            ExitCode.DOCKER_FAILED,
            f"docker compose {what} for {container_name(env)} failed{said}",
            f"{CLI_NAME} status {env.name}",
        )


def _wait_until_healthy(run: StartRun) -> None:
    """Wait for healthy; stop at once when the container exits; on timeout leave it running."""
    clock = run.ports.clock
    run.out.progress(f"Waiting up to {run.timeout} s for {run.container} to become healthy ...")
    deadline = clock.monotonic() + run.timeout
    first_restarts: int | None = None
    while True:
        state = container_state(run.ports, run.container)
        if state is None:
            raise _exited(run, None)
        first_restarts = state.restarts if first_restarts is None else first_restarts
        if state.status in _ENDED or state.restarts > first_restarts:
            raise _exited(run, state)
        if state.health == "healthy":
            return
        if state.health is None and state.status == "running":
            if run.ports.probe.health(f"{base_url(run.env)}/health").status == 200:
                return
        if cause := _server_named_cause(run, "is left running"):
            raise cause
        now = clock.monotonic()
        if now >= deadline:
            raise _timed_out(run, state)
        clock.sleep(min(POLL_SECONDS, deadline - now))


# The server's own log line for a mounted file it cannot use: exit code, mount target, verb, words in the line.
_SERVER_CAUSES = (
    (ExitCode.SERVER_CANNOT_OPEN_CDD, compose.CDD_TARGET, "open", ("Unable to open CDD",)),
    (ExitCode.SERVER_CANNOT_USE_VAULT_CA, identity.VAULT_CA_TARGET, "use", ("VAULT_CACERT", "cannot be used")),
    # The wording a live run logged for a CA file the server's user could not read.
    (ExitCode.SERVER_CANNOT_USE_VAULT_CA, identity.VAULT_CA_TARGET, "use", ("VAULT_CACERT", "Permission denied")),
)


def _server_named_cause(run: StartRun, how: str) -> CliError | None:
    """Return the failure a line logged since compose up names: a CDD it cannot open or a Vault CA it cannot use.

    Args:
        run (StartRun): The start in progress, after compose up.
        how (str): What became of the container: `is left running`, `exited with code 1 while starting`.

    Returns:
        CliError | None: The failure naming the host file and the fix; None when no line names one.
    """
    mounted = {mount.target: mount.source for mount in run.require_spec().mounts}
    lines = log_tail(run.ports, run.container, since=run.since).splitlines()
    for code, target, verb, words in _SERVER_CAUSES:
        line = next((line for line in lines if all(word in line for word in words)), None)
        if line is None or target not in mounted:
            continue
        host_file = mounted[target]
        # Windows hosts share files with other semantics: chmod there changes nothing the container sees.
        can_read = (
            "Docker can share it with the container"
            if sys.platform == "win32"
            else f"the server's user can read it: chmod 644 {shell_argument(host_file)}"
        )
        return CliError(
            code,
            f"{run.container} {how}: the server cannot {verb} the {_READ_FILES[target]} {host_file}. Check that the "
            f"file is the right one and that {can_read}\n"
            f"The server said: {line.strip()}",
            f"{CLI_NAME} log {run.env.name} --tail 200",
        )
    return None


def _exited(run: StartRun, state: ContainerState | None) -> CliError:
    """Return the failure of a container that exited during the wait; a cause its log names comes first."""
    how = "disappeared" if state is None else f"exited with code {state.exit_code}"
    if cause := _server_named_cause(run, f"{how} while starting"):
        return cause
    return CliError(
        ExitCode.CONTAINER_EXITED,
        f"{run.container} {how} while starting.\nLast log lines:\n{log_tail(run.ports, run.container)}",
        f"{CLI_NAME} log {run.env.name} --tail 200",
    )


def _timed_out(run: StartRun, state: ContainerState) -> CliError:
    """Return the failure of a start that was not healthy in time; the container stays up."""
    url = f"{base_url(run.env)}/health"
    reply = run.ports.probe.health(url)
    answer = f"{reply.status} {reply.body.strip()}" if reply.status is not None else f"no answer ({reply.error})"
    return CliError(
        ExitCode.START_TIMEOUT,
        f"{run.container} is not healthy after {run.timeout} s (Docker health: {state.health or 'none'}); "
        "it is left running, so a slow dependency can still come up. Wait longer with --timeout.\n"
        f"{url} answered: {answer}\n"
        f"Last log lines:\n{log_tail(run.ports, run.container)}",
        f"{CLI_NAME} status {run.env.name}",
    )


def _report(run: StartRun) -> None:
    """Take the status of the healthy server for the result."""
    run.report = collect_status(run.env, run.ports)
    run.warnings.extend(n for n in run.report.notices if n not in run.warnings)


START_STEPS: list[StartStep] = [
    require_complete,
    image.check_reference,
    image.notice_ignored_variables,
    bind.refuse_self_signed,
    start_inputs.check_overrides,
    start_inputs.warn_widened_permissions,
    start_inputs.supply_values,
    _pin_image,
    image.check_start_version,
    image.notice_variables_moved,
    _settle_timeout,
    _save_host_settings,
    _build_spec,
    identity.adjust_spec,
    start_inputs.apply_overrides,
    refuse_unreadable_mounts,
    port_check.refuse_taken_port,  # before _render: a refused start keeps compose.yml, so drift survives
    _render,
    _compose_up,
    start_inputs.record_overrides,
    tls.hint_expired_generated(_wait_until_healthy),
    _report,
]


def run_start(
    env: Environment, ports: Ports, out: Output, options: StartOptions, terminal: Terminal | None = None
) -> StartRun:
    """Start an environment: every START_STEPS entry in order.

    Args:
        env (Environment): A configured environment.
        ports (Ports): The ports.
        out (Output): Where progress goes.
        options (StartOptions): The command line's options.
        terminal (Terminal | None): Where to ask for ask-at-start secrets; None when prompting is off.

    Returns:
        StartRun: The finished run.
    """
    run = StartRun(env=env, ports=ports, out=out, options=options, terminal=terminal)
    for step in START_STEPS:
        step(run)
    return run


def _start_result(run: StartRun) -> tuple[dict[str, Any], str]:
    """Return a finished start's result and its text."""
    report = run.report or collect_status(run.env, run.ports)
    url = base_url(run.env) + run.env.server.get("MCP_PATH", "/mcp")
    data = {
        "environment": run.env.name,
        "container": run.container,
        "image": run.env.host.image_digest,
        "image_version": run.env.host.image_version,
        "pinned": run.pinned,
        "url": url,
        "timeout": run.timeout,
        "warnings": run.warnings,
        "status": report.to_json(),
    }
    lines = [f"{run.container} is healthy and serving MCP at {url}"]
    if run.pinned:
        lines.append(f"Pinned image {run.env.host.image_digest} (version {run.env.host.image_version or 'unknown'}).")
    lines += [f"Warning: {w}" for w in run.warnings]
    return data, "\n".join(lines)


def start(
    ports: Ports, out: Output, given_name: str | None, options: StartOptions, terminal: Terminal | None = None
) -> None:
    """Run `start`.

    Args:
        ports (Ports): The ports.
        out (Output): Where the result goes.
        given_name (str | None): The environment name on the command line.
        options (StartOptions): The command line's options.
        terminal (Terminal | None): Where to ask for ask-at-start secrets; None when prompting is off.
    """
    run = run_start(load_configured(given_name, "start"), ports, out, options, terminal)
    out.result(*_start_result(run))


# --- stop and restart -----------------------------------------------------------------------------------


def stop_environment(env: Environment, ports: Ports, out: Output, timeout: int = DEFAULT_STOP_TIMEOUT) -> bool:
    """Stop an environment's container gracefully; compose kills it after the timeout. A failure raises CliError.

    Args:
        env (Environment): The environment.
        ports (Ports): The ports.
        out (Output): Where progress goes.
        timeout (int): Seconds between the stop signal and the kill.

    Returns:
        bool: False when the environment was never started (there is nothing to stop).
    """
    if not (env.directory / compose.COMPOSE_FILE).is_file():
        return False
    out.progress(f"Stopping {container_name(env)} ...")
    compose_command(ports, out, [*compose.compose_args(env), "stop", "--timeout", str(timeout)], env)
    return True


def stop(ports: Ports, out: Output, given_name: str | None, timeout: int) -> None:
    """Run `stop`.

    Args:
        ports (Ports): The ports.
        out (Output): Where the result goes.
        given_name (str | None): The environment name on the command line.
        timeout (int): Seconds between the stop signal and the kill.
    """
    env = load_configured(given_name, "stop")
    stopped = stop_environment(env, ports, out, timeout)
    text = f"Stopped {container_name(env)}." if stopped else f"Environment {env.name!r} was never started."
    out.result({"environment": env.name, "container": container_name(env), "stopped": stopped}, text)


def restart(
    ports: Ports,
    out: Output,
    given_name: str | None,
    if_cert_changed: bool = False,
    options: StartOptions | None = None,
    terminal: Terminal | None = None,
) -> None:
    """Run `restart`: stop, then start with the stored settings and this call's options.

    Every ask-at-start secret is read and checked before the stop, so a missing, refused or cancelled one
    leaves the server running.

    Args:
        ports (Ports): The ports.
        out (Output): Where the result goes.
        given_name (str | None): The environment name on the command line.
        if_cert_changed (bool): Restart only when the certificate on disk differs from the served one.
        options (StartOptions | None): The start options (ask-at-start secret sources); defaults when None.
        terminal (Terminal | None): Where to ask for ask-at-start secrets; None when prompting is off.
    """
    env = load_configured(given_name, "restart")
    if if_cert_changed and not (change := tls.certificate_change(env, ports)).differs:
        out.result(*tls.restart_skipped(env, change))
        return
    data, text = _start_result(restart_environment(env, ports, out, options or StartOptions(), terminal))
    out.result({**data, "restarted": True}, text)


def restart_environment(
    env: Environment, ports: Ports, out: Output, options: StartOptions, terminal: Terminal | None
) -> StartRun:
    """Restart a running environment: read its values first, so a refused one stops nothing, then stop and start.

    Values a caller read ahead (`options.inputs.supplied`) are used as they are.

    Args:
        env (Environment): The environment.
        ports (Ports): The ports.
        out (Output): Where progress goes.
        options (StartOptions): The start options (ask-at-start secret sources).
        terminal (Terminal | None): Where to ask for ask-at-start secrets; None when prompting is off.

    Returns:
        StartRun: The finished start.
    """
    if options.inputs.supplied is None:
        start_inputs.read_ahead(env, options.inputs, terminal, "restart")
    stop_environment(env, ports, out)
    return run_start(env, ports, out, options, terminal)


# --- log ------------------------------------------------------------------------------------------------


@dataclass
class LogOptions:
    """What one `log` call asks for.

    Attributes:
        follow (bool): Keep printing new lines.
        tail (str | None): Lines from the end, or `all`.
        since (str | None): A timestamp or relative time such as `10m`.
    """

    follow: bool = False
    tail: str | None = None
    since: str | None = None


def log(ports: Ports, out: Output, given_name: str | None, options: LogOptions) -> None:
    """Run `log`: the server container's log, as Docker keeps it.

    Args:
        ports (Ports): The ports.
        out (Output): Where the result goes.
        given_name (str | None): The environment name on the command line.
        options (LogOptions): Follow, tail and since.

    Raises:
        CliError: Follow with JSON, no container, or docker logs failed.
    """
    env = load_configured(given_name, "log")
    if options.follow and out.json_mode:
        raise CliError(ExitCode.USAGE, "--follow shows lines as they arrive; do not use it with --json.")
    container = container_name(env)
    if container_state(ports, container) is None:
        raise CliError(
            ExitCode.CONTAINER_NOT_FOUND,
            f"Environment {env.name!r} has no container yet, so there is no log.",
            f"{CLI_NAME} start {env.name}",
        )
    args = ["logs", *(["--follow"] if options.follow else [])]
    args += [
        *(["--tail", options.tail] if options.tail else []),
        *(["--since", options.since] if options.since else []),
    ]
    args.append(container)
    if out.json_mode:
        # Without --follow the log ends; a tail is bounded and must not wait on a hung daemon.
        result = query(ports.docker, args) if options.tail else ports.docker.run(args)
        if result.returncode != 0:
            raise CliError(ExitCode.DOCKER_FAILED, f"docker logs {container} failed: {result.stderr.strip()}")
        out.result(
            {"environment": env.name, "container": container, "stdout": result.stdout, "stderr": result.stderr}, ""
        )
        return
    code = ports.docker.stream(args)
    if code not in (0, 130):
        raise CliError(ExitCode.DOCKER_FAILED, f"docker logs {container} failed with exit code {code}.")
