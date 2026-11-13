#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_several_environments.py - The administrator runs several environments side by side.

Description:
    Through the CLI seam with the fake Docker port, endpoint probe and clock: each
    environment gets its own published port (`configure --port`), `start` refuses a port another
    running environment holds and names it, `status` without a name lists every environment,
    `stop` without a name never acts on all of them while `stop --all` does, and
    CONNX_MCP_ENVIRONMENT selects the environment when no name is given. The live test runs two
    environments at once on different ports against the real image.

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
import os
import re
from pathlib import Path

import pytest

from connx_mcp import environment, exposure
from connx_mcp.docker import DockerResult
from connx_mcp.ports import default_ports
from tests.conftest import Cli, Machine
from tests.test_lifecycle import (
    DIGEST,
    comes_up,
    compose,
    configure,
    host_settings,
    image_found,
    inspect_args,
    inspect_args_for,
    serving,
    state,
)


@pytest.fixture
def cdd(tmp_path: Path) -> Path:
    path = tmp_path / "dictionaries" / "sales.cdd"
    path.parent.mkdir()
    path.write_bytes(b"CDD")
    return path


# --- configure --port -------------------------------------------------------------------------------------


def test_configure_port_sets_the_published_port(cli: Cli, machine: Machine, cdd: Path) -> None:
    configure(cli, cdd, "sales", "--port", "8001")

    assert host_settings(machine, "sales")["published_port"] == 8001


def test_configure_port_alone_changes_only_the_port(cli: Cli, machine: Machine, cdd: Path) -> None:
    configure(cli, cdd, "sales")

    code, body = cli.json("configure", "sales", "--no-input", "--port", "9100")

    assert code == 0, body
    assert body["result"]["changed"] == ["published_port"]
    assert host_settings(machine, "sales")["published_port"] == 9100


def test_configure_port_already_stored_changes_nothing(cli: Cli, machine: Machine, cdd: Path) -> None:
    configure(cli, cdd, "sales", "--port", "9100")

    code, body = cli.json("configure", "sales", "--no-input", "--port", "9100")

    assert code == 0, body
    assert body["result"]["changed"] == []
    assert host_settings(machine, "sales")["published_port"] == 9100


@pytest.mark.parametrize("port", ["0", "65536"])
def test_configure_port_refuses_a_port_outside_1_to_65535(cli: Cli, machine: Machine, cdd: Path, port: str) -> None:
    configure(cli, cdd, "sales")

    code, body = cli.json("configure", "sales", "--no-input", "--port", port)

    assert code == 2
    assert "--port" in body["error"]["message"]
    assert "published_port" not in host_settings(machine, "sales")


def test_the_published_port_in_server_settings_names_the_port_flag(cli: Cli, machine: Machine, cdd: Path) -> None:
    configure(cli, cdd, "sales")

    code, body = cli.json("configure", "sales", "--no-input", "--set", "MCP_PUBLISHED_PORT=8001")

    assert code == 2
    assert "--port" in body["error"]["message"]


# --- start refuses a port another running environment holds ---------------------------------------------

PS = ["ps", "--filter", "name=^connx-mcp-ce-", "--format", "{{.Names}}\t{{.Ports}}"]


def running(cli: Cli, *containers: tuple[str, int]) -> None:
    """Docker lists these environment containers as running, each publishing its port."""
    lines = [f"connx-mcp-ce-{name}\t0.0.0.0:{port}->8000/tcp, [::]:{port}->8000/tcp\n" for name, port in containers]
    cli.docker.reply(PS, stdout="".join(lines))


def docker_runs(cli: Cli, *lines: str) -> None:
    """Docker runs these containers and answers `ps --filter name=<regex>` as it does: only the names it matches."""
    cli.docker.replies.pop(tuple(PS), None)

    def listed(args: list[str]) -> DockerResult:
        pattern = args[args.index("--filter") + 1].removeprefix("name=")
        return DockerResult(0, "".join(line for line in lines if re.search(pattern, line.partition("\t")[0])), "")

    cli.docker.act_when(lambda args: args[:2] == ["ps", "--filter"], listed)


