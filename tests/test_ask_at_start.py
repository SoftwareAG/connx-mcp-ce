#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_ask_at_start.py - The administrator keeps secrets off disk and overrides values for one start.

Description:
    Through the CLI seam with the fake Docker port, endpoint probe and clock. A secret
    marked ask-at-start is never stored; `start` reads it from the terminal (masked), stdin or a
    named environment variable and hands it to compose through docker's process environment, so it
    never reaches a file. `start --cdd`, `--certs` and `--cdd-username` apply to that run only, and
    `status` says when the container differs from the stored environment. `start` warns when the
    server settings file's permissions have widened.

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
from datetime import timedelta
from pathlib import Path

import pytest
import typer
from click.testing import CliRunner

from connx_mcp.cli import app
from connx_mcp.compose import passed_variable
from connx_mcp.environment import parse_env
from connx_mcp.ports import Ports, default_ports
from tests.certificates import issue
from tests.conftest import Cli, Machine, as_typed, bind, mounts
from tests.test_configure_interactive import FRESH_WALK, answers, prompt_line
from tests.test_image_lifecycle import pulls
from tests.test_lifecycle import (
    MOVED_DIGEST,
    PASSWORD,
    cdd,  # noqa: F401 - the fixture
    comes_up,
    compose,
    env_dir,
    host_settings,
    image_found,
    inspect_args,
    runs_image,
    serving,
    state,
)

SECRET = "ask-at-start-secret-for-tests"
VARIABLE = "CONNX_MCP_START_CDD_PASSWORD"
DAY = timedelta(days=1)


def configure_asked(cli: Cli, dictionary: Path, name: str = "default") -> None:
    """Create an environment whose CDD password is asked at every start."""
    code, body = cli.json(
        "configure", name, "--no-input", "--set", "CDD_USERNAME=mcp", "--ask-at-start", "CDD_PASSWORD",
        "--cdd", str(dictionary),
    )  # fmt: skip
    assert code == 0, body


def ready(cli: Cli, machine: Machine) -> None:
    """The image is pulled and the container comes up healthy."""
    image_found(cli)
    comes_up(cli, machine, state())
    serving(cli)


def up_environment(cli: Cli) -> dict[str, str]:
    """The variables the compose up call was given."""
    at = next(i for i, call in enumerate(cli.docker.calls) if call[0] == "compose" and "up" in call)
    return cli.docker.environments[at]


def files_holding(directory: Path, text: str) -> list[Path]:
    return [p for p in directory.rglob("*") if p.is_file() and text in p.read_text(encoding="utf-8", errors="ignore")]


def files_leaking(directory: Path, secret: str, stored: list[str]) -> list[Path]:
    """Files holding `secret` outside the stored non-secret values that contain it (a real password can be part
    of the user name or a path, which a plain text search would report as a leak)."""
    covering = sorted((value for value in stored if secret in value), key=len, reverse=True)
    leaking = []
    for path in (p for p in directory.rglob("*") if p.is_file()):
        text = path.read_text(encoding="utf-8", errors="ignore")
        for value in covering:
            text = text.replace(value, "")
        if secret in text:
            leaking.append(path)
    return leaking


def test_a_secret_inside_a_stored_value_is_not_a_leak_but_a_written_secret_is(tmp_path: Path) -> None:
    (tmp_path / "server.env").write_text("CDD_USERNAME=connx-user\n", encoding="utf-8")
    assert files_leaking(tmp_path, "connx", ["connx-user"]) == []

    (tmp_path / "started-with.json").write_text('{"CDD_PASSWORD": "connx"}\n', encoding="utf-8")
    assert files_leaking(tmp_path, "connx", ["connx-user"]) == [tmp_path / "started-with.json"]


# --- configure: marking a secret ask-at-start ---------------------------------------------------------------


def test_an_ask_at_start_secret_is_never_stored(cli: Cli, machine: Machine, cdd: Path) -> None:  # noqa: F811
    configure_asked(cli, cdd)

    assert "CDD_PASSWORD" not in (env_dir(machine) / "server.env").read_text(encoding="utf-8")
    assert host_settings(machine)["ask_at_start"] == ["CDD_PASSWORD"]
    code, body = cli.json("configure", "--show")
    assert code == 0, body
    assert "CDD_PASSWORD" not in body["result"]["server_settings"]
    assert cli("configure", "--show").stdout.count("CDD_PASSWORD (asked at start)") == 1


