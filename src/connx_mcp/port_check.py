#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""port_check.py - The start step that refuses a port another running environment holds.

Description:
    Several environments run side by side only on different published ports. Before the compose
    file is rendered (so a refused start leaves it and its drift untouched) and `compose up`,
    `start` asks Docker which environment containers run and on which host ports, and refuses a
    port another running environment publishes, naming it and suggesting a port no environment
    uses. A port held by a stopped environment is free to take. Docker is asked only when another
    environment has been started at least once; a port held by something that is not an
    environment still fails at `compose up` with Docker's own message.

Copyright:
    Copyright 2026 Software GmbH
    Licensed under the Apache License, Version 2.0 (the "License"); you may not use this file except in compliance
    with the License. You may obtain a copy of the License at https://www.apache.org/licenses/LICENSE-2.0
    Unless required by applicable law or agreed to in writing, software distributed under the License is distributed
    on an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the License
    for the specific language governing permissions and limitations under the License.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from connx_mcp import CLI_NAME, compose, environment
from connx_mcp.docker import DockerPort, query
from connx_mcp.output import CliError, ExitCode

if TYPE_CHECKING:
    from connx_mcp.lifecycle import StartRun

PS_FORMAT = "{{.Names}}\t{{.Ports}}"
_HOST_PORT = re.compile(r":(\d+)->")


def running_ports(docker: DockerPort) -> dict[str, set[int]]:
    """Return the host ports each running environment container publishes.

    Args:
        docker (DockerPort): The Docker port.

    Returns:
        dict[str, set[int]]: Container name to its published host ports.

    Raises:
        CliError: docker ps failed.
    """
    result = query(docker, ["ps", "--filter", f"name=^{compose.project_name('')}", "--format", PS_FORMAT])
    if result.returncode != 0:
        raise CliError(ExitCode.DOCKER_FAILED, f"docker ps failed: {result.stderr.strip()}")
    held: dict[str, set[int]] = {}
    for line in result.stdout.splitlines():
        container, _, published = line.partition("\t")
        held[container.strip()] = {int(port) for port in _HOST_PORT.findall(published)}
    return held


def refuse_taken_port(run: StartRun) -> None:
    """Refuse to start on a port another running environment publishes.

    Args:
        run (StartRun): The start in progress.

    Raises:
        CliError: Another running environment holds the port (exit 90).
    """
    others = [name for name in environment.names() if name != run.env.name]
    base = environment.base_directory()
    if not any((base / name / compose.COMPOSE_FILE).is_file() for name in others):
        return
    port = run.require_spec().published_port
    held = running_ports(run.ports.docker)
    # Holders are found by container name, so an environment whose settings cannot be read still holds its port.
    holders = [name for name in others if port in held.get(compose.project_name(name), set())]
    if not holders:
        return
    in_use = {p for ports in held.values() for p in ports} | set(_configured_ports(others))
    free = next((p for p in range(compose.DEFAULT_PUBLISHED_PORT + 1, 65536) if p not in in_use), None)
    raise CliError(
        ExitCode.PORT_IN_USE,
        f"Port {port} is held by the running environment {holders[0]!r}; environment {run.env.name!r} cannot "
        f"publish on it. Give {run.env.name!r} its own port, or stop {holders[0]!r} first.",
        f"{CLI_NAME} configure {run.env.name} --port {free or '<free port>'}",
    )


def _configured_ports(names: list[str]) -> list[int]:
    """Return the ports the named environments are configured to publish, skipping any that cannot be read."""
    ports: list[int] = []
    for name in names:
        try:
            ports.append(compose.published_port(environment.load(name)))
        except CliError:
            continue
    return ports
