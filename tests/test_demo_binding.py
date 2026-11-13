#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_demo_binding.py - The demo presenter binds an environment to the demo stack.

Description:
    `configure-demo --for <env>` binds the stack and renders the realm for the environment's public
    URL; `configure <env> --demo keycloak|dex [--demo-vault]` fills its OAuth and Vault settings
    from the stack and names the demo CA as its Vault CA file. Vault without a provider, a second
    environment and self-signed TLS with OAuth are refused; `status` shows the binding; `remove`
    refuses the bound environment unless `--force`, which unbinds. Driven through the CLI seam with
    the fake Docker port and endpoint probe. The live test signs a demo user in through Keycloak
    and reads data with the CONNX login the users file maps.

Copyright:
    Copyright 2026 Software GmbH
    Licensed under the Apache License, Version 2.0 (the "License"); you may not use this file except in compliance
    with the License. You may obtain a copy of the License at https://www.apache.org/licenses/LICENSE-2.0
    Unless required by applicable law or agreed to in writing, software distributed under the License is distributed
    on an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the License
    for the specific language governing permissions and limitations under the License.
"""

from __future__ import annotations

import base64
import http.cookiejar
import json
import os
import re
import socket
import urllib.error
import urllib.parse
import urllib.request
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from connx_mcp.demo.stack import DEFAULT_STACK_HOST
from connx_mcp.ports import default_ports
from tests.certificates import issue
from tests.conftest import Cli, Machine, bind, mounts, refused_with
from tests.test_lifecycle import comes_up, image_found, inspect_args, serving, state
from tests.test_remove import down_args, ps_args, started, volume_ls_args

PASSWORD = "pw-for-tests-only"
REALM = f"http://{DEFAULT_STACK_HOST}:8080/realms/connx-mcp"
# A demo stack on another machine (its STACK_HOST): the live tests then use it and never start or purge one.
LIVE_STACK_HOST = os.environ.get("CONNX_MCP_LIVE_STACK_HOST")
DEMO_CA_TARGET = "/opt/softwareag/connx/mcp/vault-ca.pem"
DAY = timedelta(days=1)


@pytest.fixture
def cdd(tmp_path: Path) -> Path:
    """A CDD file on this machine."""
    path = tmp_path / "dictionaries" / "sales.cdd"
    path.parent.mkdir()
    path.write_bytes(b"CDD")
    return path


def environment(cli: Cli, cdd: Path, name: str = "default", *extra: str) -> None:
    """Create a complete environment without OAuth."""
    code, body = cli.json(
        "configure", name, "--no-input", "--set", "CDD_USERNAME=mcp", "--set", f"CDD_PASSWORD={PASSWORD}",
        "--cdd", str(cdd), *extra,
    )  # fmt: skip
    assert code == 0, body


def demo(cli: Cli, *services: str) -> None:
    """Configure the demo stack with the given services."""
    code, body = cli.json("configure-demo", *(f"--{service}" for service in services))
    assert code == 0, body


def demo_json(machine: Machine) -> dict[str, Any]:
    """The saved demo settings."""
    return json.loads((machine.base / ".demo" / "demo.json").read_text(encoding="utf-8"))


def shown(cli: Cli, name: str = "default") -> dict[str, Any]:
    """The environment as `configure --show` prints it."""
    code, body = cli.json("configure", name, "--show")
    assert code == 0, body
    return body["result"]


# --- configure-demo --for -----------------------------------------------------------------------------------


def test_configure_demo_for_renders_the_realm_for_the_environments_public_url(
    cli: Cli, machine: Machine, cdd: Path
) -> None:
    environment(cli, cdd, "demo", "--port", "8001")

    code, body = cli.json("configure-demo", "--keycloak", "--vault", "--for", "demo")

    assert code == 0, body
    assert body["result"]["bound_environment"] == "demo"
    assert body["result"]["public_base_url"] == "http://localhost:8001"
    saved = demo_json(machine)
    assert saved["bound_environment"] == "demo"
    assert saved["public_base_url"] == "http://localhost:8001"
    assert saved["stack_host"] == DEFAULT_STACK_HOST


def test_configure_demo_for_a_missing_environment_is_refused(cli: Cli) -> None:
    code, body = cli.json("configure-demo", "--keycloak", "--for", "nope")

    assert code == 20
    assert body["error"]["next"] == "connx-mcp-ce configure nope"


def test_a_loopback_stack_host_cannot_be_bound(cli: Cli, cdd: Path) -> None:
    environment(cli, cdd)

    code, body = cli.json("configure-demo", "--keycloak", "--stack-host", "localhost", "--for", "default")

    assert code == 2
    assert "container itself" in body["error"]["message"]
    assert body["error"]["next"] == f"connx-mcp-ce configure-demo --stack-host {DEFAULT_STACK_HOST}"


def test_an_unresolvable_stack_host_is_warned_about_with_the_hosts_line(cli: Cli, cdd: Path) -> None:
    environment(cli, cdd)
    cli.probe.unresolvable.add(DEFAULT_STACK_HOST)

    result = cli("configure-demo", "--keycloak", "--for", "default")

    assert result.exit_code == 0, result.output
    assert f"127.0.0.1 {DEFAULT_STACK_HOST}" in result.stdout
    said = " ".join(result.stdout.split())
    assert "Rancher Desktop and Docker Engine do not" in said
    assert "--stack-host <name>" in said


# --- configure <env> --demo ----------------------------------------------------------------------------------


def test_demo_keycloak_with_vault_fills_the_settings_and_names_the_demo_ca(
    cli: Cli, machine: Machine, cdd: Path
) -> None:
    environment(cli, cdd)
    demo(cli, "keycloak", "vault")

    code, body = cli.json("configure", "--demo", "keycloak", "--demo-vault")

    assert code == 0, body
    assert body["result"]["demo"]["bound_environment"] == "default"
    result = shown(cli)
    server = result["server_settings"]
    assert server["MCP_OAUTH_PROVIDER"] == "keycloak"
    assert server["MCP_PUBLIC_BASE_URL"] == "http://localhost:8000"
    assert server["MCP_OAUTH_REALM_URL"] == REALM
    assert server["MCP_OAUTH_AUDIENCE"] == "http://localhost:8000/mcp"
    assert server["MCP_CREDENTIAL_PROVIDER"] == "hashicorp"
    assert server["VAULT_ADDR"] == f"https://{DEFAULT_STACK_HOST}:8200"
    assert "VAULT_CACERT" not in server
    ca = machine.base / ".demo" / "stack" / "generated" / "tls" / "ca.pem"
    assert result["host_settings"]["vault_ca_file"] == str(ca)
    assert demo_json(machine)["bound_environment"] == "default"


def test_start_mounts_the_demo_ca_where_vault_cacert_points(cli: Cli, machine: Machine, cdd: Path) -> None:
    environment(cli, cdd)
    demo(cli, "keycloak", "vault")
    cli.json("configure", "--demo", "keycloak", "--demo-vault")
    ca = machine.base / ".demo" / "stack" / "generated" / "tls" / "ca.pem"
    ca.parent.mkdir(parents=True)
    ca.write_text("-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----\n", encoding="utf-8")
    image_found(cli)
    comes_up(cli, machine, state())
    serving(cli)

    code, body = cli.json("start")

    assert code == 0, body
    rendered = (machine.base / "default" / "compose.yml").read_text(encoding="utf-8")
    assert bind(ca, DEMO_CA_TARGET, read_only=True) in mounts(rendered)
    assert f'VAULT_CACERT: "{DEMO_CA_TARGET}"' in rendered
    assert '"host.docker.internal:host-gateway"' in rendered


def test_start_before_start_demo_says_the_demo_stack_creates_the_ca(cli: Cli, cdd: Path) -> None:
    environment(cli, cdd)
    demo(cli, "keycloak", "vault")
    cli.json("configure", "--demo", "keycloak", "--demo-vault")
    image_found(cli)

    code, body = cli.json("start")

    assert code == 80
    assert body["error"]["next"] == "connx-mcp-ce start-demo"


def test_demo_dex_fills_the_proxy_mode_settings(cli: Cli, cdd: Path) -> None:
    environment(cli, cdd)
    demo(cli, "dex")

    code, body = cli.json("configure", "--demo", "dex")

    assert code == 0, body
    server = shown(cli)["server_settings"]
    assert server["MCP_OAUTH_PROVIDER"] == "generic-oidc"
    assert server["MCP_OAUTH_CONFIG_URL"] == f"http://{DEFAULT_STACK_HOST}:5556/dex/.well-known/openid-configuration"
    assert server["MCP_OAUTH_CLIENT_ID"] == "connx-mcp"
    assert server["MCP_OAUTH_AUDIENCE"] == "connx-mcp"
    assert "MCP_CREDENTIAL_PROVIDER" not in server


def test_demo_vault_without_a_provider_is_refused_with_the_reason(cli: Cli, machine: Machine, cdd: Path) -> None:
    environment(cli, cdd)
    demo(cli, "keycloak", "vault")

    code, body = cli.json("configure", "--demo-vault")

    assert code == 2
    assert body["error"]["message"].startswith("--demo-vault: needs --demo keycloak or --demo dex.")
    assert "logins only for the demo identity providers' users" in body["error"]["message"]
    assert demo_json(machine).get("bound_environment") is None


def test_demo_vault_without_a_provider_is_reported_with_the_other_refusals(
    cli: Cli, machine: Machine, cdd: Path
) -> None:
    environment(cli, cdd)
    demo(cli, "keycloak", "vault")

    code, body = cli.json("configure", "--set", "MCP_PATH=bad", "--demo-vault")

    assert code == 2
    assert refused_with(body, "--set MCP_PATH: ")
    assert refused_with(body, "--demo-vault: needs --demo keycloak or --demo dex.")
    assert demo_json(machine).get("bound_environment") is None


def test_an_unknown_demo_provider_is_reported_with_the_other_refusals(cli: Cli, cdd: Path) -> None:
    environment(cli, cdd)

    code, body = cli.json("configure", "--set", "MCP_PATH=bad", "--demo", "okta", "--demo-vault")

    assert code == 2
    assert refused_with(body, "--set MCP_PATH: ")
    assert refused_with(body, "--demo okta: not a demo identity provider; give keycloak or dex.")
    assert not refused_with(body, "--demo-vault")


def test_a_service_the_stack_does_not_run_is_refused(cli: Cli, cdd: Path) -> None:
    environment(cli, cdd)
    demo(cli, "keycloak")

    code, body = cli.json("configure", "--demo", "keycloak", "--demo-vault")

    assert code == 142
    assert body["error"]["next"] == "connx-mcp-ce configure-demo --vault"


@pytest.mark.parametrize(
    ("request_flags", "request_refused"),
    [(["--demo", "keycloak", "--demo-vault"], False), (["--demo-vault"], True)],
    ids=["valid-request", "refused-request"],
)
def test_a_first_pass_edit_refusal_leaves_the_demo_stack_unread_and_is_reported_with_the_request_s(
    cli: Cli, machine: Machine, cdd: Path, request_flags: list[str], request_refused: bool
) -> None:
    environment(cli, cdd)
    demo(cli, "keycloak")
    (machine.base / ".demo" / "demo.json").write_text("not json", encoding="utf-8")

    code, body = cli.json("configure", "--set", "MCP_PATH=bad", *request_flags)

    assert code == 2
    assert body["error"]["message"].startswith("--set MCP_PATH: ")
    assert refused_with(body, "--demo-vault: needs --demo keycloak or --demo dex.") is request_refused
    assert "demo.json" not in body["error"]["message"]
    assert cli.probe.calls == []


def test_demo_before_configure_demo_is_refused(cli: Cli, cdd: Path) -> None:
    environment(cli, cdd)

    code, body = cli.json("configure", "--demo", "keycloak")

    assert code == 40
    assert body["error"]["next"].startswith("connx-mcp-ce configure-demo")


def test_an_unknown_demo_provider_is_refused(cli: Cli, cdd: Path) -> None:
    environment(cli, cdd)
    demo(cli, "keycloak")

    code, body = cli.json("configure", "--demo", "azure")

    assert code == 2
    assert "keycloak or dex" in body["error"]["message"]


def test_a_port_change_keeps_the_bound_stack_in_step(cli: Cli, machine: Machine, cdd: Path) -> None:
    environment(cli, cdd)
    demo(cli, "keycloak")
    cli.json("configure", "--demo", "keycloak")

    result = cli("configure", "--port", "8005", "--set", "MCP_PUBLIC_BASE_URL=http://localhost:8005",
                 "--set", "MCP_OAUTH_AUDIENCE=http://localhost:8005/mcp")  # fmt: skip

    assert result.exit_code == 0, result.output
    assert demo_json(machine)["public_base_url"] == "http://localhost:8005"
    assert "start-demo" in result.stdout


def test_an_unset_of_a_key_the_demo_fills_beats_the_fill_so_keycloak_refuses_the_change(cli: Cli, cdd: Path) -> None:
    environment(cli, cdd)
    demo(cli, "keycloak")

    code, body = cli.json("configure", "--demo", "keycloak", "--unset", "MCP_OAUTH_AUDIENCE")

    # The unset wins over the fill, so Keycloak lacks the audience it requires.
    assert code == 3, body
    assert body["error"]["next"] == "connx-mcp-ce configure default --no-input --set MCP_OAUTH_AUDIENCE=..."
    assert "MCP_OAUTH_PROVIDER" not in shown(cli)["server_settings"]


def test_a_flag_the_demo_fill_completes_is_judged_after_the_fill(cli: Cli, cdd: Path) -> None:
    environment(cli, cdd)
    demo(cli, "keycloak", "vault")

    # Vault without OAuth is refused, but the demo fills OAuth in the same call.
    code, body = cli.json(
        "configure", "--set", "MCP_CREDENTIAL_PROVIDER=hashicorp", "--demo", "keycloak", "--demo-vault"
    )

    assert code == 0, body
    assert shown(cli)["server_settings"]["MCP_OAUTH_PROVIDER"] == "keycloak"


# --- TLS: plain-http localhost or a provided certificate ----------------------------------------------------


def test_a_bound_environment_on_plain_http_localhost_is_accepted(cli: Cli, cdd: Path) -> None:
    environment(cli, cdd)
    demo(cli, "keycloak")

    code, body = cli.json("configure", "--demo", "keycloak")

    assert code == 0, body
    assert shown(cli)["server_settings"]["MCP_PUBLIC_BASE_URL"].startswith("http://localhost:")


def test_a_bound_environment_with_self_signed_tls_is_refused(cli: Cli, machine: Machine, cdd: Path) -> None:
    environment(cli, cdd, "default", "--set", "MCP_TLS_ENABLED=true", "--set", "MCP_TLS_HOSTS=localhost")
    demo(cli, "keycloak")

    code, body = cli.json("configure", "--demo", "keycloak")

    assert code == 2
    assert "self-signed" in body["error"]["message"]
    assert "--certs" in body["error"]["message"]
    assert demo_json(machine).get("bound_environment") is None


def test_a_bound_environment_with_a_provided_ca_issued_certificate_is_accepted(
    cli: Cli, cdd: Path, tmp_path: Path
) -> None:
    certs = tmp_path / "certs"
    issue(lifetime=90 * DAY, left=60 * DAY, host="localhost", self_signed=False).write(certs, "localhost")
    environment(cli, cdd, "default", "--set", "MCP_TLS_ENABLED=true", "--set", "MCP_TLS_HOSTS=localhost",
                "--certs", str(certs))  # fmt: skip
    demo(cli, "keycloak")

    code, body = cli.json("configure", "--demo", "keycloak")

    assert code == 0, body
    assert shown(cli)["server_settings"]["MCP_PUBLIC_BASE_URL"] == "https://localhost:8000"


# --- one environment per stack ------------------------------------------------------------------------------


def test_binding_a_second_environment_is_refused_with_the_rebind_command(cli: Cli, machine: Machine, cdd: Path) -> None:
    environment(cli, cdd, "first")
    environment(cli, cdd, "second", "--port", "8001")
    demo(cli, "keycloak")
    cli.json("configure", "first", "--demo", "keycloak")

    code, body = cli.json("configure", "second", "--demo", "keycloak")

    assert code == 140
    assert "'first'" in body["error"]["message"]
    assert body["error"]["next"] == "connx-mcp-ce configure-demo --for second --rebind"
    assert "MCP_OAUTH_PROVIDER" not in shown(cli, "second")["server_settings"]


def test_rebind_moves_the_stack_to_the_other_environment(cli: Cli, machine: Machine, cdd: Path) -> None:
    environment(cli, cdd, "first")
    environment(cli, cdd, "second", "--port", "8001")
    demo(cli, "keycloak")
    cli.json("configure", "first", "--demo", "keycloak")

    code, body = cli.json("configure-demo", "--for", "second", "--rebind")
    assert code == 0, body
    code, body = cli.json("configure", "second", "--demo", "keycloak")

    assert code == 0, body
    assert demo_json(machine)["bound_environment"] == "second"
    assert demo_json(machine)["public_base_url"] == "http://localhost:8001"


# --- status ---------------------------------------------------------------------------------------------------


def test_status_shows_which_environment_the_stack_is_bound_to(cli: Cli, cdd: Path) -> None:
    environment(cli, cdd)
    demo(cli, "keycloak")
    cli.json("configure", "--demo", "keycloak")
    cli.docker.reply(inspect_args(), returncode=1, stderr="Error: No such object: connx-mcp-ce-default\n")

    code, body = cli.json("status", "default")
    human = cli("status", "default")

    assert code == 0, body
    assert body["result"]["demo_bound_environment"] == "default"
    assert "Demo stack:    bound to this environment" in human.stdout


def test_the_status_list_names_the_bound_environment(cli: Cli, cdd: Path) -> None:
    environment(cli, cdd)
    demo(cli, "keycloak")
    cli.json("configure", "--demo", "keycloak")
    cli.docker.reply(inspect_args(), returncode=1, stderr="Error: No such object: connx-mcp-ce-default\n")

    code, body = cli.json("status", "--all")

    assert code == 0, body
    assert body["result"]["demo_bound_environment"] == "default"
    assert body["result"]["environments"][0]["demo_bound"] is True


# --- remove ---------------------------------------------------------------------------------------------------


def test_remove_refuses_the_bound_environment_before_asking(cli: Cli, machine: Machine, cdd: Path) -> None:
    environment(cli, cdd)
    demo(cli, "keycloak")
    cli.json("configure", "--demo", "keycloak")

    code, body = cli.json("remove", "--yes")

    assert code == 141
    assert body["error"]["next"] == "connx-mcp-ce remove default --force"
    assert (machine.base / "default").is_dir()
    assert not [c for c in cli.docker.calls if c[:1] in (["volume"], ["ps"]) or "down" in c]


def test_remove_force_removes_the_bound_environment_and_unbinds(cli: Cli, machine: Machine, cdd: Path) -> None:
    environment(cli, cdd)
    demo(cli, "keycloak")
    cli.json("configure", "--demo", "keycloak")
    started(machine)
    cli.docker.reply(down_args(machine))
    cli.docker.reply(volume_ls_args(), stdout="")

    code, body = cli.json("remove", "--yes", "--force")

    assert code == 0, body
    assert not (machine.base / "default").exists()
    assert demo_json(machine).get("bound_environment") is None


def test_remove_without_compose_file_takes_the_projects_containers_down_by_name(
    cli: Cli, machine: Machine, cdd: Path
) -> None:
    environment(cli, cdd)
    cli.docker.reply(volume_ls_args(), stdout="")
    cli.docker.reply(ps_args(), stdout="0123abcd\n")
    by_project = ["compose", "-p", "connx-mcp-ce-default", "down", "--timeout", "10", "--remove-orphans"]
    cli.docker.reply(by_project)

    code, body = cli.json("remove", "--yes")

    assert code == 0, body
    assert by_project in cli.docker.calls
    assert body["result"]["container_taken_down"] is True
    assert not (machine.base / "default").exists()


def test_remove_of_a_never_started_environment_says_there_is_no_container(cli: Cli, cdd: Path) -> None:
    environment(cli, cdd)
    cli.docker.reply(volume_ls_args(), stdout="")
    cli.docker.reply(ps_args(), stdout="")

    result, screen = cli.typed("remove", keys=["y"])

    assert result.exit_code == 0, result.output + screen
    assert "has no container to stop" in result.output + screen
    assert "is stopped and removed" not in result.output + screen
    assert not [c for c in cli.docker.calls if "down" in c]


# --- one TLS reading, port changes, MCP_PATH, an unreadable demo.json, the start refusal -------------------------


def test_tls_spelled_1_binds_an_https_public_url(cli: Cli, machine: Machine, cdd: Path, tmp_path: Path) -> None:
    certs = tmp_path / "certs"
    issue(lifetime=90 * DAY, left=60 * DAY, host="localhost", self_signed=False).write(certs, "localhost")
    environment(cli, cdd, "default", "--set", "MCP_TLS_ENABLED=true", "--set", "MCP_TLS_HOSTS=localhost",
                "--certs", str(certs))  # fmt: skip
    # The server reads MCP_TLS_ENABLED as a pydantic bool; a hand-edited server.env may spell "on" as 1.
    server_env = machine.base / "default" / "server.env"
    server_env.write_text(server_env.read_text().replace("MCP_TLS_ENABLED=true", "MCP_TLS_ENABLED=1"))
    demo(cli, "keycloak")

    code, body = cli.json("configure", "--demo", "keycloak")

    assert code == 0, body
    server = shown(cli)["server_settings"]
    assert server["MCP_PUBLIC_BASE_URL"] == "https://localhost:8000"
    assert server["MCP_OAUTH_AUDIENCE"] == "https://localhost:8000/mcp"


def test_a_port_change_alone_moves_the_demo_filled_url_and_audience(cli: Cli, machine: Machine, cdd: Path) -> None:
    environment(cli, cdd)
    demo(cli, "keycloak")
    cli.json("configure", "--demo", "keycloak")

    code, body = cli.json("configure", "--port", "8005")

    assert code == 0, body
    assert {"MCP_PUBLIC_BASE_URL", "MCP_OAUTH_AUDIENCE"} <= set(body["result"]["changed"])
    server = shown(cli)["server_settings"]
    assert server["MCP_PUBLIC_BASE_URL"] == "http://localhost:8005"
    assert server["MCP_OAUTH_AUDIENCE"] == "http://localhost:8005/mcp"
    assert server["MCP_OAUTH_REALM_URL"] == REALM
    assert demo_json(machine)["public_base_url"] == "http://localhost:8005"


def test_a_port_change_keeps_a_public_url_the_administrator_gave(cli: Cli, machine: Machine, cdd: Path) -> None:
    environment(cli, cdd, "default", "--set", "MCP_PUBLIC_BASE_URL=http://127.0.0.1:8000")
    demo(cli, "keycloak")
    cli.json("configure", "--demo", "keycloak")

    code, body = cli.json("configure", "--port", "8005")

    assert code == 0, body
    assert shown(cli)["server_settings"]["MCP_PUBLIC_BASE_URL"] == "http://127.0.0.1:8000"
    assert demo_json(machine)["public_base_url"] == "http://127.0.0.1:8000"


def test_keycloak_binding_with_another_mcp_path_is_refused(cli: Cli, machine: Machine, cdd: Path) -> None:
    environment(cli, cdd, "default", "--set", "MCP_PATH=/sql")
    demo(cli, "keycloak", "dex")

    code, body = cli.json("configure", "--demo", "keycloak")

    assert code == 2
    assert "MCP_PATH" in body["error"]["message"]
    assert demo_json(machine).get("bound_environment") is None
    code, body = cli.json("configure", "--demo", "dex")
    assert code == 0, body


def test_an_unreadable_demo_settings_file_stops_configure_and_start(cli: Cli, machine: Machine, cdd: Path) -> None:
    environment(cli, cdd)
    demo(cli, "keycloak")
    cli.json("configure", "--demo", "keycloak")
    settings_file = machine.base / ".demo" / "demo.json"
    settings_file.write_text("{not json", encoding="utf-8")

    configured = cli.json("configure", "--port", "8005")
    started_ = cli.json("start")

    for code, body in (configured, started_):
        assert code == 92, body
        assert str(settings_file) in body["error"]["message"]


def test_status_names_an_unreadable_demo_settings_file(cli: Cli, machine: Machine, cdd: Path) -> None:
    environment(cli, cdd)
    (machine.base / ".demo").mkdir(parents=True)
    (machine.base / ".demo" / "demo.json").write_text("{not json", encoding="utf-8")
    cli.docker.reply(inspect_args(), returncode=1, stderr="Error: No such object: connx-mcp-ce-default\n")

    code, body = cli.json("status", "default")

    assert code == 0, body
    assert any("demo.json" in notice for notice in body["result"]["notices"])


def test_configure_demo_for_a_self_signed_tls_environment_is_refused(cli: Cli, machine: Machine, cdd: Path) -> None:
    environment(cli, cdd, "default", "--set", "MCP_TLS_ENABLED=true", "--set", "MCP_TLS_HOSTS=localhost")

    code, body = cli.json("configure-demo", "--keycloak", "--for", "default")

    assert code == 2
    assert "self-signed" in body["error"]["message"]
    assert body["error"]["next"] == "connx-mcp-ce configure default --certs <directory>"
    assert not (machine.base / ".demo" / "demo.json").exists()


def test_configure_demo_for_a_keycloak_environment_on_another_mcp_path_is_refused(
    cli: Cli, machine: Machine, cdd: Path
) -> None:
    environment(
        cli, cdd, "default", "--set", "MCP_OAUTH_PROVIDER=keycloak",
        "--set", "MCP_PUBLIC_BASE_URL=http://127.0.0.1:8000",
        "--set", "MCP_OAUTH_REALM_URL=https://kc.example.com/realms/connx",
        "--set", "MCP_OAUTH_AUDIENCE=http://127.0.0.1:8000/sql", "--set", "MCP_PATH=/sql",
    )  # fmt: skip

    code, body = cli.json("configure-demo", "--keycloak", "--for", "default")

    assert code == 2
    assert "MCP_PATH" in body["error"]["message"]
    assert body["error"]["next"] == "connx-mcp-ce configure default --unset MCP_PATH"
    assert not (machine.base / ".demo" / "demo.json").exists()


def test_start_refuses_a_bound_environment_with_self_signed_tls(cli: Cli, machine: Machine, cdd: Path) -> None:
    environment(cli, cdd)
    demo(cli, "keycloak")
    cli.json("configure", "--demo", "keycloak")
    server_env = machine.base / "default" / "server.env"
    server_env.write_text(
        server_env.read_text(encoding="utf-8") + "MCP_TLS_ENABLED=true\nMCP_TLS_HOSTS=localhost\n", encoding="utf-8"
    )

    code, body = cli.json("start")

    assert code == 2
    assert "self-signed" in body["error"]["message"]
    assert body["error"]["next"] == "connx-mcp-ce configure default --certs <directory>"
    assert not [call for call in cli.docker.calls if "up" in call]


# --- live: a demo user signs in through Keycloak and reads data ------------------------------------------------


def _reachable(url: str) -> str:
    """The URL as this machine reaches it: the stack host maps to localhost when it does not resolve here."""
    try:
        socket.getaddrinfo(DEFAULT_STACK_HOST, None)
        return url
    except OSError:
        return url.replace(f"//{DEFAULT_STACK_HOST}:", "//localhost:")


class _KeepRedirect(urllib.request.HTTPRedirectHandler):
    """Follow redirects, except the one to the server's callback: that one carries the code."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001, ANN201, D102
        if "/auth/callback" in newurl:
            raise _Callback(newurl)
        return super().redirect_request(req, fp, code, msg, headers, _reachable(newurl))