def started_before(cli: Cli, machine: Machine, cdd: Path, name: str, *extra: str) -> None:
    """Configure and start an environment once, then forget the calls."""
    configure(cli, cdd, name, *extra)
    image_found(cli)
    comes_up(cli, machine, state(), name=name)
    port = int(extra[extra.index("--port") + 1]) if "--port" in extra else 8000
    serving(cli, port)
    cli.docker.reply(PS, stdout="")
    code, body = cli.json("start", name)
    assert code == 0, body
    cli.docker.calls.clear()


def test_start_refuses_a_port_another_running_environment_holds(cli: Cli, machine: Machine, cdd: Path) -> None:
    started_before(cli, machine, cdd, "sales")
    configure(cli, cdd, "test")
    image_found(cli)
    running(cli, ("sales", 8000))

    code, body = cli.json("start", "test")

    assert code == 90
    assert body["error"]["code"] == "port_in_use"
    assert "8000" in body["error"]["message"]
    assert "'sales'" in body["error"]["message"]
    assert body["error"]["next"] == "connx-mcp-ce configure test --port 8001"
    assert not [c for c in cli.docker.calls if "up" in c]


def test_a_start_refused_for_its_port_keeps_the_compose_file_and_the_drift(
    cli: Cli, machine: Machine, cdd: Path
) -> None:
    started_before(cli, machine, cdd, "sales")
    started_before(cli, machine, cdd, "test", "--port", "8001")
    rendered = machine.base / "test" / "compose.yml"
    before = rendered.read_bytes()
    earlier = rendered.stat().st_mtime - 60
    os.utime(rendered, (earlier, earlier))
    configure(cli, cdd, "test", "--port", "8000")
    image_found(cli)
    running(cli, ("sales", 8000), ("test", 8001))

    code, body = cli.json("start", "test")

    assert code == 90, body
    assert rendered.read_bytes() == before
    assert rendered.stat().st_mtime == earlier
    cli.docker.reply(inspect_args("test"), stdout=state() + "\n")
    code, body = cli.json("status", "test")
    assert body["result"]["drift"] == ["the settings changed after the last start; restart to apply them"]


def test_start_takes_a_port_a_stopped_environment_also_uses(cli: Cli, machine: Machine, cdd: Path) -> None:
    started_before(cli, machine, cdd, "sales")
    configure(cli, cdd, "test")
    image_found(cli)
    running(cli)
    comes_up(cli, machine, state(), name="test")

    code, body = cli.json("start", "test")

    assert code == 0, body


def test_two_environments_start_side_by_side_on_different_ports(cli: Cli, machine: Machine, cdd: Path) -> None:
    started_before(cli, machine, cdd, "sales")
    configure(cli, cdd, "test", "--port", "8001")
    image_found(cli)
    running(cli, ("sales", 8000))
    comes_up(cli, machine, state(), name="test")
    serving(cli, 8001)

    code, body = cli.json("start", "test")

    assert code == 0, body
    assert body["result"]["url"] == "http://localhost:8001/mcp"
    assert '"8001:8000"' in (machine.base / "test" / "compose.yml").read_text(encoding="utf-8")


def test_the_suggested_port_skips_ports_other_environments_use(cli: Cli, machine: Machine, cdd: Path) -> None:
    started_before(cli, machine, cdd, "sales")
    configure(cli, cdd, "hr", "--port", "8001")
    configure(cli, cdd, "test")
    image_found(cli)
    running(cli, ("sales", 8000))

    code, body = cli.json("start", "test")

    assert code == 90
    assert body["error"]["next"] == "connx-mcp-ce configure test --port 8002"


def test_restarting_an_environment_does_not_collide_with_itself(cli: Cli, machine: Machine, cdd: Path) -> None:
    started_before(cli, machine, cdd, "sales")
    started_before(cli, machine, cdd, "test", "--port", "8001")
    image_found(cli)
    running(cli, ("sales", 8000), ("test", 8001))
    comes_up(cli, machine, state(), name="sales")

    code, body = cli.json("start", "sales")

    assert code == 0, body