def test_marking_a_stored_secret_ask_at_start_removes_it_from_disk(
    cli: Cli,
    machine: Machine,
    cdd: Path,  # noqa: F811
) -> None:
    code, body = cli.json(
        "configure", "--no-input", "--set", "CDD_USERNAME=mcp", "--set", f"CDD_PASSWORD={PASSWORD}", "--cdd", str(cdd)
    )  # fmt: skip
    assert code == 0, body

    code, body = cli.json("configure", "--no-input", "--ask-at-start", "CDD_PASSWORD")

    assert code == 0, body
    assert files_holding(machine.base, PASSWORD) == []


def test_the_walk_marks_a_secret_ask_at_start_with_a_question_mark(
    cli: Cli,
    machine: Machine,
    cdd: Path,  # noqa: F811
) -> None:
    keys = answers(FRESH_WALK, {"CDD_USERNAME": "mcp", "CDD_PASSWORD": "?", "cdd": str(cdd)})

    result, screen = cli.typed("configure", keys=keys)

    assert result.exit_code == 0, result.output + screen
    assert "? asks for a secret at every start" in screen
    assert host_settings(machine)["ask_at_start"] == ["CDD_PASSWORD"]
    assert "CDD_PASSWORD" not in (env_dir(machine) / "server.env").read_text(encoding="utf-8")

    result, screen = cli.typed("configure", keys=answers(FRESH_WALK, {}))

    assert result.exit_code == 0, result.output + screen
    assert "(asked at start)" in prompt_line(screen, "CDD_PASSWORD")
    assert host_settings(machine)["ask_at_start"] == ["CDD_PASSWORD"]


# --- start: reading the secret ------------------------------------------------------------------------------


def test_start_without_a_source_under_no_input_names_the_secret(
    cli: Cli,
    machine: Machine,
    cdd: Path,  # noqa: F811
) -> None:
    configure_asked(cli, cdd)

    code, body = cli.json("start", "--no-input")

    assert code == 3
    assert "CDD_PASSWORD" in body["error"]["message"]
    assert "--secret-env CDD_PASSWORD=" in body["error"]["next"]
    assert not [c for c in cli.docker.calls if c[0] == "compose" and "up" in c]


def test_start_reads_the_secret_from_a_named_variable_and_never_writes_it(
    cli: Cli,
    machine: Machine,
    cdd: Path,  # noqa: F811
) -> None:
    configure_asked(cli, cdd)
    ready(cli, machine)

    code, body = cli.json("start", "--secret-env", "CDD_PASSWORD=CI_CDD_PASSWORD", env={"CI_CDD_PASSWORD": SECRET})

    assert code == 0, body
    assert up_environment(cli) == {VARIABLE: SECRET}
    rendered = (env_dir(machine) / "compose.yml").read_text(encoding="utf-8")
    assert f'CDD_PASSWORD: "${{{VARIABLE}:-}}"' in rendered
    assert files_holding(machine.base, SECRET) == []
    assert SECRET not in json.dumps(body)


def test_start_reads_the_secret_from_stdin(cli: Cli, machine: Machine, cdd: Path) -> None:  # noqa: F811
    configure_asked(cli, cdd)
    ready(cli, machine)

    code, body = cli.json("start", "--secret-stdin", "CDD_PASSWORD", input=SECRET + "\n")

    assert code == 0, body
    assert up_environment(cli) == {VARIABLE: SECRET}


def test_start_asks_on_the_terminal_without_echoing(cli: Cli, machine: Machine, cdd: Path) -> None:  # noqa: F811
    configure_asked(cli, cdd)
    ready(cli, machine)

    result, screen = cli.typed("start", keys=[SECRET])

    assert result.exit_code == 0, result.output + screen
    assert "CDD_PASSWORD" in screen
    assert SECRET not in screen + result.output
    assert up_environment(cli) == {VARIABLE: SECRET}


def test_a_named_variable_that_is_not_set_is_refused(cli: Cli, machine: Machine, cdd: Path) -> None:  # noqa: F811
    configure_asked(cli, cdd)

    code, body = cli.json("start", "--secret-env", "CDD_PASSWORD=NOT_SET_ANYWHERE")

    assert code == 61
    assert "NOT_SET_ANYWHERE" in body["error"]["message"]