class _Callback(Exception):
    """The redirect to the server's callback, with the authorization code."""


class _LocalhostIsSecure(http.cookiejar.DefaultCookiePolicy):
    """Send a Secure cookie over http to localhost only, as browsers do (localhost is a secure context).

    Keycloak marks its login cookies Secure even over http; without this the restart cookie never returns.
    """

    def return_ok_secure(self, cookie: http.cookiejar.Cookie, request: urllib.request.Request) -> bool:
        host = urllib.parse.urlparse(request.get_full_url()).hostname
        return host in ("localhost", "127.0.0.1") or super().return_ok_secure(cookie, request)


def _refusal(error: urllib.error.HTTPError, jar: http.cookiejar.CookieJar) -> str:
    """Why Keycloak refused the login form: its status, its page's error text and the cookies held (names and
    flags only, never a value)."""
    page = error.read().decode(errors="replace")
    shown = re.findall(r'class="[^"]*(?:alert|error|instruction)[^"]*"[^>]*>\s*([^<]{3,200})', page)
    cookies = [f"{c.name} (domain {c.domain}, path {c.path}, secure {c.secure})" for c in jar]
    return f"Keycloak answered {error.code} {error.reason} at {error.url}: {shown or page[:300]!r}; cookies held: {cookies}"


def _sign_in(base_url: str, user: str, realm: str = REALM) -> dict[str, Any]:
    """Sign a demo user in through the Keycloak login form (authorization code flow); return the token response."""
    jar = http.cookiejar.CookieJar(_LocalhostIsSecure())
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar), _KeepRedirect)
    redirect = f"{base_url}/auth/callback"
    query = urllib.parse.urlencode(
        {"client_id": "connx-mcp-generic", "response_type": "code", "scope": "openid", "redirect_uri": redirect}
    )
    page = opener.open(_reachable(f"{realm}/protocol/openid-connect/auth?{query}"), timeout=30).read().decode()
    found = re.search(r'action="([^"]+)"', page)
    assert found, "no Keycloak login form"
    action = _reachable(found.group(1).replace("&amp;", "&"))
    try:
        opener.open(action, urllib.parse.urlencode({"username": user, "password": user}).encode(), timeout=30)
        raise AssertionError("Keycloak did not redirect to the server's callback")
    except urllib.error.HTTPError as error:
        raise AssertionError(_refusal(error, jar)) from None
    except _Callback as callback:
        code = urllib.parse.parse_qs(urllib.parse.urlparse(str(callback)).query)["code"][0]
    form = {"grant_type": "authorization_code", "code": code, "redirect_uri": redirect}
    request = urllib.request.Request(
        _reachable(f"{realm}/protocol/openid-connect/token"),
        urllib.parse.urlencode(form).encode(),
        {"Authorization": "Basic " + base64.b64encode(b"connx-mcp-generic:connx-mcp-generic-secret").decode()},
    )
    with urllib.request.urlopen(request, timeout=30) as reply:
        return json.loads(reply.read())


