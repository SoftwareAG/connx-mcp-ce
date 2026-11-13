#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_discover.py - The administrator and an LLM discover the commands, the JSON envelope and exit codes.

Description:
    Help lists the commands and a getting-started sequence, every command's help shows
    options, examples and prerequisites, `--json` gives one object, exit codes are documented, and
    nothing prompts without a terminal or with `--no-input`.

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
import re
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest
import typer

from connx_mcp import cli as cli_module
from connx_mcp import version
from tests.conftest import Cli

# The command list from the spec, in the administrator's vocabulary.
COMMANDS = [
    "configure",
    "export",
    "login",
    "download",
    "update",
    "start",
    "stop",
    "restart",
    "log",
    "status",
    "remove",
    "install-skill",
    "uninstall-skill",
    "configure-demo",
    "start-demo",
    "stop-demo",
    "guide",
    "exit-codes",
]
DAEMON_COMMANDS = [
    "login",
    "download",
    "update",
    "start",
    "stop",
    "restart",
    "log",
    "status",
    "remove",
    "install-skill",
    "start-demo",
    "stop-demo",
]
# Commands that succeed on a machine with no environment: `status` lists none, `guide` names configure.
SUCCEED_WHEN_EMPTY = {"status", "guide", "exit-codes"}


def test_bare_command_lists_every_command_and_a_first_steps_sequence(cli: Cli) -> None:
    result = cli()

    assert result.exit_code == 0
    for command in COMMANDS:
        assert re.search(rf"^\s+{re.escape(command)}\s", result.stdout, re.MULTILINE), command
    started = result.stdout.index("First steps")
    sequence = result.stdout[started:]
    positions = [
        sequence.index(f"connx-mcp-ce {step}") for step in ("configure", "login", "download", "start", "status")
    ]
    assert positions == sorted(positions)


def test_help_names_the_exit_codes_command_and_fits_one_screen(cli: Cli) -> None:
    result = cli("--help")

    assert result.exit_code == 0
    assert "connx-mcp-ce exit-codes" in result.stdout
    assert not re.search(r"^\s+10\s+docker_missing\s", result.stdout, re.MULTILINE)
    assert len(result.stdout.splitlines()) <= 60


def test_exit_codes_documents_each_code(cli: Cli) -> None:
    result = cli("exit-codes")

    assert result.exit_code == 0
    for code, name in [(0, "ok"), (2, "usage"), (3, "input_required"), (10, "docker_missing")]:
        assert re.search(rf"^\s+{code}\s+{name}\s", result.stdout, re.MULTILINE), name


def test_exit_codes_json_lists_each_code_with_its_name_and_meaning(cli: Cli) -> None:
    code, body = cli.json("exit-codes")

    assert code == 0, body
    codes = body["result"]["exit_codes"]
    assert {"exit_code": 0, "code": "ok", "meaning": "success"} in codes
    assert {"exit_code": 2, "code": "usage", "meaning": "wrong command, option or value"} in codes
    assert [c["exit_code"] for c in codes] == sorted(c["exit_code"] for c in codes)


def test_exit_codes_name_the_tool_connx_mcp_ce_in_every_meaning(cli: Cli) -> None:
    code, body = cli.json("exit-codes")
    text = cli("exit-codes").stdout

    assert code == 0, body
    meanings = {c["code"]: c["meaning"] for c in body["result"]["exit_codes"]}
    assert meanings["image_not_found"] == "the environment's image is not on this machine (connx-mcp-ce download)"
    assert meanings["registry_unauthorized"] == "the registry refused the sign-in or the download (connx-mcp-ce login)"
    assert meanings["image_too_old"] == "the image is older than this connx-mcp-ce supports"
    assert not [m for m in meanings.values() if re.search(r"connx-mcp(?!-ce\b)[\w.-]*", m)]
    assert "(connx-mcp-ce configure-demo)" in text
    assert not re.search(r"connx-mcp(?!-ce\b)[\w.-]*", text)


