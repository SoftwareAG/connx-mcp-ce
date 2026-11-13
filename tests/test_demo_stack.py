#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_demo_stack.py - The demo presenter configures, starts and stops the demo stack.

Description:
    `configure-demo` chooses Keycloak, dex and/or Vault and seeds or takes a
    users file; `start-demo` validates that file, renders and makes TLS inside the seed container
    and waits until the chosen services are healthy; `stop-demo` stops them and `--purge` also
    removes generated files, the demo CA, the seed image and the binding. Driven through the CLI
    seam with the fake Docker port.

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
import stat
from pathlib import Path

import pytest
import yaml

from connx_mcp.demo.stack import DemoSettings
from connx_mcp.docker import DockerResult
from tests.conftest import Cli, Machine

PERSONAS = ["alice", "bob", "carol", "dave"]
MAPPED_USERS = """\
identities:
  - name: alice
    id: alice-id
    connx: { username: mcp-restricted, restricted: true }
  - name: bob
    id: bob-id
    password_env: DEMO_BOB_PASSWORD
"""


def _demo_dir(machine: Machine) -> Path:
    """The demo stack's state directory on the test machine."""
    return machine.base / ".demo"


def _mode(path: Path) -> int:
    """A file's permission bits."""
    return stat.S_IMODE(path.stat().st_mode)


def _compose(call: list[str]) -> list[str] | None:
    """The compose sub-command and its arguments, after the project flags; None for other calls."""
    if call[:1] != ["compose"] or "--env-file" not in call:
        return None
    return call[call.index("--env-file") + 2 :]


def _compose_calls(cli: Cli) -> list[list[str]]:
    """Every compose sub-command made so far, project flags dropped."""
    return [sub for sub in (_compose(call) for call in cli.docker.calls) if sub is not None]


def _accept_compose(cli: Cli) -> None:
    """Let every compose command succeed."""
    cli.docker.reply_when(lambda args: _compose(args) is not None)


def _mapped_users_file(tmp_path: Path) -> Path:
    """Write a users file that maps alice to a CONNX login."""
    path = tmp_path / "customer.yaml"
    path.write_text(MAPPED_USERS)
    return path


# configure-demo -------------------------------------------------------------------------------------------------


def test_configure_demo_selects_services_and_seeds_the_users_file(cli: Cli, machine: Machine) -> None:
    code, envelope = cli.json("configure-demo", "--keycloak", "--vault")

    assert code == 0, envelope
    result = envelope["result"]
    assert result["services"] == ["keycloak", "vault"]
    users_file = Path(result["users_file"])
    assert users_file == _demo_dir(machine) / "users.yaml"
    assert result["users_file_created"] is True
    assert result["mapped"] == 0
    seeded = yaml.safe_load(users_file.read_text())
    assert [identity["name"] for identity in seeded["identities"]] == PERSONAS
    assert all("connx" not in identity for identity in seeded["identities"])


@pytest.mark.skipif(os.name == "nt", reason="POSIX modes")
def test_seeded_users_file_and_demo_settings_are_owner_only(cli: Cli, machine: Machine) -> None:
    cli.json("configure-demo", "--dex")

    assert _mode(_demo_dir(machine) / "users.yaml") == 0o600
    assert _mode(_demo_dir(machine) / "demo.json") == 0o600


@pytest.mark.skipif(os.name == "nt", reason="POSIX modes")
def test_configure_demo_keeps_the_base_and_demo_directories_owner_only(cli: Cli, machine: Machine) -> None:
    old = os.umask(0o022)
    try:
        code, envelope = cli.json("configure-demo", "--dex")
    finally:
        os.umask(old)

    assert code == 0, envelope
    assert _mode(machine.base) == 0o700
    assert _mode(_demo_dir(machine)) == 0o700


