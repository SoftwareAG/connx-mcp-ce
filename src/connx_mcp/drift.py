#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""drift.py - The status check that tells when a running container differs from its stored environment.

Description:
    `start` renders the compose file from the environment's settings, so a server settings or host
    settings file written after the compose file holds a change the running container has not
    picked up: a restart applies it. A container started with one-run overrides (`start --cdd`,
    `--certs`, `--cdd-username`, or a stored secret replaced by `--secret-env`/`--secret-stdin`, named
    by key only; recorded in started-with.json) differs from the stored environment
    until a start without them. `since_start` fills StatusReport.drift with one line per difference;
    `status` and the fleet list show the same lines.

Copyright:
    Copyright 2026 Software GmbH
    Licensed under the Apache License, Version 2.0 (the "License"); you may not use this file except in compliance
    with the License. You may obtain a copy of the License at https://www.apache.org/licenses/LICENSE-2.0
    Unless required by applicable law or agreed to in writing, software distributed under the License is distributed
    on an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the License
    for the specific language governing permissions and limitations under the License.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from connx_mcp import compose, environment, start_inputs
from connx_mcp.docker import CONTAINER_UP
from connx_mcp.environment import Environment
from connx_mcp.ports import Ports

if TYPE_CHECKING:
    from connx_mcp.lifecycle import StatusReport

SETTINGS_CHANGED = "the settings changed after the last start; restart to apply them"


def since_start(env: Environment, ports: Ports, report: StatusReport) -> None:
    """Fill the report's drift: settings changed after the last start, and one-run overrides in use.

    Args:
        env (Environment): The environment.
        ports (Ports): The ports (unused: the facts are files).
        report (StatusReport): The report being filled.
    """
    _settings_changed(env, report)
    _overrides_in_use(env, report)


def _settings_changed(env: Environment, report: StatusReport) -> None:
    """Note drift when a settings file is newer than the compose file the running container came from."""
    rendered = env.directory / compose.COMPOSE_FILE
    if report.state not in CONTAINER_UP or not rendered.is_file():
        return
    started = rendered.stat().st_mtime
    settings = [env.directory / environment.SERVER_FILE, env.directory / environment.HOST_FILE]
    if any(path.is_file() and path.stat().st_mtime > started for path in settings):
        report.drift.append(SETTINGS_CHANGED)


def _overrides_in_use(env: Environment, report: StatusReport) -> None:
    """Note drift when the container (running or stopped) was started with one-run overrides."""
    if report.state == "not created" or not (overrides := start_inputs.started_with(env)):
        return
    report.drift.append(
        f"started with one-run overrides ({', '.join(overrides)}); start without them to apply the stored settings"
    )