@pytest.mark.parametrize("command", COMMANDS)
def test_each_command_help_shows_options_examples_and_prerequisites(cli: Cli, command: str) -> None:
    result = cli(command, "--help")

    assert result.exit_code == 0
    assert "--json" in result.stdout
    assert "--no-input" in result.stdout
    assert re.search(rf"Examples:\n\s+connx-mcp-ce {re.escape(command)}\b", result.stdout)
    assert "Prerequisites:" in result.stdout


@pytest.mark.parametrize("args", [["--help"], *([command, "--help"] for command in COMMANDS)], ids=["top", *COMMANDS])
def test_help_examples_name_a_cdd_style_environment_not_prod(cli: Cli, args: list[str]) -> None:
    # One environment per CDD: examples use `sales` (sales.cdd), never a stage name like `prod`.
    result = cli(*args)

    assert result.exit_code == 0
    assert not re.search(r"\bprod\b", result.stdout, re.IGNORECASE)


@pytest.mark.parametrize("command", COMMANDS)
def test_every_command_answers_json_with_one_object(cli: Cli, command: str) -> None:
    result = cli(command, "--json", "--no-input")

    envelope = json.loads(result.stdout)
    assert envelope["command"] == command
    if command in SUCCEED_WHEN_EMPTY:
        assert (result.exit_code, envelope["ok"]) == (0, True)
        return
    assert result.exit_code != 0
    assert envelope["ok"] is False
    assert envelope["command"] == command
    assert envelope["error"]["exit_code"] == result.exit_code
    assert envelope["error"]["message"]
    assert "next" in envelope["error"]


def test_every_command_is_built(cli: Cli) -> None:
    for command in COMMANDS:
        envelope = json.loads(cli(command, "--json", "--no-input").stdout)
        assert envelope.get("error", {}).get("code") != "not_implemented", command


def test_usage_error_with_json_is_still_one_object(cli: Cli) -> None:
    result = cli("start", "--no-such-option", "--json")

    assert result.exit_code == 2
    envelope = json.loads(result.stdout)
    assert envelope["error"]["code"] == "usage"
    assert "--no-such-option" in envelope["error"]["message"]
    assert envelope["error"]["next"] == "connx-mcp-ce start --help"


def test_unknown_command_with_json_is_one_object(cli: Cli) -> None:
    result = cli("strat", "--json")

    assert result.exit_code == 2
    envelope = json.loads(result.stdout)
    assert envelope["error"]["code"] == "usage"
    assert envelope["error"]["next"] == "connx-mcp-ce --help"


def test_an_aborted_prompt_with_json_exits_cancelled_in_the_envelope(cli: Cli, monkeypatch: pytest.MonkeyPatch) -> None:
    def end_of_input(*_: object) -> None:
        raise EOFError

    monkeypatch.setattr(cli_module, "check_docker", end_of_input)
    result = cli("status", "--json")

    assert result.exit_code == 100
    envelope = json.loads(result.stdout)
    assert envelope["ok"] is False
    assert (envelope["error"]["code"], envelope["error"]["exit_code"]) == ("cancelled", 100)


def test_human_error_goes_to_stderr_with_the_next_step(cli: Cli) -> None:
    result = cli("export")

    assert result.exit_code == 20
    assert result.stdout == ""
    assert "There is no environment 'default'" in result.stderr
    assert "Next: connx-mcp-ce configure default" in result.stderr


@pytest.mark.parametrize("command", COMMANDS)
@pytest.mark.parametrize("flags", [["--no-input"], []], ids=["no-input", "stdin-not-a-terminal"])
def test_no_command_prompts_without_a_terminal(cli: Cli, command: str, flags: list[str]) -> None:
    # CliRunner's stdin is never a terminal; an empty input would make any prompt abort.
    result = cli(command, *flags, input="")

    assert result.exit_code not in ((1,) if command in SUCCEED_WHEN_EMPTY else (0, 1)), result.output
    assert "Aborted" not in result.output