def test_configure_demo_keeps_an_edited_users_file(cli: Cli, machine: Machine) -> None:
    cli.json("configure-demo", "--keycloak")
    users_file = _demo_dir(machine) / "users.yaml"
    users_file.write_text(MAPPED_USERS)

    code, envelope = cli.json("configure-demo", "--dex", "--no-keycloak", env={"DEMO_BOB_PASSWORD": "x"})

    assert code == 0, envelope
    assert envelope["result"]["services"] == ["dex"]
    assert envelope["result"]["users_file_created"] is False
    assert users_file.read_text() == MAPPED_USERS


def test_configure_demo_without_a_service_choice_needs_input(cli: Cli) -> None:
    code, envelope = cli.json("configure-demo", "--no-input")

    assert code == 3
    assert envelope["error"]["code"] == "input_required"
    assert "--keycloak" in envelope["error"]["next"]


def test_configure_demo_refuses_deselecting_every_service(cli: Cli) -> None:
    cli.json("configure-demo", "--vault")

    code, envelope = cli.json("configure-demo", "--no-vault")

    assert code == 2
    assert "at least one" in envelope["error"]["message"]


@pytest.mark.skipif(os.name == "nt", reason="POSIX modes")
def test_users_file_option_uses_another_file_owner_only(cli: Cli, machine: Machine, tmp_path: Path) -> None:
    users_file = _mapped_users_file(tmp_path)
    users_file.chmod(0o644)

    code, envelope = cli.json(
        "configure-demo", "--keycloak", "--users-file", str(users_file), env={"DEMO_BOB_PASSWORD": "x"}
    )

    assert code == 0, envelope
    assert envelope["result"]["users_file"] == str(users_file)
    assert envelope["result"]["mapped"] == 1
    assert _mode(users_file) == 0o600
    assert not (_demo_dir(machine) / "users.yaml").exists()


def test_users_file_option_refuses_a_missing_file(cli: Cli, tmp_path: Path) -> None:
    code, envelope = cli.json("configure-demo", "--keycloak", "--users-file", str(tmp_path / "nope.yaml"))

    assert code == 41
    assert envelope["error"]["code"] == "demo_users_invalid"
    assert "not found" in envelope["error"]["message"]


@pytest.mark.parametrize(
    ("content", "says"),
    [
        ("identities:\n  - name: alice\n    id: a\n    colour: red\n", "unknown field(s) colour"),
        ("identities:\n  - name: alice\n    id: a\n    connx: { username: '' }\n", "'username' is required"),
        ("identities:\n  - name: a\n    id: x\n  - name: a\n    id: y\n", "duplicate name 'a'"),
        ("identities: [\n  password: hunter2-secret\n", "not valid YAML at line"),
    ],
)
def test_users_file_is_validated_strictly(cli: Cli, tmp_path: Path, content: str, says: str) -> None:
    users_file = tmp_path / "bad.yaml"
    users_file.write_text(content)

    code, envelope = cli.json("configure-demo", "--keycloak", "--users-file", str(users_file))

    assert code == 41
    assert says in envelope["error"]["message"]
    assert "hunter2" not in json.dumps(envelope)


# start-demo ------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("command", ["start-demo", "stop-demo", "configure-demo"])
def test_an_unreadable_demo_settings_file_exits_file_unreadable(cli: Cli, machine: Machine, command: str) -> None:
    cli.json("configure-demo", "--dex")
    (_demo_dir(machine) / "demo.json").write_text("{not json", encoding="utf-8")

    code, envelope = cli.json(command)

    assert code == 92, envelope
    assert envelope["error"]["code"] == "file_unreadable"
    assert "demo.json" in envelope["error"]["message"]


def test_start_demo_needs_a_configured_stack(cli: Cli) -> None:
    code, envelope = cli.json("start-demo")

    assert code == 40
    assert envelope["error"]["code"] == "demo_not_configured"
    assert envelope["error"]["next"].startswith("connx-mcp-ce configure-demo")


