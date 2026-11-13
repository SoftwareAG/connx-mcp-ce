#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_lifecycle.py - The administrator starts, stops and reads the log of an environment.

Description:
    Through the CLI seam with the fake Docker port, endpoint probe and clock: `start`
    renders compose and pins the image digest on first start, waits for healthy within a timeout
    it remembers, stops waiting the moment the container exits, and on timeout prints the log tail
    and the `/health` body while leaving the container running; `stop`, `restart`, `log` and
    `status <env>` do what their help says. The live test runs the same flow against the real
    image from the host.

Copyright:
    Copyright 2026 Software GmbH
    Licensed under the Apache License, Version 2.0 (the "License"); you may not use this file except in compliance
    with the License. You may obtain a copy of the License at https://www.apache.org/licenses/LICENSE-2.0
    Unless required by applicable law or agreed to in writing, software distributed under the License is distributed
    on an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the License
    for the specific language governing permissions and limitations under the License.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from connx_mcp.docker import DockerResult
from connx_mcp.ports import default_ports
from connx_mcp.probe import HealthReply
from tests.conftest import Cli, Machine, mounts

PASSWORD = "pw-for-tests-only"
IMAGE = "softwareagcr.azurecr.io/connx-mcp-server-ce:15.0"
DIGEST = "softwareagcr.azurecr.io/connx-mcp-server-ce@sha256:" + "a" * 64
MOVED_DIGEST = "softwareagcr.azurecr.io/connx-mcp-server-ce@sha256:" + "b" * 64
IMAGE_FORMAT = '{"id":{{json .Id}},"digests":{{json .RepoDigests}},"labels":{{json .Config.Labels}}}'
STATE_FORMAT = (
    '{"status":{{json .State.Status}},'
    '"health":{{if .State.Health}}{{json .State.Health.Status}}{{else}}null{{end}},'
    '"exit_code":{{.State.ExitCode}},"restarts":{{.RestartCount}}}'
)
HEALTHY_BODY = '{"status": "ready", "connx_odbc_ok": true}'
LOG_LINES = (
    "2026-10-13T10:00:00.000000001Z Starting CONNX MCP server 15.0.3 (build 26012) on Python 3.14.0...\n"
    "2026-10-13T10:00:02.000000000Z Uvicorn running on http://0.0.0.0:8000\n"
)
LOG_ERRORS = "2026-10-13T10:00:01.000000000Z ERROR: Settings refused: MCP_NO_SUCH_KEY is not a setting\n"


@pytest.fixture
def cdd(tmp_path: Path) -> Path:
    path = tmp_path / "dictionaries" / "sales.cdd"
    path.parent.mkdir()
    path.write_bytes(b"CDD")
    return path


def configure(cli: Cli, cdd: Path, name: str = "default", *extra: str) -> None:
    """Create a complete environment."""
    code, body = cli.json(
        "configure", name, "--no-input", "--set", "CDD_USERNAME=mcp", "--set", f"CDD_PASSWORD={PASSWORD}",
        "--cdd", str(cdd), *extra,
    )  # fmt: skip
    assert code == 0, body


def env_dir(machine: Machine, name: str = "default") -> Path:
    return machine.base / name


def compose(machine: Machine, name: str = "default") -> list[str]:
    directory = env_dir(machine, name)
    return ["compose", "-p", f"connx-mcp-ce-{name}", "-f", str(directory / "compose.yml")]


def state(status: str = "running", health: str | None = "healthy", exit_code: int = 0, restarts: int = 0) -> str:
    return json.dumps({"status": status, "health": health, "exit_code": exit_code, "restarts": restarts})


def inspect_args(name: str = "default") -> list[str]:
    return inspect_args_for(f"connx-mcp-ce-{name}")


def inspect_args_for(container: str) -> list[str]:
    """The state inspect of a container by its full name, environment or not."""
    return ["container", "inspect", "--format", STATE_FORMAT, container]


def image_id(digest: str = DIGEST) -> str:
    """The local image ID of a pulled digest: a different image per digest."""
    return "sha256:" + hashlib.sha256(digest.encode()).hexdigest()