@pytest.mark.parametrize(
    ("command", "summary"),
    [
        (
            "configure",
            "Create or change an environment's settings. With no options, the command asks you for each setting",
        ),
        ("login", "Sign in to the container registry with your credentials."),
        ("restart", "Restart an environment to apply your changes."),
        ("status", "List your environments, or show one in detail."),
        ("remove", "Delete an environment after you confirm."),
        (
            "install-skill",
            "Install the connx-sql-expert skill. The command copies the skill from the environment's image",
        ),
        ("configure-demo", "Configure the demo stack. Choose its services (Keycloak, dex, Vault) and its users file."),
        ("guide", "Tell you the next command to run. The command checks these steps in order"),
    ],
)
def test_a_command_help_opens_with_its_plain_summary(cli: Cli, command: str, summary: str) -> None:
    result = cli(command, "--help")

    assert result.exit_code == 0
    assert summary in " ".join(result.stdout.split())


@pytest.mark.parametrize(
    ("command", "summary"),
    [("install-skill", "Install the connx-sql-expert skill."), ("configure-demo", "Configure the demo stack.")],
)
def test_the_command_list_shows_a_summary_whole(cli: Cli, command: str, summary: str) -> None:
    result = cli("--help")

    assert re.search(rf"^\s+{re.escape(command)}\s+{re.escape(summary)}$", result.stdout, re.MULTILINE)


def test_the_command_list_shows_every_summary_whole(cli: Cli) -> None:
    result = cli("--help")

    listed = result.stdout[result.stdout.index("Commands:") :].split("\n\n")[0].splitlines()[1:]
    assert len(listed) == len(COMMANDS)
    assert [line for line in listed if line.endswith("...")] == []


def _value_parameters() -> list[tuple[str, str, str | None]]:
    """Return (command, parameter, metavar) for every argument and every option that takes a value."""
    group = typer.main.get_command(cli_module.app)
    found = []
    for name, command in getattr(group, "commands").items():
        for param in command.params:
            option = param.param_type_name == "option"
            if option and (getattr(param, "is_flag") or getattr(param, "count")):
                continue
            label = param.opts[0] if option else str(param.name)
            found.append((name, label, param.metavar))
    return found


def test_every_value_shows_an_upper_case_shape_or_its_choices_as_metavar() -> None:
    shape = re.compile(r"[A-Z]+(=[A-Z]+)?|[A-Za-z]+(\|[a-z]+)+")

    wrong = [(c, p, m) for c, p, m in _value_parameters() if m is None or not shape.fullmatch(m)]

    assert wrong == []


def test_one_option_shows_one_metavar_on_every_command() -> None:
    metavars: dict[str, set[str | None]] = {}
    for _, param, metavar in _value_parameters():
        metavars.setdefault(param, set()).add(metavar)

    assert {p: m for p, m in metavars.items() if len(m) > 1} == {}


@pytest.mark.parametrize("command", COMMANDS)
def test_no_help_shows_a_click_type_as_metavar(cli: Cli, command: str) -> None:
    result = cli(command, "--help")

    assert re.findall(r"<(?:str|path|directory|file|int|float|text|choice|bool)[^>]*>", result.stdout) == []


def test_help_usage_and_header_name_the_community_edition_tool(cli: Cli) -> None:
    result = cli("--help")

    assert result.exit_code == 0
    assert result.stdout.startswith("Usage: connx-mcp-ce ")
    header = "Set up, run and maintain CONNX MCP Server Community Edition environments on this machine."
    assert header in " ".join(result.stdout.split())


def test_python_dash_m_prints_the_usage_under_the_tool_name() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "connx_mcp", "--help"], capture_output=True, text=True, timeout=60, check=False
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith("Usage: connx-mcp-ce ")


def test_the_tool_name_imports_from_the_package_without_loading_anything_else() -> None:
    probe = "import sys; before = set(sys.modules); from connx_mcp import CLI_NAME; " + (
        "print(CLI_NAME, sorted(m for m in set(sys.modules) - before if m.startswith('connx_mcp')))"
    )
    result = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, timeout=60, check=False)

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "connx-mcp-ce ['connx_mcp']"


def test_the_cli_version_is_the_installed_distributions_version() -> None:
    declared = tomllib.loads((Path(__file__).parents[1] / "pyproject.toml").read_text(encoding="utf-8"))["project"][
        "version"
    ]

    assert version.installed_version() == declared