def test_the_compose_file_is_owner_only(cli: Cli, machine: Machine, cdd: Path) -> None:  # noqa: F811
    configure_asked(cli, cdd)
    ready(cli, machine)

    code, body = cli.json("start", "--secret-env", "CDD_PASSWORD=CI_CDD_PASSWORD", env={"CI_CDD_PASSWORD": SECRET})

    assert code == 0, body
    if os.name != "nt":
        assert (env_dir(machine) / "compose.yml").stat().st_mode & 0o777 == 0o600


def test_restart_reads_ask_at_start_secrets_too(cli: Cli, machine: Machine, cdd: Path) -> None:  # noqa: F811
    configure_asked(cli, cdd)
    ready(cli, machine)
    assert cli.json("start", "--secret-stdin", "CDD_PASSWORD", input=SECRET)[0] == 0
    cli.docker.reply([*compose(machine), "stop", "--timeout", "10"])
    cli.docker.calls.clear()
    cli.docker.environments.clear()

    code, body = cli.json("restart", "--no-input")
    assert code == 3
    assert not [c for c in cli.docker.calls if c[0] == "compose" and "up" in c]
    assert not stops(cli)

    code, body = cli.json("restart", "--secret-stdin", "CDD_PASSWORD", input=SECRET)
    assert code == 0, body
    assert up_environment(cli) == {VARIABLE: SECRET}


def moved_image(cli: Cli) -> None:
    """The registry has a newer image, and the restarted container runs it."""
    pulls(cli)
    image_found(cli, digest=MOVED_DIGEST, version="15.0.4.26100")
    runs_image(cli, MOVED_DIGEST)


def stops(cli: Cli) -> list[list[str]]:
    """The compose stop calls made so far."""
    return [c for c in cli.docker.calls if c[0] == "compose" and "stop" in c]


def running_asked(cli: Cli, machine: Machine, dictionary: Path) -> None:
    """An environment with an ask-at-start CDD password runs; its stop is answered; the calls are cleared."""
    configure_asked(cli, dictionary)
    ready(cli, machine)
    assert cli.json("start", "--secret-stdin", "CDD_PASSWORD", input=SECRET)[0] == 0
    cli.docker.reply([*compose(machine), "stop", "--timeout", "10"])
    cli.docker.calls.clear()
    cli.docker.environments.clear()


def test_restart_reads_a_named_variable_before_the_stop(cli: Cli, machine: Machine, cdd: Path) -> None:  # noqa: F811
    running_asked(cli, machine, cdd)

    code, body = cli.json("restart", "--secret-env", "CDD_PASSWORD=NOT_SET_ANYWHERE")

    assert code == 61
    assert not stops(cli)


def test_restart_checks_a_refused_value_before_the_stop(cli: Cli, machine: Machine, cdd: Path) -> None:  # noqa: F811
    running_asked(cli, machine, cdd)

    code, body = cli.json("restart", "--secret-env", "CDD_PASSWORD=BROKEN", env={"BROKEN": "two\nlines"})

    assert code == 2
    assert not stops(cli)


def test_a_cancelled_prompt_leaves_the_server_running(cli: Cli, machine: Machine, cdd: Path) -> None:  # noqa: F811
    running_asked(cli, machine, cdd)

    result, screen = cli.typed("restart", keys=[])

    assert result.exit_code == 100
    assert "CDD_PASSWORD" in screen
    assert not stops(cli)


def test_restart_asks_on_the_terminal_before_the_stop(cli: Cli, machine: Machine, cdd: Path) -> None:  # noqa: F811
    running_asked(cli, machine, cdd)

    result, screen = cli.typed("restart", keys=[SECRET])

    assert result.exit_code == 0, result.output + screen
    assert up_environment(cli) == {VARIABLE: SECRET}


def test_update_without_a_secret_source_refuses_and_leaves_the_server_running(
    cli: Cli,
    machine: Machine,
    cdd: Path,  # noqa: F811
) -> None:
    running_asked(cli, machine, cdd)
    moved_image(cli)

    code, body = cli.json("update", "--restart", "--no-input")

    assert code == 3
    assert "CDD_PASSWORD" in body["error"]["message"]
    assert "connx-mcp-ce restart default --secret-env CDD_PASSWORD=" in body["error"]["next"]
    assert not stops(cli)