def image_found(cli: Cli, digest: str = DIGEST, version: str | None = "15.0.3.26012") -> None:
    """The image reference (and the digest itself) resolves to a local image with a repo digest and a version label."""
    facts = json.dumps(
        {
            "id": image_id(digest),
            "digests": [digest],
            "labels": {"org.opencontainers.image.version": version} if version else None,
        }
    )
    for ref in (IMAGE, digest):
        cli.docker.reply(["image", "inspect", "--format", IMAGE_FORMAT, ref], stdout=facts + "\n")


def runs_image(cli: Cli, digest: str = DIGEST, name: str = "default") -> None:
    """The server container was created from the image of `digest`."""
    cli.docker.reply(
        ["container", "inspect", "--format", "{{.Image}}", f"connx-mcp-ce-{name}"], stdout=image_id(digest)
    )


def comes_up(cli: Cli, machine: Machine, *states: str, name: str = "default") -> None:
    """Compose up succeeds and the container passes through the given states."""
    cli.docker.reply([*compose(machine, name), "up", "--detach", "--pull", "never", "--no-build"])
    cli.docker.replies_in_turn(inspect_args(name), *(DockerResult(0, s + "\n", "") for s in states))
    cli.docker.reply(
        ["logs", "--tail", "40", "--timestamps", f"connx-mcp-ce-{name}"], stdout=LOG_LINES, stderr=LOG_ERRORS
    )
    # What the wait reads for a cause the server names: the lines logged since compose up.
    cli.docker.reply_when(lambda args: args[:2] == ["logs", "--since"], stdout=LOG_LINES, stderr=LOG_ERRORS)
    runs_image(cli, name=name)


def serving(cli: Cli, port: int = 8000) -> None:
    cli.probe.health_replies[f"http://localhost:{port}/health"] = HealthReply(status=200, body=HEALTHY_BODY)


def host_settings(machine: Machine, name: str = "default") -> dict:
    return json.loads((env_dir(machine, name) / "host.json").read_text(encoding="utf-8"))


# --- start: compose, digest pin, healthy ----------------------------------------------------------------


def test_start_renders_compose_pins_the_digest_and_waits_until_healthy(cli: Cli, machine: Machine, cdd: Path) -> None:
    configure(cli, cdd)
    image_found(cli)
    comes_up(cli, machine, state(health="starting"), state(health="healthy"))
    serving(cli)

    code, body = cli.json("start")

    assert code == 0, body
    result = body["result"]
    assert result["container"] == "connx-mcp-ce-default"
    assert result["image"] == DIGEST
    assert result["status"]["docker_health"] == "healthy"
    assert result["status"]["health"]["http_status"] == 200
    assert host_settings(machine)["image_digest"] == DIGEST
    assert host_settings(machine)["image_version"] == "15.0.3.26012"
    rendered = (env_dir(machine) / "compose.yml").read_text(encoding="utf-8")
    assert f'image: "{DIGEST}"' in rendered
    assert str(cdd) in [m["source"] for m in mounts(rendered)]
    assert 'name: "connx-mcp-ce-default"' in rendered
    assert 'name: "connx-mcp-ce-default-certs"' in rendered
    assert "restart: unless-stopped" in rendered
    assert '"host.docker.internal:host-gateway"' in rendered


def test_a_later_start_keeps_the_pinned_digest_when_the_tag_moved(cli: Cli, machine: Machine, cdd: Path) -> None:
    configure(cli, cdd)
    image_found(cli)
    comes_up(cli, machine, state())
    serving(cli)
    assert cli.json("start")[0] == 0
    image_found(cli, digest=MOVED_DIGEST, version="15.0.4.26100")
    cli.docker.calls.clear()

    code, body = cli.json("start")

    assert code == 0, body
    assert body["result"]["image"] == DIGEST
    assert f'image: "{DIGEST}"' in (env_dir(machine) / "compose.yml").read_text(encoding="utf-8")
    assert body["result"]["pinned"] is False


