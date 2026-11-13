#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_configure.py - The administrator or an LLM configures an environment with flags.

Description:
    Environments live in the base directory, a missing name means `default`, invalid
    names are refused with a suggestion, server settings and host settings are kept apart, `--set`,
    `--unset` and explicit empty follow each key's domain, retired keys are refused by name, unknown
    keys are accepted with a warning, `--no-input` lists exactly the missing fields, the server
    settings file is owner-only, and `--show` masks every secret.

Copyright:
    Copyright 2026 Software GmbH
    Licensed under the Apache License, Version 2.0 (the "License"); you may not use this file except in compliance
    with the License. You may obtain a copy of the License at https://www.apache.org/licenses/LICENSE-2.0
    Unless required by applicable law or agreed to in writing, software distributed under the License is distributed
    on an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the License
    for the specific language governing permissions and limitations under the License.
"""

from __future__ import annotations

import io
import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest
import typer
from click.testing import CliRunner

from connx_mcp import environment, settings
from connx_mcp.cli import app
from connx_mcp.docker import SubprocessDocker
from connx_mcp.ports import Ports
from tests.conftest import Cli, Machine

PASSWORD = "pw-for-tests-only"
# A CONNX login with what an env file could misread: quotes, $, a backslash, = and an inline #; no ;, which
# the CONNX driver refuses in a credential.
TRICKY_USERNAME = 'it\'s $HOME "x" \\ ok a=b #c'


@pytest.fixture
def cdd(tmp_path: Path) -> Path:
    path = tmp_path / "dictionaries" / "sales.cdd"
    path.parent.mkdir()
    path.write_bytes(b"CDD")
    return path


def complete(cdd: Path, *extra: str) -> list[str]:
    """Flags that make a complete environment."""
    return [
        "--no-input",
        "--set",
        "CDD_USERNAME=mcp",
        "--set",
        f"CDD_PASSWORD={PASSWORD}",
        "--cdd",
        str(cdd),
        *extra,
    ]


def read_env(path: Path) -> dict[str, str]:
    """Parse a server settings file written by the CLI (simple KEY=VALUE or KEY='VALUE' lines)."""
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line or line.startswith("#"):
            continue
        key, _, value = line.partition("=")
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        values[key] = value
    return values


# --- Where environments live and what they are called -------------------------------------------------


def test_a_missing_name_configures_default_in_the_base_directory(cli: Cli, machine: Machine, cdd: Path) -> None:
    code, body = cli.json("configure", *complete(cdd))

    assert code == 0, body
    assert body["result"]["environment"] == "default"
    assert (machine.base / "default" / "server.env").is_file()


def test_without_mcp_environment_path_the_base_directory_is_under_home(
    cli: Cli, machine: Machine, cdd: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("MCP_ENVIRONMENT_PATH")

    code, body = cli.json("configure", "sales", *complete(cdd))

    assert code == 0, body
    assert (machine.home / ".connx" / "connx-mcp-ce" / "sales" / "server.env").is_file()


def test_mcp_environment_path_names_the_base_directory_with_the_home_directory_expanded(
    cli: Cli, machine: Machine, cdd: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MCP_ENVIRONMENT_PATH", str(Path("~") / "chosen"))

    code, body = cli.json("configure", "sales", *complete(cdd))

    assert code == 0, body
    assert (machine.home / "chosen" / "sales" / "server.env").is_file()
    assert not (machine.home / ".connx").exists()
    assert not machine.base.exists()


def test_the_root_help_names_the_default_base_directory(cli: Cli) -> None:
    result = cli("--help")

    assert result.exit_code == 0, result.output
    assert "(default ~/.connx/connx-mcp-ce)" in " ".join(result.stdout.split())


def test_connx_mcp_environment_selects_the_environment(
    cli: Cli, machine: Machine, cdd: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CONNX_MCP_ENVIRONMENT", "staging")

    code, body = cli.json("configure", *complete(cdd))

    assert code == 0, body
    assert body["result"]["environment"] == "staging"
    assert (machine.base / "staging" / "server.env").is_file()


@pytest.mark.parametrize(
    ("name", "suggestion"),
    [("Sales", "sales"), ("my env", "my-env"), ("_x", "x"), ("a" * 40, "a" * 32), ("sales.eu", "sales-eu")],
)
def test_an_invalid_name_is_refused_with_a_valid_form(
    cli: Cli, machine: Machine, cdd: Path, name: str, suggestion: str
) -> None:
    code, body = cli.json("configure", name, *complete(cdd))

    assert code == 2
    assert body["error"]["code"] == "usage"
    assert suggestion in body["error"]["message"]
    assert body["error"]["next"] == f"connx-mcp-ce configure {suggestion}"
    assert not machine.base.exists() or not any(machine.base.iterdir())


# --- Server settings and host settings ------------------------------------------------------------------


def test_server_settings_hold_only_the_keys_set_and_host_settings_hold_the_cdd(
    cli: Cli, machine: Machine, cdd: Path
) -> None:
    code, body = cli.json("configure", *complete(cdd, "--set", "CONNX_LICENSE_HOST=host.docker.internal"))

    assert code == 0, body
    directory = machine.base / "default"
    assert read_env(directory / "server.env") == {
        "CDD_USERNAME": "mcp",
        "CDD_PASSWORD": PASSWORD,
        "CONNX_LICENSE_HOST": "host.docker.internal",
    }
    host = json.loads((directory / "host.json").read_text(encoding="utf-8"))
    assert host == {"cdd_file": str(cdd)}


def test_a_later_configure_changes_only_what_it_names(cli: Cli, machine: Machine, cdd: Path) -> None:
    cli.json("configure", *complete(cdd, "--set", "MCP_PATH=/sql"))

    code, body = cli.json("configure", "--no-input", "--set", "MCP_SESSION_TIMEOUT_SECONDS=600")

    assert code == 0, body
    assert body["result"]["created"] is False
    assert read_env(machine.base / "default" / "server.env") == {
        "CDD_USERNAME": "mcp",
        "CDD_PASSWORD": PASSWORD,
        "MCP_PATH": "/sql",
        "MCP_SESSION_TIMEOUT_SECONDS": "600",
    }


def test_unset_returns_a_key_to_the_server_default(cli: Cli, machine: Machine, cdd: Path) -> None:
    cli.json("configure", *complete(cdd, "--set", "MCP_PATH=/sql"))

    code, body = cli.json("configure", "--no-input", "--unset", "MCP_PATH")

    assert code == 0, body
    assert "MCP_PATH" not in read_env(machine.base / "default" / "server.env")


def test_host_settings_a_newer_connx_mcp_wrote_survive_a_change(cli: Cli, machine: Machine, cdd: Path) -> None:
    cli.json("configure", *complete(cdd))
    host_file = machine.base / "default" / "host.json"
    newer = {**json.loads(host_file.read_text(encoding="utf-8")), "future_knob": {"nested": [1, 2]}}
    host_file.write_text(json.dumps(newer), encoding="utf-8")

    code, body = cli.json("configure", "--no-input", "--set", "MCP_PATH=/sql")

    assert code == 0, body
    assert json.loads(host_file.read_text(encoding="utf-8")) == newer


def test_a_relative_cdd_path_is_stored_absolute(
    cli: Cli, machine: Machine, cdd: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(cdd.parent)

    code, body = cli.json("configure", *complete(Path(cdd.name)))

    assert code == 0, body
    host = json.loads((machine.base / "default" / "host.json").read_text(encoding="utf-8"))
    assert host["cdd_file"] == str(cdd)


def test_a_missing_cdd_file_is_refused(cli: Cli, machine: Machine, tmp_path: Path) -> None:
    code, body = cli.json("configure", *complete(tmp_path / "nowhere.cdd"))

    assert code == 2
    assert "nowhere.cdd" in body["error"]["message"]


@pytest.mark.parametrize(
    ("key", "flag"), [("MCP_CDD_FILE", "--cdd"), ("MCP_CERT_SOURCE", "--certs"), ("MCP_IMAGE", "--image")]
)
def test_a_host_setting_is_refused_in_server_settings(
    cli: Cli, machine: Machine, cdd: Path, key: str, flag: str
) -> None:
    code, body = cli.json("configure", *complete(cdd, "--set", f"{key}=/x"))

    assert code == 2
    assert key in body["error"]["message"]
    assert flag in body["error"]["message"]


@pytest.mark.parametrize("key", ["MCP_OAUTH_STATE_SOURCE"])
def test_a_host_variable_without_a_flag_yet_is_refused_without_naming_a_flag(
    cli: Cli, machine: Machine, cdd: Path, key: str
) -> None:
    code, body = cli.json("configure", *complete(cdd, "--set", f"{key}=x"))

    assert code == 2
    assert key in body["error"]["message"]
    assert "host setting" in body["error"]["message"]
    assert "connx-mcp-ce fills it in" in body["error"]["message"]


@pytest.mark.parametrize("setting", ["MCP_PORT=9000", "MCP_SERVER_EDITION=Lab"])
def test_a_key_fixed_in_the_image_is_refused(cli: Cli, machine: Machine, cdd: Path, setting: str) -> None:
    code, body = cli.json("configure", *complete(cdd, "--set", setting))

    assert code == 2
    assert setting.split("=")[0] in body["error"]["message"]
    assert "fixed in the server image" in body["error"]["message"]


@pytest.mark.parametrize("setting", ["ACCEPT_EULA=y", "accept_eula=Y"])
def test_the_eula_is_refused_as_a_server_setting(cli: Cli, machine: Machine, cdd: Path, setting: str) -> None:
    code, body = cli.json("configure", *complete(cdd, "--set", setting))

    assert code == 2
    assert setting.split("=")[0] in body["error"]["message"]
    assert "connx-mcp-ce accepts the EULA when it starts the server" in body["error"]["message"]


@pytest.mark.parametrize(
    ("key", "how"),
    [
        ("MCP_ENV_FILE", "connx-mcp-ce fills it in from the environment"),
        ("image_digest", "start and update record it when they pin the image"),
        ("image_version", "start and update record it with the pinned image"),
        ("start_timeout", "start --timeout records it"),
        ("ask_at_start", "mark a secret with --ask-at-start KEY"),
    ],
)
def test_a_host_setting_without_a_flag_is_refused_with_how_it_is_set(
    cli: Cli, machine: Machine, cdd: Path, key: str, how: str
) -> None:
    code, body = cli.json("configure", *complete(cdd, "--set", f"{key}=x"))

    assert code == 2
    assert body["error"]["message"] == f"--set {key}: it is a host setting, not a server setting; {how}."


@pytest.mark.parametrize("flag", ["--set", "--ask-at-start"])
def test_an_invalid_setting_name_is_refused_without_being_shown(
    cli: Cli, machine: Machine, cdd: Path, flag: str
) -> None:
    pasted = "p@ss!"
    item = f"{pasted}=x" if flag == "--set" else pasted

    code, body = cli.json("configure", *complete(cdd, flag, item))

    assert code == 2
    assert body["error"]["message"] == (
        f"{flag}: the name is not a valid setting name; a setting name is letters, digits and underscores, "
        "not starting with a digit."
    )


# --- Value domains --------------------------------------------------------------------------------------


@pytest.mark.parametrize("key", ["MCP_TRUSTED_ORIGINS", "CDD_PASSWORD"])
def test_explicit_empty_is_kept_where_the_domain_allows_it(cli: Cli, machine: Machine, cdd: Path, key: str) -> None:
    code, body = cli.json("configure", *complete(cdd, "--set", f"{key}="))

    assert code == 0, body
    text = (machine.base / "default" / "server.env").read_text(encoding="utf-8")
    assert f"{key}=" in text.splitlines() or f"{key}=''" in text.splitlines()


@pytest.mark.parametrize("key", ["MCP_PATH", "CDD_USERNAME", "MCP_SESSION_TIMEOUT_SECONDS", "CONNX_LICENSE_HOST"])
def test_explicit_empty_is_refused_with_the_reason_where_the_domain_forbids_it(
    cli: Cli, machine: Machine, cdd: Path, key: str
) -> None:
    code, body = cli.json("configure", *complete(cdd, "--set", f"{key}="))

    assert code == 2
    message = body["error"]["message"]
    assert message.startswith(f"--set {key}: it cannot be empty; give a value, or --unset it to return to ")
    assert not (machine.base / "default").exists()


@pytest.mark.parametrize(
    ("setting", "reason"),
    [
        ("MCP_SESSION_TIMEOUT_SECONDS=59", "at least 60"),
        ("MCP_SESSION_TIMEOUT_SECONDS=soon", "whole number"),
        ("MCP_STARTUP_WAIT_SECONDS=-1", "at least 0"),
        ("MCP_SESSION_CLEANUP_INTERVAL_SECONDS=0", "at least 1"),
        ("CONNX_LICENSE_PORT=70000", "at most 65535"),
        ("MCP_PATH=mcp", "starts with /"),
        ("MCP_TRUSTED_ORIGINS=https://a.example.com/path", "origin"),
        ("CONNX_LICENSE_HOST=http://lic.example.com", "host name"),
        ("CDD_PASSWORD=a;b", ";"),
    ],
)
def test_a_value_outside_its_domain_is_refused_with_the_reason(
    cli: Cli, machine: Machine, cdd: Path, setting: str, reason: str
) -> None:
    code, body = cli.json("configure", *complete(cdd, "--set", setting))

    assert code == 2
    assert setting.partition("=")[0] in body["error"]["message"]
    assert reason in body["error"]["message"]


def test_a_schema_key_in_another_case_is_validated(cli: Cli, machine: Machine, cdd: Path) -> None:
    code, body = cli.json("configure", *complete(cdd, "--set", "mcp_path=bad"))

    assert code == 2
    assert "starts with /" in body["error"]["message"]


def test_a_schema_key_in_another_case_is_stored_under_its_own_name(cli: Cli, machine: Machine, cdd: Path) -> None:
    code, body = cli.json("configure", *complete(cdd, "--set", "Mcp_Path=/sql"))

    assert code == 0, body
    assert read_env(machine.base / "default" / "server.env")["MCP_PATH"] == "/sql"
    assert not any("unvalidated" in w for w in body["result"]["warnings"])

    code, body = cli.json("configure", "--no-input", "--unset", "mcp_path")

    assert code == 0, body
    assert "MCP_PATH" not in read_env(machine.base / "default" / "server.env")


def test_every_invalid_value_is_reported_at_once(cli: Cli, machine: Machine, cdd: Path) -> None:
    code, body = cli.json("configure", *complete(cdd, "--set", "MCP_PATH=x", "--set", "MCP_SESSION_TIMEOUT_SECONDS=1"))

    assert code == 2
    assert "MCP_PATH" in body["error"]["message"]
    assert "MCP_SESSION_TIMEOUT_SECONDS" in body["error"]["message"]


def test_a_value_with_quotes_and_dollars_survives_the_file(cli: Cli, machine: Machine, cdd: Path) -> None:
    code, body = cli.json("configure", *complete(cdd, "--set", f"CDD_USERNAME={TRICKY_USERNAME}"))

    assert code == 0, body
    code, body = cli.json("configure", "--show")
    assert body["result"]["server_settings"]["CDD_USERNAME"] == TRICKY_USERNAME


@pytest.mark.parametrize("key", ["CDD_USERNAME", "MCP_FUTURE_KNOB", "CDD_PASSWORD"])
@pytest.mark.parametrize("brk", ["\n", "\r", "\r\n", " ", "\x85", "\x0b", "\x00"])
def test_a_value_with_a_line_break_cannot_inject_another_setting(
    cli: Cli, machine: Machine, cdd: Path, key: str, brk: str
) -> None:
    code, body = cli.json("configure", *complete(cdd, "--set", f"{key}=a{brk}CDD_USERNAME=evil"))

    assert code == 2
    assert key in body["error"]["message"]
    assert "evil" not in json.dumps(body)
    assert not (machine.base / "default").exists()


def test_a_set_of_a_bare_value_is_not_echoed(cli: Cli, machine: Machine, cdd: Path) -> None:
    mistyped = "pa$$ word!"
    code, body = cli.json("configure", *complete(cdd, "--set", mistyped))

    assert code == 2
    assert "KEY=VALUE" in body["error"]["message"]
    assert mistyped not in json.dumps(body)


# --- Retired and unknown keys ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "key", ["CONNX_BASE_URL", "CONNX_RELATIVE_CDD_PATH_AND_NAME", "REQUESTS_VERIFY_SSL", "QUERY_ENDPOINT"]
)
def test_a_retired_key_is_refused_by_name(cli: Cli, machine: Machine, cdd: Path, key: str) -> None:
    code, body = cli.json("configure", *complete(cdd, "--set", f"{key}=x"))

    assert code == 2
    assert key in body["error"]["message"]
    assert "retired" in body["error"]["message"]


@pytest.mark.parametrize("key", ["CONNX_BASE_URL", "MCP_PORT", "MCP_CDD_FILE", "ACCEPT_EULA"])
def test_a_refused_key_in_a_hand_edited_environment_can_be_unset(
    cli: Cli, machine: Machine, cdd: Path, key: str
) -> None:
    cli.json("configure", *complete(cdd))
    server_env = machine.base / "default" / "server.env"
    server_env.write_text(server_env.read_text(encoding="utf-8") + f"{key}=x\n", encoding="utf-8")

    code, body = cli.json("configure", "--no-input", "--unset", key)

    assert code == 0, body
    assert key not in read_env(server_env)


def test_an_unknown_key_is_accepted_with_an_unvalidated_warning(cli: Cli, machine: Machine, cdd: Path) -> None:
    code, body = cli.json("configure", *complete(cdd, "--set", "MCP_FUTURE_KNOB=7"))

    assert code == 0, body
    assert (
        "--set MCP_FUTURE_KNOB: it is not a setting this connx-mcp-ce knows, so it is stored unvalidated."
        in body["result"]["warnings"]
    )
    assert read_env(machine.base / "default" / "server.env")["MCP_FUTURE_KNOB"] == "7"


def test_the_unknown_key_warning_reaches_a_person_too(cli: Cli, machine: Machine, cdd: Path) -> None:
    result = cli("configure", *complete(cdd, "--set", "MCP_FUTURE_KNOB=7"))

    assert result.exit_code == 0, result.output
    assert "MCP_FUTURE_KNOB" in result.output
    assert "unvalidated" in result.output


def test_a_dependent_key_without_its_dependency_is_warned_about(cli: Cli, machine: Machine, cdd: Path) -> None:
    code, body = cli.json("configure", *complete(cdd, "--set", "CONNX_LICENSE_PORT=7501"))

    assert code == 0, body
    assert any("CONNX_LICENSE_PORT" in w and "CONNX_LICENSE_HOST" in w for w in body["result"]["warnings"])


def test_an_unset_licence_host_is_warned_about(cli: Cli, machine: Machine, cdd: Path) -> None:
    code, body = cli.json("configure", *complete(cdd))
    assert code == 0, body
    assert any("CONNX_LICENSE_HOST" in w for w in body["result"]["warnings"])

    code, body = cli.json("configure", "--no-input", "--set", "CONNX_LICENSE_HOST=host.docker.internal")
    assert code == 0, body
    assert not any("CONNX_LICENSE_HOST" in w for w in body["result"]["warnings"])


def test_a_cross_field_rule_sees_the_host_settings_too(
    cli: Cli, machine: Machine, cdd: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def rule(facts: settings.Facts) -> list[settings.Problem]:
        """A rule shaped like the certificate checks: a server value is wrong for a host setting."""
        if facts.server.get("MCP_PATH") == "/sql" and facts.host.cdd_file:
            return [settings.Problem("MCP_PATH", f"MCP_PATH /sql cannot serve {Path(facts.host.cdd_file).name}.")]
        return []

    monkeypatch.setattr(settings, "RULES", [*settings.RULES, rule])

    code, body = cli.json("configure", *complete(cdd, "--set", "MCP_PATH=/sql"))

    assert code == 2
    assert "cannot serve sales.cdd" in body["error"]["message"]


# --- Missing required values ----------------------------------------------------------------------------


def test_no_input_with_required_values_missing_lists_exactly_the_missing_fields(cli: Cli, machine: Machine) -> None:
    code, body = cli.json("configure", "--no-input", "--set", "CDD_USERNAME=mcp")

    assert code == 3
    assert body["error"]["code"] == "input_required"
    message = body["error"]["message"]
    assert "CDD_PASSWORD" in message
    assert "--cdd" in message
    assert "CDD_USERNAME" not in message
    assert "CONNX_LICENSE_HOST" not in message
    assert body["error"]["next"].startswith("connx-mcp-ce configure")
    assert not (machine.base / "default").exists()


def test_unsetting_a_required_value_is_refused_naming_it(cli: Cli, machine: Machine, cdd: Path) -> None:
    cli.json("configure", *complete(cdd))

    code, body = cli.json("configure", "--no-input", "--unset", "CDD_USERNAME")

    assert code == 3
    assert "CDD_USERNAME" in body["error"]["message"]
    assert read_env(machine.base / "default" / "server.env")["CDD_USERNAME"] == "mcp"


# --- Owner-only permissions -----------------------------------------------------------------------------


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX mode bits")
def test_the_server_settings_file_is_readable_only_by_the_owner(cli: Cli, machine: Machine, cdd: Path) -> None:
    old = os.umask(0o022)
    try:
        cli.json("configure", *complete(cdd))
        cli.json("configure", "--no-input", "--set", "MCP_PATH=/sql")
    finally:
        os.umask(old)

    directory = machine.base / "default"
    assert stat.S_IMODE((directory / "server.env").stat().st_mode) == 0o600
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX mode bits")
def test_the_base_directory_is_readable_only_by_the_owner(cli: Cli, machine: Machine, cdd: Path) -> None:
    old = os.umask(0o022)
    try:
        code, body = cli.json("configure", *complete(cdd))
    finally:
        os.umask(old)

    assert code == 0, body
    assert stat.S_IMODE(machine.base.stat().st_mode) == 0o700


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX mode bits")
def test_a_widened_server_settings_file_is_replaced_owner_only(
    cli: Cli, machine: Machine, cdd: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cli.json("configure", *complete(cdd))
    server_env = machine.base / "default" / "server.env"
    server_env.chmod(0o644)
    modes: list[int] = []
    real_replace = os.replace

    def watch(src: str | os.PathLike[str], dst: str | os.PathLike[str]) -> None:
        modes.append(stat.S_IMODE(os.stat(src).st_mode))
        real_replace(src, dst)

    monkeypatch.setattr(os, "replace", watch)

    code, body = cli.json("configure", "--no-input", "--set", "MCP_PATH=/sql")

    assert code == 0, body
    assert modes == [0o600]
    assert stat.S_IMODE(server_env.stat().st_mode) == 0o600
    assert sorted(p.name for p in server_env.parent.iterdir()) == ["host.json", "server.env"]


def test_on_windows_icacls_drops_inherited_access_and_grants_only_the_user(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The real icacls path runs in the Windows live suite (docs/release-guide.md); here a fake runner records it.
    monkeypatch.setenv("USERNAME", "alice")
    folder = tmp_path / "env"
    folder.mkdir()
    file = folder / "server.env"
    file.write_text("", encoding="utf-8")
    calls: list[list[str]] = []

    def runner(args: list[str], **_: object) -> None:
        calls.append(args)

    environment.restrict_to_owner(folder, windows=True, run=runner)
    environment.restrict_to_owner(file, windows=True, run=runner)

    assert calls == [
        ["icacls", str(folder), "/inheritance:r", "/grant:r", "alice:(OI)(CI)F"],
        ["icacls", str(file), "/inheritance:r", "/grant:r", "alice:F"],
    ]


@pytest.mark.live
@pytest.mark.skipif(sys.platform != "win32", reason="the real icacls path; Windows live suite only")
def test_on_windows_the_settings_folder_acl_names_only_the_user(tmp_path: Path) -> None:
    folder = tmp_path / "env"
    folder.mkdir()

    environment.restrict_to_owner(folder)

    acl = subprocess.run(["icacls", str(folder)], capture_output=True, text=True, check=True).stdout
    entries = [line.strip() for line in acl.splitlines()[:-1] if ":" in line]
    assert entries, acl
    assert all(os.environ["USERNAME"].lower() in entry.lower() for entry in entries), acl
    assert "(I)" not in acl, acl


# --- Secrets kept out of argv ------------------------------------------------------------------------------


def test_set_key_dash_reads_the_value_from_stdin(cli: Cli, machine: Machine, cdd: Path) -> None:
    code, body = cli.json(
        "configure", "--no-input", "--set", "CDD_USERNAME=mcp", "--set", "CDD_PASSWORD=-", "--cdd", str(cdd),
        input=PASSWORD + "\n",
    )  # fmt: skip

    assert code == 0, body
    assert read_env(machine.base / "default" / "server.env")["CDD_PASSWORD"] == PASSWORD
    assert not [w for w in body["result"]["warnings"] if "history" in w]
    assert PASSWORD not in json.dumps(body)


def test_set_with_a_secret_value_warns_it_is_in_argv_and_names_key_dash(cli: Cli, cdd: Path) -> None:
    code, body = cli.json("configure", *complete(cdd))

    assert code == 0, body
    warning = next(w for w in body["result"]["warnings"] if "CDD_PASSWORD" in w)
    assert "shell history" in warning
    assert "--set CDD_PASSWORD=-" in warning
    assert PASSWORD not in warning


def test_set_with_a_plain_or_empty_value_does_not_warn(cli: Cli, cdd: Path) -> None:
    code, body = cli.json(
        "configure", "--no-input", "--set", "CDD_USERNAME=mcp", "--set", "CDD_PASSWORD=", "--cdd", str(cdd),
    )  # fmt: skip

    assert code == 0, body
    assert not [w for w in body["result"]["warnings"] if "history" in w]


def test_set_key_dash_takes_one_key_only(cli: Cli, machine: Machine, cdd: Path) -> None:
    code, body = cli.json(
        "configure", "--no-input", "--set", "CDD_USERNAME=-", "--set", "CDD_PASSWORD=-", "--cdd", str(cdd),
        input=PASSWORD,
    )  # fmt: skip

    assert code == 2, body
    assert "CDD_USERNAME" in body["error"]["message"] and "CDD_PASSWORD" in body["error"]["message"]
    assert not (machine.base / "default").exists()


def test_set_key_dash_from_a_keyboard_is_refused_before_it_shows(cli: Cli, machine: Machine, cdd: Path) -> None:
    class Keyboard(io.BytesIO):
        def isatty(self) -> bool:
            return True

    result = CliRunner().invoke(  # click's runner: typer's takes no stream input
        typer.main.get_command(app),
        ["configure", "--no-input", "--set", "CDD_PASSWORD=-", "--json"],
        input=Keyboard(PASSWORD.encode()),
        obj=Ports(cli.docker, cli.probe, clock=cli.clock),
    )

    assert result.exit_code == 2, result.output
    message = json.loads(result.stdout)["error"]["message"]
    assert "CDD_PASSWORD=-" in message and "keyboard" in message
    assert not (machine.base / "default").exists()


def test_configure_help_names_set_key_dash(cli: Cli) -> None:
    result = cli("configure", "--help")

    assert "KEY=-" in result.output


# --- Show -----------------------------------------------------------------------------------------------


def test_show_masks_every_secret_in_json(cli: Cli, machine: Machine, cdd: Path) -> None:
    cli.json("configure", *complete(cdd, "--set", "MCP_PATH=/sql", "--set", "MCP_EXTRA_TOKEN=tok-value"))

    code, body = cli.json("configure", "--show")

    assert code == 0, body
    result = body["result"]
    assert result["environment"] == "default"
    assert result["server_settings"]["CDD_PASSWORD"] == "***hidden***"
    assert result["server_settings"]["MCP_EXTRA_TOKEN"] == "***hidden***"
    assert result["server_settings"]["MCP_PATH"] == "/sql"
    assert result["host_settings"] == {"cdd_file": str(cdd)}
    assert PASSWORD not in json.dumps(body)
    assert "tok-value" not in json.dumps(body)


def test_show_masks_every_secret_in_human_output(cli: Cli, machine: Machine, cdd: Path) -> None:
    cli.json("configure", *complete(cdd))

    result = cli("configure", "--show")

    assert result.exit_code == 0, result.output
    assert "CDD_PASSWORD" in result.output
    assert "***hidden***" in result.output
    assert PASSWORD not in result.output
    assert "CDD_USERNAME" in result.output
    assert str(cdd) in result.output


def test_configure_never_prints_a_secret(cli: Cli, machine: Machine, cdd: Path) -> None:
    human = cli("configure", *complete(cdd))
    code, body = cli.json("configure", "sales", *complete(cdd))

    assert PASSWORD not in human.output
    assert PASSWORD not in json.dumps(body)


def test_a_refused_secret_is_never_printed(cli: Cli, machine: Machine, cdd: Path) -> None:
    refused = "refused-secret;value"
    human = cli("configure", *complete(cdd, "--set", f"CDD_PASSWORD={refused}"))
    code, body = cli.json("configure", *complete(cdd, "--set", f"CDD_PASSWORD={refused}"))

    assert code == 2
    assert "CDD_PASSWORD" in body["error"]["message"]
    assert refused not in human.output
    assert refused not in json.dumps(body)


def test_show_of_a_missing_environment_names_the_command_that_creates_it(cli: Cli, machine: Machine) -> None:
    code, body = cli.json("configure", "sales", "--show")

    assert code == 20
    assert body["error"]["code"] == "environment_not_found"
    assert body["error"]["next"] == "connx-mcp-ce configure sales"


def test_show_cannot_be_combined_with_changes(cli: Cli, machine: Machine, cdd: Path) -> None:
    cli.json("configure", *complete(cdd))

    code, body = cli.json("configure", "--show", "--set", "MCP_PATH=/x")

    assert code == 2


# --- Live: compose reads the file as written ------------------------------------------------------------


@pytest.mark.live
def test_compose_reads_the_server_settings_file_verbatim(cli: Cli, machine: Machine, cdd: Path, tmp_path: Path) -> None:
    code, body = cli.json("configure", *complete(cdd, "--set", f"CDD_USERNAME={TRICKY_USERNAME}"))
    assert code == 0, body
    compose = tmp_path / "compose.yml"
    server_env = machine.base / "default" / "server.env"
    compose.write_text(f"services:\n  probe:\n    image: busybox\n    env_file: {json.dumps(str(server_env))}\n")

    # What the container receives, not `compose config`'s rendering (which escapes a literal `$` as `$$`).
    def received(key: str) -> str:
        args = ["compose", "-f", str(compose), "run", "--rm", "--no-deps", "probe", "printenv", key]
        result = SubprocessDocker().run(args)
        assert result.returncode == 0, result.stderr
        return result.stdout.removesuffix("\n")

    assert received("CDD_USERNAME") == TRICKY_USERNAME
    assert received("CDD_PASSWORD") == PASSWORD