def test_update_restarts_with_a_secret_from_a_named_variable(cli: Cli, machine: Machine, cdd: Path) -> None:  # noqa: F811
    running_asked(cli, machine, cdd)
    moved_image(cli)

    code, body = cli.json(
        "update", "--restart", "--secret-env", "CDD_PASSWORD=CI_CDD_PASSWORD", env={"CI_CDD_PASSWORD": SECRET}
    )

    assert code == 0, body
    assert body["result"]["restarted"] is True
    assert up_environment(cli) == {VARIABLE: SECRET}
    assert SECRET not in json.dumps(body)


def test_update_restarts_with_a_secret_from_stdin(cli: Cli, machine: Machine, cdd: Path) -> None:  # noqa: F811
    running_asked(cli, machine, cdd)
    moved_image(cli)

    code, body = cli.json("update", "--restart", "--secret-stdin", "CDD_PASSWORD", input=SECRET)

    assert code == 0, body
    assert up_environment(cli) == {VARIABLE: SECRET}


def test_secret_stdin_from_a_keyboard_points_to_the_question(cli: Cli, machine: Machine, cdd: Path) -> None:  # noqa: F811
    running_asked(cli, machine, cdd)

    class Keyboard(io.BytesIO):
        def isatty(self) -> bool:
            return True

    result = CliRunner().invoke(  # click's runner: typer's takes no stream input
        typer.main.get_command(app),
        ["restart", "--secret-stdin", "CDD_PASSWORD", "--json"],
        input=Keyboard(SECRET.encode()),
        obj=Ports(cli.docker, cli.probe, clock=cli.clock),
    )

    assert result.exit_code == 2, result.output
    body = json.loads(result.stdout)
    assert "--secret-stdin" in body["error"]["message"]
    assert "without showing it" in body["error"]["message"]
    assert not stops(cli)


def test_a_stored_secret_given_for_one_run_shows_as_drift(cli: Cli, machine: Machine, cdd: Path) -> None:  # noqa: F811
    configure_stored(cli, cdd)
    ready(cli, machine)

    code, body = cli.json("start", "--secret-env", "CDD_PASSWORD=CI_CDD_PASSWORD", env={"CI_CDD_PASSWORD": SECRET})

    assert code == 0, body
    code, body = cli.json("status", "default")
    assert any("--secret-env CDD_PASSWORD" in d for d in body["result"]["drift"])
    assert SECRET not in json.dumps(body)
    assert files_holding(machine.base, SECRET) == []


def test_human_start_shows_composes_own_progress(cli: Cli, machine: Machine, cdd: Path) -> None:  # noqa: F811
    configure_asked(cli, cdd)
    ready(cli, machine)
    cli.docker.reply(
        [*compose(machine), "up", "--detach", "--pull", "never", "--no-build"],
        stderr=" Container connx-mcp-ce-default  Started\n",
    )

    result = cli("start", "--secret-stdin", "CDD_PASSWORD", input=SECRET)

    assert result.exit_code == 0, result.output
    assert "Container connx-mcp-ce-default  Started" in result.stderr


# --- start: one-run overrides -------------------------------------------------------------------------------


def configure_stored(cli: Cli, dictionary: Path) -> None:
    code, body = cli.json(
        "configure", "--no-input", "--set", "CDD_USERNAME=mcp", "--set", f"CDD_PASSWORD={PASSWORD}",
        "--cdd", str(dictionary),
    )  # fmt: skip
    assert code == 0, body


def test_start_cdd_mounts_another_dictionary_for_that_run_only(
    cli: Cli,
    machine: Machine,
    cdd: Path,  # noqa: F811
    tmp_path: Path,
) -> None:
    configure_stored(cli, cdd)
    other = tmp_path / "other.cdd"
    other.write_bytes(b"CDD")
    ready(cli, machine)

    code, body = cli.json("start", "--cdd", str(other))

    assert code == 0, body
    rendered = (env_dir(machine) / "compose.yml").read_text(encoding="utf-8")
    assert str(other) in [m["source"] for m in mounts(rendered)]
    assert host_settings(machine)["cdd_file"] == str(cdd)
    assert any("--cdd" in w for w in body["result"]["warnings"])


def test_start_cdd_refuses_a_missing_file_and_retries_start_not_configure(
    cli: Cli,
    machine: Machine,
    cdd: Path,  # noqa: F811
    tmp_path: Path,
) -> None:
    configure_stored(cli, cdd)
    missing = tmp_path / "missing.cdd"

    code, body = cli.json("start", "--cdd", str(missing))

    assert code == 60, body
    assert body["error"]["code"] == "start_override_invalid"
    assert "--cdd" in body["error"]["message"]
    assert str(missing) in body["error"]["message"]
    assert "configure" not in body["error"]["message"]
    assert body["error"]["next"] == "connx-mcp-ce start default --cdd <path of the .cdd file>"


