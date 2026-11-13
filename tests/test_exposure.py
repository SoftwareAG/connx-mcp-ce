#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_exposure.py - An environment without OAuth, published on every interface, is warned about.

Description:
    The server port is published on all network interfaces. With OAuth `none`, anyone who reaches it runs SQL
    as the stored CDD login: `configure` says so once, and `status`, the status list and `guide` warn about it
    without making it guide's next step. An environment with an OAuth provider, one that listens on this
    machine only (`--listen localhost`) or one whose open network is accepted gets no such warning.

Copyright:
    Copyright 2026 Software GmbH
    Licensed under the Apache License, Version 2.0 (the "License"); you may not use this file except in compliance
    with the License. You may obtain a copy of the License at https://www.apache.org/licenses/LICENSE-2.0
    Unless required by applicable law or agreed to in writing, software distributed under the License is distributed
    on an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the License
    for the specific language governing permissions and limitations under the License.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.conftest import Cli, Machine
from tests.test_guide import guide, image_missing, never_started
from tests.test_lifecycle import comes_up, configure, env_dir, image_found, serving, state
from tests.test_oauth_vault import KEYCLOAK

EXPOSED = "can query your data through environment"


def exposure_warnings(lines: list[str]) -> list[str]:
    return [line for line in lines if EXPOSED in line]


@pytest.fixture
def cdd(tmp_path: Path) -> Path:
    path = tmp_path / "dictionaries" / "sales.cdd"
    path.parent.mkdir()
    path.write_bytes(b"CDD")
    return path


def keycloak(cli: Cli, cdd: Path, name: str = "default") -> None:
    configure(cli, cdd, name, *(arg for item in KEYCLOAK for arg in ("--set", item)))


def stopped(cli: Cli, name: str = "default") -> None:
    image_found(cli)
    never_started(cli, name)


def test_configure_without_oauth_warns_once_that_the_network_reaches_the_cdd_login(cli: Cli, cdd: Path) -> None:
    code, body = cli.json(
        "configure", "--no-input", "--set", "CDD_USERNAME=mcp", "--set", "CDD_PASSWORD=", "--cdd", str(cdd)
    )

    assert code == 0, body
    [warning] = exposure_warnings(body["result"]["warnings"])
    assert warning.splitlines() == [
        "Anyone who can reach this computer on port 8000 can query your data through environment 'default'.",
        "  Callers do not sign in, so every query runs as the CDD login stored in this environment.",
        "  To enforce sign-in to the CONNX MCP Server, choose an OAuth provider using: connx-mcp-ce configure default",
        "  Otherwise, use this environment only on a network you trust.",
    ]
    human = cli("configure", "--no-input", "--set", "MCP_PATH=/sql").stdout
    assert human.count(EXPOSED) == 1


def test_configure_with_oauth_gives_no_exposure_warning(cli: Cli, cdd: Path) -> None:
    keycloak(cli, cdd)

    code, body = cli.json("configure", "--no-input", "--set", "MCP_PATH=/sql")

    assert code == 0, body
    assert exposure_warnings(body["result"]["warnings"]) == []


def test_status_warns_about_an_environment_without_oauth(cli: Cli, cdd: Path) -> None:
    configure(cli, cdd)
    stopped(cli)

    code, body = cli.json("status", "default")

    assert code == 0, body
    assert len(exposure_warnings(body["result"]["notices"])) == 1
    stopped(cli)
    assert "Warning: " in next(line for line in cli("status", "default").stdout.splitlines() if EXPOSED in line)


def test_status_gives_no_exposure_warning_with_oauth(cli: Cli, cdd: Path) -> None:
    keycloak(cli, cdd)
    stopped(cli)

    code, body = cli.json("status", "default")

    assert code == 0, body
    assert exposure_warnings(body["result"]["notices"]) == []


def test_the_status_list_warns_about_each_environment_without_oauth(cli: Cli, cdd: Path) -> None:
    configure(cli, cdd, "open")
    keycloak(cli, cdd, "signed")
    stopped(cli, "open")
    stopped(cli, "signed")

    code, body = cli.json("status")

    assert code == 0, body
    rows = {row["environment"]: row for row in body["result"]["environments"]}
    assert len(exposure_warnings(rows["open"]["notices"])) == 1
    assert exposure_warnings(rows["signed"]["notices"]) == []
    stopped(cli, "open")
    stopped(cli, "signed")
    [line] = exposure_warnings(cli("status").stdout.splitlines())
    assert line.startswith("Warning: open: ")


