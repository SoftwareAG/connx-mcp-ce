#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_tls.py - The administrator runs TLS and is warned before certificates expire.

Description:
    Through the CLI seam with the fake Docker port and endpoint probe: `configure
    --certs` validates a certificate directory the way the server will at startup; the directory,
    or the named volume when none is given, is mounted at the server's certificate path; `status`
    and `start` show the served certificate's expiry with thresholds relative to its lifetime, and
    a renewed certificate on disk that is not yet served; `restart --if-cert-changed` restarts only
    when the served certificate differs from the one on disk; an expired generated self-signed
    certificate gets the hint to purge the certificate volume. Test certificates of chosen
    lifetimes come from tests/certificates.py.

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
from datetime import timedelta
from pathlib import Path

import pytest

from connx_mcp import environment, exposure
from connx_mcp.ports import default_ports
from connx_mcp.probe import HealthReply
from tests.certificates import Issued, issue
from tests.conftest import Cli, Machine, bind, mounts
from tests.test_lifecycle import (
    DIGEST,
    HEALTHY_BODY,
    LOG_LINES,
    compose,
    configure,
    env_dir,
    host_settings,
    image_found,
    inspect_args,
    runs_image,
    state,
)

HOST = "mcp.example.com"
DAY = timedelta(days=1)
HOUR = timedelta(hours=1)
PURGE_HINT = "docker volume rm connx-mcp-ce-default-certs"


@pytest.fixture
def cdd(tmp_path: Path) -> Path:
    """A CDD file for the environment to mount."""
    path = tmp_path / "dictionaries" / "sales.cdd"
    path.parent.mkdir()
    path.write_bytes(b"CDD")
    return path


@pytest.fixture
def certs(tmp_path: Path) -> Path:
    """An empty certificate directory."""
    path = tmp_path / "certs"
    path.mkdir()
    return path


def tls_on(cli: Cli, cdd: Path, *extra: str) -> None:
    """Configure the default environment with TLS on for HOST, plus any extra configure arguments."""
    configure(cli, cdd, "default", "--set", "MCP_TLS_ENABLED=true", "--set", f"MCP_TLS_HOSTS={HOST}", *extra)


def configure_certs(cli: Cli, certs: Path) -> tuple[int, dict]:
    """Run `configure --certs` and return its exit code and JSON envelope."""
    return cli.json("configure", "--no-input", "--certs", str(certs))


# --- configure --certs --------------------------------------------------------------------------------------


def test_configure_takes_a_directory_with_one_pair_of_any_name(
    cli: Cli, machine: Machine, cdd: Path, certs: Path
) -> None:
    tls_on(cli, cdd)
    issue(lifetime=90 * DAY, left=60 * DAY, self_signed=False).write(certs, "whatever")

    code, body = configure_certs(cli, certs)

    assert code == 0, body
    assert host_settings(machine)["cert_dir"] == str(certs)


def test_configure_takes_the_pair_named_after_the_primary_host(
    cli: Cli, machine: Machine, cdd: Path, certs: Path
) -> None:
    configure(cli, cdd, "default", "--set", "MCP_TLS_ENABLED=true", "--set", f"MCP_TLS_HOSTS={HOST},10.0.0.5")
    issue(lifetime=90 * DAY, left=60 * DAY, self_signed=False).write(certs, HOST)
    issue(lifetime=90 * DAY, left=60 * DAY, host="other.example.com", self_signed=False).write(certs, "other")

    code, body = configure_certs(cli, certs)

    assert code == 0, body


def test_configure_refuses_several_pairs_none_named_after_the_primary_host(cli: Cli, cdd: Path, certs: Path) -> None:
    tls_on(cli, cdd)
    issue(lifetime=90 * DAY, left=60 * DAY).write(certs, "one")
    issue(lifetime=90 * DAY, left=60 * DAY).write(certs, "two")

    code, body = configure_certs(cli, certs)

    assert code == 2
    message = body["error"]["message"]
    assert f"{HOST}.crt" in message
    assert "one.crt" in message and "two.crt" in message