def test_start_certs_bind_mounts_a_directory_for_that_run_only(
    cli: Cli,
    machine: Machine,
    cdd: Path,  # noqa: F811
    tmp_path: Path,
) -> None:
    configure_stored(cli, cdd)
    certs = tmp_path / "certs"
    certs.mkdir()
    ready(cli, machine)

    code, body = cli.json("start", "--certs", str(certs))

    assert code == 0, body
    rendered = (env_dir(machine) / "compose.yml").read_text(encoding="utf-8")
    assert bind(certs, "/opt/softwareag/connx/mcp/certs") in mounts(rendered)
    assert "certs" not in host_settings(machine)


def test_start_certs_refuses_a_missing_directory(cli: Cli, cdd: Path, tmp_path: Path) -> None:  # noqa: F811
    configure_stored(cli, cdd)

    code, body = cli.json("start", "--certs", str(tmp_path / "nowhere"))

    assert code == 60
    assert body["error"]["message"] == f"--certs {as_typed(tmp_path / 'nowhere')}: no such directory."
    assert body["error"]["code"] == "start_override_invalid"
    assert "configure" not in body["error"]["message"]
    assert body["error"]["next"].startswith("connx-mcp-ce start default --certs ")


def test_start_certs_refuses_a_directory_the_server_would_refuse(
    cli: Cli,
    machine: Machine,
    cdd: Path,  # noqa: F811
    tmp_path: Path,
) -> None:
    configure_stored(cli, cdd)
    certs = tmp_path / "certs"
    issue(lifetime=90 * DAY, left=-DAY, self_signed=False).write(certs, "server")

    code, body = cli.json("start", "--certs", str(certs))

    assert code == 60, body
    assert "expired" in body["error"]["message"]
    assert not [c for c in cli.docker.calls if "up" in c]


def test_the_tls_key_password_can_be_asked_at_start(
    cli: Cli,
    machine: Machine,
    cdd: Path,  # noqa: F811
) -> None:
    configure_stored(cli, cdd)
    code, body = cli.json(
        "configure", "--no-input", "--set", "MCP_TLS_ENABLED=true", "--set", "MCP_TLS_HOSTS=mcp.example.com",
        "--ask-at-start", "MCP_TLS_KEY_PASSWORD",
    )  # fmt: skip
    assert code == 0, body
    ready(cli, machine)

    code, body = cli.json(
        "start", "--secret-env", "MCP_TLS_KEY_PASSWORD=CI_KEY_PASSWORD", env={"CI_KEY_PASSWORD": SECRET}
    )

    assert code == 0, body
    assert up_environment(cli) == {"CONNX_MCP_START_MCP_TLS_KEY_PASSWORD": SECRET}
    assert files_holding(machine.base, SECRET) == []


def test_another_cdd_login_needs_its_password_for_that_run(
    cli: Cli,
    machine: Machine,
    cdd: Path,  # noqa: F811
) -> None:
    configure_stored(cli, cdd)
    ready(cli, machine)

    code, body = cli.json("start", "--no-input", "--cdd-username", "analyst")
    assert code == 3
    assert "CDD_PASSWORD" in body["error"]["message"]

    code, body = cli.json(
        "start", "--cdd-username", "analyst", "--secret-env", "CDD_PASSWORD=ANALYST_PW", env={"ANALYST_PW": SECRET}
    )
    assert code == 0, body
    assert up_environment(cli) == {"CONNX_MCP_START_CDD_USERNAME": "analyst", VARIABLE: SECRET}
    server = (env_dir(machine) / "server.env").read_text(encoding="utf-8")
    assert "CDD_USERNAME=mcp" in server
    assert f"CDD_PASSWORD={PASSWORD}" in server


