#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_import_export.py - The administrator imports and exports environments.

Description:
    Through the CLI seam: `configure --from-env-file` validates every key of a
    server-format settings file, flags given alongside win, retired and invalid keys are reported by
    name and line, and a safe export imports with its secrets asked at start. The rules a line shares
    with `--set` (value refusals, precedence, ask-at-start marking) are pinned in test_edit.py.
    `export` writes the server settings in the server's env-file format without secrets and the
    host settings as comments; `export --include-secrets --output` then an import on a fresh base
    directory recreates the environment.

Copyright:
    Copyright 2026 Software GmbH
    Licensed under the Apache License, Version 2.0 (the "License"); you may not use this file except in compliance
    with the License. You may obtain a copy of the License at https://www.apache.org/licenses/LICENSE-2.0
    Unless required by applicable law or agreed to in writing, software distributed under the License is distributed
    on an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the License
    for the specific language governing permissions and limitations under the License.
"""

from __future__ import annotations

import dataclasses
import json
import stat
import sys
from pathlib import Path

import pytest

from connx_mcp import settings
from connx_mcp.environment import parse_env
from tests.conftest import Cli, Machine, as_typed
from tests.test_configure import PASSWORD, complete, read_env
from tests.test_demo_binding import demo

SECRET = "client-secret-for-tests-only"


@pytest.fixture
def cdd(tmp_path: Path) -> Path:
    path = tmp_path / "dictionaries" / "sales.cdd"
    path.parent.mkdir()
    path.write_bytes(b"CDD")
    return path


def settings_file(tmp_path: Path, text: str, name: str = "sent.env") -> Path:
    path = tmp_path / name
    path.write_bytes(text.encode("utf-8"))
    return path


def stored(machine: Machine, name: str = "default") -> tuple[dict[str, str], dict[str, object]]:
    directory = machine.base / name
    host = json.loads((directory / "host.json").read_text(encoding="utf-8"))
    return read_env(directory / "server.env"), host


# --- Import ------------------------------------------------------------------------------------------------


def test_an_imported_file_creates_the_environment(cli: Cli, machine: Machine, cdd: Path, tmp_path: Path) -> None:
    sent = settings_file(
        tmp_path,
        "# from a colleague\n"
        "CDD_USERNAME=mcp\n"
        f"CDD_PASSWORD='{PASSWORD}'\n"
        "\n"
        'MCP_PATH="/sql"\n'
        "export MCP_SESSION_TIMEOUT_SECONDS = 600   # ten minutes\n",
    )

    code, body = cli.json("configure", "--no-input", "--from-env-file", str(sent), "--cdd", str(cdd))

    assert code == 0, body
    assert body["result"]["created"] is True
    server, host = stored(machine)
    assert server == {
        "CDD_USERNAME": "mcp",
        "CDD_PASSWORD": PASSWORD,
        "MCP_PATH": "/sql",
        "MCP_SESSION_TIMEOUT_SECONDS": "600",
    }
    assert host == {"cdd_file": str(cdd)}


def test_flags_given_alongside_the_file_win(cli: Cli, machine: Machine, cdd: Path, tmp_path: Path) -> None:
    sent = settings_file(
        tmp_path,
        f"CDD_USERNAME=mcp\nCDD_PASSWORD={PASSWORD}\nMCP_PATH=no-leading-slash\nMCP_SESSION_TIMEOUT_SECONDS=600\n",
    )

    code, body = cli.json(
        "configure", "--no-input", "--from-env-file", str(sent), "--cdd", str(cdd),
        "--set", "MCP_PATH=/sql", "--unset", "MCP_SESSION_TIMEOUT_SECONDS", "--set", "cdd_username=analyst",
    )  # fmt: skip

    assert code == 0, body
    server, _ = stored(machine)
    assert server == {"CDD_USERNAME": "analyst", "CDD_PASSWORD": PASSWORD, "MCP_PATH": "/sql"}


def test_refusals_come_file_first_then_flags_then_ask_at_start_then_cross_field_rules(
    cli: Cli, machine: Machine, cdd: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def rule(facts: settings.Facts) -> list[settings.Problem]:
        """A cross-field rule that refuses every environment."""
        return [settings.Problem("MCP_PATH", "the cross-field rule refuses it.")]

    monkeypatch.setattr(settings, "RULES", [*settings.RULES, rule])
    sent = settings_file(tmp_path, f"CDD_USERNAME=mcp\nCDD_PASSWORD={PASSWORD}\nMCP_PATH=no-leading-slash\n")

    code, body = cli.json(
        "configure", "--no-input", "--from-env-file", str(sent), "--cdd", str(cdd),
        "--ask-at-start", "CDD_USERNAME", "--port", "0", "--set", "MCP_SESSION_TIMEOUT_SECONDS=soon",
    )  # fmt: skip

    assert code == 2
    lines = body["error"]["message"].splitlines()
    assert [lines.index(next(x for x in lines if part in x)) for part in [
        f"{sent}, line 3", "MCP_SESSION_TIMEOUT_SECONDS", "--port 0: ", "--ask-at-start CDD_USERNAME: it is not a secret",
        "the cross-field rule refuses it",
    ]] == [0, 1, 2, 3, 4]  # fmt: skip
    assert not (machine.base / "default").exists()


def test_retired_and_invalid_keys_are_reported_by_name(cli: Cli, machine: Machine, cdd: Path, tmp_path: Path) -> None:
    sent = settings_file(
        tmp_path,
        f"CDD_USERNAME=mcp\nCDD_PASSWORD={PASSWORD}\n"
        "CONNX_BASE_URL=http://old\n"
        "SALES_ENDPOINT=http://older\n"
        "MCP_PORT=9000\n"
        "MCP_CDD_FILE=/srv/x.cdd\n"
        "not a setting\n"
        "MCP_SESSION_TIMEOUT_SECONDS=soon\n",
    )

    code, body = cli.json("configure", "--no-input", "--from-env-file", str(sent), "--cdd", str(cdd))

    assert code == 2
    message = body["error"]["message"]
    for line, name in [(3, "CONNX_BASE_URL"), (4, "SALES_ENDPOINT"), (5, "MCP_PORT"), (6, "MCP_CDD_FILE"),
                       (8, "MCP_SESSION_TIMEOUT_SECONDS")]:  # fmt: skip
        assert f"line {line} ({name}): " in message
    assert "line 7" in message
    assert "retired" in message
    assert not (machine.base / "default").exists()


def test_unknown_keys_are_imported_with_a_warning(cli: Cli, machine: Machine, cdd: Path, tmp_path: Path) -> None:
    sent = settings_file(tmp_path, f"CDD_USERNAME=mcp\nCDD_PASSWORD={PASSWORD}\nMCP_FUTURE_KNOB=on\n")

    code, body = cli.json("configure", "--no-input", "--from-env-file", str(sent), "--cdd", str(cdd))

    assert code == 0, body
    assert any("MCP_FUTURE_KNOB" in w for w in body["result"]["warnings"])
    assert stored(machine)[0]["MCP_FUTURE_KNOB"] == "on"


@pytest.mark.parametrize(
    "line",
    [
        'MCP_PATH="/a\nMCP_LOG_LEVEL=DEBUG"',
        'MCP_PATH="/a\\nMCP_LOG_LEVEL=DEBUG"',
        "MCP_PATH=/a\x00b",
        "MCP_PATH=/a MCP_LOG_LEVEL=DEBUG",
        "MCP_PATH='/a\rb'",
    ],
    ids=["quoted-newline", "escaped-newline", "nul", "line-separator", "carriage-return"],
)
def test_a_value_holding_a_line_break_or_nul_is_refused(
    cli: Cli, machine: Machine, cdd: Path, tmp_path: Path, line: str
) -> None:
    sent = settings_file(tmp_path, f"CDD_USERNAME=mcp\nCDD_PASSWORD={PASSWORD}\n{line}\n")

    code, body = cli.json("configure", "--no-input", "--from-env-file", str(sent), "--cdd", str(cdd))

    assert code == 2, body
    assert "(MCP_PATH): it cannot hold a line break or NUL character" in body["error"]["message"]
    assert not (machine.base / "default").exists()


def test_an_unclosed_quote_is_reported(cli: Cli, machine: Machine, cdd: Path, tmp_path: Path) -> None:
    sent = settings_file(tmp_path, f"CDD_USERNAME=mcp\nCDD_PASSWORD='{PASSWORD}\n")

    code, body = cli.json("configure", "--no-input", "--from-env-file", str(sent), "--cdd", str(cdd))

    assert code == 2
    assert "line 2: CDD_PASSWORD" in body["error"]["message"]
    assert PASSWORD not in body["error"]["message"]


TWICE = "CDD_USERNAME=mcp\nCDD_PASSWORD={password}\nMCP_PATH=/a\nmcp_path=/b\n"


def test_a_key_given_twice_is_imported_from_its_last_line_with_a_warning(
    cli: Cli, machine: Machine, cdd: Path, tmp_path: Path
) -> None:
    sent = settings_file(tmp_path, TWICE.format(password=PASSWORD))

    code, body = cli.json("configure", "--no-input", "--from-env-file", str(sent), "--cdd", str(cdd))

    assert code == 0, body
    assert f"{sent}, line 4 (MCP_PATH): it is given again (line 3 is ignored)." in body["result"]["warnings"]
    assert stored(machine)[0]["MCP_PATH"] == "/b"


def test_the_warning_of_a_key_given_twice_is_shown_in_the_human_output(
    cli: Cli, machine: Machine, cdd: Path, tmp_path: Path
) -> None:
    sent = settings_file(tmp_path, TWICE.format(password=PASSWORD))

    human = cli("configure", "--no-input", "--from-env-file", str(sent), "--cdd", str(cdd))

    assert human.exit_code == 0, human.output
    assert f"Warning: {sent}, line 4 (MCP_PATH): it is given again (line 3 is ignored)." in human.output


def test_a_flag_beats_a_key_given_twice_in_the_file_and_the_warning_is_still_shown(
    cli: Cli, machine: Machine, cdd: Path, tmp_path: Path
) -> None:
    sent = settings_file(tmp_path, f"CDD_USERNAME=mcp\nCDD_PASSWORD={PASSWORD}\nMCP_PATH=/a\nMCP_PATH=/b\n")

    code, body = cli.json(
        "configure", "--no-input", "--from-env-file", str(sent), "--cdd", str(cdd), "--set", "MCP_PATH=/flag"
    )

    assert code == 0, body
    assert f"{sent}, line 4 (MCP_PATH): it is given again (line 3 is ignored)." in body["result"]["warnings"]
    assert stored(machine)[0]["MCP_PATH"] == "/flag"


def test_the_ignored_line_of_a_key_given_twice_is_not_validated(
    cli: Cli, machine: Machine, cdd: Path, tmp_path: Path
) -> None:
    sent = settings_file(
        tmp_path, f"CDD_USERNAME=mcp\nMCP_PATH=no-leading-slash\nCDD_PASSWORD={PASSWORD}\nMCP_PATH=/sql\n"
    )

    code, body = cli.json("configure", "--no-input", "--from-env-file", str(sent), "--cdd", str(cdd))

    assert code == 0, body
    assert f"{sent}, line 4 (MCP_PATH): it is given again (line 2 is ignored)." in body["result"]["warnings"]
    assert stored(machine)[0]["MCP_PATH"] == "/sql"


def test_a_lone_dollar_on_a_line_a_later_one_replaces_is_not_warned_about(
    cli: Cli, machine: Machine, cdd: Path, tmp_path: Path
) -> None:
    sent = settings_file(tmp_path, f"CDD_USERNAME=mcp\nCDD_PASSWORD=a$b\nCDD_PASSWORD={PASSWORD}\n")

    code, body = cli.json("configure", "--no-input", "--from-env-file", str(sent), "--cdd", str(cdd))

    assert code == 0, body
    assert f"{sent}, line 3 (CDD_PASSWORD): it is given again (line 2 is ignored)." in body["result"]["warnings"]
    assert not [w for w in body["result"]["warnings"] if "lone $" in w]
    assert stored(machine)[0]["CDD_PASSWORD"] == PASSWORD


def test_a_parse_error_between_two_lines_of_a_key_is_the_only_refusal(
    cli: Cli, machine: Machine, cdd: Path, tmp_path: Path
) -> None:
    sent = settings_file(
        tmp_path, f"CDD_USERNAME=mcp\nCDD_PASSWORD={PASSWORD}\nMCP_PATH=/x\nMCP_PATH='/a' junk\nMCP_PATH=/b\n"
    )

    code, body = cli.json("configure", "--no-input", "--from-env-file", str(sent), "--cdd", str(cdd))

    assert code == 2, body
    assert body["error"]["message"] == f"{sent}, line 4: MCP_PATH has text after its closing quote."
    assert not (machine.base / "default").exists()


@pytest.mark.parametrize("problem", ["missing", "directory", "not-utf8"])
def test_an_unreadable_file_fails_with_its_own_code(cli: Cli, machine: Machine, tmp_path: Path, problem: str) -> None:
    path = tmp_path / "sent.env"
    if problem == "directory":
        path.mkdir()
    elif problem == "not-utf8":
        path.write_bytes(b"MCP_PATH=/\xff\n")

    code, body = cli.json("configure", "--no-input", "--from-env-file", str(path))

    assert code == 110
    assert body["error"]["code"] == "import_file_unreadable"
    assert str(path) in body["error"]["message"]


def test_an_import_into_an_existing_environment_keeps_what_the_file_leaves_out(
    cli: Cli, machine: Machine, cdd: Path, tmp_path: Path
) -> None:
    cli.json("configure", *complete(cdd, "--set", "MCP_PATH=/sql"))
    sent = settings_file(tmp_path, "MCP_SESSION_TIMEOUT_SECONDS=600\n")

    code, body = cli.json("configure", "--no-input", "--from-env-file", str(sent))

    assert code == 0, body
    assert body["result"]["created"] is False
    assert body["result"]["changed"] == ["MCP_SESSION_TIMEOUT_SECONDS"]
    server, host = stored(machine)
    assert server == {
        "CDD_USERNAME": "mcp",
        "CDD_PASSWORD": PASSWORD,
        "MCP_PATH": "/sql",
        "MCP_SESSION_TIMEOUT_SECONDS": "600",
    }
    assert "ask_at_start" not in host


# --- Export ------------------------------------------------------------------------------------------------


def configured(cli: Cli, cdd: Path, *extra: str, future_secret: str = "--set") -> None:
    code, body = cli.json(
        "configure",
        *complete(cdd, "--set", "MCP_PATH=/it's", "--port", "8001", *extra),
        *(
            ["--set", "MCP_FUTURE_KEY_SECRET=x"]
            if future_secret == "--set"
            else ["--ask-at-start", "MCP_FUTURE_KEY_SECRET"]
        ),
    )
    assert code == 0, body


def test_export_writes_server_settings_without_secrets_and_host_settings_as_comments(
    cli: Cli, machine: Machine, cdd: Path
) -> None:
    configured(cli, cdd)

    result = cli("export")

    assert result.exit_code == 0, result.output
    text = result.stdout
    assert PASSWORD not in text
    lines = text.splitlines()
    assert "CDD_USERNAME=mcp" in lines
    assert 'MCP_PATH="/it\'s"' in lines
    assert f"# host cdd_file = {json.dumps(str(cdd))}" in lines
    assert "# host published_port = 8001" in lines
    assert not any(line.startswith(("CDD_PASSWORD", "MCP_FUTURE_KEY_SECRET")) for line in lines)
    assert any(line.startswith("# secret CDD_PASSWORD") for line in lines)
    assert any(line.startswith("# secret MCP_FUTURE_KEY_SECRET") for line in lines)


def test_export_in_json_carries_the_content_and_what_was_left_out(cli: Cli, machine: Machine, cdd: Path) -> None:
    configured(cli, cdd)

    code, body = cli.json("export")

    assert code == 0, body
    assert body["result"]["secrets_included"] is False
    assert body["result"]["secrets_left_out"] == ["CDD_PASSWORD", "MCP_FUTURE_KEY_SECRET"]
    assert "CDD_USERNAME=mcp" in body["result"]["content"]
    assert PASSWORD not in json.dumps(body)


def test_exporting_a_missing_environment_fails(cli: Cli, machine: Machine) -> None:
    code, body = cli.json("export", "sales")

    assert code == 20
    assert body["error"]["next"] == "connx-mcp-ce configure sales"


@pytest.mark.parametrize("json_mode", [False, True], ids=["human", "json"])
def test_secrets_are_never_printed_only_written_to_a_file(
    cli: Cli, machine: Machine, cdd: Path, json_mode: bool
) -> None:
    configured(cli, cdd)

    result = cli("export", "--include-secrets", *(["--json"] if json_mode else []))

    assert result.exit_code == 2
    assert PASSWORD not in result.output
    assert "--output" in result.output


def test_an_exported_file_is_owner_only_and_names_where_it_went(
    cli: Cli, machine: Machine, cdd: Path, tmp_path: Path
) -> None:
    configured(cli, cdd)
    target = tmp_path / "out" / "sales.env"
    target.parent.mkdir()

    code, body = cli.json("export", "--include-secrets", "--output", str(target))

    assert code == 0, body
    assert body["result"]["file"] == str(target)
    assert body["result"]["secrets_included"] is True
    assert "content" not in body["result"]
    assert PASSWORD not in json.dumps(body)
    assert f"CDD_PASSWORD={PASSWORD}" in target.read_text(encoding="utf-8").splitlines()
    if sys.platform != "win32":
        assert stat.S_IMODE(target.stat().st_mode) == 0o600


def test_an_unwritable_output_fails_with_its_own_code(cli: Cli, machine: Machine, cdd: Path, tmp_path: Path) -> None:
    configured(cli, cdd)

    code, body = cli.json("export", "--output", str(tmp_path / "no-such-dir" / "x.env"))

    assert code == 111
    assert body["error"]["code"] == "export_write_failed"


# --- Round trips ---------------------------------------------------------------------------------------------


def move_to_a_fresh_machine(machine: Machine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    fresh = tmp_path / "other-machine"
    monkeypatch.setenv("MCP_ENVIRONMENT_PATH", str(fresh))
    return fresh


def test_export_with_secrets_then_import_recreates_the_environment_on_another_machine(
    cli: Cli, machine: Machine, cdd: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    configured(cli, cdd, "--set", "CONNX_LICENSE_HOST=host.docker.internal", "--set", "MCP_SESSION_TIMEOUT_SECONDS=600")
    host_file = machine.base / "default" / "host.json"
    newer = {**json.loads(host_file.read_text(encoding="utf-8")), "future_knob": {"nested": [1, 2]}}
    host_file.write_text(json.dumps(newer), encoding="utf-8")
    cli.json("configure", "--no-input", "--set", 'CDD_PASSWORD=it\'s $HOME \\ "quoted"')
    original_host = stored(machine)[1]
    original_raw = (machine.base / "default" / "server.env").read_text(encoding="utf-8")
    moved = tmp_path / "moved.env"
    assert cli.json("export", "--include-secrets", "--output", str(moved))[0] == 0

    fresh = move_to_a_fresh_machine(machine, tmp_path, monkeypatch)
    code, body = cli.json("configure", "sales", "--no-input", "--from-env-file", str(moved))

    assert code == 0, body
    assert (fresh / "sales" / "server.env").read_text(encoding="utf-8") == original_raw
    # A key only a newer connx-mcp-ce knows stays in its own host.json; the file names it but never carries it.
    assert original_host.pop("future_knob") == {"nested": [1, 2]}
    assert "# host.json key 'future_knob' left out" in moved.read_text(encoding="utf-8")
    assert json.loads((fresh / "sales" / "host.json").read_text(encoding="utf-8")) == original_host
    assert parse_env(original_raw)["CDD_PASSWORD"] == 'it\'s $HOME \\ "quoted"'


def test_a_safe_export_imports_with_its_secrets_asked_at_start(
    cli: Cli, machine: Machine, cdd: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    configured(cli, cdd, future_secret="--ask-at-start")
    safe = tmp_path / "safe.env"
    assert cli.json("export", "--output", str(safe))[0] == 0

    fresh = move_to_a_fresh_machine(machine, tmp_path, monkeypatch)
    code, body = cli.json("configure", "--no-input", "--from-env-file", str(safe))

    assert code == 0, body
    server = read_env(fresh / "default" / "server.env")
    host = json.loads((fresh / "default" / "host.json").read_text(encoding="utf-8"))
    assert "CDD_PASSWORD" not in server
    assert host["ask_at_start"] == ["CDD_PASSWORD", "MCP_FUTURE_KEY_SECRET"]


def test_the_demo_settings_beat_the_files_and_its_secrets_are_not_asked_at_start(
    cli: Cli, machine: Machine, cdd: Path, tmp_path: Path
) -> None:
    demo(cli, "dex")
    sent = settings_file(
        tmp_path,
        f"CDD_USERNAME=mcp\nCDD_PASSWORD={PASSWORD}\nMCP_PATH=/sql\n"
        "MCP_OAUTH_PROVIDER=nonsense\nMCP_OAUTH_CONFIG_URL=not-a-url\n# secret MCP_OAUTH_CLIENT_SECRET:\n",
    )

    code, body = cli.json("configure", "--no-input", "--from-env-file", str(sent), "--cdd", str(cdd), "--demo", "dex")

    assert code == 0, body
    server, host = stored(machine)
    assert server["MCP_PATH"] == "/sql"
    assert server["MCP_OAUTH_PROVIDER"] == "generic-oidc"
    assert server["MCP_OAUTH_CONFIG_URL"].endswith("/dex/.well-known/openid-configuration")
    assert server["MCP_OAUTH_CLIENT_SECRET"]
    assert "ask_at_start" not in host


def test_the_local_demo_vault_ca_beats_the_files_vault_ca_line_without_judging_it(
    cli: Cli, machine: Machine, cdd: Path, tmp_path: Path
) -> None:
    demo(cli, "keycloak", "vault")
    sent = settings_file(
        tmp_path,
        f"CDD_USERNAME=mcp\nCDD_PASSWORD={PASSWORD}\n# host vault_ca_file = {json.dumps(str(tmp_path / 'no-such-ca.pem'))}\n",
    )

    code, body = cli.json(
        "configure", "--no-input", "--from-env-file", str(sent), "--cdd", str(cdd), "--demo", "keycloak", "--demo-vault"
    )

    assert code == 0, body
    ca = machine.base / ".demo" / "stack" / "generated" / "tls" / "ca.pem"
    assert stored(machine)[1]["vault_ca_file"] == str(ca)


# --- Review fixes: host comments, image settings, one rule set -------------------------------------------------

DIGEST = "softwareagcr.azurecr.io/connx-mcp-server-ce@sha256:" + "a" * 64


def test_a_host_setting_name_in_capitals_is_read_as_its_own_name(
    cli: Cli, machine: Machine, cdd: Path, tmp_path: Path
) -> None:
    sent = settings_file(tmp_path, f"CDD_USERNAME=mcp\nCDD_PASSWORD={PASSWORD}\n# host PUBLISHED_PORT = 8001\n")

    code, body = cli.json("configure", "--no-input", "--from-env-file", str(sent), "--cdd", str(cdd))

    assert code == 0, body
    assert stored(machine)[1]["published_port"] == 8001


def test_a_host_comment_that_is_not_json_names_the_reason(
    cli: Cli, machine: Machine, cdd: Path, tmp_path: Path
) -> None:
    sent = settings_file(tmp_path, f"CDD_USERNAME=mcp\nCDD_PASSWORD={PASSWORD}\n# host published_port = 80x\n")

    code, body = cli.json("configure", "--no-input", "--from-env-file", str(sent), "--cdd", str(cdd))

    assert code == 2
    assert body["error"]["message"] == (
        f"{sent}, line 3: host setting published_port is not a JSON value (extra data at character 3); give a "
        "number, or a string in double quotes."
    )


def test_a_host_comment_naming_a_missing_file_names_the_path(
    cli: Cli, machine: Machine, cdd: Path, tmp_path: Path
) -> None:
    missing = tmp_path / "nowhere.cdd"
    sent = settings_file(
        tmp_path, f"CDD_USERNAME=mcp\nCDD_PASSWORD={PASSWORD}\n# host cdd_file = {json.dumps(str(missing))}\n"
    )

    code, body = cli.json("configure", "--no-input", "--from-env-file", str(sent))

    assert code == 2
    assert body["error"]["message"] == (
        f"{sent}, line 3 (cdd_file): no such file; the line gives {as_typed(missing)}. "
        "Give --cdd to choose it on this machine."
    )


def test_a_host_comment_with_a_single_backslash_says_to_write_it_twice(
    cli: Cli, machine: Machine, cdd: Path, tmp_path: Path
) -> None:
    sent = settings_file(
        tmp_path, f'CDD_USERNAME=mcp\nCDD_PASSWORD={PASSWORD}\n# host cdd_file = "C:\\Users\\x\\sales.cdd"\n'
    )

    code, body = cli.json("configure", "--no-input", "--from-env-file", str(sent), "--cdd", str(cdd))

    assert code == 2
    assert body["error"]["message"] == (
        f"{sent}, line 3: host setting cdd_file is not a JSON value (invalid \\escape at character 4); a backslash "
        "must be written twice (\\\\), or use / in the path."
    )


@pytest.mark.parametrize(
    ("line", "name"),
    [
        ('# host image_digest = "garbage"', "image_digest"),
        ('# host image_digest = "repo@sha256:1234"', "image_digest"),
        ('# host image = "Not An Image"', "image"),
        ('# host image_version = "15.0 beta"', "image_version"),
        ("# host image_version = null", "image_version"),
    ],
    ids=["digest-garbage", "digest-short", "image-spaces", "version-spaces", "null"],
)
def test_imported_image_settings_are_checked(
    cli: Cli, machine: Machine, cdd: Path, tmp_path: Path, line: str, name: str
) -> None:
    sent = settings_file(tmp_path, f"CDD_USERNAME=mcp\nCDD_PASSWORD={PASSWORD}\n{line}\n")

    code, body = cli.json("configure", "--no-input", "--from-env-file", str(sent), "--cdd", str(cdd))

    assert code == 2, body
    assert f"line 3 ({name}): " in body["error"]["message"]
    assert not (machine.base / "default").exists()


def test_an_imported_image_pin_is_kept_and_update_named_to_re_pin_it(
    cli: Cli, machine: Machine, cdd: Path, tmp_path: Path
) -> None:
    sent = settings_file(
        tmp_path,
        f"CDD_USERNAME=mcp\nCDD_PASSWORD={PASSWORD}\n"
        '# host image = "registry.example:5000/team/connx-mcp-server-ce:15.0"\n'
        f'# host image_digest = "{DIGEST}"\n# host image_version = "15.0.3.26012"\n',
    )

    code, body = cli.json("configure", "--no-input", "--from-env-file", str(sent), "--cdd", str(cdd))

    assert code == 0, body
    host = stored(machine)[1]
    assert host["image_digest"] == DIGEST
    assert host["image"] == "registry.example:5000/team/connx-mcp-server-ce:15.0"
    assert any("connx-mcp-ce update" in w for w in body["result"]["warnings"])


@pytest.mark.parametrize(
    ("rule", "flags", "line"),
    [
        ("server_setting_problem", ["--set", "MCP_PATH=/sql"], "MCP_PATH=/sql"),
        ("published_port's parser", ["--port", "8001"], "# host published_port = 8001"),
        ("ask_at_start_problem", ["--ask-at-start", "MCP_FUTURE_KEY_SECRET"], "# secret MCP_FUTURE_KEY_SECRET:"),
    ],
    ids=["server-setting", "host-setting", "ask-at-start"],
)
def test_a_rule_change_shows_in_both_the_flags_and_the_import(
    cli: Cli,
    machine: Machine,
    cdd: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    rule: str,
    flags: list[str],
    line: str,
) -> None:

    if rule == "published_port's parser":  # what a host flag and a `# host` line both go through

        def refuse(raw: str) -> int:
            raise ValueError("refused by the changed rule")

        row = dataclasses.replace(settings.HOST_SCHEMA["published_port"], parse=refuse)
        monkeypatch.setitem(settings.HOST_SCHEMA, "published_port", row)
    else:
        monkeypatch.setattr(settings, rule, lambda *_: "refused by the changed rule")
    sent = settings_file(tmp_path, f"CDD_USERNAME=mcp\nCDD_PASSWORD={PASSWORD}\n{line}\n")

    flag_code, by_flag = cli.json("configure", *complete(cdd, *flags))
    file_code, by_file = cli.json("configure", "--no-input", "--from-env-file", str(sent), "--cdd", str(cdd))

    assert (flag_code, file_code) == (2, 2), (by_flag, by_file)
    assert "refused by the changed rule" in by_flag["error"]["message"]
    assert f"{sent}, line 3 (" in by_file["error"]["message"]
    assert "refused by the changed rule" in by_file["error"]["message"]


def test_a_lone_dollar_is_kept_and_warned_by_key_and_line(
    cli: Cli, machine: Machine, cdd: Path, tmp_path: Path
) -> None:
    sent = settings_file(
        tmp_path,
        "CDD_USERNAME=mcp\nCDD_PASSWORD=\"pa$word\"\nMCP_PATH=/a$$b\nMCP_OAUTH_CLIENT_ID='$literal'\n",
    )

    code, body = cli.json("configure", "--no-input", "--from-env-file", str(sent), "--cdd", str(cdd))

    assert code == 0, body
    warnings = [w for w in body["result"]["warnings"] if "$" in w]
    assert len(warnings) == 1
    assert warnings[0].startswith(f"{sent}, line 2 (CDD_PASSWORD): it holds a lone $, which compose would expand;")
    assert "pa$word" not in json.dumps(body)
    assert stored(machine)[0]["CDD_PASSWORD"] == "pa$word"


def test_export_to_a_dash_prints_and_refuses_secrets(cli: Cli, machine: Machine, cdd: Path) -> None:
    configured(cli, cdd)

    printed = cli("export", "--output", "-")
    refused = cli("export", "--include-secrets", "--output", "-")

    assert printed.exit_code == 0, printed.output
    assert "CDD_USERNAME=mcp" in printed.stdout.splitlines()
    assert refused.exit_code == 2
    assert PASSWORD not in refused.output
    assert not Path("-").exists()


# --- Settings edits: an imported line is judged by its own side, whatever the flags --------------------------


def test_a_host_setting_written_as_a_server_line_is_refused_even_when_its_flag_is_given(
    cli: Cli, machine: Machine, cdd: Path, tmp_path: Path
) -> None:
    sent = settings_file(tmp_path, f"CDD_USERNAME=mcp\nCDD_PASSWORD={PASSWORD}\ncdd_file={cdd}\n")

    without_flag = cli.json("configure", "--no-input", "--from-env-file", str(sent))
    with_flag = cli.json("configure", "--no-input", "--from-env-file", str(sent), "--cdd", str(cdd))

    for code, body in (without_flag, with_flag):
        assert code == 2, body
        assert f"{sent}, line 3 (cdd_file): it is a host setting" in body["error"]["message"]
        assert "--cdd" in body["error"]["message"]
    assert not (machine.base / "default").exists()


def test_imported_stack_ports_without_a_stack_host_are_cleared_as_stored_ones_are(
    cli: Cli, machine: Machine, cdd: Path, tmp_path: Path
) -> None:
    sent = settings_file(
        tmp_path, f'CDD_USERNAME=mcp\nCDD_PASSWORD={PASSWORD}\n# host demo_stack_ports = "keycloak=8181"\n'
    )

    code, body = cli.json("configure", "--no-input", "--from-env-file", str(sent), "--cdd", str(cdd), "--port", "9100")

    assert code == 0, body
    assert stored(machine)[1] == {"cdd_file": str(cdd), "published_port": 9100}


@pytest.mark.parametrize(
    "stack_host, ports",
    [("stack01", "keycloak=9090,dex=1,vault=8200"), ("stack02", "keycloak=8080,dex=1,vault=8200")],
    ids=["same-stack-merged", "other-stack-not-merged"],
)
def test_a_stack_port_flag_merges_with_the_bound_ports_only_for_the_stack_the_files_host_line_keeps(
    cli: Cli, machine: Machine, cdd: Path, tmp_path: Path, stack_host: str, ports: str
) -> None:
    bound = cli.json(
        "configure", *complete(cdd, "--demo", "keycloak", "--stack-host", "stack01", "--stack-port", "keycloak=9090")
    )
    assert bound[0] == 0, bound[1]
    sent = settings_file(tmp_path, f'# host demo_stack_host = "{stack_host}"\n')

    code, body = cli.json("configure", "--from-env-file", str(sent), "--demo", "keycloak", "--stack-port", "dex=1")

    assert code == 0, body
    assert stored(machine)[1]["demo_stack_host"] == stack_host
    assert stored(machine)[1]["demo_stack_ports"] == ports