def test_a_lone_environment_starts_without_asking_docker_what_runs(cli: Cli, machine: Machine, cdd: Path) -> None:
    configure(cli, cdd, "sales")
    image_found(cli)
    comes_up(cli, machine, state(), name="sales")
    serving(cli)

    code, body = cli.json("start", "sales")

    assert code == 0, body
    assert PS not in cli.docker.calls


def test_a_container_with_a_lookalike_name_holds_no_environments_port(cli: Cli, machine: Machine, cdd: Path) -> None:
    started_before(cli, machine, cdd, "prod")
    configure(cli, cdd, "test")
    image_found(cli)
    # Real Docker never answers the `^connx-mcp-ce-` filter with `connx-mcp-prod`, a look-alike name without `-ce`:
    # the fake does, so the test shows the exact-name lookup of each environment's container leaves it out too.
    cli.docker.reply(PS, stdout="connx-mcp-prod\t0.0.0.0:8000->8000/tcp, [::]:8000->8000/tcp\n")
    comes_up(cli, machine, state(), name="test")
    serving(cli)

    code, body = cli.json("start", "test")

    assert code == 0, body


def test_a_container_with_a_lookalike_name_is_not_reported_as_an_environment(
    cli: Cli, machine: Machine, cdd: Path
) -> None:
    configure(cli, cdd, "prod")
    cli.docker.reply(inspect_args("prod"), returncode=1, stderr="Error: No such object: connx-mcp-ce-prod\n")
    cli.docker.reply(inspect_args_for("connx-mcp-prod"), stdout=state() + "\n")

    code, body = cli.json("status")

    assert code == 0, body
    rows = body["result"]["environments"]
    assert [(row["environment"], row["container"], row["state"]) for row in rows] == [
        ("prod", "connx-mcp-ce-prod", "not created")
    ]
    assert not [call for call in cli.docker.calls if "connx-mcp-prod" in call]


def test_an_environment_named_demo_has_its_own_container_name(cli: Cli, machine: Machine, cdd: Path) -> None:
    configure(cli, cdd, "demo")
    cli.docker.reply(inspect_args("demo"), returncode=1, stderr="Error: No such object: connx-mcp-ce-demo\n")

    code, body = cli.json("status", "demo")

    assert code == 0, body
    assert body["result"]["container"] == "connx-mcp-ce-demo"


def test_the_port_check_counts_no_demo_stack_container(cli: Cli, machine: Machine, cdd: Path) -> None:
    started_before(cli, machine, cdd, "sales")
    configure(cli, cdd, "test")
    image_found(cli)
    docker_runs(
        cli,
        "connx-mcp-ce-sales\t0.0.0.0:8000->8000/tcp\n",
        "connx-mcp-ce_demo-keycloak-1\t0.0.0.0:8001->8080/tcp\n",
    )

    code, body = cli.json("start", "test")

    assert code == 90
    assert "'sales'" in body["error"]["message"]
    assert "keycloak" not in body["error"]["message"]
    # Only environment containers count: the demo stack's port is no environment's, so it is the one suggested.
    assert body["error"]["next"] == "connx-mcp-ce configure test --port 8001"


# --- status lists every environment ---------------------------------------------------------------------