def test_start_demo_refuses_until_a_user_has_a_connx_login(cli: Cli, machine: Machine) -> None:
    cli.json("configure-demo", "--keycloak")

    code, envelope = cli.json("start-demo")

    assert code == 42
    assert envelope["error"]["code"] == "demo_no_mapped_user"
    assert str(_demo_dir(machine) / "users.yaml") in envelope["error"]["message"]
    assert _compose_calls(cli) == []


def test_start_demo_revalidates_the_users_file(cli: Cli, machine: Machine) -> None:
    cli.json("configure-demo", "--keycloak")
    (_demo_dir(machine) / "users.yaml").write_text("identities: []\n")

    code, envelope = cli.json("start-demo")

    assert code == 41
    assert "non-empty list" in envelope["error"]["message"]


def _configured(cli: Cli, tmp_path: Path, *services: str) -> Path:
    """Configure the demo with these services and a mapped users file; accept compose commands."""
    users_file = _mapped_users_file(tmp_path)
    code, envelope = cli.json("configure-demo", *services, "--users-file", str(users_file), env=SECRET_ENV)
    assert code == 0, envelope
    _accept_compose(cli)
    return users_file


SECRET_ENV = {"DEMO_BOB_PASSWORD": "bob-oauth-pass"}
SEED_IMAGE_RM = ["image", "rm", "connx-mcp-ce_demo-seed:local"]


def test_start_demo_runs_the_stack_as_its_own_compose_project(cli: Cli, machine: Machine, tmp_path: Path) -> None:
    _configured(cli, tmp_path, "--keycloak")

    code, envelope = cli.json("start-demo", env=SECRET_ENV)

    assert code == 0, envelope
    compose_calls = [call for call in cli.docker.calls if _compose(call) is not None]
    assert compose_calls
    assert all(call[1:3] == ["--project-name", "connx-mcp-ce_demo"] for call in compose_calls)
    stack_env = (_demo_dir(machine) / "stack.env").read_text(encoding="utf-8")
    assert "COMPOSE_PROJECT_NAME='connx-mcp-ce_demo'" in stack_env.splitlines()


def test_start_demo_renders_makes_tls_in_the_seed_container_and_waits(
    cli: Cli, machine: Machine, tmp_path: Path
) -> None:
    users_file = _configured(cli, tmp_path, "--keycloak", "--vault")

    code, envelope = cli.json("start-demo", env=SECRET_ENV)

    assert code == 0, envelope
    stack = _demo_dir(machine) / "stack"
    calls = _compose_calls(cli)
    assert calls[0] == ["build", "-q", "seed"]

    render = calls[1]
    assert render[:4] == ["run", "--rm", "--no-deps", "-T"]
    if hasattr(os, "getuid"):
        assert render[render.index("--user") + 1] == f"{os.getuid()}:{os.getgid()}"
    assert render[render.index("--entrypoint") + 1] == "python3"
    assert "MCP_PUBLIC_BASE_URL=http://localhost:8000" in render
    assert "DEMO_BOB_PASSWORD" in render
    assert f"{stack.as_posix()}:/stack:ro" in render
    assert f"{users_file.as_posix()}:/users/customer.yaml:ro" in render
    assert render[-8:] == [
        "seed",
        "/stack/render.py",
        "--users",
        "/users/customer.yaml",
        "--stack",
        "/stack",
        "--out",
        "/out",
    ]

    tls = calls[2]
    assert tls[tls.index("--entrypoint") + 1] == "/seed/make-tls.sh"
    assert f"{(stack / 'generated' / 'tls').as_posix()}:/out" in tls

    up = calls[3]
    assert up[:2] == ["up", "-d"]
    assert "--wait" in up
    assert up[-3:] == ["keycloak", "vault", "seed"]
    assert "dex" not in up

    result = envelope["result"]
    assert result["services"] == ["keycloak", "vault"]
    assert result["endpoints"] == {
        "keycloak": "http://host.docker.internal:8080/realms/connx-mcp",
        "vault": "https://host.docker.internal:8200",
    }
    assert result["vault_ca"] == str(stack / "generated" / "tls" / "ca.pem")
    assert (stack / "docker-compose.yml").is_file()
    assert (stack / "generated" / "tls").is_dir()
    assert (stack / "generated" / "vault").is_dir()