def test_start_layers_the_override_file_and_never_touches_it(cli: Cli, machine: Machine, cdd: Path) -> None:
    configure(cli, cdd)
    override = env_dir(machine) / "compose.override.yml"
    override.write_text("services:\n  connx-mcp:\n    mem_limit: 2g\n", encoding="utf-8")
    before = override.stat().st_mtime_ns
    image_found(cli)
    up = [*compose(machine), "-f", str(override), "up", "--detach", "--pull", "never", "--no-build"]
    cli.docker.reply(up)
    cli.docker.reply(inspect_args(), stdout=state() + "\n")
    runs_image(cli)
    serving(cli)

    code, body = cli.json("start")

    assert code == 0, body
    assert up in cli.docker.calls
    assert override.read_text(encoding="utf-8") == "services:\n  connx-mcp:\n    mem_limit: 2g\n"
    assert override.stat().st_mtime_ns == before


def test_the_published_port_comes_from_host_settings(cli: Cli, machine: Machine, cdd: Path) -> None:
    configure(cli, cdd)
    settings = host_settings(machine)
    (env_dir(machine) / "host.json").write_text(json.dumps({**settings, "published_port": 8443}), encoding="utf-8")
    image_found(cli)
    comes_up(cli, machine, state())
    serving(cli, port=8443)

    code, body = cli.json("start")

    assert code == 0, body
    assert '"8443:8000"' in (env_dir(machine) / "compose.yml").read_text(encoding="utf-8")
    assert body["result"]["url"] == "http://localhost:8443/mcp"


# --- start: timeout, early exit ---------------------------------------------------------------------------


def test_start_waits_180_seconds_by_default_then_leaves_the_container_running(
    cli: Cli, machine: Machine, cdd: Path
) -> None:
    configure(cli, cdd)
    image_found(cli)
    comes_up(cli, machine, state(health="starting"))
    cli.probe.health_replies["http://localhost:8000/health"] = HealthReply(
        status=503, body='{"status": "not ready", "reason": "CONNX is not answering"}'
    )

    code, body = cli.json("start")

    assert code == 33
    assert body["error"]["code"] == "start_timeout"
    message = body["error"]["message"]
    assert "180 s" in message
    assert "left running" in message
    assert '503 {"status": "not ready", "reason": "CONNX is not answering"}' in message
    assert "Uvicorn running on http://0.0.0.0:8000" in message
    assert body["error"]["next"] == "connx-mcp-ce status default"
    assert 180 <= cli.clock.now < 183
    assert not [c for c in cli.docker.calls if {"stop", "down", "kill", "rm"} & set(c)]


def test_start_timeout_is_remembered_as_a_host_setting(cli: Cli, machine: Machine, cdd: Path) -> None:
    configure(cli, cdd)
    image_found(cli)
    comes_up(cli, machine, state(health="starting"))

    code, _ = cli.json("start", "--timeout", "600")
    assert code == 33
    assert 600 <= cli.clock.now < 603
    assert host_settings(machine)["start_timeout"] == 600

    cli.clock.now = 0
    code, body = cli.json("start")

    assert code == 33
    assert "600 s" in body["error"]["message"]
    assert 600 <= cli.clock.now < 603


def test_start_stops_waiting_at_once_when_the_container_exits(cli: Cli, machine: Machine, cdd: Path) -> None:
    configure(cli, cdd)
    image_found(cli)
    comes_up(cli, machine, state(health="starting"), state(status="exited", health="unhealthy", exit_code=1))

    code, body = cli.json("start")

    assert code == 32
    assert body["error"]["code"] == "container_exited"
    message = body["error"]["message"]
    assert "exited with code 1" in message
    lines = message.splitlines()
    starting = lines.index("Starting CONNX MCP server 15.0.3 (build 26012) on Python 3.14.0...")
    assert lines[starting + 1] == "ERROR: Settings refused: MCP_NO_SUCH_KEY is not a setting"
    assert lines[starting + 2] == "Uvicorn running on http://0.0.0.0:8000"
    assert body["error"]["next"] == "connx-mcp-ce log default --tail 200"
    assert cli.clock.now < 5


