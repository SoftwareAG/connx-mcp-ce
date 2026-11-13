#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""exposure.py - The warning for an environment that the network reaches without OAuth sign-in.

Description:
    compose publishes the server port on every network interface (`<port>:8000`, no host address) unless
    the environment listens on this machine only (`--listen localhost`, `127.0.0.1:<port>:8000`): callers on
    other machines are the point. With OAuth `none`, anyone who reaches the port runs SQL as the stored CDD
    login. `configure`, `status`, the status list and `guide` say so as a warning, never guide's next step,
    until the administrator accepts the open network (`--accept-open-network`). `status` reports the facts.

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

from connx_mcp import CLI_NAME, compose, settings
from connx_mcp.environment import Environment
from connx_mcp.ports import Ports

if TYPE_CHECKING:
    from connx_mcp.lifecycle import StatusReport


def open_to_the_network(env: Environment) -> bool:
    """Return whether the network reaches the environment without OAuth sign-in and nobody accepted that.

    Args:
        env (Environment): The environment.

    Returns:
        bool: False with an OAuth provider, a port published on this machine only, or the risk accepted.
    """
    return not (
        settings.oauth_on(env.server)
        or env.host.listen_address == settings.LISTEN_LOCALHOST
        or env.host.open_network_accepted
    )


def warning(env: Environment) -> str | None:
    """Return the warning for an environment without OAuth, whose published port the network reaches.

    Args:
        env (Environment): The environment.

    Returns:
        str | None: The warning; None when callers sign in with OAuth, the port is published on this machine
            only, or the administrator accepted the open network.
    """
    if not open_to_the_network(env):
        return None
    return (
        f"Anyone who can reach this computer on port {compose.published_port(env)} can query your data through "
        f"environment {env.name!r}.\n"
        "  Callers do not sign in, so every query runs as the CDD login stored in this environment.\n"
        "  To enforce sign-in to the CONNX MCP Server, choose an OAuth provider using: "
        f"{CLI_NAME} configure {env.name}\n"
        "  Otherwise, use this environment only on a network you trust."
    )


def status_check(env: Environment, ports: Ports, report: StatusReport) -> None:
    """Fill a status report's network facts and add the exposure warning to its notices.

    Args:
        env (Environment): The environment.
        ports (Ports): The ports (unused: the warning comes from the settings alone).
        report (StatusReport): The report to add to.
    """
    report.network = {
        "listen": env.host.listen_address or "all",
        "oauth": settings.oauth_on(env.server),
        "open_network_accepted": bool(env.host.open_network_accepted),
    }
    if (notice := warning(env)) is not None:
        report.notices.append(notice)
