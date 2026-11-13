#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_configure_interactive.py - A first-time administrator configures an environment on a terminal.

Description:
    `configure` on a terminal walks every settings group once, showing each key's current
    value or default and whether it is set. Enter keeps the shown value, a value sets it, `-` returns
    to the server default, `""` sets an explicit empty value where the setting allows it. Secrets are
    never echoed and show `***hidden***`. A group switch set to `none` suppresses its dependent
    fields, and choosing one option asks only that option's fields. The prompts are driven through
    prompt_toolkit's pipe input via the CLI seam.

Copyright:
    Copyright 2026 Software GmbH
    Licensed under the Apache License, Version 2.0 (the "License"); you may not use this file except in compliance
    with the License. You may obtain a copy of the License at https://www.apache.org/licenses/LICENSE-2.0
    Unless required by applicable law or agreed to in writing, software distributed under the License is distributed
    on an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the License
    for the specific language governing permissions and limitations under the License.
"""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest

from tests.conftest import Cli, Machine, as_typed

PASSWORD = "pw-for-tests-only"
DOWN = "\x1b[B"

# The order a fresh environment walks its prompts in: groups in order, then the CDD file. The licence
# port is missing because it depends on the licence host, which a fresh environment does not set; the OAuth
# and Vault switches stay at none, so none of their fields are asked.
FRESH_WALK = [
    "CDD_USERNAME",
    "CDD_PASSWORD",
    "CONNX_LICENSE_HOST",
    "CONNX_HOST",
    "MCP_PATH",
    "MCP_SESSION_TIMEOUT_SECONDS",
    "MCP_SESSION_CLEANUP_INTERVAL_SECONDS",
    "MCP_TRUSTED_ORIGINS",
    "MCP_STARTUP_WAIT_SECONDS",
    "MCP_TLS_ENABLED",
    "MCP_OAUTH_PROVIDER",
    "MCP_CREDENTIAL_PROVIDER",
    "cdd",
]


@pytest.fixture
def cdd(tmp_path: Path) -> Path:
    path = tmp_path / "dictionaries" / "sales.cdd"
    path.parent.mkdir()
    path.write_bytes(b"CDD")
    return path


def answers(order: Sequence[str], given: Mapping[str, str | list[str]]) -> list[str]:
    """The keystrokes for a walk: Enter for every prompt not in `given`; a list answers one prompt several times."""
    keys: list[str] = []
    for key in order:
        answer = given.get(key, "")
        keys.extend(answer if isinstance(answer, list) else [answer])
    return keys


def first_time(dictionary: Path, **given: str | list[str]) -> list[str]:
    """Keystrokes that create a complete environment, plus the answers given."""
    return answers(FRESH_WALK, {"CDD_USERNAME": "mcp", "CDD_PASSWORD": PASSWORD, "cdd": str(dictionary), **given})


def server_env(machine: Machine, name: str = "default") -> str:
    return (machine.base / name / "server.env").read_text(encoding="utf-8")


def shown(cli: Cli, name: str = "default") -> dict[str, str]:
    code, body = cli.json("configure", name, "--show")
    assert code == 0, body
    return body["result"]["server_settings"]


def prompt_line(screen: str, key: str) -> str:
    """The first line the terminal drew for a key's prompt."""
    return next(line for line in screen.splitlines() if line.lstrip("? ").startswith(f"{key} "))


# --- The walk ---------------------------------------------------------------------------------------------


def test_a_first_time_walk_shows_every_group_and_saves_the_answers(cli: Cli, machine: Machine, cdd: Path) -> None:
    result, screen = cli.typed("configure", keys=first_time(cdd))

    assert result.exit_code == 0, result.output + screen
    assert "Created environment 'default'" in result.output
    for title in ("Credentials", "CONNX engine and licensing", "Listener and sessions"):
        assert title in screen
    assert shown(cli) == {"CDD_USERNAME": "mcp", "CDD_PASSWORD": "***hidden***"}
    code, body = cli.json("configure", "--show")
    assert body["result"]["host_settings"] == {"cdd_file": str(cdd)}


def test_each_prompt_shows_the_value_or_default_and_whether_it_is_set(cli: Cli, cdd: Path) -> None:
    cli("configure", "--no-input", "--set", "CDD_USERNAME=mcp", "--set", "CDD_PASSWORD=", "--cdd", str(cdd))
    cli("configure", "--no-input", "--set", "MCP_PATH=/api", "--set", "MCP_TRUSTED_ORIGINS=")

    result, screen = cli.typed("configure", keys=answers(FRESH_WALK, {}))

    assert result.exit_code == 0, result.output + screen
    assert "'/api' (set)" in prompt_line(screen, "MCP_PATH")
    assert "'300' (not set: server default)" in prompt_line(screen, "MCP_SESSION_TIMEOUT_SECONDS")
    assert "'' (set: explicitly empty)" in prompt_line(screen, "MCP_TRUSTED_ORIGINS")
    assert "(not set, no default)" in prompt_line(screen, "CONNX_HOST")
    assert "'mcp' (set)" in prompt_line(screen, "CDD_USERNAME")
    assert str(cdd) in screen