def test_status_without_a_name_lists_every_environment(cli: Cli, machine: Machine, cdd: Path) -> None:
    started_before(cli, machine, cdd, "sales")
    started_before(cli, machine, cdd, "test", "--port", "8001")
    configure(cli, cdd, "hr", "--port", "8002")
    cli.docker.reply(inspect_args("sales"), stdout=state() + "\n")
    cli.docker.reply(inspect_args("test"), stdout=state(status="exited", health="unhealthy", exit_code=1) + "\n")
    cli.docker.reply(inspect_args("hr"), returncode=1, stderr="Error: No such object: connx-mcp-ce-hr\n")
    cli.probe.calls.clear()

    code, body = cli.json("status")

    assert code == 0, body
    rows = {row["environment"]: row for row in body["result"]["environments"]}
    assert list(rows) == ["hr", "sales", "test"]
    assert rows["sales"] == {
        "environment": "sales",
        "container": "connx-mcp-ce-sales",
        "state": "running",
        "port": 8000,
        "version": "15.0.3.26012",
        "image": DIGEST,
        "docker_health": "healthy",
        "serving": True,
        "drift": [],
        "demo_bound": False,
        "demo_stack_host": None,
        "notices": [exposure.warning(environment.load("sales"))],
    }
    assert rows["test"]["state"] == "exited"
    assert rows["test"]["port"] == 8001
    assert rows["test"]["serving"] is False
    assert rows["hr"]["state"] == "not created"
    assert rows["hr"]["version"] is None
    assert cli.probe.calls == ["http://localhost:8000/health"]


def test_human_status_lists_one_line_per_environment(cli: Cli, machine: Machine, cdd: Path) -> None:
    started_before(cli, machine, cdd, "sales")
    configure(cli, cdd, "hr", "--port", "8002")
    cli.docker.reply(inspect_args("sales"), stdout=state() + "\n")
    cli.docker.reply(inspect_args("hr"), returncode=1, stderr="Error: No such object: connx-mcp-ce-hr\n")

    result = cli("status")

    assert result.exit_code == 0, result.output
    lines = result.stdout.splitlines()
    assert lines[0].split() == ["ENVIRONMENT", "STATE", "PORT", "VERSION", "HEALTH", "DRIFT"]
    assert lines[1].split() == ["hr", "not", "created", "8002", "-", "-", "-"]
    assert lines[2].split() == ["sales", "running", "8000", "15.0.3.26012", "serving", "-"]


def test_status_with_no_environments_names_configure(cli: Cli) -> None:
    code, body = cli.json("status")

    assert code == 0, body
    assert body["result"]["environments"] == []
    assert "connx-mcp-ce configure" in cli("status").stdout


