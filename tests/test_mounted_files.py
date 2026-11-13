#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_mounted_files.py - `start` names a CDD or Vault CA file the server cannot read, instead of timing out.

Description:
    Through the CLI seam with the fake Docker port, endpoint probe and clock: on Linux hosts `start`
    refuses, before compose up, a mounted CDD or Vault CA file that other users cannot read, naming
    the path and `chmod 644 <path>`; Windows and macOS hosts skip that check. While waiting for
    health, `start` recognises the server's own log line for a CDD it cannot open or a Vault CA it
    cannot use and fails at once with that cause, instead of `start_timeout` or a bare
    `container_exited`.

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
import sys
from pathlib import Path

import pytest

from connx_mcp.docker import DockerResult
from tests.conftest import Cli, Machine
from tests.fakes import EPOCH
from tests.test_lifecycle import comes_up, compose, configure, image_found, serving, state
from tests.test_oauth_vault import KEYCLOAK, VAULT, sets

POSIX_ONLY = pytest.mark.skipif(os.name == "nt", reason="POSIX file modes and the chmod hint")


def composed_up(cli: Cli, machine: Machine) -> bool:
    return [*compose(machine), "up", "--detach", "--pull", "never", "--no-build"] in cli.docker.calls


@pytest.fixture
def cdd(tmp_path: Path) -> Path:
    path = tmp_path / "dictionaries" / "sales.cdd"
    path.parent.mkdir()
    path.write_bytes(b"CDD")
    path.chmod(0o644)
    return path


@pytest.fixture
def vault_ca(tmp_path: Path) -> Path:
    path = tmp_path / "pki" / "vault-ca.pem"
    path.parent.mkdir()
    path.write_text("-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----\n", encoding="utf-8")
    path.chmod(0o644)
    return path


# --- Before compose up: a file others cannot read ----------------------------------------------------------