@pytest.mark.skipif(os.name == "nt", reason="POSIX modes")
def test_start_demo_keeps_every_file_with_a_cdd_password_owner_only(cli: Cli, machine: Machine, tmp_path: Path) -> None:
    _configured(cli, tmp_path, "--keycloak", "--vault")
    demo = _demo_dir(machine)
    demo.chmod(0o755)  # as an earlier connx-mcp-ce left it
    entries = demo / "stack" / "generated" / "vault" / "entries.json"

    def render(args: list[str]) -> DockerResult:
        """Write the Vault entries world-readable, as the verbatim render.py does."""
        entries.write_text('[{"connx": {"password": "cdd-pass"}}]\n')
        entries.chmod(0o644)
        return DockerResult(0, "", "")

    cli.docker.act_when(lambda args: "/stack/render.py" in args, render)
    old = os.umask(0o022)
    try:
        code, envelope = cli.json("start-demo", env=SECRET_ENV)
    finally:
        os.umask(old)

    assert code == 0, envelope
    for directory in (machine.base, demo, demo / "stack", demo / "stack" / "generated"):
        assert _mode(directory) == 0o700, directory
    for file in (entries, demo / "stack.env", demo / "demo.json"):
        assert _mode(file) == 0o600, file


def test_start_demo_never_shows_a_secret(cli: Cli, tmp_path: Path) -> None:
    _configured(cli, tmp_path, "--keycloak", "--vault")

    human = cli("start-demo", env=SECRET_ENV)
    _, envelope = cli.json("start-demo", env=SECRET_ENV)

    assert human.exit_code == 0, human.output
    for text in (human.output, json.dumps(envelope)):
        assert "bob-oauth-pass" not in text
        assert "connx-mcp-dev-read" not in text
        assert "connx-mcp-dev-root" not in text
    assert "for demonstration use only" in human.output


def test_start_demo_warns_that_the_network_reaches_vault_and_keycloak(cli: Cli, tmp_path: Path) -> None:
    _configured(cli, tmp_path, "--keycloak", "--vault")

    code, envelope = cli.json("start-demo", env=SECRET_ENV)
    human = cli("start-demo", env=SECRET_ENV)

    assert code == 0, envelope
    [warning] = envelope["result"]["warnings"]
    for fact in ("Other computers can reach the demo services", "Vault", "root token", "CDD logins", "Keycloak",
                 "admin/admin", "trusted machine"):  # fmt: skip
        assert fact in warning, fact
    assert "network interface" not in warning
    assert f"Warning: {warning}" in human.stdout


def test_start_demo_with_dex_only_warns_without_naming_vault_or_keycloak(cli: Cli, tmp_path: Path) -> None:
    _configured(cli, tmp_path, "--dex")

    code, envelope = cli.json("start-demo", env=SECRET_ENV)

    assert code == 0, envelope
    [warning] = envelope["result"]["warnings"]
    assert "Other computers can reach the demo services" in warning
    assert "Vault" not in warning
    assert "Keycloak" not in warning


def test_start_demo_without_vault_skips_tls_and_reseeds_a_running_seed(cli: Cli, tmp_path: Path) -> None:
    _configured(cli, tmp_path, "--dex")
    cli.docker.reply_when(lambda args: _compose(args) == ["ps", "-q", "seed"], stdout="abc123\n")

    code, envelope = cli.json("start-demo", env=SECRET_ENV)

    assert code == 0, envelope
    calls = _compose_calls(cli)
    assert not any("/seed/make-tls.sh" in call for call in calls)
    up = next(call for call in calls if call[0] == "up")
    assert up[-1] == "dex"
    assert calls[-1] == ["restart", "seed"]
    assert envelope["result"]["endpoints"] == {"dex": "http://host.docker.internal:5556/dex"}


