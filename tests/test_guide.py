#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_guide.py - The administrator or an LLM runs `guide` and learns the one next step.

Description:
    Through the CLI seam with the fake Docker port and endpoint probe: `guide` inspects
    Docker, the environments, their images, health, certificates and the demo binding with the
    `status` checks and names exactly one next step as a runnable command, in a fixed priority
    order, with any warnings; `--json` gives the same in the shared envelope. One test per state
    in that order, and a walk-through from an empty machine in which each suggested command, run
    through the seam, advances to the next step until the environment is healthy. The live test
    follows `guide` against real Docker and the real image from the host.

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
import shlex
import shutil
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from connx_mcp import environment, exposure
from connx_mcp.docker import DockerResult
from connx_mcp.ports import default_ports
from connx_mcp.probe import HealthReply
from tests.certificates import issue
from tests.conftest import Cli, Machine, as_typed
from tests.fakes import FakeDocker
from tests.test_install_skill import skill_in_image
from tests.test_lifecycle import (
    DIGEST,
    HEALTHY_BODY,
    IMAGE,
    IMAGE_FORMAT,
    PASSWORD,
    compose,
    configure,
    env_dir,
    host_settings,
    image_found,
    image_id,
    inspect_args,
    runs_image,
    serving,
    state,
)

REGISTRY = "softwareagcr.azurecr.io"
DAY = timedelta(days=1)
HOST = "mcp.example.com"
NO_SUCH_CONTAINER = "Error response from daemon: No such container: connx-mcp-ce-{name}"


@pytest.fixture(autouse=True)
def docker_config(machine: Machine, monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest) -> Path:
    """Docker's client configuration lives in the test's home directory, as it does on a fresh machine.

    A live test keeps the real one (conftest's machine fixture): its context names the daemon's socket.
    """
    if request.node.get_closest_marker("live") is None:
        monkeypatch.delenv("DOCKER_CONFIG", raising=False)
    return machine.home / ".docker" / "config.json"


@pytest.fixture
def cdd(tmp_path: Path) -> Path:
    """A CDD file on this machine."""
    path = tmp_path / "dictionaries" / "sales.cdd"
    path.parent.mkdir()
    path.write_bytes(b"CDD")
    return path


def signed_in(docker_config: Path, registry: str = REGISTRY) -> None:
    """Docker holds a sign-in for the registry, as `docker login` records it."""
    docker_config.parent.mkdir(parents=True, exist_ok=True)
    docker_config.write_text(json.dumps({"auths": {registry: {}}, "credsStore": "desktop"}), encoding="utf-8")


def image_missing(cli: Cli) -> None:
    """The environment's image reference is not on this machine."""
    cli.docker.reply(["image", "inspect", "--format", IMAGE_FORMAT, IMAGE], 1, stderr=f"Error: No such image: {IMAGE}")


def never_started(cli: Cli, name: str = "default") -> None:
    """The environment has no container."""
    cli.docker.reply(inspect_args(name), 1, stderr=NO_SUCH_CONTAINER.format(name=name))


def pinned(machine: Machine, name: str = "default") -> None:
    """The environment's image digest is pinned, as its first start leaves it."""
    path = env_dir(machine, name) / "host.json"
    path.write_text(
        json.dumps({**host_settings(machine, name), "image_digest": DIGEST, "image_version": "15.0.3.26012"}),
        encoding="utf-8",
    )


def running(cli: Cli, machine: Machine, health: str | None = "healthy", name: str = "default") -> None:
    """The environment is pinned and its container runs the pinned image."""
    pinned(machine, name)
    image_found(cli)
    cli.docker.reply(inspect_args(name), stdout=state(health=health) + "\n")
    runs_image(cli, name=name)


def bound_to_keycloak(cli: Cli) -> None:
    """The demo stack runs Keycloak and the environment signs in with it."""
    assert cli.json("configure-demo", "--keycloak")[0] == 0
    code, body = cli.json("configure", "--demo", "keycloak")
    assert code == 0, body


def guide(cli: Cli, *args: str) -> dict[str, Any]:
    """Run `guide --json` and return its result, which must succeed."""
    code, body = cli.json("guide", *args)
    assert code == 0, body
    assert body["ok"] is True
    assert body["command"] == "guide"
    return body["result"]


# --- one test per state, in priority order -------------------------------------------------------------------


def test_without_docker_the_next_step_is_installing_it(cli: Cli) -> None:
    cli.docker = FakeDocker.from_recording("docker-missing")

    result = guide(cli)

    assert result["stage"] == "docker"
    assert result["next"] == "docker version"
    assert "https://docs.docker.com/engine/install/" in result["reason"]
    assert cli.docker.calls == []


