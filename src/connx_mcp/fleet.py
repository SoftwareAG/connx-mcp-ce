#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""fleet.py - Every environment on this machine at once: the status list and `stop --all`.

Description:
    `status` with no name (and no CONNX_MCP_ENVIRONMENT), or with `--all`, lists every environment
    with its state, published port, pinned version, health and drift, built from the same
    StatusReport as `status <env>`, and names the environment the demo stack is bound to. An environment
    whose stored image names no registry is listed as refused with its repair; the rest still report. `stop`
    acts on all environments only with `--all`; without a name it stops the selected one
    (CONNX_MCP_ENVIRONMENT, else `default`), never all of them.

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
from typing import Any

from connx_mcp import CLI_NAME, compose, environment, image, lifecycle
from connx_mcp.demo import bind
from connx_mcp.environment import Environment
from connx_mcp.lifecycle import StatusReport
from connx_mcp.output import CliError, ExitCode, Output
from connx_mcp.ports import Ports

UNREADABLE = "unreadable"
# The state of an environment whose stored image names no registry: only that environment is refused.
REFUSED = "refused"
COLUMNS = ("ENVIRONMENT", "STATE", "PORT", "VERSION", "HEALTH", "DRIFT")
_ALL_WITH_NAME = "`{command} --all` acts on every environment; give either --all or a name, not both."


def _row(env: Environment, report: StatusReport) -> dict[str, Any]:
    """Return one environment's line of the list as a JSON object; version and digest come from the image check."""
    facts = report.image
    return {
        "environment": env.name,
        "container": report.container,
        "state": report.state,
        "port": compose.published_port(env),
        "version": facts.version if facts else None,
        "image": facts.digest if facts else None,
        "docker_health": report.docker_health,
        "serving": report.serving,
        "drift": report.drift,
        "demo_bound": report.demo_bound_environment == env.name,
        "demo_stack_host": report.demo_stack["stack_host"] if report.demo_stack else None,
        "notices": report.notices,
    }


def _unreported(name: str, error: CliError, state: str = UNREADABLE) -> dict[str, Any]:
    """Return the line of an environment that is not reported (unreadable or refused), with what to do about it."""
    return {
        "environment": name,
        "container": compose.project_name(name),
        "state": state,
        "port": None,
        "version": None,
        "image": None,
        "docker_health": None,
        "serving": False,
        "drift": [],
        "notices": [],
        "error": error.to_json(),
    }


def _health(row: dict[str, Any]) -> str:
    """Return the health column: serving, else Docker's health, else `-`."""
    if row["serving"]:
        return "serving"
    return f"not serving ({row['docker_health']})" if row["docker_health"] else "-"


def _table(rows: list[dict[str, Any]]) -> str:
    """Render the list as aligned columns."""
    cells = [list(COLUMNS)] + [
        [
            r["environment"],
            r["state"],
            str(r["port"] or "-"),
            r["version"] or "-",
            _health(r),
            "; ".join(r["drift"]) or "-",
        ]
        for r in rows
    ]
    widths = [max(len(line[i]) for line in cells) for i in range(len(COLUMNS) - 1)]
    table = [
        "  ".join([*(cell.ljust(w) for cell, w in zip(line[:-1], widths, strict=True)), line[-1]]) for line in cells
    ]
    for r in rows:
        if error := r.get("error"):
            table += ["", error["message"], f"Next: {error['next']}"]
    table += [f"Warning: {r['environment']}: {n}" for r in rows for n in r["notices"]]
    return "\n".join(table)


def status(ports: Ports, out: Output, given_name: str | None, every: bool) -> None:
    """Run `status`: one environment in detail, or the list of every environment.

    Args:
        ports (Ports): The ports.
        out (Output): Where the result goes.
        given_name (str | None): The environment name on the command line.
        every (bool): `--all`: list every environment even when CONNX_MCP_ENVIRONMENT selects one.

    Raises:
        CliError: `--all` with a name.
    """
    if every and given_name is not None:
        raise CliError(ExitCode.USAGE, _ALL_WITH_NAME.format(command="status"), f"{CLI_NAME} status {given_name}")
    if not every and (given_name is not None or os.environ.get(environment.ENVIRONMENT_VARIABLE)):
        lifecycle.status(ports, out, given_name)
        return
    base = environment.base_directory()
    rows: list[dict[str, Any]] = []
    for name in environment.names():
        try:
            env = environment.load(name)
        except CliError as error:
            rows.append(_unreported(name, error))
            continue
        if (refusal := image.refused_reference(env)) is not None:
            rows.append(_unreported(name, refusal, REFUSED))
            continue
        rows.append(_row(env, lifecycle.collect_status(env, ports)))
    text = _table(rows) if rows else f"No environments in {base}.\nNext: {CLI_NAME} configure"
    bound, notice = bind.status_binding()
    if bound is not None:
        text += f"\nDemo stack: bound to {bound!r}."
    data: dict[str, Any] = {"base_directory": str(base), "environments": rows, "demo_bound_environment": bound}
    if notice:
        text += f"\nWarning: {notice}"
        data["demo_notice"] = notice
    out.result(data, text)


def stop(ports: Ports, out: Output, given_name: str | None, timeout: int, every: bool) -> None:
    """Run `stop`: the named or selected environment, or with `--all` every environment.

    Args:
        ports (Ports): The ports.
        out (Output): Where the result goes.
        given_name (str | None): The environment name on the command line.
        timeout (int): Seconds between the stop signal and the kill.
        every (bool): `--all`: stop every environment.

    Raises:
        CliError: `--all` with a name, or an environment failed to stop (the others are still stopped).
    """
    if not every:
        lifecycle.stop(ports, out, given_name, timeout)
        return
    if given_name is not None:
        raise CliError(ExitCode.USAGE, _ALL_WITH_NAME.format(command="stop"), f"{CLI_NAME} stop {given_name}")
    stopped: list[str] = []
    never_started: list[str] = []
    failures: list[str] = []
    for name in environment.names():
        try:
            if lifecycle.stop_environment(environment.load(name), ports, out, timeout):
                stopped.append(name)
            else:
                never_started.append(name)
        except CliError as error:
            failures.append(f"{name}: {error.message}")
    if failures:
        raise CliError(
            ExitCode.DOCKER_FAILED,
            "Some environments did not stop:\n" + "\n".join(failures) + f"\nStopped: {', '.join(stopped) or 'none'}.",
            f"{CLI_NAME} status",
        )
    text = f"Stopped {', '.join(stopped)}." if stopped else "No environment has a container to stop."
    out.result({"stopped": stopped, "never_started": never_started}, text)