def test_a_container_that_restarts_during_the_wait_counts_as_exited(cli: Cli, machine: Machine, cdd: Path) -> None:
    configure(cli, cdd)
    image_found(cli)
    comes_up(cli, machine, state(health="starting"), state(health="starting", exit_code=1, restarts=1))

    code, body = cli.json("start")

    assert code == 32
    assert cli.clock.now < 5


def test_an_image_without_a_health_check_is_healthy_when_health_answers(cli: Cli, machine: Machine, cdd: Path) -> None:
    configure(cli, cdd)
    image_found(cli)
    comes_up(cli, machine, state(health=None))
    serving(cli)

    code, body = cli.json("start")

    assert code == 0, body


def test_human_start_shows_progress_on_stderr_and_the_result_on_stdout(cli: Cli, machine: Machine, cdd: Path) -> None:
    configure(cli, cdd)
    image_found(cli)
    comes_up(cli, machine, state())
    serving(cli)

    result = cli("start")

    assert result.exit_code == 0, result.output
    assert "Waiting up to 180 s for connx-mcp-ce-default" in result.stderr
    assert "connx-mcp-ce-default is healthy and serving MCP at http://localhost:8000/mcp" in result.stdout
    assert "Pinned image" in result.stdout


# --- start: refusals --------------------------------------------------------------------------------------


def test_start_of_an_unknown_environment_names_configure(cli: Cli) -> None:
    code, body = cli.json("start", "sales")

    assert code == 20
    assert body["error"]["next"] == "connx-mcp-ce configure sales"


def test_start_without_the_image_names_download(cli: Cli, machine: Machine, cdd: Path) -> None:
    configure(cli, cdd)
    cli.docker.reply(
        ["image", "inspect", "--format", IMAGE_FORMAT, IMAGE],
        returncode=1,
        stderr=f"Error response from daemon: No such image: {IMAGE}\n",
    )

    code, body = cli.json("start")

    assert code == 30
    assert body["error"]["code"] == "image_not_found"
    assert body["error"]["next"] == "connx-mcp-ce download default"
    assert "image_digest" not in host_settings(machine)


def test_start_refuses_a_cdd_file_that_has_gone(cli: Cli, machine: Machine, cdd: Path) -> None:
    configure(cli, cdd)
    cdd.unlink()

    code, body = cli.json("start")

    assert code == 35
    assert str(cdd) in body["error"]["message"]


def test_a_failed_compose_up_shows_dockers_message(cli: Cli, machine: Machine, cdd: Path) -> None:
    configure(cli, cdd)
    image_found(cli)
    cli.docker.reply(
        [*compose(machine), "up", "--detach", "--pull", "never", "--no-build"],
        returncode=1,
        stderr="Error response from daemon: driver failed programming external connectivity: "
        "Bind for 0.0.0.0:8000 failed: port is already allocated\n",
    )

    code, body = cli.json("start")

    assert code == 31
    assert "port is already allocated" in body["error"]["message"]


# --- stop and restart -------------------------------------------------------------------------------------


def started(cli: Cli, machine: Machine, cdd: Path, name: str = "default") -> None:
    configure(cli, cdd, name)
    image_found(cli)
    comes_up(cli, machine, state(), name=name)
    serving(cli)
    code, body = cli.json("start", name)
    assert code == 0, body
    cli.docker.calls.clear()
    cli.probe.calls.clear()


def quick(call: list[str]) -> bool:
    """Whether a docker call answers at once on a working daemon: version, inspect, ps or a bounded log tail."""
    if call[:2] == ["compose", "version"]:
        return True
    if call[:1] == ["compose"]:
        return "ps" in call[call.index("-f") + 2 :] if "-f" in call else False
    if call[:1] == ["logs"]:
        return "--tail" in call and "--follow" not in call
    return call[:1] in (["version"], ["ps"]) or call[1:2] == ["inspect"]