def _mcp(url: str, token: str, session: str | None, payload: dict[str, Any]) -> tuple[str | None, dict[str, Any]]:
    """Post one JSON-RPC message to the server's MCP endpoint; return the session id and the reply."""
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        **({"Mcp-Session-Id": session} if session else {}),
    }
    with urllib.request.urlopen(urllib.request.Request(url, json.dumps(payload).encode(), headers), timeout=60) as r:
        body = r.read().decode()
        session = r.headers.get("Mcp-Session-Id", session)
    if not body.strip():
        return session, {}
    data = [line[5:].strip() for line in body.splitlines() if line.startswith("data:")]
    return session, json.loads(data[-1] if data else body)


@pytest.mark.live
def test_a_demo_user_signs_in_through_keycloak_and_reads_with_the_mapped_login(
    cli: Cli, machine: Machine, tmp_path: Path
) -> None:
    """Needs the pulled image, free ports 8000, 8080 and 8200, and the CONNX_MCP_LIVE_* values; run from the host."""
    needed = ["CONNX_MCP_LIVE_CDD", "CONNX_MCP_LIVE_USER", "CONNX_MCP_LIVE_PASSWORD"]
    if missing := [n for n in needed if n not in os.environ]:
        pytest.skip(f"set {', '.join(missing)} to sign in against the real image and demo stack")
    if LIVE_STACK_HOST:
        pytest.skip("CONNX_MCP_LIVE_STACK_HOST names a remote demo stack: the remote sign-in test runs instead")
    real = default_ports()
    cli.docker, cli.probe, cli.clock = real.docker, real.probe, real.clock  # type: ignore[assignment]
    login = os.environ["CONNX_MCP_LIVE_USER"]
    users_file = tmp_path / "users.yaml"
    users_file.write_text(
        "identities:\n  - name: alice\n    id: 4bb91078-2e33-4fe4-b386-bdf1d51f1e6a\n"
        f"    connx: {{ username: {login}, password_env: CONNX_MCP_LIVE_PASSWORD }}\n",
        encoding="utf-8",
    )
    licence = os.environ.get("CONNX_MCP_LIVE_LICENSE_HOST", "host.docker.internal")
    code, body = cli.json(
        "configure", "live-15", "--no-input", "--set", f"CDD_USERNAME={login}",
        "--set", f"CDD_PASSWORD={os.environ['CONNX_MCP_LIVE_PASSWORD']}", "--cdd", os.environ["CONNX_MCP_LIVE_CDD"],
        "--set", f"CONNX_LICENSE_HOST={licence}",
    )  # fmt: skip
    assert code == 0, body
    code, body = cli.json(
        "configure-demo", "--keycloak", "--vault", "--users-file", str(users_file), "--for", "live-15"
    )
    assert code == 0, body
    code, body = cli.json("configure", "live-15", "--demo", "keycloak", "--demo-vault")
    assert code == 0, body
    try:
        code, body = cli.json("start-demo")
        assert code == 0, body
        code, body = cli.json("start", "live-15")
        assert code == 0, body

        tokens = _sign_in("http://localhost:8000", "alice")
        claims = json.loads(base64.urlsafe_b64decode(tokens["access_token"].split(".")[1] + "=="))
        assert claims["iss"] == REALM  # the issuer the browser saw is the one the server checks
        url = "http://localhost:8000/mcp"
        hello = {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "live-15", "version": "1"}}
        session, reply = _mcp(url, tokens["access_token"], None, {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                                                   "params": hello})  # fmt: skip
        assert "result" in reply, reply
        _mcp(url, tokens["access_token"], session, {"jsonrpc": "2.0", "method": "notifications/initialized"})
        call = {"name": "list_databases", "arguments": {}}
        _, reply = _mcp(url, tokens["access_token"], session, {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                                                               "params": call})  # fmt: skip
        assert reply["result"].get("isError") is not True, reply
        logs = real.docker.run(["logs", "connx-mcp-ce-live-15"])
        assert f"-> connx user {login}" in logs.stdout + logs.stderr
    finally:
        cli.json("stop", "live-15")
        real.docker.run(["compose", "-p", "connx-mcp-ce-live-15", "down", "--volumes"])
        cli.json("stop-demo", "--purge")