@pytest.mark.parametrize("lone", ["crt", "key"])
def test_configure_refuses_a_lone_certificate_or_key(cli: Cli, cdd: Path, certs: Path, lone: str) -> None:
    tls_on(cli, cdd)
    issue(lifetime=90 * DAY, left=60 * DAY).write(certs, HOST)
    (certs / f"{HOST}.{'key' if lone == 'crt' else 'crt'}").unlink()

    code, body = configure_certs(cli, certs)

    assert code == 2
    assert f"{HOST}.{lone}" in body["error"]["message"]


def test_configure_refuses_an_expired_certificate(cli: Cli, machine: Machine, cdd: Path, certs: Path) -> None:
    tls_on(cli, cdd)
    issue(lifetime=90 * DAY, left=-DAY, self_signed=False).write(certs, HOST)

    code, body = configure_certs(cli, certs)

    assert code == 2
    assert "expired" in body["error"]["message"]
    assert "cert_dir" not in host_settings(machine)


def test_configure_refuses_a_directory_that_does_not_exist(cli: Cli, cdd: Path, tmp_path: Path) -> None:
    tls_on(cli, cdd)

    code, body = configure_certs(cli, tmp_path / "nowhere")

    assert code == 2
    assert "no such directory" in body["error"]["message"]


def test_configure_takes_an_empty_directory_for_the_server_to_generate_into(cli: Cli, cdd: Path, certs: Path) -> None:
    tls_on(cli, cdd)

    code, body = configure_certs(cli, certs)

    assert code == 0, body


def test_tls_without_hosts_or_a_certificate_pair_is_refused(cli: Cli, cdd: Path) -> None:
    configure(cli, cdd)

    code, body = cli.json("configure", "--no-input", "--set", "MCP_TLS_ENABLED=true")

    assert code == 2
    assert "MCP_TLS_HOSTS" in body["error"]["message"]


def test_tls_without_hosts_is_taken_when_the_directory_holds_a_pair(cli: Cli, cdd: Path, certs: Path) -> None:
    configure(cli, cdd)
    issue(lifetime=90 * DAY, left=60 * DAY, self_signed=False).write(certs, "server")

    code, body = cli.json("configure", "--no-input", "--set", "MCP_TLS_ENABLED=true", "--certs", str(certs))

    assert code == 0, body


def test_a_certificate_directory_without_tls_warns_it_has_no_effect(cli: Cli, cdd: Path, certs: Path) -> None:
    configure(cli, cdd)

    code, body = configure_certs(cli, certs)

    assert code == 0, body
    assert any("MCP_TLS_ENABLED" in w for w in body["result"]["warnings"])


@pytest.mark.parametrize("spelling", ["yes", "on", "1", "TRUE"])
def test_status_and_configure_agree_tls_is_on_for_every_spelling_the_server_takes(
    cli: Cli, machine: Machine, cdd: Path, certs: Path, spelling: str
) -> None:
    # The server parses MCP_TLS_ENABLED as a pydantic bool; a hand-edited server.env may spell "on" any of its ways.
    issued = issue(lifetime=90 * DAY, left=60 * DAY, self_signed=False)
    issued.write(certs, HOST)
    running_with_tls(cli, issued, cdd=cdd)
    server_env = env_dir(machine) / "server.env"
    server_env.write_text(server_env.read_text().replace("MCP_TLS_ENABLED=true", f"MCP_TLS_ENABLED={spelling}"))

    code, configured = configure_certs(cli, certs)
    status_code, status = cli.json("status", "default")

    assert code == 0, configured
    assert not any("has no effect" in w for w in configured["result"]["warnings"]), configured
    assert status_code == 0, status
    assert status["result"]["certificate"] is not None


# --- the certificate mount ----------------------------------------------------------------------------------


def comes_up_healthy(cli: Cli, machine: Machine, port: int = 8000) -> None:
    """Compose up succeeds and the server answers healthy over HTTPS on a port."""
    cli.docker.reply([*compose(machine), "up", "--detach", "--pull", "never", "--no-build"])
    cli.docker.reply(inspect_args(), stdout=state() + "\n")
    cli.probe.health_replies[f"https://localhost:{port}/health"] = HealthReply(status=200, body=HEALTHY_BODY)
    runs_image(cli)