def test_a_stopped_daemon_is_the_next_step_before_anything_else(cli: Cli, cdd: Path) -> None:
    configure(cli, cdd)
    cli.docker = FakeDocker.from_recording("daemon-stopped")

    result = guide(cli)

    assert result["stage"] == "docker"
    assert result["next"] == "docker info"


def test_on_an_empty_machine_the_next_step_is_configure(cli: Cli, machine: Machine) -> None:
    result = guide(cli)

    assert result["stage"] == "no_environment"
    assert result["environment"] == "default"
    assert result["next"] == "connx-mcp-ce configure"
    assert str(machine.base) in result["reason"]


def test_on_an_empty_machine_without_mcp_environment_path_guide_names_the_default_base_directory(
    cli: Cli, machine: Machine, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("MCP_ENVIRONMENT_PATH")

    result = guide(cli)

    assert result["stage"] == "no_environment"
    assert str(machine.home / ".connx" / "connx-mcp-ce") in result["reason"]


def test_a_named_environment_that_does_not_exist_is_configured_by_name(cli: Cli, cdd: Path) -> None:
    configure(cli, cdd)

    result = guide(cli, "sales")

    assert result["stage"] == "no_environment"
    assert result["next"] == "connx-mcp-ce configure sales"


def test_an_environment_whose_cdd_file_is_gone_is_not_configured(cli: Cli, cdd: Path) -> None:
    configure(cli, cdd)
    cdd.unlink()

    result = guide(cli)

    assert result["stage"] == "not_configured"
    assert result["next"] == "connx-mcp-ce configure default --cdd <path of the .cdd file>"
    assert result["fill_in"] == ["<path of the .cdd file>"]
    assert str(cdd) in result["reason"]


def test_an_unreadable_environment_is_not_configured(cli: Cli, machine: Machine, cdd: Path) -> None:
    configure(cli, cdd)
    (env_dir(machine) / "host.json").write_text("{not json", encoding="utf-8")

    result = guide(cli)

    assert result["stage"] == "not_configured"
    assert result["next"] == "connx-mcp-ce configure default"
    assert "cannot be read" in result["reason"]


def test_without_a_registry_sign_in_the_next_step_is_login(cli: Cli, cdd: Path) -> None:
    configure(cli, cdd)
    image_missing(cli)

    result = guide(cli)

    assert result["stage"] == "no_image"
    assert result["next"] == "connx-mcp-ce login"
    assert REGISTRY in result["reason"]


def test_signed_in_without_the_image_the_next_step_is_download(cli: Cli, cdd: Path, docker_config: Path) -> None:
    configure(cli, cdd)
    image_missing(cli)
    signed_in(docker_config)

    result = guide(cli)

    assert result["stage"] == "no_image"
    assert result["next"] == "connx-mcp-ce download default"
    assert IMAGE in result["reason"]


def test_with_the_image_pulled_the_next_step_is_start(cli: Cli, cdd: Path) -> None:
    configure(cli, cdd)
    image_found(cli)
    never_started(cli)

    result = guide(cli)

    assert result["stage"] == "not_started"
    assert result["next"] == "connx-mcp-ce start default"


def test_a_server_that_exited_with_an_error_shows_its_log_rather_than_starting_it_again(
    cli: Cli, machine: Machine, cdd: Path
) -> None:
    configure(cli, cdd)
    pinned(machine)
    image_found(cli)
    cli.docker.reply(inspect_args(), stdout=state(status="exited", health="unhealthy", exit_code=3) + "\n")
    runs_image(cli)

    result = guide(cli)

    assert result["stage"] == "not_started"
    assert result["next"] == "connx-mcp-ce log default --tail 100"
    assert "exit code 3" in result["reason"]
    assert "connx-mcp-ce start default" in result["reason"]


def test_a_cleanly_stopped_server_is_started_again(cli: Cli, machine: Machine, cdd: Path) -> None:
    configure(cli, cdd)
    pinned(machine)
    image_found(cli)
    cli.docker.reply(inspect_args(), stdout=state(status="exited", health=None, exit_code=0) + "\n")
    runs_image(cli)

    result = guide(cli)

    assert result["next"] == "connx-mcp-ce start default"


def test_a_paused_server_is_unpaused(cli: Cli, machine: Machine, cdd: Path) -> None:
    configure(cli, cdd)
    pinned(machine)
    image_found(cli)
    cli.docker.reply(inspect_args(), stdout=state(status="paused", health="healthy") + "\n")
    runs_image(cli)

    result = guide(cli)

    assert result["stage"] == "unhealthy"
    assert result["next"] == "docker unpause connx-mcp-ce-default"


def test_a_running_server_that_does_not_answer_health_is_unhealthy(cli: Cli, machine: Machine, cdd: Path) -> None:
    configure(cli, cdd)
    running(cli, machine, health="unhealthy")
    cli.probe.health_replies["http://localhost:8000/health"] = HealthReply(status=503, body='{"status": "degraded"}')

    result = guide(cli)

    assert result["stage"] == "unhealthy"
    assert result["next"] == "connx-mcp-ce log default --tail 100"
    assert "503" in result["reason"]


def test_a_server_still_starting_is_checked_again_with_status(cli: Cli, machine: Machine, cdd: Path) -> None:
    configure(cli, cdd)
    running(cli, machine, health="starting")

    result = guide(cli)

    assert result["stage"] == "unhealthy"
    assert result["next"] == "connx-mcp-ce status default"


def test_a_provided_certificate_near_expiry_is_renewed_and_restarted(
    cli: Cli, machine: Machine, cdd: Path, tmp_path: Path
) -> None:
    certs = tmp_path / "certs"
    served = issue(lifetime=90 * DAY, left=5 * DAY, self_signed=False)
    served.write(certs, HOST)
    configure(cli, cdd, "default", "--set", "MCP_TLS_ENABLED=true", "--set", f"MCP_TLS_HOSTS={HOST}",
              "--certs", str(certs))  # fmt: skip
    running(cli, machine)
    cli.probe.health_replies["https://localhost:8000/health"] = HealthReply(status=200, body=HEALTHY_BODY)
    cli.probe.certificates[("localhost", 8000)] = served.der

    result = guide(cli)

    assert result["stage"] == "certificate"
    assert result["next"] == "connx-mcp-ce restart default --if-cert-changed"
    assert "critical" in result["reason"]
    assert any(str(certs) in w for w in result["warnings"])


def test_a_renewed_certificate_not_yet_served_is_served_by_a_restart(
    cli: Cli, machine: Machine, cdd: Path, tmp_path: Path
) -> None:
    certs = tmp_path / "certs"
    issue(lifetime=90 * DAY, left=89 * DAY, self_signed=False).write(certs, HOST)
    configure(cli, cdd, "default", "--set", "MCP_TLS_ENABLED=true", "--set", f"MCP_TLS_HOSTS={HOST}",
              "--certs", str(certs))  # fmt: skip
    running(cli, machine)
    cli.probe.health_replies["https://localhost:8000/health"] = HealthReply(status=200, body=HEALTHY_BODY)
    cli.probe.certificates[("localhost", 8000)] = issue(lifetime=90 * DAY, left=60 * DAY, self_signed=False).der

    result = guide(cli)

    assert result["stage"] == "certificate"
    assert result["next"] == "connx-mcp-ce restart default --if-cert-changed"
    assert "not served yet" in result["reason"]


def test_a_generated_certificate_near_expiry_is_replaced_by_purging_its_volume(
    cli: Cli, machine: Machine, cdd: Path
) -> None:
    configure(cli, cdd, "default", "--set", "MCP_TLS_ENABLED=true", "--set", f"MCP_TLS_HOSTS={HOST}")
    running(cli, machine)
    cli.probe.health_replies["https://localhost:8000/health"] = HealthReply(status=200, body=HEALTHY_BODY)
    cli.probe.certificates[("localhost", 8000)] = issue(lifetime=90 * DAY, left=20 * DAY).der

    result = guide(cli)

    assert result["stage"] == "certificate"
    assert "docker volume rm connx-mcp-ce-default-certs" in result["next"]
    assert result["next"].endswith("connx-mcp-ce start default")
    assert "deletes" in result["reason"]


def test_a_bound_environment_whose_url_moved_renders_the_realm_again(cli: Cli, machine: Machine, cdd: Path) -> None:
    configure(cli, cdd)
    assert cli.json("configure-demo", "--keycloak", "--for", "default")[0] == 0
    path = env_dir(machine) / "host.json"
    path.write_text(json.dumps({**host_settings(machine), "published_port": 8005}), encoding="utf-8")
    running(cli, machine)
    serving(cli, port=8005)

    result = guide(cli)

    assert result["stage"] == "demo_binding"
    assert result["next"] == "connx-mcp-ce start-demo"
    assert "8005" in result["reason"]


def test_a_stack_bound_to_a_removed_environment_is_rebound(cli: Cli, machine: Machine, cdd: Path) -> None:
    configure(cli, cdd, "gone")
    assert cli.json("configure-demo", "--keycloak", "--for", "gone")[0] == 0
    shutil.rmtree(env_dir(machine, "gone"))
    configure(cli, cdd)
    running(cli, machine)
    serving(cli)

    result = guide(cli)

    assert result["stage"] == "demo_binding"
    assert result["next"] == "connx-mcp-ce configure-demo --for default --rebind"
    assert "'gone'" in result["reason"]


@pytest.mark.parametrize(
    ("line", "fix"),
    [
        ("MCP_TLS_ENABLED=true\nMCP_TLS_HOSTS=localhost\n", "connx-mcp-ce configure default --certs <directory>"),
        ("MCP_PATH=/sql\n", "connx-mcp-ce configure default --unset MCP_PATH"),
    ],
    ids=["self-signed", "mcp-path"],
)
def test_a_stopped_bound_environment_that_start_refuses_gets_starts_own_fix(
    cli: Cli, machine: Machine, cdd: Path, line: str, fix: str
) -> None:
    configure(cli, cdd)
    bound_to_keycloak(cli)
    server_env = env_dir(machine) / "server.env"
    server_env.write_text(server_env.read_text(encoding="utf-8") + line, encoding="utf-8")
    code, body = cli.json("start")
    assert code == 2, body

    result = guide(cli)

    assert result["stage"] == "demo_binding"
    assert result["next"] == body["error"]["next"] == fix
    assert result["reason"] == body["error"]["message"]


def test_a_healthy_environment_without_the_skill_ends_with_install_skill(cli: Cli, machine: Machine, cdd: Path) -> None:
    configure(cli, cdd)
    running(cli, machine)
    serving(cli)

    result = guide(cli)

    assert result["stage"] == "no_skill"
    assert result["done"] is False
    assert result["next"] == "connx-mcp-ce install-skill default --harness <harness>"
    assert result["fill_in"] == ["<harness>"]
    assert result["warnings"] == [exposure.warning(environment.load("default"))]


def test_install_skill_names_the_harnesses_found_on_this_machine(cli: Cli, machine: Machine, cdd: Path) -> None:
    configure(cli, cdd)
    running(cli, machine)
    serving(cli)
    cli.docker.executables["claude"] = "/usr/local/bin/claude"

    result = guide(cli)

    assert result["next"] == "connx-mcp-ce install-skill default --harness claude-code"
    assert result["fill_in"] == []


def skill_installed(cli: Cli, *scope: str) -> None:
    """Install the skill through the CLI, from the pinned image."""
    skill_in_image(cli)
    code, body = cli.json("install-skill", "--harness", "claude-code", *scope)
    assert code == 0, body


def test_a_healthy_environment_with_the_skill_is_done(cli: Cli, machine: Machine, cdd: Path) -> None:
    configure(cli, cdd)
    running(cli, machine)
    serving(cli)
    skill_installed(cli)

    result = guide(cli)

    assert result["stage"] == "healthy"
    assert result["done"] is True
    assert result["next"] is None
    lines = cli("guide").stdout.splitlines()
    assert not any(line.startswith("Next:") for line in lines)
    assert any("Nothing to do" in line for line in lines)


def test_a_removed_skill_copy_brings_install_skill_back(cli: Cli, machine: Machine, cdd: Path) -> None:
    configure(cli, cdd)
    running(cli, machine)
    serving(cli)
    skill_installed(cli)
    shutil.rmtree(machine.home / ".claude" / "skills" / "connx-sql-expert")

    result = guide(cli)

    assert result["stage"] == "no_skill"


def test_a_project_install_of_the_skill_counts(cli: Cli, machine: Machine, cdd: Path, tmp_path: Path) -> None:
    configure(cli, cdd)
    running(cli, machine)
    serving(cli)
    (tmp_path / "project").mkdir()
    skill_installed(cli, "--project", str(tmp_path / "project"))

    assert guide(cli)["done"] is True


def test_another_file_at_the_recorded_zip_brings_install_skill_back(
    cli: Cli, machine: Machine, cdd: Path, tmp_path: Path
) -> None:
    configure(cli, cdd)
    running(cli, machine)
    serving(cli)
    skill_in_image(cli)
    out = tmp_path / "out"
    code, body = cli.json("install-skill", "--harness", "claude-code", "--zip", str(out))
    assert code == 0, body
    (out / "connx-sql-expert.zip").write_bytes(b"not connx-mcp-ce's zip")

    result = guide(cli)

    assert result["stage"] == "no_skill"


def skill_uninstalled(cli: Cli, *flags: str) -> None:
    """Uninstall the skill through the CLI, without a question."""
    code, body = cli.json("uninstall-skill", *flags, "--yes")
    assert code == 0, body


def test_a_skill_uninstalled_on_purpose_is_a_skipped_step(cli: Cli, machine: Machine, cdd: Path) -> None:
    configure(cli, cdd)
    running(cli, machine)
    serving(cli)
    skill_installed(cli)
    skill_uninstalled(cli)

    result = guide(cli)

    assert result["stage"] != "no_skill"
    assert result["done"] is True
    assert result["next"] is None
    assert re.search(
        r"You uninstalled the connx-sql-expert skill on \d{4}-\d{2}-\d{2}, so guide skips its step\.$", result["reason"]
    )
    assert "install-skill" not in cli("guide").stdout


def test_a_partial_uninstall_leaves_the_skill_step_as_it_was(cli: Cli, machine: Machine, cdd: Path) -> None:
    configure(cli, cdd)
    running(cli, machine)
    serving(cli)
    skill_installed(cli)
    skill_in_image(cli)
    code, body = cli.json("install-skill", "--harness", "hermes", "--user")
    assert code == 0, body
    skill_uninstalled(cli, "--harness", "hermes")
    assert guide(cli)["stage"] == "healthy"

    shutil.rmtree(machine.home / ".claude" / "skills" / "connx-sql-expert")

    assert guide(cli)["stage"] == "no_skill"


def test_a_later_install_makes_guide_follow_the_skill_again(cli: Cli, machine: Machine, cdd: Path) -> None:
    configure(cli, cdd)
    running(cli, machine)
    serving(cli)
    skill_installed(cli)
    skill_uninstalled(cli)
    skill_installed(cli)
    assert guide(cli)["stage"] == "healthy"

    shutil.rmtree(machine.home / ".claude" / "skills" / "connx-sql-expert")

    assert guide(cli)["stage"] == "no_skill"


def instructions_installed(cli: Cli, project: Path) -> Path:
    """Install the skill and its instructions for a project with codex; return the AGENTS.md written."""
    skill_in_image(cli)
    project.mkdir()
    code, body = cli.json("install-skill", "--harness", "codex", "--project", str(project))
    assert code == 0, body
    return project / "AGENTS.md"


def test_a_removed_skill_instructions_section_is_a_hint_and_guide_stays_done(
    cli: Cli, machine: Machine, cdd: Path, tmp_path: Path
) -> None:
    configure(cli, cdd)
    running(cli, machine)
    serving(cli)
    agents = instructions_installed(cli, tmp_path / "sales")
    agents.write_text("# AGENTS.md\n\nMy own rules only.\n", encoding="utf-8")

    result = guide(cli)

    assert result["done"] is True
    restore = f"connx-mcp-ce install-skill default --harness codex --project {as_typed(tmp_path / 'sales')}"
    [hint] = [w for w in result["warnings"] if str(agents) in w]
    assert "no longer holds the skill instructions" in hint
    assert hint.endswith(restore)


def test_broken_skill_instructions_markers_are_a_hint_and_guide_stays_done(
    cli: Cli, machine: Machine, cdd: Path, tmp_path: Path
) -> None:
    configure(cli, cdd)
    running(cli, machine)
    serving(cli)
    agents = instructions_installed(cli, tmp_path / "sales")
    agents.write_text(agents.read_text(encoding="utf-8").replace("<!-- connx-mcp-ce end -->", ""), encoding="utf-8")

    result = guide(cli)

    assert result["done"] is True
    [hint] = [w for w in result["warnings"] if str(agents) in w]
    assert "markers" in hint
    assert "connx-mcp-ce install-skill default --harness codex --project" in hint


def test_an_unreadable_skill_instructions_file_is_a_hint_naming_the_read_failure(
    cli: Cli, machine: Machine, cdd: Path, tmp_path: Path
) -> None:
    configure(cli, cdd)
    running(cli, machine)
    serving(cli)
    agents = instructions_installed(cli, tmp_path / "sales")
    agents.write_bytes(agents.read_bytes() + b"\xfc\n")

    result = guide(cli)

    assert result["done"] is True
    [hint] = [w for w in result["warnings"] if str(agents) in w]
    assert "cannot be read as UTF-8 text" in hint
    assert "no longer holds" not in hint


def test_an_older_install_record_still_counts_the_skill_and_gives_its_hints(
    cli: Cli, machine: Machine, cdd: Path, tmp_path: Path
) -> None:
    configure(cli, cdd)
    running(cli, machine)
    serving(cli)
    agents = instructions_installed(cli, tmp_path / "sales")
    record_file = env_dir(machine) / "skill-install.json"
    record = json.loads(record_file.read_text(encoding="utf-8"))
    copy = tmp_path / "sales" / ".agents" / "skills" / "connx-sql-expert"
    old = {"places": [str(copy)], "zip": None, "instructions": record["instructions"]}
    record_file.write_text(json.dumps(old), encoding="utf-8")
    agents.write_text("# AGENTS.md\n\nMy own rules only.\n", encoding="utf-8")

    result = guide(cli)

    assert result["done"] is True
    [hint] = [w for w in result["warnings"] if str(agents) in w]
    assert "no longer holds the skill instructions" in hint
    shutil.rmtree(copy)
    assert guide(cli)["stage"] == "no_skill"


def test_a_skill_copy_from_an_earlier_install_keeps_guide_done(
    cli: Cli, machine: Machine, cdd: Path, tmp_path: Path
) -> None:
    configure(cli, cdd)
    running(cli, machine)
    serving(cli)
    skill_installed(cli)
    instructions_installed(cli, tmp_path / "sales")
    shutil.rmtree(tmp_path / "sales" / ".agents")

    assert guide(cli)["done"] is True
    shutil.rmtree(machine.home / ".claude" / "skills" / "connx-sql-expert")
    assert guide(cli)["stage"] == "no_skill"


def test_an_intact_skill_instructions_section_gives_no_hint(
    cli: Cli, machine: Machine, cdd: Path, tmp_path: Path
) -> None:
    configure(cli, cdd)
    running(cli, machine)
    serving(cli)
    agents = instructions_installed(cli, tmp_path / "sales")

    result = guide(cli)

    assert result["done"] is True
    assert not [w for w in result["warnings"] if str(agents) in w]


def test_a_skill_installed_without_instructions_is_done_with_no_instructions_hint(
    cli: Cli, machine: Machine, cdd: Path, tmp_path: Path
) -> None:
    configure(cli, cdd)
    running(cli, machine)
    serving(cli)
    (tmp_path / "sales").mkdir()
    skill_in_image(cli)
    code, body = cli.json(
        "install-skill", "--harness", "codex", "--project", str(tmp_path / "sales"), "--no-instructions"
    )
    assert code == 0, body

    result = guide(cli)

    assert result["done"] is True
    assert not [w for w in result["warnings"] if "skill instructions" in w]
    assert not (tmp_path / "sales" / "AGENTS.md").exists()


def test_an_unreadable_docker_configuration_is_a_warning_and_login_the_step(
    cli: Cli, cdd: Path, docker_config: Path
) -> None:
    configure(cli, cdd)
    image_missing(cli)
    docker_config.parent.mkdir(parents=True)
    docker_config.write_text('{"auths": {"secret-looking": ', encoding="utf-8")

    result = guide(cli)

    assert result["next"] == "connx-mcp-ce login"
    assert any(str(docker_config) in w and "JSONDecodeError" in w for w in result["warnings"])
    assert "secret-looking" not in json.dumps(result)


# --- warnings, several environments, human output -------------------------------------------------------------


def test_status_notices_and_drift_are_listed_as_warnings(cli: Cli, machine: Machine, cdd: Path) -> None:
    configure(cli, cdd)
    running(cli, machine)
    serving(cli)
    (env_dir(machine) / "compose.yml").write_text("# an earlier start\n", encoding="utf-8")
    os.utime(env_dir(machine) / "compose.yml", (0, 0))
    moved = "softwareagcr.azurecr.io/connx-mcp-server-ce@sha256:" + "b" * 64
    image_found(cli, digest=moved, version="15.0.4.26100")
    cli.docker.reply(["image", "inspect", "--format", IMAGE_FORMAT, DIGEST], stdout=json.dumps(
        {"id": image_id(DIGEST), "digests": [DIGEST], "labels": None}))  # fmt: skip

    result = guide(cli)

    assert result["stage"] == "no_skill"
    assert any("connx-mcp-ce update default" in w for w in result["warnings"])
    assert any("settings changed" in w for w in result["warnings"])


def test_the_most_urgent_environment_names_the_step_and_every_environment_is_listed(
    cli: Cli, machine: Machine, cdd: Path
) -> None:
    configure(cli, cdd)
    configure(cli, cdd, "test", "--port", "8001")
    running(cli, machine)
    serving(cli)
    never_started(cli, "test")

    result = guide(cli)

    assert (result["stage"], result["environment"], result["next"]) == (
        "not_started",
        "test",
        "connx-mcp-ce start test",
    )
    assert "2 of 2 environments need attention" in result["reason"]
    assert [(e["environment"], e["stage"]) for e in result["environments"]] == [
        ("default", "no_skill"),
        ("test", "not_started"),
    ]


def test_connx_mcp_environment_selects_the_environment_to_guide(cli: Cli, machine: Machine, cdd: Path) -> None:
    configure(cli, cdd)
    configure(cli, cdd, "test", "--port", "8001")
    running(cli, machine)
    serving(cli)

    code, body = cli.json("guide", env={"CONNX_MCP_ENVIRONMENT": "default"})

    assert code == 0, body
    assert body["result"]["stage"] == "no_skill"
    assert [e["environment"] for e in body["result"]["environments"]] == ["default"]


def test_human_output_names_the_next_step_and_the_warnings(cli: Cli, machine: Machine, cdd: Path) -> None:
    configure(cli, cdd)
    pinned(machine)
    image_found(cli)
    cli.docker.reply(inspect_args(), stdout=state(status="exited", health=None, exit_code=3) + "\n")
    runs_image(cli)

    result = cli("guide")

    assert result.exit_code == 0, result.output
    lines = result.stdout.splitlines()
    assert "Next: connx-mcp-ce log default --tail 100" in lines
    assert any("exit code 3" in line for line in lines)


def test_guide_never_prompts_and_never_changes_anything(cli: Cli, machine: Machine) -> None:
    result = guide(cli, "--no-input")

    assert result["next"] == "connx-mcp-ce configure"
    assert not machine.base.exists() or list(machine.base.iterdir()) == []


# --- following guide: from an empty machine, and from every state, it never loops -----------------------------


class World:
    """A machine whose Docker remembers its install, sign-in, pull and start, so each suggested command moves it on.

    `/health` answers only once compose has brought the server up. The placeholders a suggested command
    names are filled the way an administrator would: the CDD file, a CA-issued certificate directory, a harness.
    """

    def __init__(self, cli: Cli, machine: Machine, docker_config: Path, tmp_path: Path) -> None:
        self.cli, self.machine, self.docker_config = cli, machine, docker_config
        self.cdd = tmp_path / "dictionaries" / "given.cdd"
        self.cdd.parent.mkdir(parents=True, exist_ok=True)
        self.cdd.write_bytes(b"CDD")
        self.certs = tmp_path / "ca-issued"
        self.certificate = issue(lifetime=90 * DAY, left=60 * DAY, host="localhost", self_signed=False)
        self.certificate.write(self.certs, "localhost")
        self.pulled = False
        self.started = False
        facts = json.dumps(
            {"id": image_id(), "digests": [DIGEST], "labels": {"org.opencontainers.image.version": "15.0.3.26012"}}
        )
        missing = DockerResult(1, "", f"Error: No such image: {IMAGE}")
        no_container = DockerResult(1, "", NO_SUCH_CONTAINER.format(name="default"))
        docker = cli.docker
        docker.act_when(lambda a: a[:1] == ["login"], self._login)
        docker.act_when(lambda a: a == ["pull", IMAGE], self._pull)
        docker.act_when(
            lambda a: a[:3] == ["image", "inspect", "--format"],
            lambda a: DockerResult(0, facts + "\n", "") if self.pulled else missing,
        )
        docker.act_when(lambda a: a[:2] == ["compose", "-p"] and "up" in a, self._up)
        docker.act_when(
            lambda a: a == inspect_args(),
            lambda a: DockerResult(0, state() + "\n", "") if self.started else no_container,
        )
        runs_image(cli)
        skill_in_image(cli)

    def without_docker(self) -> World:
        """Docker is not installed yet."""
        self.docker_path = self.cli.docker.executables["docker"]
        self.cli.docker.executables["docker"] = None
        return self

    def _login(self, args: list[str]) -> DockerResult:
        """Record the sign-in in Docker's configuration, as docker login does."""
        signed_in(self.docker_config)
        return DockerResult(0, "Login Succeeded\n", "")

    def _pull(self, args: list[str]) -> DockerResult:
        """Pull the image."""
        self.pulled = True
        return DockerResult(0, "", "")

    def _up(self, args: list[str]) -> DockerResult:
        """Start the container: from now on it answers `/health`, over TLS with the certificate given."""
        self.started = True
        serving(self.cli)
        self.cli.probe.health_replies["https://localhost:8000/health"] = HealthReply(status=200, body=HEALTHY_BODY)
        self.cli.probe.certificates[("localhost", 8000)] = self.certificate.der
        return DockerResult(0, "", "")

    def follow(self, command: str) -> int:
        """Run a suggested command as an administrator would; return its exit code."""
        if not command.startswith("connx-mcp-ce "):
            self.cli.docker.executables["docker"] = self.docker_path  # the administrator installs Docker
            return 0
        filled = {"<path of the .cdd file>": self.cdd, "<directory>": self.certs, "<harness>": "claude-code"}
        for placeholder, value in filled.items():
            command = command.replace(placeholder, shlex.quote(str(value)))
        args, stdin = shlex.split(command)[1:], None
        if args[0] == "configure" and len(args) <= 2:  # what a person types at configure's prompts
            args += ["--no-input", "--set", "CDD_USERNAME=mcp", "--set", f"CDD_PASSWORD={PASSWORD}", "--cdd",
                     str(self.cdd)]  # fmt: skip
        if args[0] == "login":
            args += ["--username", "admin", "--password-stdin"]
            stdin = "registry-token-for-tests"
        code, body = self.cli.json(*args, input=stdin)
        assert code == 0, (command, body)
        return code


def walk(cli: Cli, world: World, limit: int = 12) -> list[tuple[str, str | None]]:
    """Follow guide's suggestion until it says done; return each (stage, next) it gave."""
    seen: list[tuple[str, str | None]] = []
    for _ in range(limit):
        result = guide(cli)
        seen.append((result["stage"], result["next"]))
        if result["done"]:
            return seen
        world.follow(result["next"])
    raise AssertionError(f"guide never said done: {seen}")


def test_following_guide_from_an_empty_machine_reaches_a_healthy_environment(
    cli: Cli, machine: Machine, docker_config: Path, tmp_path: Path
) -> None:
    world = World(cli, machine, docker_config, tmp_path).without_docker()

    seen = walk(cli, world)

    assert [stage for stage, _ in seen] == [
        "docker", "no_environment", "no_image", "no_image", "not_started", "no_skill", "healthy",
    ]  # fmt: skip
    assert [*compose(machine), "up", "--detach", "--pull", "never", "--no-build"] in cli.docker.calls


def _cdd_gone(cli: Cli, machine: Machine, world: World) -> None:
    moved = world.cdd.with_name("moved.cdd")
    moved.write_bytes(b"CDD")
    configure(cli, moved)
    moved.unlink()


def _bound_with(line: str) -> Any:
    def arrange(cli: Cli, machine: Machine, world: World) -> None:
        configure(cli, world.cdd)
        bound_to_keycloak(cli)
        server_env = env_dir(machine) / "server.env"
        server_env.write_text(server_env.read_text(encoding="utf-8") + line, encoding="utf-8")

    return arrange


@pytest.mark.parametrize(
    "arrange",
    [
        lambda cli, machine, world: world.without_docker(),
        _cdd_gone,
        _bound_with("MCP_TLS_ENABLED=true\nMCP_TLS_HOSTS=localhost\n"),
        _bound_with("MCP_PATH=/sql\n"),
    ],
    ids=["empty-machine", "cdd-gone", "bound-self-signed", "bound-mcp-path"],
)
def test_following_guide_never_loops(
    cli: Cli, machine: Machine, docker_config: Path, tmp_path: Path, arrange: Any
) -> None:
    """No suggestion, once followed, leaves guide suggesting it again, and every walk ends in done.

    The steps that ask the administrator to read (`log`, `status`) are left out: only their diagnosis moves on.
    """
    world = World(cli, machine, docker_config, tmp_path)
    arrange(cli, machine, world)

    seen = walk(cli, world)

    assert all(before != after for before, after in zip(seen, seen[1:])), seen
    assert len(set(seen)) == len(seen), seen
    assert seen[-1] == ("healthy", None)


# --- live -----------------------------------------------------------------------------------------------------


@pytest.mark.live
def test_following_guide_with_real_docker_reaches_a_healthy_environment(cli: Cli, machine: Machine) -> None:
    """Needs CONNX_MCP_LIVE_CDD, CONNX_MCP_LIVE_USER and CONNX_MCP_LIVE_PASSWORD; the image must be pulled."""
    needed = ["CONNX_MCP_LIVE_CDD", "CONNX_MCP_LIVE_USER", "CONNX_MCP_LIVE_PASSWORD"]
    if missing := [n for n in needed if n not in os.environ]:
        pytest.skip(f"set {', '.join(missing)} to run against the real image")
    real = default_ports()
    cli.docker, cli.probe, cli.clock = real.docker, real.probe, real.clock  # type: ignore[assignment]
    licence = os.environ.get("CONNX_MCP_LIVE_LICENSE_HOST", "host.docker.internal")
    answers = [
        "--no-input", "--set", f"CDD_USERNAME={os.environ['CONNX_MCP_LIVE_USER']}",
        "--set", f"CDD_PASSWORD={os.environ['CONNX_MCP_LIVE_PASSWORD']}", "--cdd", os.environ["CONNX_MCP_LIVE_CDD"],
        "--set", f"CONNX_LICENSE_HOST={licence}", "--port", "8016",
    ]  # fmt: skip
    seen: list[str] = []
    try:
        for _ in range(8):
            result = guide(cli, "live-16")
            seen.append(result["stage"])
            if result["done"]:
                break
            args = shlex.split(result["next"].replace("<harness>", "claude-code"))
            assert args[0] == "connx-mcp-ce", result
            args = args[1:] + (answers if args[1] == "configure" else [])
            args += ["--timeout", "300"] if args[0] == "start" else []
            code, body = cli.json(*args)
            assert code == 0, (result["next"], body)
        assert seen[-1] == "healthy", seen
    finally:
        cli.json("stop", "live-16")
        real.docker.run(["compose", "-p", "connx-mcp-ce-live-16", "down", "--volumes"])
