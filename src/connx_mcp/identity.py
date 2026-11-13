#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""identity.py - What OAuth sign-in and the Credential Vault change when an environment starts.

Description:
    With an OAuth provider or HashiCorp Vault on, the server dials its identity provider and Vault
    at boot, so `start` waits longer by default (420 s, not 180 s) and the compose file gives the
    health check a 300 s start period. A Vault CA file given on this machine (`--vault-ca`) is
    mounted read-only at a fixed path inside the container, and VAULT_CACERT is pointed there, so
    the administrator never handles a container path. The settings rows and cross-field rules of
    both groups live in the settings schema; this module holds only the start-time part.

Copyright:
    Copyright 2026 Software GmbH
    Licensed under the Apache License, Version 2.0 (the "License"); you may not use this file except in compliance
    with the License. You may obtain a copy of the License at https://www.apache.org/licenses/LICENSE-2.0
    Unless required by applicable law or agreed to in writing, software distributed under the License is distributed
    on an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the License
    for the specific language governing permissions and limitations under the License.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING

from connx_mcp import CLI_NAME, settings
from connx_mcp.compose import Mount
from connx_mcp.demo import stack
from connx_mcp.output import CliError, ExitCode

if TYPE_CHECKING:
    from connx_mcp.lifecycle import StartRun

SLOW_START_TIMEOUT = 420
HEALTH_START_PERIOD = 300
VAULT_CA_TARGET = "/opt/softwareag/connx/mcp/vault-ca.pem"


def slow_start(values: Mapping[str, str]) -> bool:
    """Return whether the server dials an identity provider or Vault at boot.

    Args:
        values (Mapping[str, str]): Server settings.

    Returns:
        bool: True when OAuth or Vault is on.
    """
    return settings.oauth_on(values) or settings.vault_on(values)


def default_start_timeout(values: Mapping[str, str], otherwise: int) -> int:
    """Return the start timeout when neither `--timeout` nor host settings give one.

    Args:
        values (Mapping[str, str]): Server settings.
        otherwise (int): The timeout without OAuth or Vault.

    Returns:
        int: 420 s with OAuth or Vault on, else `otherwise`.
    """
    return SLOW_START_TIMEOUT if slow_start(values) else otherwise


def adjust_spec(run: StartRun) -> None:
    """Start step: give the health check its start period and mount the Vault CA where VAULT_CACERT points.

    Args:
        run (StartRun): The start in progress; its render input is built.

    Raises:
        CliError: The Vault CA file is gone from this machine.
    """
    env, spec = run.env, run.require_spec()
    if slow_start(env.server):
        spec.health_start_period = HEALTH_START_PERIOD
    ca_file = env.host.vault_ca_file
    if not (ca_file and settings.vault_on(env.server)):
        return
    if not Path(ca_file).is_file():
        if Path(ca_file) == stack.vault_ca_path():
            reason, fix = "the demo stack creates it when it starts with Vault", f"{CLI_NAME} start-demo"
        else:
            reason, fix = "give the file again", f"{CLI_NAME} configure {env.name} --vault-ca <path of the CA file>"
        raise CliError(
            ExitCode.VAULT_CA_NOT_FOUND,
            f"The Vault CA file {ca_file} of environment {env.name!r} does not exist: {reason}.",
            fix,
        )
    spec.mounts.append(Mount(source=ca_file, target=VAULT_CA_TARGET, read_only=True))
    spec.environment["VAULT_CACERT"] = VAULT_CA_TARGET