def test_the_certificate_directory_is_mounted_at_the_servers_certificate_path(
    cli: Cli, machine: Machine, cdd: Path, certs: Path
) -> None:
    tls_on(cli, cdd, "--certs", str(certs))
    issue(lifetime=90 * DAY, left=60 * DAY, self_signed=False).write(certs, HOST)
    image_found(cli)
    comes_up_healthy(cli, machine)

    code, body = cli.json("start")

    assert code == 0, body
    rendered = (env_dir(machine) / "compose.yml").read_text(encoding="utf-8")
    assert bind(certs, "/opt/softwareag/connx/mcp/certs", read_only=True) in mounts(rendered)
    assert "connx-mcp-ce-default-certs" not in rendered


def test_an_empty_certificate_directory_is_mounted_writable_for_the_server_to_generate_into(
    cli: Cli, machine: Machine, cdd: Path, certs: Path
) -> None:
    tls_on(cli, cdd, "--certs", str(certs))
    image_found(cli)
    comes_up_healthy(cli, machine)

    code, body = cli.json("start")

    assert code == 0, body
    rendered = (env_dir(machine) / "compose.yml").read_text(encoding="utf-8")
    assert bind(certs, "/opt/softwareag/connx/mcp/certs") in mounts(rendered)


def test_without_a_directory_the_named_volume_is_mounted(cli: Cli, machine: Machine, cdd: Path) -> None:
    tls_on(cli, cdd)
    image_found(cli)
    comes_up_healthy(cli, machine)

    code, body = cli.json("start")

    assert code == 0, body
    rendered = (env_dir(machine) / "compose.yml").read_text(encoding="utf-8")
    assert 'source: certs\n        target: "/opt/softwareag/connx/mcp/certs"' in rendered
    assert 'name: "connx-mcp-ce-default-certs"' in rendered


# --- the served certificate's expiry ------------------------------------------------------------------------


def running_with_tls(cli: Cli, served: Issued, *extra: str, cdd: Path) -> None:
    """A configured TLS environment whose running container serves `served`."""
    tls_on(cli, cdd, *extra)
    cli.docker.reply(inspect_args(), stdout=state() + "\n")
    cli.probe.health_replies["https://localhost:8000/health"] = HealthReply(status=200, body=HEALTHY_BODY)
    cli.probe.certificates[("localhost", 8000)] = served.der


@pytest.mark.parametrize(
    ("lifetime", "left", "level"),
    [
        (90 * DAY, 60 * DAY, "ok"),
        (90 * DAY, 20 * DAY, "warning"),
        (90 * DAY, 5 * DAY, "critical"),
        (45 * DAY, 16 * DAY, "ok"),
        (45 * DAY, 14 * DAY, "warning"),
        (6 * DAY, 30 * HOUR, "warning"),
        (6 * DAY, 20 * HOUR, "critical"),
        (365 * DAY, 30 * DAY, "critical"),
        (90 * DAY, -HOUR, "expired"),
    ],
)
def test_status_rates_the_served_certificate_against_its_lifetime(
    cli: Cli, cdd: Path, lifetime: timedelta, left: timedelta, level: str
) -> None:
    running_with_tls(cli, issue(lifetime=lifetime, left=left, self_signed=False), cdd=cdd)

    code, body = cli.json("status", "default")

    assert code == 0, body
    served = body["result"]["certificate"]["served"]
    assert served["level"] == level
    assert served["days_left"] == pytest.approx(left / DAY, abs=0.01)
    notices = body["result"]["notices"]
    if level == "ok":
        assert not [n for n in notices if "certificate" in n]
    else:
        assert any(n.startswith(f"{level.capitalize()}: the served certificate") for n in notices), notices


def test_human_status_shows_the_certificate_expiry(cli: Cli, cdd: Path) -> None:
    running_with_tls(cli, issue(lifetime=90 * DAY, left=5 * DAY + HOUR, self_signed=False), cdd=cdd)

    result = cli("status", "default")

    assert result.exit_code == 0, result.output
    assert "Certificate:   expires in 5 days" in result.stdout
    assert "Critical: the served certificate" in result.stdout


def test_status_without_tls_does_not_read_a_certificate(cli: Cli, machine: Machine, cdd: Path) -> None:
    configure(cli, cdd)
    cli.docker.reply(inspect_args(), stdout=state() + "\n")

    code, body = cli.json("status", "default")

    assert code == 0, body
    assert body["result"]["certificate"] is None
    assert not [c for c in cli.probe.calls if c.startswith("cert ")]