def test_status_says_when_the_container_differs_from_the_stored_environment(
    cli: Cli,
    machine: Machine,
    cdd: Path,  # noqa: F811
    tmp_path: Path,
) -> None:
    configure_stored(cli, cdd)
    other = tmp_path / "other.cdd"
    other.write_bytes(b"CDD")
    ready(cli, machine)
    assert cli.json("start", "--cdd", str(other))[0] == 0

    code, body = cli.json("status", "default")
    assert code == 0, body
    drift = body["result"]["drift"]
    assert any("one-run overrides" in d and "--cdd" in d for d in drift)
    code, body = cli.json("status")
    assert body["result"]["environments"][0]["drift"] == drift

    assert cli.json("start")[0] == 0
    code, body = cli.json("status", "default")
    assert not any("one-run overrides" in d for d in body["result"]["drift"])


def test_status_of_a_never_created_container_has_no_override_drift(
    cli: Cli,
    machine: Machine,
    cdd: Path,  # noqa: F811
    tmp_path: Path,
) -> None:
    configure_stored(cli, cdd)
    other = tmp_path / "other.cdd"
    other.write_bytes(b"CDD")
    ready(cli, machine)
    assert cli.json("start", "--cdd", str(other))[0] == 0
    cli.docker.reply(inspect_args(), returncode=1, stderr="Error: No such object: connx-mcp-ce-default\n")

    code, body = cli.json("status", "default")

    assert not any("one-run overrides" in d for d in body["result"]["drift"])


# --- start: permissions and help ----------------------------------------------------------------------------


@pytest.mark.skipif(os.name == "nt", reason="POSIX file modes")
def test_start_warns_when_the_server_settings_file_is_readable_by_others(
    cli: Cli,
    machine: Machine,
    cdd: Path,  # noqa: F811
) -> None:
    configure_stored(cli, cdd)
    (env_dir(machine) / "server.env").chmod(0o644)
    ready(cli, machine)

    code, body = cli.json("start")

    assert code == 0, body
    assert any("server.env" in w and "0644" in w and "chmod 600" in w for w in body["result"]["warnings"])


def test_start_help_says_overrides_and_asked_secrets_last_until_the_next_start(cli: Cli) -> None:
    text = " ".join(cli("start", "--help").stdout.split())

    assert "keeps one-run overrides and ask-at-start secrets until the next start, also across reboots" in text
    for option in ("--cdd", "--certs", "--cdd-username", "--secret-env KEY=VARIABLE", "--secret-stdin KEY"):
        assert option in text


# --- live: the real image -----------------------------------------------------------------------------------


@pytest.mark.live
def test_an_ask_at_start_password_reaches_the_real_server(cli: Cli, machine: Machine) -> None:
    """Needs CONNX_MCP_LIVE_CDD, CONNX_MCP_LIVE_USER and CONNX_MCP_LIVE_PASSWORD; the image must be pulled."""
    needed = ["CONNX_MCP_LIVE_CDD", "CONNX_MCP_LIVE_USER", "CONNX_MCP_LIVE_PASSWORD"]
    if missing := [n for n in needed if n not in os.environ]:
        pytest.skip(f"set {', '.join(missing)} to run against the real image")
    real = default_ports()
    cli.docker, cli.probe, cli.clock = real.docker, real.probe, real.clock  # type: ignore[assignment]
    licence = os.environ.get("CONNX_MCP_LIVE_LICENSE_HOST", "host.docker.internal")
    code, body = cli.json(
        "configure", "live-06", "--no-input", "--set", f"CDD_USERNAME={os.environ['CONNX_MCP_LIVE_USER']}",
        "--ask-at-start", "CDD_PASSWORD", "--cdd", os.environ["CONNX_MCP_LIVE_CDD"],
        "--set", f"CONNX_LICENSE_HOST={licence}",
    )  # fmt: skip
    assert code == 0, body
    try:
        code, body = cli.json(
            "start", "live-06", "--timeout", "300", "--secret-env", "CDD_PASSWORD=CONNX_MCP_LIVE_PASSWORD"
        )
        assert code == 0, body
        assert body["result"]["status"]["serving"] is True
        directory = machine.base / "live-06"
        stored = parse_env((directory / "server.env").read_text(encoding="utf-8"))
        assert "CDD_PASSWORD" not in stored
        assert f'CDD_PASSWORD: "${{{passed_variable("CDD_PASSWORD")}:-}}"' in (directory / "compose.yml").read_text(
            encoding="utf-8"
        )
        assert files_leaking(machine.base, os.environ["CONNX_MCP_LIVE_PASSWORD"], [*stored.values()]) == []
    finally:
        cli.json("stop", "live-06")
        real.docker.run(["compose", "-p", "connx-mcp-ce-live-06", "down", "--volumes"])