def test_start_demo_reports_an_unhealthy_service(cli: Cli, tmp_path: Path) -> None:
    _configured(cli, tmp_path, "--keycloak")
    cli.docker.reply_when(
        lambda args: (_compose(args) or [""])[0] == "up", returncode=1, stderr="container keycloak is unhealthy"
    )
    cli.docker.reply_when(lambda args: _compose(args) == ["ps"], stdout="keycloak   unhealthy\n")

    code, envelope = cli.json("start-demo", env=SECRET_ENV)

    assert code == 43
    message = envelope["error"]["message"]
    assert "container keycloak is unhealthy" in message
    assert "keycloak   unhealthy" in message
    assert "docker compose" in envelope["error"]["next"]


def test_start_demo_shows_why_rendering_failed(cli: Cli, tmp_path: Path) -> None:
    _configured(cli, tmp_path, "--keycloak")
    cli.docker.reply_when(
        lambda args: "/stack/render.py" in args, returncode=1, stderr="render.py: identity 'x': bad thing"
    )

    code, envelope = cli.json("start-demo", env=SECRET_ENV)

    assert code == 43
    assert "render.py: identity 'x': bad thing" in envelope["error"]["message"]
    assert not any(call[0] == "up" for call in _compose_calls(cli))


# stop-demo -------------------------------------------------------------------------------------------------------


def test_stop_demo_needs_a_configured_stack(cli: Cli) -> None:
    code, envelope = cli.json("stop-demo")

    assert code == 40


def test_stop_demo_stops_and_keeps_generated_files(cli: Cli, machine: Machine, tmp_path: Path) -> None:
    _configured(cli, tmp_path, "--keycloak", "--vault")
    cli.json("start-demo", env=SECRET_ENV)
    cli.docker.calls.clear()

    code, envelope = cli.json("stop-demo")

    assert code == 0, envelope
    assert _compose_calls(cli) == [["down", "--remove-orphans"]]
    assert (_demo_dir(machine) / "stack" / "generated").is_dir()


def test_stop_demo_purge_removes_generated_files_ca_seed_image_and_binding(
    cli: Cli, machine: Machine, tmp_path: Path
) -> None:
    users_file = _configured(cli, tmp_path, "--keycloak", "--vault")
    cli.json("start-demo", env=SECRET_ENV)
    settings_file = _demo_dir(machine) / "demo.json"
    settings = json.loads(settings_file.read_text())
    settings["bound_environment"] = "default"
    settings_file.write_text(json.dumps(settings))
    cli.docker.reply(SEED_IMAGE_RM)
    cli.docker.calls.clear()

    code, envelope = cli.json("stop-demo", "--purge")

    assert code == 0, envelope
    assert _compose_calls(cli) == [["down", "--remove-orphans"]]
    assert SEED_IMAGE_RM in cli.docker.calls
    assert not (_demo_dir(machine) / "stack").exists()
    assert json.loads(settings_file.read_text())["bound_environment"] is None
    assert users_file.is_file()
    assert envelope["result"]["purged"] is True
    assert envelope["result"]["seed_image_removed"] is True


def test_stop_demo_purge_takes_a_missing_seed_image_as_removed(cli: Cli, tmp_path: Path) -> None:
    _configured(cli, tmp_path, "--keycloak")
    cli.docker.reply(SEED_IMAGE_RM, returncode=1, stderr="Error response from daemon: No such image: x")

    code, envelope = cli.json("stop-demo", "--purge")

    assert code == 0, envelope
    assert envelope["result"]["seed_image_removed"] is True