def test_start_warns_about_a_certificate_near_expiry(cli: Cli, machine: Machine, cdd: Path) -> None:
    tls_on(cli, cdd)
    image_found(cli)
    comes_up_healthy(cli, machine)
    cli.probe.certificates[("localhost", 8000)] = issue(lifetime=6 * DAY, left=10 * HOUR, self_signed=False).der

    code, body = cli.json("start")

    assert code == 0, body
    assert any(w.startswith("Critical: the served certificate") for w in body["result"]["warnings"])


# --- renewed on disk, not yet served -----------------------------------------------------------------------


def test_status_reports_a_renewed_certificate_on_disk_that_is_not_yet_served(cli: Cli, cdd: Path, certs: Path) -> None:
    old = issue(lifetime=90 * DAY, left=10 * DAY, self_signed=False)
    issue(lifetime=90 * DAY, left=89 * DAY, self_signed=False).write(certs, HOST)
    running_with_tls(cli, old, "--certs", str(certs), cdd=cdd)

    code, body = cli.json("status", "default")

    assert code == 0, body
    assert body["result"]["certificate"]["renewed_not_served"] is True
    assert body["result"]["certificate"]["on_disk"]["days_left"] == pytest.approx(89, abs=0.01)
    assert any("connx-mcp-ce restart default --if-cert-changed" in n for n in body["result"]["notices"])


def test_status_is_quiet_when_the_served_certificate_is_the_one_on_disk(cli: Cli, cdd: Path, certs: Path) -> None:
    current = issue(lifetime=90 * DAY, left=80 * DAY, self_signed=False)
    current.write(certs, HOST)
    running_with_tls(cli, current, "--certs", str(certs), cdd=cdd)

    code, body = cli.json("status", "default")

    assert code == 0, body
    assert body["result"]["certificate"]["renewed_not_served"] is False
    assert body["result"]["notices"] == [exposure.warning(environment.load("default"))]


def test_status_reads_the_certificate_directory_the_container_was_started_with(
    cli: Cli, machine: Machine, cdd: Path, certs: Path, tmp_path: Path
):
    tls_on(cli, cdd, "--certs", str(certs))
    issue(lifetime=90 * DAY, left=10 * DAY, self_signed=False).write(certs, HOST)
    one_run = tmp_path / "one-run-certs"
    served = issue(lifetime=90 * DAY, left=80 * DAY, self_signed=False)
    served.write(one_run, HOST)
    image_found(cli)
    comes_up_healthy(cli, machine)
    cli.probe.certificates[("localhost", 8000)] = served.der
    code, body = cli.json("start", "--certs", str(one_run))
    assert code == 0, body

    code, body = cli.json("status", "default")

    assert code == 0, body
    certificate = body["result"]["certificate"]
    assert certificate["directory"] == str(one_run)
    assert certificate["renewed_not_served"] is False
    assert not [n for n in body["result"]["notices"] if "differs from the served one" in n]


# --- restart --if-cert-changed ------------------------------------------------------------------------------


def test_restart_if_cert_changed_does_nothing_while_the_served_certificate_is_current(
    cli: Cli, machine: Machine, cdd: Path, certs: Path
) -> None:
    current = issue(lifetime=90 * DAY, left=80 * DAY, self_signed=False)
    current.write(certs, HOST)
    running_with_tls(cli, current, "--certs", str(certs), cdd=cdd)

    code, body = cli.json("restart", "--if-cert-changed")

    assert code == 0, body
    assert body["result"]["restarted"] is False
    assert not [c for c in cli.docker.calls if c[:2] == ["compose", "-p"]]


def test_restart_if_cert_changed_restarts_for_a_renewed_certificate(
    cli: Cli, machine: Machine, cdd: Path, certs: Path
) -> None:
    issue(lifetime=90 * DAY, left=89 * DAY, self_signed=False).write(certs, HOST)
    running_with_tls(cli, issue(lifetime=90 * DAY, left=2 * DAY, self_signed=False), "--certs", str(certs), cdd=cdd)
    image_found(cli)
    (env_dir(machine) / "compose.yml").write_text("# an earlier start\n", encoding="utf-8")
    cli.docker.reply([*compose(machine), "stop", "--timeout", "10"])
    cli.docker.reply([*compose(machine), "up", "--detach", "--pull", "never", "--no-build"])
    runs_image(cli)

    code, body = cli.json("restart", "--if-cert-changed")

    assert code == 0, body
    assert body["result"]["restarted"] is True
    assert [*compose(machine), "stop", "--timeout", "10"] in cli.docker.calls