def test_quick_docker_calls_time_out_and_long_ones_never(cli: Cli, machine: Machine, cdd: Path) -> None:
    configure(cli, cdd)
    image_found(cli)
    comes_up(cli, machine, state())
    serving(cli)
    cli.docker.reply(["logs", "--tail", "5", "connx-mcp-ce-default"], stdout="line one\n")

    for args in (["start"], ["status", "default"], ["log", "--tail", "5"]):
        code, body = cli.json(*args)
        assert code == 0, body

    calls = list(zip(cli.docker.calls, cli.docker.timeouts, strict=True))
    assert [c for c, t in calls if quick(c) and t is None] == []
    assert [c for c, t in calls if not quick(c) and t is not None] == []
    assert any(c[:1] == ["compose"] and "up" in c for c, _ in calls)


def test_status_ends_as_not_running_when_the_daemon_stops_answering(cli: Cli, machine: Machine, cdd: Path) -> None:
    started(cli, machine, cdd)
    cli.docker.hang_when(lambda args: args[:2] == ["container", "inspect"])

    code, body = cli.json("status", "default")

    assert code == 11, body
    assert "did not answer" in body["error"]["message"]
    assert body["error"]["next"] == "docker info"


def test_stop_stops_gracefully_and_kills_after_10_seconds(cli: Cli, machine: Machine, cdd: Path) -> None:
    started(cli, machine, cdd)
    cli.docker.reply([*compose(machine), "stop", "--timeout", "10"])

    code, body = cli.json("stop")

    assert code == 0, body
    assert body["result"]["stopped"] is True
    assert [*compose(machine), "stop", "--timeout", "10"] in cli.docker.calls


def test_stop_takes_its_kill_timeout(cli: Cli, machine: Machine, cdd: Path) -> None:
    started(cli, machine, cdd)
    cli.docker.reply([*compose(machine), "stop", "--timeout", "45"])

    code, body = cli.json("stop", "--timeout", "45")

    assert code == 0, body


def test_stop_of_a_never_started_environment_does_nothing(cli: Cli, machine: Machine, cdd: Path) -> None:
    configure(cli, cdd)

    code, body = cli.json("stop")

    assert code == 0, body
    assert body["result"]["stopped"] is False
    assert not [c for c in cli.docker.calls if c[0] == "compose" and "stop" in c]


def test_restart_stops_then_starts(cli: Cli, machine: Machine, cdd: Path) -> None:
    started(cli, machine, cdd)
    cli.docker.reply([*compose(machine), "stop", "--timeout", "10"])

    code, body = cli.json("restart")

    assert code == 0, body
    assert body["result"]["restarted"] is True
    stop_at = cli.docker.calls.index([*compose(machine), "stop", "--timeout", "10"])
    up_at = cli.docker.calls.index([*compose(machine), "up", "--detach", "--pull", "never", "--no-build"])
    assert stop_at < up_at


# --- log --------------------------------------------------------------------------------------------------


def test_log_follows_tails_and_filters_by_time(cli: Cli, machine: Machine, cdd: Path) -> None:
    started(cli, machine, cdd)
    args = ["logs", "--follow", "--tail", "100", "--since", "10m", "connx-mcp-ce-default"]
    cli.docker.reply(args, stdout="line one\n", stderr="line two\n")

    result = cli("log", "--follow", "--tail", "100", "--since", "10m")

    assert result.exit_code == 0, result.output
    assert args in cli.docker.calls
    assert "line one" in result.stdout
    assert "line two" in result.stderr


def test_log_as_json_returns_the_lines(cli: Cli, machine: Machine, cdd: Path) -> None:
    started(cli, machine, cdd)
    cli.docker.reply(["logs", "--tail", "5", "connx-mcp-ce-default"], stdout="line one\n")

    code, body = cli.json("log", "--tail", "5")

    assert code == 0, body
    assert body["result"]["stdout"] == "line one\n"


def test_log_follow_cannot_be_json(cli: Cli, machine: Machine, cdd: Path) -> None:
    configure(cli, cdd)

    code, body = cli.json("log", "--follow")

    assert code == 2