@pytest.mark.live
def test_a_demo_user_signs_in_at_a_remote_stack_and_reads_with_the_mapped_login(cli: Cli, machine: Machine) -> None:
    """Needs CONNX_MCP_LIVE_STACK_HOST (the remote stack's STACK_HOST, its realm rendered for
    http://localhost:8000 and its alice mapped to a CONNX login), CONNX_MCP_LIVE_VAULT_CA (a copy of its
    generated/tls/ca.pem), CONNX_MCP_LIVE_VAULT_READ_TOKEN when the stack does not use the published default,
    the pulled image, a free port 8000 and the CONNX_MCP_LIVE_* values. Never starts or stops the stack."""
    needed = [
        "CONNX_MCP_LIVE_STACK_HOST", "CONNX_MCP_LIVE_VAULT_CA", "CONNX_MCP_LIVE_CDD", "CONNX_MCP_LIVE_USER",
        "CONNX_MCP_LIVE_PASSWORD",
    ]  # fmt: skip
    if missing := [n for n in needed if not os.environ.get(n)]:
        pytest.skip(f"set {', '.join(missing)} to sign in at a demo stack on another machine")
    real = default_ports()
    cli.docker, cli.probe, cli.clock = real.docker, real.probe, real.clock  # type: ignore[assignment]
    host = os.environ["CONNX_MCP_LIVE_STACK_HOST"]
    licence = os.environ.get("CONNX_MCP_LIVE_LICENSE_HOST", "host.docker.internal")
    code, body = cli.json(
        "configure", "live-19", "--no-input", "--set", f"CDD_USERNAME={os.environ['CONNX_MCP_LIVE_USER']}",
        "--set", f"CDD_PASSWORD={os.environ['CONNX_MCP_LIVE_PASSWORD']}", "--cdd", os.environ["CONNX_MCP_LIVE_CDD"],
        "--set", f"CONNX_LICENSE_HOST={licence}",
    )  # fmt: skip
    assert code == 0, body
    token = os.environ.get("CONNX_MCP_LIVE_VAULT_READ_TOKEN")
    code, body = cli.json(
        "configure", "live-19", "--demo", "keycloak", "--demo-vault", "--stack-host", host,
        "--vault-ca", os.environ["CONNX_MCP_LIVE_VAULT_CA"], *(["--set", "VAULT_TOKEN=-"] if token else []),
        input=token,
    )  # fmt: skip
    assert code == 0, body
    assert not [w for w in body["result"]["warnings"] if "does not answer" in w or "does not resolve" in w], body
    assert not (machine.base / ".demo").exists()
    try:
        code, body = cli.json("start", "live-19")
        assert code == 0, body
        code, body = cli.json("status", "live-19")
        assert code == 0, body
        assert body["result"]["demo_stack"]["stack_host"] == host
        realm = body["result"]["demo_stack"]["discovery"].removesuffix("/.well-known/openid-configuration")
        assert not [n for n in body["result"]["notices"] if "does not answer" in n or "names its issuer" in n]

        tokens = _sign_in("http://localhost:8000", "alice", realm)
        claims = json.loads(base64.urlsafe_b64decode(tokens["access_token"].split(".")[1] + "=="))
        assert claims["iss"] == realm  # the issuer the browser saw is the one the server checks
        url = "http://localhost:8000/mcp"
        hello = {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "live-19", "version": "1"}}
        session, reply = _mcp(url, tokens["access_token"], None, {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                                                   "params": hello})  # fmt: skip
        assert "result" in reply, reply
        _mcp(url, tokens["access_token"], session, {"jsonrpc": "2.0", "method": "notifications/initialized"})
        call = {"name": "list_databases", "arguments": {}}
        _, reply = _mcp(url, tokens["access_token"], session, {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                                                               "params": call})  # fmt: skip
        assert reply["result"].get("isError") is not True, reply
        logs = real.docker.run(["logs", "connx-mcp-ce-live-19"])
        assert "-> connx user " in logs.stdout + logs.stderr
    finally:
        cli.json("stop", "live-19")
        real.docker.run(["compose", "-p", "connx-mcp-ce-live-19", "down", "--volumes"])