@POSIX_ONLY
def test_an_owner_only_cdd_is_refused_before_compose_up(
    cli: Cli, machine: Machine, cdd: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    configure(cli, cdd)
    image_found(cli)
    comes_up(cli, machine, state())
    cdd.chmod(0o600)

    code, body = cli.json("start")

    assert code == 49
    assert body["error"]["code"] == "mounted_file_unreadable"
    message = body["error"]["message"]
    assert str(cdd) in message
    assert "CDD file" in message
    assert body["error"]["next"] == f"chmod 644 {cdd}"
    assert not composed_up(cli, machine)


@POSIX_ONLY
def test_an_owner_only_vault_ca_is_refused_before_compose_up(
    cli: Cli, machine: Machine, cdd: Path, vault_ca: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    configure(cli, cdd, "default", *sets(*KEYCLOAK, *VAULT), "--vault-ca", str(vault_ca))
    image_found(cli)
    comes_up(cli, machine, state())
    vault_ca.chmod(0o640)

    code, body = cli.json("start")

    assert code == 49
    message = body["error"]["message"]
    assert f"Vault CA file {vault_ca} (mode 0640)" in message
    assert "CDD file" not in message
    assert body["error"]["next"] == f"chmod 644 {vault_ca}"
    assert not composed_up(cli, machine)


@POSIX_ONLY
@pytest.mark.parametrize("platform", ["win32", "darwin"])
def test_a_windows_or_macos_host_skips_the_check(
    cli: Cli, machine: Machine, cdd: Path, monkeypatch: pytest.MonkeyPatch, platform: str
) -> None:
    # Docker on Windows and macOS reaches the file through the VM's file sharing; under Lima's reverse-sshfs the
    # host user reads it, so an owner-only file can still work.
    configure(cli, cdd)
    image_found(cli)
    comes_up(cli, machine, state())
    serving(cli)
    cdd.chmod(0o600)
    monkeypatch.setattr(sys, "platform", platform)

    code, body = cli.json("start")

    assert code == 0, body
    assert composed_up(cli, machine)


# --- While waiting: the server's own line -------------------------------------------------------------------

CDD_REFUSED = (
    "2026-10-16T09:00:00.000000000Z Starting CONNX MCP server 15.0.0 (build 559) on Python 3.14.0...\n"
    "2026-10-16T09:00:01.000000000Z ERROR: Unable to open CDD. (/opt/softwareag/connx/ini/mcp.cdd) "
    "Data Dictionary OPEN failed 14\n"
)


def logs(cli: Cli, stdout: str, stderr: str = "") -> None:
    """The container's log: the whole tail and the part since this start's compose up."""
    cli.docker.reply(["logs", "--tail", "40", "--timestamps", "connx-mcp-ce-default"], stdout=stdout, stderr=stderr)
    cli.docker.reply_when(since_compose_up, stdout=stdout, stderr=stderr)


def since_compose_up(args: list[str]) -> bool:
    return args[:2] == ["logs", "--since"]


@POSIX_ONLY
def test_a_cdd_the_server_cannot_open_fails_at_once_not_at_the_timeout(cli: Cli, machine: Machine, cdd: Path) -> None:
    configure(cli, cdd)
    image_found(cli)
    comes_up(cli, machine, state(health="starting"))
    logs(cli, CDD_REFUSED)

    code, body = cli.json("start")

    assert code == 50
    assert body["error"]["code"] == "server_cannot_open_cdd"
    message = body["error"]["message"]
    assert str(cdd) in message
    assert f"chmod 644 {cdd}" in message
    assert "ERROR: Unable to open CDD. (/opt/softwareag/connx/ini/mcp.cdd) Data Dictionary OPEN failed 14" in message
    assert body["error"]["next"] == "connx-mcp-ce log default --tail 200"
    assert cli.clock.now < 5


@POSIX_ONLY
@pytest.mark.parametrize(
    "said",
    [
        "ERROR: VAULT_CACERT /opt/softwareag/connx/mcp/vault-ca.pem cannot be used: not a PEM bundle",
        "ERROR: VAULT_CACERT /opt/softwareag/connx/mcp/vault-ca.pem (Permission denied)",
    ],
)
def test_a_vault_ca_the_server_cannot_use_is_named_when_it_exits(
    cli: Cli, machine: Machine, cdd: Path, vault_ca: Path, said: str
) -> None:
    configure(cli, cdd, "default", *sets(*KEYCLOAK, *VAULT), "--vault-ca", str(vault_ca))
    image_found(cli)
    comes_up(cli, machine, state(health="starting"), state(status="exited", health="unhealthy", exit_code=1))
    # The line comes as the server exits: the poll before it saw a clean log.
    cli.docker.replies_in_turn(
        ["logs", "--since", str(int(EPOCH)), "--tail", "40", "--timestamps", "connx-mcp-ce-default"],
        DockerResult(0, CDD_REFUSED.splitlines(keepends=True)[0], ""),
        DockerResult(0, "", f"2026-10-16T09:00:01.000000000Z {said}\n"),
    )

    code, body = cli.json("start")

    assert code == 51
    assert body["error"]["code"] == "server_cannot_use_vault_ca"
    message = body["error"]["message"]
    assert "exited with code 1" in message
    assert str(vault_ca) in message
    assert f"chmod 644 {vault_ca}" in message
    assert said in message
    assert body["error"]["next"] == "connx-mcp-ce log default --tail 200"


def test_on_a_windows_host_the_server_line_is_named_without_a_chmod(
    cli: Cli, machine: Machine, cdd: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    configure(cli, cdd)
    image_found(cli)
    comes_up(cli, machine, state(health="starting"))
    logs(cli, CDD_REFUSED)
    monkeypatch.setattr(sys, "platform", "win32")

    code, body = cli.json("start")

    assert code == 50
    message = body["error"]["message"]
    assert str(cdd) in message
    assert "chmod" not in message
    assert "Docker can share it with the container" in message


def test_the_help_documents_each_new_exit_code(cli: Cli) -> None:
    result = cli("exit-codes")

    assert result.exit_code == 0
    for code in ("49  mounted_file_unreadable", "50  server_cannot_open_cdd", "51  server_cannot_use_vault_ca"):
        assert code in result.stdout


def test_a_line_from_an_earlier_start_is_not_taken_for_this_ones_cause(cli: Cli, machine: Machine, cdd: Path) -> None:
    configure(cli, cdd)
    image_found(cli)
    comes_up(cli, machine, state(health="starting"), state())
    serving(cli)
    # The container kept from the last start still holds its old line; nothing was logged since compose up.
    cli.docker.reply(["logs", "--tail", "40", "--timestamps", "connx-mcp-ce-default"], stdout=CDD_REFUSED)
    cli.docker.reply_when(since_compose_up, stdout="")

    code, body = cli.json("start")

    assert code == 0, body