def test_status_neither_lists_nor_mentions_an_environment_under_a_lookalike_base_directory(
    cli: Cli, machine: Machine, cdd: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("MCP_ENVIRONMENT_PATH")
    lookalike = machine.home / ".connx" / "connx-mcp"
    (lookalike / "quokka").mkdir(parents=True)
    (lookalike / "quokka" / "server.env").write_text("CONNX_USERNAME=quokka\n", encoding="utf-8")
    configure(cli, cdd, "prod")
    cli.docker.reply(inspect_args("prod"), returncode=1, stderr="Error: No such object: connx-mcp-ce-prod\n")

    code, body = cli.json("status")
    human = cli("status")

    assert code == 0, body
    assert body["result"]["base_directory"] == str(machine.home / ".connx" / "connx-mcp-ce")
    assert [row["environment"] for row in body["result"]["environments"]] == ["prod"]
    assert human.exit_code == 0, human.output
    # The look-alike name without `-ce` is a prefix of the base directory's: only a mention not followed by `-ce`
    # is the look-alike.
    parent = str(lookalike.parent)
    for output, path in ((human.output, parent), (json.dumps(body), json.dumps(parent)[1:-1])):
        assert re.search(re.escape(path) + r"[/\\]+connx-mcp(?!-ce\b)[\w.-]*", output) is None, output
        assert "quokka" not in output


def test_status_lists_settings_changed_since_the_last_start_as_drift(cli: Cli, machine: Machine, cdd: Path) -> None:
    started_before(cli, machine, cdd, "sales")
    rendered = (machine.base / "sales" / "compose.yml").stat().st_mtime
    configure(cli, cdd, "sales", "--set", "MCP_PATH=/sql")
    os.utime(machine.base / "sales" / "server.env", (rendered + 5, rendered + 5))
    cli.docker.reply(inspect_args("sales"), stdout=state() + "\n")

    code, body = cli.json("status")

    assert code == 0, body
    drift = body["result"]["environments"][0]["drift"]
    assert drift == ["the settings changed after the last start; restart to apply them"]
    code, body = cli.json("status", "sales")
    assert body["result"]["drift"] == drift


def test_a_stopped_environment_has_no_drift(cli: Cli, machine: Machine, cdd: Path) -> None:
    started_before(cli, machine, cdd, "sales")
    rendered = (machine.base / "sales" / "compose.yml").stat().st_mtime
    configure(cli, cdd, "sales", "--port", "8005")
    os.utime(machine.base / "sales" / "host.json", (rendered + 5, rendered + 5))
    cli.docker.reply(inspect_args("sales"), stdout=state(status="exited", health=None) + "\n")

    code, body = cli.json("status")

    assert body["result"]["environments"][0]["drift"] == []


def test_status_all_lists_even_when_an_environment_is_selected(cli: Cli, machine: Machine, cdd: Path) -> None:
    configure(cli, cdd, "sales")
    cli.docker.reply(inspect_args("sales"), returncode=1, stderr="Error: No such object: connx-mcp-ce-sales\n")

    code, body = cli.json("status", "--all", env={"CONNX_MCP_ENVIRONMENT": "sales"})

    assert code == 0, body
    assert [row["environment"] for row in body["result"]["environments"]] == ["sales"]


def test_status_all_takes_no_name(cli: Cli) -> None:
    code, body = cli.json("status", "sales", "--all")

    assert code == 2


# --- stop and stop --all --------------------------------------------------------------------------------


def stops(cli: Cli, machine: Machine, name: str, returncode: int = 0, stderr: str = "") -> list[str]:
    args = [*compose(machine, name), "stop", "--timeout", "10"]
    cli.docker.reply(args, returncode=returncode, stderr=stderr)
    return args


def test_stop_without_a_name_never_stops_every_environment(cli: Cli, machine: Machine, cdd: Path) -> None:
    started_before(cli, machine, cdd, "default")
    started_before(cli, machine, cdd, "sales", "--port", "8001")
    default_stop, sales_stop = stops(cli, machine, "default"), stops(cli, machine, "sales")

    code, body = cli.json("stop")

    assert code == 0, body
    assert body["result"]["environment"] == "default"
    assert default_stop in cli.docker.calls
    assert sales_stop not in cli.docker.calls


def test_stop_all_stops_every_started_environment(cli: Cli, machine: Machine, cdd: Path) -> None:
    started_before(cli, machine, cdd, "sales")
    started_before(cli, machine, cdd, "test", "--port", "8001")
    configure(cli, cdd, "hr", "--port", "8002")
    calls = [stops(cli, machine, "sales"), stops(cli, machine, "test")]

    code, body = cli.json("stop", "--all")

    assert code == 0, body
    assert body["result"]["stopped"] == ["sales", "test"]
    assert body["result"]["never_started"] == ["hr"]
    assert [c for c in cli.docker.calls if "stop" in c] == calls


def test_stop_all_stops_the_rest_when_one_fails_and_names_it(cli: Cli, machine: Machine, cdd: Path) -> None:
    started_before(cli, machine, cdd, "sales")
    started_before(cli, machine, cdd, "test", "--port", "8001")
    stops(cli, machine, "sales", returncode=1, stderr="Error response from daemon: cannot stop container\n")
    test_stop = stops(cli, machine, "test")

    code, body = cli.json("stop", "--all")

    assert code == 31
    assert "sales" in body["error"]["message"]
    assert "cannot stop container" in body["error"]["message"]
    assert test_stop in cli.docker.calls


def test_stop_all_takes_no_name(cli: Cli) -> None:
    code, body = cli.json("stop", "sales", "--all")

    assert code == 2
    assert body["error"]["code"] == "usage"


def test_stop_all_with_no_environments_does_nothing(cli: Cli) -> None:
    code, body = cli.json("stop", "--all")

    assert code == 0, body
    assert body["result"]["stopped"] == []


# --- one unreadable environment -------------------------------------------------------------------------


def corrupt(machine: Machine, name: str) -> None:
    """Break an environment's host settings file."""
    (machine.base / name / "host.json").write_text("{not json", encoding="utf-8")


def test_status_lists_an_unreadable_environment_and_the_others(cli: Cli, machine: Machine, cdd: Path) -> None:
    started_before(cli, machine, cdd, "sales")
    configure(cli, cdd, "hr", "--port", "8002")
    corrupt(machine, "hr")
    cli.docker.reply(inspect_args("sales"), stdout=state() + "\n")

    code, body = cli.json("status")

    assert code == 0, body
    rows = {row["environment"]: row for row in body["result"]["environments"]}
    assert rows["sales"]["state"] == "running"
    assert rows["hr"]["state"] == "unreadable"
    assert "host.json" in rows["hr"]["error"]["message"]
    assert rows["hr"]["error"]["next"] == "connx-mcp-ce configure hr"


def test_human_status_shows_an_unreadable_environment_with_its_next_step(cli: Cli, machine: Machine, cdd: Path) -> None:
    configure(cli, cdd, "hr")
    corrupt(machine, "hr")

    result = cli("status")

    assert result.exit_code == 0, result.output
    lines = result.stdout.splitlines()
    assert lines[1].split()[:2] == ["hr", "unreadable"]
    assert "connx-mcp-ce configure hr" in result.stdout


def test_status_of_an_unreadable_environment_says_what_to_do(cli: Cli, machine: Machine, cdd: Path) -> None:
    configure(cli, cdd, "hr")
    corrupt(machine, "hr")

    code, body = cli.json("status", "hr")

    assert code == 91
    assert body["error"]["code"] == "environment_unreadable"
    assert body["error"]["next"] == "connx-mcp-ce configure hr"


def test_an_unreadable_environment_does_not_block_starting_another(cli: Cli, machine: Machine, cdd: Path) -> None:
    started_before(cli, machine, cdd, "hr", "--port", "8002")
    corrupt(machine, "hr")
    configure(cli, cdd, "sales")
    image_found(cli)
    running(cli, ("hr", 8002))
    comes_up(cli, machine, state(), name="sales")
    serving(cli)

    code, body = cli.json("start", "sales")

    assert code == 0, body


def test_an_unreadable_environment_still_holds_its_running_port(cli: Cli, machine: Machine, cdd: Path) -> None:
    started_before(cli, machine, cdd, "hr")
    corrupt(machine, "hr")
    configure(cli, cdd, "sales")
    image_found(cli)
    running(cli, ("hr", 8000))

    code, body = cli.json("start", "sales")

    assert code == 90, body
    assert "'hr'" in body["error"]["message"]


def test_a_refused_port_with_no_free_port_left_gives_no_port_hint(cli: Cli, machine: Machine, cdd: Path) -> None:
    started_before(cli, machine, cdd, "sales")
    configure(cli, cdd, "test")
    image_found(cli)
    every = "".join(f"0.0.0.0:{port}->8000/tcp, " for port in range(8000, 65536))
    cli.docker.reply(PS, stdout=f"connx-mcp-ce-sales\t{every}\n")

    code, body = cli.json("start", "test")

    assert code == 90, body
    assert body["error"]["next"] == "connx-mcp-ce configure test --port <free port>"


# --- CONNX_MCP_ENVIRONMENT ------------------------------------------------------------------------------


def test_the_environment_variable_selects_the_environment_for_every_command(
    cli: Cli, machine: Machine, cdd: Path
) -> None:
    selected = {"CONNX_MCP_ENVIRONMENT": "sales"}
    code, body = cli.json(
        "configure", "--no-input", "--set", "CDD_USERNAME=mcp", "--set", "CDD_PASSWORD=pw", "--cdd", str(cdd),
        env=selected,
    )  # fmt: skip
    assert (code, body["result"]["environment"]) == (0, "sales"), body
    assert not (machine.base / "default").exists()

    image_found(cli)
    comes_up(cli, machine, state(), name="sales")
    serving(cli)
    code, body = cli.json("start", env=selected)
    assert (code, body["result"]["container"]) == (0, "connx-mcp-ce-sales"), body

    code, body = cli.json("status", env=selected)
    assert (code, body["result"]["environment"]) == (0, "sales"), body

    cli.docker.reply(["logs", "--tail", "5", "connx-mcp-ce-sales"], stdout="line\n")
    code, body = cli.json("log", "--tail", "5", env=selected)
    assert (code, body["result"]["container"]) == (0, "connx-mcp-ce-sales"), body

    stops(cli, machine, "sales")
    code, body = cli.json("stop", env=selected)
    assert (code, body["result"]["environment"]) == (0, "sales"), body


def test_a_name_on_the_command_line_wins_over_the_environment_variable(cli: Cli, machine: Machine, cdd: Path) -> None:
    configure(cli, cdd, "sales")
    configure(cli, cdd, "test")

    code, body = cli.json("configure", "test", "--show", env={"CONNX_MCP_ENVIRONMENT": "sales"})

    assert (code, body["result"]["environment"]) == (0, "test"), body


def test_an_invalid_environment_variable_is_refused_by_name(cli: Cli) -> None:
    code, body = cli.json("stop", env={"CONNX_MCP_ENVIRONMENT": "Sales Team"})

    assert code == 2
    assert "'Sales Team'" in body["error"]["message"]


# --- live: two environments at once ---------------------------------------------------------------------


@pytest.mark.live
def test_two_environments_run_at_once_on_different_ports(cli: Cli, machine: Machine) -> None:
    """Needs CONNX_MCP_LIVE_CDD, CONNX_MCP_LIVE_USER and CONNX_MCP_LIVE_PASSWORD; the image must be pulled."""
    needed = ["CONNX_MCP_LIVE_CDD", "CONNX_MCP_LIVE_USER", "CONNX_MCP_LIVE_PASSWORD"]
    if missing := [n for n in needed if n not in os.environ]:
        pytest.skip(f"set {', '.join(missing)} to run against the real image")
    real = default_ports()
    cli.docker, cli.probe, cli.clock = real.docker, real.probe, real.clock  # type: ignore[assignment]
    licence = os.environ.get("CONNX_MCP_LIVE_LICENSE_HOST", "host.docker.internal")
    names = {"live-09a": "18000", "live-09b": "18001"}
    for name, port in names.items():
        code, body = cli.json(
            "configure", name, "--no-input", "--set", f"CDD_USERNAME={os.environ['CONNX_MCP_LIVE_USER']}",
            "--set", f"CDD_PASSWORD={os.environ['CONNX_MCP_LIVE_PASSWORD']}", "--cdd", os.environ["CONNX_MCP_LIVE_CDD"],
            "--set", f"CONNX_LICENSE_HOST={licence}", "--port", port,
        )  # fmt: skip
        assert code == 0, body
    try:
        for name in names:
            code, body = cli.json("start", name, "--timeout", "300")
            assert code == 0, body

        code, body = cli.json("status")
        assert code == 0, body
        rows = {row["environment"]: row for row in body["result"]["environments"]}
        for name, port in names.items():
            assert rows[name]["port"] == int(port)
            assert rows[name]["serving"] is True

        code, body = cli.json("configure", "live-09c", "--no-input", "--set", "CDD_USERNAME=x", "--set",
                              "CDD_PASSWORD=x", "--cdd", os.environ["CONNX_MCP_LIVE_CDD"], "--port", "18000")  # fmt: skip
        assert code == 0, body
        code, body = cli.json("start", "live-09c")
        assert code == 90, body
        assert "'live-09a'" in body["error"]["message"]
    finally:
        code, body = cli.json("stop", "--all")
        assert code == 0, body
        for name in [*names, "live-09c"]:
            real.docker.run(["compose", "-p", f"connx-mcp-ce-{name}", "down", "--volumes"])