def test_a_value_with_a_backslash_is_shown_as_typed(cli: Cli, tmp_path: Path) -> None:
    dictionary = tmp_path / "sales\\2026.cdd"  # One file name on POSIX; a folder and its file on Windows.
    dictionary.parent.mkdir(parents=True, exist_ok=True)
    dictionary.write_bytes(b"CDD")
    cli(
        "configure", "--no-input", "--set", "CDD_USERNAME=CORP\\mcp", "--set", "CDD_PASSWORD=", "--cdd", str(dictionary)
    )

    result, screen = cli.typed("configure", keys=answers(FRESH_WALK, {}))

    assert result.exit_code == 0, result.output + screen
    assert "'CORP\\mcp' (set)" in prompt_line(screen, "CDD_USERNAME")
    assert f"CDD file '{dictionary}' (set)" in screen


def test_dependent_fields_are_asked_once_what_they_depend_on_is_set(cli: Cli, cdd: Path) -> None:
    walk = [*FRESH_WALK[:3], "CONNX_LICENSE_PORT", *FRESH_WALK[3:]]

    result, screen = cli.typed(
        "configure",
        keys=answers(
            walk,
            {
                "CDD_USERNAME": "mcp",
                "CDD_PASSWORD": PASSWORD,
                "CONNX_LICENSE_HOST": "lic.example.com",
                "CONNX_LICENSE_PORT": "7501",
                "cdd": str(cdd),
            },
        ),
    )

    assert result.exit_code == 0, result.output + screen
    assert shown(cli)["CONNX_LICENSE_PORT"] == "7501"


def test_a_fresh_walk_does_not_ask_for_fields_whose_dependency_is_unset(cli: Cli, cdd: Path) -> None:
    result, screen = cli.typed("configure", keys=first_time(cdd))

    assert result.exit_code == 0, result.output + screen
    assert "CONNX_LICENSE_PORT" not in screen


# --- Answers: keep, set, back to default, explicit empty ------------------------------------------------


@pytest.fixture
def configured(cli: Cli, cdd: Path) -> Cli:
    code, body = cli.json(
        "configure",
        "--no-input",
        "--set",
        "CDD_USERNAME=mcp",
        "--set",
        f"CDD_PASSWORD={PASSWORD}",
        "--set",
        "MCP_PATH=/api",
        "--set",
        "MCP_STARTUP_WAIT_SECONDS=30",
        "--cdd",
        str(cdd),
    )
    assert code == 0, body
    return cli


def test_enter_keeps_every_shown_value(configured: Cli, machine: Machine) -> None:
    before = server_env(machine)

    result, screen = configured.typed("configure", keys=answers(FRESH_WALK, {}))

    assert result.exit_code == 0, result.output + screen
    assert server_env(machine) == before


def test_a_value_sets_and_a_dash_returns_to_the_server_default(configured: Cli) -> None:
    result, screen = configured.typed(
        "configure", keys=answers(FRESH_WALK, {"MCP_PATH": "-", "MCP_SESSION_TIMEOUT_SECONDS": "600"})
    )

    assert result.exit_code == 0, result.output + screen
    values = shown(configured)
    assert "MCP_PATH" not in values
    assert values["MCP_SESSION_TIMEOUT_SECONDS"] == "600"
    assert values["MCP_STARTUP_WAIT_SECONDS"] == "30"


def test_two_quotes_set_an_explicit_empty_value_where_allowed(configured: Cli) -> None:
    result, screen = configured.typed("configure", keys=answers(FRESH_WALK, {"MCP_TRUSTED_ORIGINS": '""'}))

    assert result.exit_code == 0, result.output + screen
    assert shown(configured)["MCP_TRUSTED_ORIGINS"] == ""


def test_two_quotes_where_empty_is_not_allowed_are_explained_and_asked_again(configured: Cli) -> None:
    result, screen = configured.typed("configure", keys=answers(FRESH_WALK, {"MCP_STARTUP_WAIT_SECONDS": ['""', "45"]}))

    assert result.exit_code == 0, result.output + screen
    assert "MCP_STARTUP_WAIT_SECONDS: it cannot be empty; type - to return to " in screen
    assert shown(configured)["MCP_STARTUP_WAIT_SECONDS"] == "45"