def test_stop_demo_purge_says_when_the_seed_image_stays(cli: Cli, tmp_path: Path) -> None:
    _configured(cli, tmp_path, "--keycloak")
    cli.docker.reply(SEED_IMAGE_RM, returncode=1, stderr="conflict: image is being used by container abc")

    code, envelope = cli.json("stop-demo", "--purge")
    text = " ".join(cli("stop-demo", "--purge").stdout.split())

    assert code == 0, envelope
    assert envelope["result"]["seed_image_removed"] is False
    assert "being used" in envelope["result"]["seed_image_error"]
    assert "seed image connx-mcp-ce_demo-seed:local was kept" in text
    assert "docker image rm connx-mcp-ce_demo-seed:local" in text


def test_stop_demo_purge_says_start_demo_makes_a_new_ca_and_how_to_bind_again(
    cli: Cli, machine: Machine, tmp_path: Path
) -> None:
    _configured(cli, tmp_path, "--keycloak", "--vault")
    settings_file = _demo_dir(machine) / "demo.json"
    settings = json.loads(settings_file.read_text())
    settings["bound_environment"] = "default"
    settings_file.write_text(json.dumps(settings))
    cli.docker.reply(SEED_IMAGE_RM)

    text = " ".join(cli("stop-demo", "--purge").stdout.split())

    ca = _demo_dir(machine) / "stack" / "generated" / "tls" / "ca.pem"
    assert f"start-demo creates a new demo CA at {ca}" in text
    assert "restart the environments that use it" in text
    assert "connx-mcp-ce configure-demo --for default" in text
    assert "VAULT_CACERT" not in text


def test_stop_demo_shows_why_compose_failed(cli: Cli, tmp_path: Path) -> None:
    _configured(cli, tmp_path, "--keycloak")
    cli.docker.reply_when(lambda args: (_compose(args) or [""])[0] == "down", returncode=1, stderr="daemon hiccup")

    code, envelope = cli.json("stop-demo")

    assert code == 43
    assert "daemon hiccup" in envelope["error"]["message"]


# help ------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("command", ["configure-demo", "start-demo", "stop-demo"])
def test_help_states_the_demo_limits(cli: Cli, command: str) -> None:
    help_text = " ".join(cli(command, "--help").stdout.split())

    assert "A machine has one demo stack" in help_text
    assert "for demonstration use only" in help_text
    assert "not production certified" in help_text


# the server settings the demo stack hands an environment ---------------------------------------------------------


def test_demo_settings_give_the_keycloak_server_settings_with_vault() -> None:
    settings = DemoSettings(services=("keycloak", "vault"), public_base_url="https://mcp.example:8000/")

    assert settings.server_settings("keycloak", vault=True) == {
        "MCP_OAUTH_PROVIDER": "keycloak",
        "MCP_PUBLIC_BASE_URL": "https://mcp.example:8000",
        "MCP_OAUTH_REALM_URL": "http://host.docker.internal:8080/realms/connx-mcp",
        "MCP_OAUTH_AUDIENCE": "https://mcp.example:8000/mcp",
        "MCP_CREDENTIAL_PROVIDER": "hashicorp",
        "VAULT_ADDR": "https://host.docker.internal:8200",
        "VAULT_TOKEN": "connx-mcp-dev-read",
    }
    assert (settings.keycloak_client_id, settings.keycloak_client_secret) == (
        "connx-mcp-generic",
        "connx-mcp-generic-secret",
    )


def test_demo_settings_give_the_dex_server_settings_in_proxy_mode() -> None:
    settings = DemoSettings(services=("dex",))

    assert settings.server_settings("dex", vault=False) == {
        "MCP_OAUTH_PROVIDER": "generic-oidc",
        "MCP_PUBLIC_BASE_URL": "http://localhost:8000",
        "MCP_OAUTH_CONFIG_URL": "http://host.docker.internal:5556/dex/.well-known/openid-configuration",
        "MCP_OAUTH_CLIENT_ID": "connx-mcp",
        "MCP_OAUTH_CLIENT_SECRET": "connx-mcp-secret",
        "MCP_OAUTH_AUDIENCE": "connx-mcp",
    }