def test_guide_warns_but_keeps_its_next_step(cli: Cli, cdd: Path) -> None:
    configure(cli, cdd)
    stopped(cli)

    code, body = cli.json("guide")

    assert code == 0, body
    result = body["result"]
    assert (result["stage"], result["next"]) == ("not_started", "connx-mcp-ce start default")
    assert len(exposure_warnings(result["warnings"])) == 1
    assert EXPOSED not in result["reason"]


def test_guide_warns_before_the_image_is_on_this_machine(cli: Cli, cdd: Path) -> None:
    configure(cli, cdd)
    image_missing(cli)

    result = guide(cli)

    assert (result["stage"], result["next"]) == ("no_image", "connx-mcp-ce login")
    assert len(exposure_warnings(result["warnings"])) == 1


# --- Listening on this machine only, or accepting the open network ---------------------------------------------


def test_listen_localhost_publishes_the_port_on_this_machine_only(cli: Cli, machine: Machine, cdd: Path) -> None:
    configure(cli, cdd, "default", "--listen", "localhost")
    image_found(cli)
    comes_up(cli, machine, state())
    serving(cli)

    code, body = cli.json("start")

    assert code == 0, body
    assert '"127.0.0.1:8000:8000"' in (env_dir(machine) / "compose.yml").read_text(encoding="utf-8")


def test_an_environment_listening_on_this_machine_only_is_not_warned_about(cli: Cli, cdd: Path) -> None:
    code, body = cli.json(
        "configure", "--no-input", "--set", "CDD_USERNAME=mcp", "--set", "CDD_PASSWORD=", "--cdd", str(cdd),
        "--listen", "localhost",
    )  # fmt: skip

    assert code == 0, body
    assert exposure_warnings(body["result"]["warnings"]) == []
    stopped(cli)
    code, body = cli.json("status", "default")
    assert exposure_warnings(body["result"]["notices"]) == []
    assert body["result"]["network"] == {"listen": "localhost", "oauth": False, "open_network_accepted": False}


def test_listen_all_publishes_on_every_interface_again(cli: Cli, cdd: Path) -> None:
    configure(cli, cdd, "default", "--listen", "localhost")

    code, body = cli.json("configure", "--no-input", "--listen", "all")

    assert code == 0, body
    assert len(exposure_warnings(body["result"]["warnings"])) == 1
    assert "listen_address" not in cli.json("configure", "--show")[1]["result"]["host_settings"]


def test_listen_refuses_anything_but_localhost_or_all(cli: Cli, cdd: Path) -> None:
    configure(cli, cdd)

    code, body = cli.json("configure", "--no-input", "--listen", "0.0.0.0")

    assert code == 2
    assert "--listen" in body["error"]["message"]
    assert "localhost" in body["error"]["message"]


def test_an_accepted_open_network_is_not_warned_about_again(cli: Cli, cdd: Path) -> None:
    configure(cli, cdd)

    code, body = cli.json("configure", "--no-input", "--accept-open-network")

    assert code == 0, body
    assert exposure_warnings(body["result"]["warnings"]) == []
    stopped(cli)
    code, body = cli.json("status", "default")
    assert exposure_warnings(body["result"]["notices"]) == []
    assert body["result"]["network"] == {"listen": "all", "oauth": False, "open_network_accepted": True}
    stopped(cli)
    assert exposure_warnings(guide(cli)["warnings"]) == []


def test_no_accept_open_network_brings_the_warning_back(cli: Cli, cdd: Path) -> None:
    configure(cli, cdd, "default", "--accept-open-network")

    code, body = cli.json("configure", "--no-input", "--no-accept-open-network")

    assert code == 0, body
    assert len(exposure_warnings(body["result"]["warnings"])) == 1
    assert "open_network_accepted" not in cli.json("configure", "--show")[1]["result"]["host_settings"]


def test_export_and_import_carry_where_the_port_listens_and_the_accepted_risk(
    cli: Cli, cdd: Path, tmp_path: Path
) -> None:
    configure(cli, cdd, "default", "--listen", "localhost", "--accept-open-network")
    sent = tmp_path / "default.env"
    sent.write_text(cli("export", "default").stdout, encoding="utf-8")

    code, body = cli.json("configure", "copy", "--no-input", "--from-env-file", str(sent), "--set", "CDD_PASSWORD=x")

    assert code == 0, body
    host = cli.json("configure", "copy", "--show")[1]["result"]["host_settings"]
    assert (host["listen_address"], host["open_network_accepted"]) == ("localhost", True)