def test_an_invalid_value_is_explained_and_asked_again(configured: Cli) -> None:
    result, screen = configured.typed(
        "configure", keys=answers(FRESH_WALK, {"MCP_SESSION_TIMEOUT_SECONDS": ["30", "90"]})
    )

    assert result.exit_code == 0, result.output + screen
    assert "it must be at least 60" in screen
    assert shown(configured)["MCP_SESSION_TIMEOUT_SECONDS"] == "90"


def test_a_required_field_without_a_value_is_asked_again(cli: Cli, cdd: Path) -> None:
    result, screen = cli.typed("configure", keys=first_time(cdd, CDD_USERNAME=["", "mcp"]))

    assert result.exit_code == 0, result.output + screen
    assert "CDD_USERNAME: it is required; type a value." in screen


def test_a_dash_on_a_required_field_is_refused(configured: Cli) -> None:
    result, screen = configured.typed("configure", keys=answers(FRESH_WALK, {"CDD_USERNAME": ["-", ""]}))

    assert result.exit_code == 0, result.output + screen
    assert "CDD_USERNAME: it is required; type a value." in screen
    assert shown(configured)["CDD_USERNAME"] == "mcp"


def test_a_cdd_file_that_does_not_exist_is_asked_again(cli: Cli, cdd: Path, tmp_path: Path) -> None:
    result, screen = cli.typed("configure", keys=first_time(cdd, cdd=[str(tmp_path / "nope.cdd"), str(cdd)]))

    assert result.exit_code == 0, result.output + screen
    assert f"--cdd {as_typed(tmp_path / 'nope.cdd')}: no such file." in screen


def test_input_ending_mid_walk_saves_nothing(cli: Cli, machine: Machine) -> None:
    result, screen = cli.typed("configure", keys=["mcp"])

    assert result.exit_code == 100
    assert "nothing was saved" in result.output
    assert not (machine.base / "default").exists()


def test_no_input_never_walks(cli: Cli, machine: Machine) -> None:
    result, screen = cli.typed("configure", "--no-input", keys=[])

    assert result.exit_code == 3
    assert screen == ""


def test_flags_on_a_terminal_change_only_what_they_name(configured: Cli) -> None:
    result, screen = configured.typed("configure", "--set", "MCP_PATH=/x", keys=[])

    assert result.exit_code == 0, result.output + screen
    assert screen == ""
    assert shown(configured)["MCP_PATH"] == "/x"


# --- Secrets ------------------------------------------------------------------------------------------------


def test_a_secret_is_never_echoed(cli: Cli, cdd: Path) -> None:
    result, screen = cli.typed("configure", keys=first_time(cdd))

    assert result.exit_code == 0, result.output + screen
    assert PASSWORD not in screen
    assert PASSWORD not in result.output


def test_a_stored_secret_shows_hidden_and_enter_keeps_it(configured: Cli, machine: Machine) -> None:
    result, screen = configured.typed("configure", keys=answers(FRESH_WALK, {}))

    assert result.exit_code == 0, result.output + screen
    assert "***hidden*** (set)" in prompt_line(screen, "CDD_PASSWORD")
    assert PASSWORD in server_env(machine)


def test_a_refused_secret_is_explained_without_echoing_it(configured: Cli) -> None:
    refused = "pw;with-semicolon"

    result, screen = configured.typed("configure", keys=answers(FRESH_WALK, {"CDD_PASSWORD": [refused, ""]}))

    assert result.exit_code == 0, result.output + screen
    assert "cannot take a ; in a credential" in screen
    assert refused not in screen
    assert refused not in result.output


def test_a_secret_can_be_set_to_explicit_empty(configured: Cli, machine: Machine) -> None:
    result, screen = configured.typed("configure", keys=answers(FRESH_WALK, {"CDD_PASSWORD": '""'}))

    assert result.exit_code == 0, result.output + screen
    assert shown(configured)["CDD_PASSWORD"] == "***hidden***"
    assert PASSWORD not in server_env(machine)


def test_the_ports_do_not_depend_on_the_prompts() -> None:
    loaded = subprocess.run(
        [sys.executable, "-c", "import sys, connx_mcp.ports; print('connx_mcp.prompts' in sys.modules)"],
        capture_output=True, text=True, check=True,
    )  # fmt: skip

    assert loaded.stdout.strip() == "False"


def test_the_walk_says_connx_host_overrides_the_host_in_the_cdd(configured: Cli) -> None:
    result, screen = configured.typed("configure", keys=answers(FRESH_WALK, {}))

    assert result.exit_code == 0, result.output + screen
    assert "CONNX host that replaces the host in the CDD" in screen