def test_log_of_a_never_started_environment_names_start(cli: Cli, machine: Machine, cdd: Path) -> None:
    configure(cli, cdd)
    cli.docker.reply(inspect_args(), returncode=1, stderr="Error: No such container: connx-mcp-ce-default\n")

    code, body = cli.json("log")

    assert code == 34
    assert body["error"]["next"] == "connx-mcp-ce start default"


# --- status -----------------------------------------------------------------------------------------------


def test_status_combines_docker_health_and_the_health_answer(cli: Cli, machine: Machine, cdd: Path) -> None:
    started(cli, machine, cdd)

    code, body = cli.json("status", "default")

    assert code == 0, body
    result = body["result"]
    assert result["state"] == "running"
    assert result["docker_health"] == "healthy"
    assert result["serving"] is True
    assert result["health"] == {
        "url": "http://localhost:8000/health",
        "http_status": 200,
        "body": {"status": "ready", "connx_odbc_ok": True},
        "error": None,
    }


def test_status_probes_https_when_tls_is_on(cli: Cli, machine: Machine, cdd: Path) -> None:
    configure(cli, cdd, "default", "--set", "MCP_TLS_ENABLED=true", "--set", "MCP_TLS_HOSTS=localhost")
    cli.docker.reply(inspect_args(), stdout=state(health="unhealthy") + "\n")
    cli.probe.health_replies["https://localhost:8000/health"] = HealthReply(status=503, body="not ready")

    code, body = cli.json("status", "default")

    assert code == 0, body
    assert body["result"]["docker_health"] == "unhealthy"
    assert body["result"]["health"]["http_status"] == 503
    assert body["result"]["serving"] is False
    assert cli.probe.calls[0] == "https://localhost:8000/health"


def test_status_of_a_never_started_environment_does_not_probe(cli: Cli, machine: Machine, cdd: Path) -> None:
    configure(cli, cdd)
    cli.docker.reply(inspect_args(), returncode=1, stderr="Error: No such object: connx-mcp-ce-default\n")

    result = cli("status", "default")

    assert result.exit_code == 0, result.output
    assert "not created" in result.stdout
    assert cli.probe.calls == []


# --- live: the real image ---------------------------------------------------------------------------------


@pytest.mark.live
def test_configure_start_status_stop_against_the_real_image(cli: Cli, machine: Machine) -> None:
    """Needs CONNX_MCP_LIVE_CDD, CONNX_MCP_LIVE_USER and CONNX_MCP_LIVE_PASSWORD; the image must be pulled."""
    needed = ["CONNX_MCP_LIVE_CDD", "CONNX_MCP_LIVE_USER", "CONNX_MCP_LIVE_PASSWORD"]
    if missing := [n for n in needed if n not in os.environ]:
        pytest.skip(f"set {', '.join(missing)} to run against the real image")
    real = default_ports()
    cli.docker, cli.probe, cli.clock = real.docker, real.probe, real.clock  # type: ignore[assignment]
    licence = os.environ.get("CONNX_MCP_LIVE_LICENSE_HOST", "host.docker.internal")
    code, body = cli.json(
        "configure", "live-03", "--no-input", "--set", f"CDD_USERNAME={os.environ['CONNX_MCP_LIVE_USER']}",
        "--set", f"CDD_PASSWORD={os.environ['CONNX_MCP_LIVE_PASSWORD']}", "--cdd", os.environ["CONNX_MCP_LIVE_CDD"],
        "--set", f"CONNX_LICENSE_HOST={licence}",
    )  # fmt: skip
    assert code == 0, body
    try:
        code, body = cli.json("start", "live-03", "--timeout", "300")
        assert code == 0, body
        assert "@sha256:" in host_settings(machine, "live-03")["image_digest"]

        code, body = cli.json("status", "live-03")
        assert code == 0, body
        assert body["result"]["docker_health"] == "healthy"
        assert body["result"]["serving"] is True
    finally:
        code, body = cli.json("stop", "live-03")
        assert code == 0, body
        real.docker.run(["compose", "-p", "connx-mcp-ce-live-03", "down", "--volumes"])