def test_restart_if_cert_changed_needs_a_certificate_directory(cli: Cli, cdd: Path) -> None:
    running_with_tls(cli, issue(lifetime=90 * DAY, left=80 * DAY), cdd=cdd)

    code, body = cli.json("restart", "--if-cert-changed")

    assert code == 70
    assert body["error"]["code"] == "certificate_directory_not_set"
    assert body["error"]["next"] == "connx-mcp-ce configure default --certs <directory>"


# --- an expired generated self-signed certificate -----------------------------------------------------------


def test_an_expired_generated_certificate_gets_the_purge_hint(cli: Cli, cdd: Path) -> None:
    running_with_tls(cli, issue(lifetime=30 * DAY, left=-DAY), cdd=cdd)

    code, body = cli.json("status", "default")

    assert code == 0, body
    notice = next(n for n in body["result"]["notices"] if n.startswith("Expired:"))
    assert "generated self-signed" in notice
    assert PURGE_HINT in notice


def test_a_provided_certificate_never_gets_the_purge_hint(cli: Cli, cdd: Path, certs: Path) -> None:
    provided = issue(lifetime=30 * DAY, left=2 * DAY)
    provided.write(certs, HOST)
    running_with_tls(cli, provided, "--certs", str(certs), cdd=cdd)

    code, body = cli.json("status", "default")

    assert code == 0, body
    assert body["result"]["notices"]
    assert not [n for n in body["result"]["notices"] if PURGE_HINT in n]


def reads_the_certificate_volume(args: list[str]) -> bool:
    """Match the Docker call that reads the generated certificate from the certificate volume."""
    return args[:2] == ["run", "--rm"] and "connx-mcp-ce-default-certs:/opt/softwareag/connx/mcp/certs:ro" in args


def exits_while_starting(cli: Cli, machine: Machine, cdd: Path) -> None:
    """A TLS environment whose container exits with code 1 while starting."""
    tls_on(cli, cdd)
    image_found(cli)
    cli.docker.reply([*compose(machine), "up", "--detach", "--pull", "never", "--no-build"])
    cli.docker.reply(inspect_args(), stdout=state(status="exited", health="unhealthy", exit_code=1) + "\n")
    cli.docker.reply(["logs", "--tail", "40", "--timestamps", "connx-mcp-ce-default"], stdout=LOG_LINES)
    cli.docker.reply_when(lambda args: args[:2] == ["logs", "--since"], stdout=LOG_LINES)


def test_a_start_that_exits_on_an_expired_generated_certificate_gets_the_purge_hint(
    cli: Cli, machine: Machine, cdd: Path
) -> None:
    exits_while_starting(cli, machine, cdd)
    expired = issue(lifetime=365 * DAY, left=-2 * DAY)
    cli.docker.reply_when(reads_the_certificate_volume, stdout=expired.pem.decode("ascii"))

    code, body = cli.json("start")

    assert code == 71
    assert body["error"]["code"] == "certificate_expired"
    message = body["error"]["message"]
    assert "exited with code 1" in message
    assert "Starting CONNX MCP server" in message
    assert PURGE_HINT in message
    assert PURGE_HINT in body["error"]["next"]


def test_a_start_that_exits_with_a_valid_generated_certificate_keeps_its_failure(
    cli: Cli, machine: Machine, cdd: Path
) -> None:
    exits_while_starting(cli, machine, cdd)
    valid = issue(lifetime=365 * DAY, left=200 * DAY)
    cli.docker.reply_when(reads_the_certificate_volume, stdout=valid.pem.decode("ascii"))

    code, body = cli.json("start")

    assert code == 32
    assert PURGE_HINT not in body["error"]["message"]


def test_a_start_that_exits_before_any_certificate_was_generated_keeps_its_failure(
    cli: Cli, machine: Machine, cdd: Path
) -> None:
    exits_while_starting(cli, machine, cdd)
    cli.docker.reply_when(reads_the_certificate_volume, returncode=1, stderr="cat: can't open '*.crt'\n")

    code, body = cli.json("start")

    assert code == 32


def exited_with_tls(cli: Cli, machine: Machine, cdd: Path) -> None:
    """A TLS environment, pinned by an earlier start, whose container has exited."""
    tls_on(cli, cdd)
    host_file = env_dir(machine) / "host.json"
    host_file.write_text(json.dumps({**host_settings(machine), "image_digest": DIGEST}), encoding="utf-8")
    image_found(cli)
    runs_image(cli)
    cli.docker.reply(inspect_args(), stdout=state(status="exited", health="unhealthy", exit_code=1) + "\n")


def test_status_of_an_exited_container_with_an_expired_generated_certificate_gets_the_purge_hint(
    cli: Cli, machine: Machine, cdd: Path
) -> None:
    exited_with_tls(cli, machine, cdd)
    cli.docker.reply_when(reads_the_certificate_volume, stdout=issue(lifetime=30 * DAY, left=-DAY).pem.decode("ascii"))

    code, body = cli.json("status", "default")

    assert code == 0, body
    notice = next(n for n in body["result"]["notices"] if PURGE_HINT in n)
    assert "generated certificate expired" in notice


def test_status_of_an_exited_container_with_a_valid_generated_certificate_has_no_purge_hint(
    cli: Cli, machine: Machine, cdd: Path
) -> None:
    exited_with_tls(cli, machine, cdd)
    cli.docker.reply_when(reads_the_certificate_volume, stdout=issue(lifetime=30 * DAY, left=DAY).pem.decode("ascii"))

    code, body = cli.json("status", "default")

    assert code == 0, body
    assert not [n for n in body["result"]["notices"] if PURGE_HINT in n]


# --- live: a certificate valid for hours ----------------------------------------------------------------------


@pytest.mark.live
def test_a_certificate_valid_for_hours_shows_the_critical_notice(cli: Cli, machine: Machine, certs: Path) -> None:
    """Needs CONNX_MCP_LIVE_CDD, CONNX_MCP_LIVE_USER and CONNX_MCP_LIVE_PASSWORD; the image must be pulled."""
    needed = ["CONNX_MCP_LIVE_CDD", "CONNX_MCP_LIVE_USER", "CONNX_MCP_LIVE_PASSWORD"]
    if missing := [n for n in needed if n not in os.environ]:
        pytest.skip(f"set {', '.join(missing)} to run against the real image")
    real = default_ports()
    cli.docker, cli.probe, cli.clock = real.docker, real.probe, real.clock  # type: ignore[assignment]
    issue(lifetime=6 * HOUR, left=5 * HOUR, host="localhost", self_signed=False).write(certs, "localhost")
    certs.chmod(0o755)
    for file in certs.iterdir():
        file.chmod(0o644)
    licence = os.environ.get("CONNX_MCP_LIVE_LICENSE_HOST", "host.docker.internal")
    code, body = cli.json(
        "configure", "live-07", "--no-input", "--set", f"CDD_USERNAME={os.environ['CONNX_MCP_LIVE_USER']}",
        "--set", f"CDD_PASSWORD={os.environ['CONNX_MCP_LIVE_PASSWORD']}", "--cdd", os.environ["CONNX_MCP_LIVE_CDD"],
        "--set", f"CONNX_LICENSE_HOST={licence}", "--set", "MCP_TLS_ENABLED=true",
        "--set", "MCP_TLS_HOSTS=localhost", "--certs", str(certs),
    )  # fmt: skip
    assert code == 0, body
    try:
        code, body = cli.json("start", "live-07", "--timeout", "300")
        assert code == 0, body
        assert any(w.startswith("Critical: the served certificate") for w in body["result"]["warnings"])

        code, body = cli.json("status", "live-07")
        assert code == 0, body
        assert body["result"]["certificate"]["served"]["level"] == "critical"
        assert body["result"]["certificate"]["renewed_not_served"] is False
    finally:
        cli.json("stop", "live-07")
        real.docker.run(["compose", "-p", "connx-mcp-ce-live-07", "down", "--volumes"])
