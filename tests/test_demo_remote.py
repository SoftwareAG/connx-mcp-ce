#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_demo_remote.py - An environment uses a demo stack that runs on another machine.

Description:
    `configure <env> --demo keycloak|dex [--demo-vault] --stack-host <name>` binds an environment to a
    demo stack this machine does not run: issuers, audience and Vault's address follow the stack's
    name and ports, the Vault CA is a copy given with `--vault-ca`, and the Vault read token comes from
    stdin or ask-at-start, the published default (with a warning) otherwise. Where the stack answers,
    a discovery document naming another issuer is refused with the name to give. `status` and `guide`
    check the stack from this machine; `remove` and the local stack's binding rules leave a remote
    binding alone. Driven through the CLI seam with the fake Docker port and endpoint probe.

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
import shlex
from pathlib import Path
from typing import Any

import pytest

from connx_mcp.probe import HealthReply, UrllibProbe
from tests.conftest import Cli, Machine, refused_with
from tests.test_lifecycle import image_found, inspect_args
from tests.test_remove import down_args, started, volume_ls_args

PASSWORD = "pw-for-tests-only"
READ_TOKEN = "read-token-for-tests-only"
STACK = "stack01"
REALM = f"http://{STACK}:8080/realms/connx-mcp"
DISCOVERY = f"{REALM}/.well-known/openid-configuration"
DEX = f"http://{STACK}:5556/dex"
VAULT_HEALTH = f"https://{STACK}:8200/v1/sys/health"
CORP_VAULT = "https://vault.corp.example:8200"


@pytest.fixture
def cdd(tmp_path: Path) -> Path:
    """A CDD file on this machine."""
    path = tmp_path / "dictionaries" / "sales.cdd"
    path.parent.mkdir()
    path.write_bytes(b"CDD")
    return path


@pytest.fixture
def ca(tmp_path: Path) -> Path:
    """A copy of the stack machine's demo CA."""
    path = tmp_path / "copied-ca.pem"
    path.write_text("-----BEGIN CERTIFICATE-----\n", encoding="utf-8")
    return path


def environment(cli: Cli, cdd: Path, name: str = "default") -> None:
    """Create a complete environment without OAuth."""
    code, body = cli.json(
        "configure", name, "--no-input", "--set", "CDD_USERNAME=mcp", "--set", f"CDD_PASSWORD={PASSWORD}",
        "--cdd", str(cdd),
    )  # fmt: skip
    assert code == 0, body


def no_container(cli: Cli) -> None:
    """The environment has no container yet."""
    cli.docker.reply(inspect_args(), returncode=1, stderr="Error: No such object: connx-mcp-ce-default\n")


def discovery(cli: Cli, url: str = DISCOVERY, issuer: str = REALM) -> None:
    """Make the stack's discovery document answer with an issuer."""
    cli.probe.health_replies[url] = HealthReply(200, json.dumps({"issuer": issuer}))


def stored(machine: Machine, name: str = "default") -> tuple[dict[str, str], dict[str, Any]]:
    """The environment's server settings (unmasked, as stored) and host settings."""
    directory = machine.base / name
    server = dict(
        line.split("=", 1)
        for line in (directory / "server.env").read_text(encoding="utf-8").splitlines()
        if "=" in line
    )
    return server, json.loads((directory / "host.json").read_text(encoding="utf-8"))


def bind_remote(cli: Cli, ca: Path, *extra: str, name: str = "default", token: str | None = READ_TOKEN) -> Any:
    """Bind an environment to the remote stack with Keycloak and Vault, the read token piped in."""
    stdin = ["--set", "VAULT_TOKEN=-"] if token is not None else []
    code, body = cli.json(
        "configure", name, "--demo", "keycloak", "--demo-vault", "--stack-host", STACK, "--vault-ca", str(ca),
        *stdin, *extra, input=token,
    )  # fmt: skip
    assert code == 0, body
    return body


# --- configure --stack-host ---------------------------------------------------------------------------------


def test_a_remote_binding_renders_the_issuer_and_vault_address_from_the_stack_host(
    cli: Cli, machine: Machine, cdd: Path, ca: Path
) -> None:
    environment(cli, cdd)
    discovery(cli)

    body = bind_remote(cli, ca)

    server, host = stored(machine)
    assert server["MCP_OAUTH_PROVIDER"] == "keycloak"
    assert server["MCP_OAUTH_REALM_URL"] == "http://stack01:8080/realms/connx-mcp"
    assert server["MCP_OAUTH_AUDIENCE"] == "http://localhost:8000/mcp"
    assert server["VAULT_ADDR"] == "https://stack01:8200"
    assert host["demo_stack_host"] == STACK
    assert host["vault_ca_file"] == str(ca)
    assert body["result"]["demo"] == {
        "bound_environment": "default",
        "public_base_url": "http://localhost:8000",
        "stack_host": STACK,
        "remote": True,
    }
    assert not (machine.base / ".demo").exists()


def test_the_read_token_comes_from_stdin_and_never_reaches_the_output(
    cli: Cli, machine: Machine, cdd: Path, ca: Path
) -> None:
    environment(cli, cdd)
    discovery(cli)

    body = bind_remote(cli, ca)

    server, _ = stored(machine)
    assert server["VAULT_TOKEN"] in (READ_TOKEN, f"'{READ_TOKEN}'")
    assert READ_TOKEN not in json.dumps(body)
    assert not [w for w in body["result"]["warnings"] if "default read token" in w]


def test_without_a_read_token_the_published_default_applies_with_a_warning_naming_it(
    cli: Cli, machine: Machine, cdd: Path, ca: Path
) -> None:
    environment(cli, cdd)
    discovery(cli)

    body = bind_remote(cli, ca, token=None)

    server, _ = stored(machine)
    assert server["VAULT_TOKEN"] == "connx-mcp-dev-read"
    warnings = " ".join(body["result"]["warnings"])
    assert "default read token" in warnings
    assert "VAULT_READ_TOKEN" in warnings
    assert "connx-mcp-dev-read" not in json.dumps(body)


def test_every_remote_binding_warns_that_the_network_reaches_the_stack(cli: Cli, cdd: Path, ca: Path) -> None:
    environment(cli, cdd)
    discovery(cli)

    body = bind_remote(cli, ca)

    warnings = " ".join(body["result"]["warnings"])
    assert "8080, 5556 and 8200" in warnings
    assert "KEYCLOAK_ADMIN_PASSWORD" in warnings
    assert "The demo stack is for demonstration use only, not production certified." in warnings


def test_an_ask_at_start_read_token_is_kept_and_never_defaulted(
    cli: Cli, machine: Machine, cdd: Path, ca: Path
) -> None:
    environment(cli, cdd)
    discovery(cli)

    body = bind_remote(cli, ca, "--ask-at-start", "VAULT_TOKEN", token=None)

    server, host = stored(machine)
    assert "VAULT_TOKEN" not in server
    assert host["ask_at_start"] == ["VAULT_TOKEN"]
    assert not [w for w in body["result"]["warnings"] if "default read token" in w]


def test_a_rebind_keeps_the_read_token_given_before(cli: Cli, machine: Machine, cdd: Path, ca: Path) -> None:
    environment(cli, cdd)
    discovery(cli)
    bind_remote(cli, ca)

    code, body = cli.json("configure", "--demo", "keycloak", "--demo-vault")

    assert code == 0, body
    server, host = stored(machine)
    assert server["VAULT_TOKEN"] in (READ_TOKEN, f"'{READ_TOKEN}'")
    assert host["demo_stack_host"] == STACK
    assert host["vault_ca_file"] == str(ca)


def test_dex_on_other_ports_is_rendered_from_the_stack_ports(cli: Cli, machine: Machine, cdd: Path) -> None:
    environment(cli, cdd)
    discovery(cli, f"http://{STACK}:15556/dex/.well-known/openid-configuration", f"http://{STACK}:15556/dex")

    code, body = cli.json("configure", "--demo", "dex", "--stack-host", STACK, "--stack-port", "dex=15556,vault=18200")

    assert code == 0, body
    server, host = stored(machine)
    assert server["MCP_OAUTH_PROVIDER"] == "generic-oidc"
    assert server["MCP_OAUTH_CONFIG_URL"] == "http://stack01:15556/dex/.well-known/openid-configuration"
    assert server["MCP_OAUTH_CLIENT_ID"] == "connx-mcp"
    assert server["MCP_OAUTH_AUDIENCE"] == "connx-mcp"
    assert host["demo_stack_ports"] == "keycloak=8080,dex=15556,vault=18200"


def test_a_stack_port_flag_may_be_repeated(cli: Cli, machine: Machine, cdd: Path) -> None:
    environment(cli, cdd)

    code, body = cli.json(
        "configure", "--demo", "keycloak", "--stack-host", STACK, "--stack-port", "keycloak=9090",
        "--stack-port", "dex=15556",
    )  # fmt: skip

    assert code == 0, body
    server, host = stored(machine)
    assert server["MCP_OAUTH_REALM_URL"] == "http://stack01:9090/realms/connx-mcp"
    assert host["demo_stack_ports"] == "keycloak=9090,dex=15556,vault=8200"


def test_an_unknown_stack_port_service_is_refused(cli: Cli, machine: Machine, cdd: Path) -> None:
    environment(cli, cdd)

    code, body = cli.json("configure", "--demo", "keycloak", "--stack-host", STACK, "--stack-port", "ldap=389")

    assert code == 2
    assert "ldap" in body["error"]["message"]
    assert "demo_stack_host" not in stored(machine)[1]


def test_a_discovery_document_naming_another_issuer_is_refused_with_the_name_to_give(
    cli: Cli, machine: Machine, cdd: Path
) -> None:
    environment(cli, cdd)
    fqdn = f"{STACK}.example.com"
    discovery(cli, f"http://{fqdn}:8080/realms/connx-mcp/.well-known/openid-configuration", REALM)

    code, body = cli.json("configure", "--demo", "keycloak", "--stack-host", fqdn)

    assert code == 190
    assert REALM in body["error"]["message"]
    assert body["error"]["next"] == f"connx-mcp-ce configure default --demo keycloak --stack-host {STACK}"
    server, host = stored(machine)
    assert "demo_stack_host" not in host
    assert "MCP_OAUTH_REALM_URL" not in server


@pytest.mark.parametrize("provider", ["keycloak", "okta"])
def test_a_first_pass_edit_refusal_leaves_the_remote_stack_unasked_and_is_reported_with_the_request_s(
    cli: Cli, machine: Machine, cdd: Path, provider: str
) -> None:
    environment(cli, cdd)
    fqdn = f"{STACK}.example.com"
    discovery(cli, f"http://{fqdn}:8080/realms/connx-mcp/.well-known/openid-configuration", REALM)

    code, body = cli.json("configure", "--set", "MCP_PATH=bad", "--demo", provider, "--stack-host", fqdn)

    assert code == 2
    assert body["error"]["message"].startswith("--set MCP_PATH: ")
    assert refused_with(body, "--demo okta: ") is (provider == "okta")
    assert REALM not in body["error"]["message"]
    assert cli.probe.calls == []
    assert "demo_stack_host" not in stored(machine)[1]


def test_a_first_pass_refused_only_by_a_rule_still_asks_the_remote_stack(cli: Cli, cdd: Path) -> None:
    # The rules judge the result after the demo fill, so only an edit refusal keeps the demo out.
    environment(cli, cdd)

    code, body = cli.json("configure", "--set", "MCP_TLS_ENABLED=true", "--demo", "keycloak", "--stack-host", STACK)

    assert code == 2
    assert not body["error"]["message"].startswith("--set MCP_TLS_ENABLED: ")
    assert f"resolves {STACK}" in cli.probe.calls
    assert DISCOVERY in cli.probe.calls


def test_an_unreachable_stack_is_only_a_warning_at_configure_time(cli: Cli, machine: Machine, cdd: Path) -> None:
    environment(cli, cdd)

    code, body = cli.json("configure", "--demo", "keycloak", "--stack-host", STACK)

    assert code == 0, body
    assert any("does not answer" in w and DISCOVERY in w for w in body["result"]["warnings"])
    assert stored(machine)[1]["demo_stack_host"] == STACK


def test_a_stack_host_this_machine_cannot_resolve_is_a_warning(cli: Cli, machine: Machine, cdd: Path) -> None:
    environment(cli, cdd)
    cli.probe.unresolvable.add(STACK)

    code, body = cli.json("configure", "--demo", "keycloak", "--stack-host", STACK)

    assert code == 0, body
    assert any(f"{STACK} does not resolve" in w and "DNS" in w for w in body["result"]["warnings"])
    assert DISCOVERY not in cli.probe.calls


def test_a_remote_vault_without_a_ca_copy_is_refused(cli: Cli, machine: Machine, cdd: Path) -> None:
    environment(cli, cdd)
    discovery(cli)

    code, body = cli.json("configure", "--demo", "keycloak", "--demo-vault", "--stack-host", STACK)

    assert code == 3
    assert "generated/tls/ca.pem" in body["error"]["message"]
    assert body["error"]["next"] == "connx-mcp-ce configure default --vault-ca <copy of the stack's ca.pem>"
    assert "demo_stack_host" not in stored(machine)[1]


@pytest.mark.parametrize("host", ["localhost", "127.0.0.1", "not a name"])
def test_a_loopback_or_malformed_stack_host_is_refused(cli: Cli, machine: Machine, cdd: Path, host: str) -> None:
    environment(cli, cdd)

    code, body = cli.json("configure", "--demo", "keycloak", "--stack-host", host)

    assert code == 2
    assert "--stack-host" in body["error"]["message"]
    assert "demo_stack_host" not in stored(machine)[1]


def test_a_stack_host_without_demo_is_refused(cli: Cli, cdd: Path) -> None:
    environment(cli, cdd)

    code, body = cli.json("configure", "--stack-host", STACK)

    assert code == 2
    assert "--demo" in body["error"]["message"]


@pytest.mark.parametrize(
    ("request_flags", "request_refused"),
    [(["--demo", "keycloak"], False), (["--demo-vault"], True)],
    ids=["valid-request", "refused-request"],
)
def test_a_stack_port_without_a_stack_host_is_reported_with_the_other_refusals(
    cli: Cli, cdd: Path, request_flags: list[str], request_refused: bool
) -> None:
    environment(cli, cdd)

    code, body = cli.json("configure", "--set", "MCP_PATH=bad", *request_flags, "--stack-port", "keycloak=9090")

    assert code == 2
    assert refused_with(body, "--set MCP_PATH: ")
    assert refused_with(
        body, "--stack-port: it needs --stack-host; the ports belong to a demo stack on another machine."
    )
    assert refused_with(body, "--demo-vault: needs --demo keycloak or --demo dex.") is request_refused
    assert cli.probe.calls == []


def test_an_empty_stack_host_ends_the_remote_binding(cli: Cli, machine: Machine, cdd: Path) -> None:
    environment(cli, cdd)
    cli.json("configure", "--demo", "keycloak", "--stack-host", STACK, "--stack-port", "keycloak=9090")

    code, body = cli.json("configure", "--stack-host", "")

    assert code == 0, body
    host = stored(machine)[1]
    assert "demo_stack_host" not in host
    assert "demo_stack_ports" not in host


def test_a_remote_binding_needs_no_rebind_while_the_local_stack_serves_another_environment(
    cli: Cli, machine: Machine, cdd: Path
) -> None:
    environment(cli, cdd, "local")
    environment(cli, cdd, "remote")
    assert cli.json("configure-demo", "--keycloak", "--for", "local")[0] == 0

    code, body = cli.json("configure", "remote", "--demo", "keycloak", "--stack-host", STACK)

    assert code == 0, body
    demo = json.loads((machine.base / ".demo" / "demo.json").read_text(encoding="utf-8"))
    assert demo["bound_environment"] == "local"


def test_binding_the_locally_bound_environment_to_a_remote_stack_leaves_the_local_binding_alone(
    cli: Cli, machine: Machine, cdd: Path
) -> None:
    environment(cli, cdd)
    assert cli.json("configure-demo", "--keycloak", "--for", "default")[0] == 0

    code, body = cli.json("configure", "--demo", "keycloak", "--stack-host", STACK)

    assert code == 0, body
    demo = json.loads((machine.base / ".demo" / "demo.json").read_text(encoding="utf-8"))
    assert demo["bound_environment"] == "default"


def test_configure_demo_for_a_remote_bound_environment_is_refused(cli: Cli, cdd: Path) -> None:
    environment(cli, cdd)
    cli.json("configure", "--demo", "keycloak", "--stack-host", STACK)

    code, body = cli.json("configure-demo", "--keycloak", "--for", "default")

    assert code == 2
    assert STACK in body["error"]["message"]
    assert body["error"]["next"] == "connx-mcp-ce configure default --stack-host '' --demo keycloak"


def test_the_remote_binding_survives_an_export_and_import(
    cli: Cli, machine: Machine, cdd: Path, tmp_path: Path
) -> None:
    environment(cli, cdd)
    cli.json("configure", "--demo", "keycloak", "--stack-host", STACK, "--stack-port", "keycloak=9090")
    exported = tmp_path / "default.env"
    assert cli.json("export", "--output", str(exported))[0] == 0

    code, body = cli.json(
        "configure", "copy", "--no-input", "--from-env-file", str(exported), "--set", f"CDD_PASSWORD={PASSWORD}"
    )

    assert code == 0, body
    host = stored(machine, "copy")[1]
    assert host["demo_stack_host"] == STACK
    assert host["demo_stack_ports"] == "keycloak=9090,dex=5556,vault=8200"


# --- status and guide --------------------------------------------------------------------------------------


def test_status_shows_the_remote_binding_and_a_healthy_stack_raises_no_notice(cli: Cli, cdd: Path, ca: Path) -> None:
    environment(cli, cdd)
    discovery(cli)
    bind_remote(cli, ca)
    cli.probe.health_replies[VAULT_HEALTH] = HealthReply(200, "{}")
    no_container(cli)

    code, body = cli.json("status", "default")

    assert code == 0, body
    stack = body["result"]["demo_stack"]
    assert stack == {
        "stack_host": STACK,
        "ports": {"keycloak": 8080, "dex": 5556, "vault": 8200},
        "discovery": DISCOVERY,
        "vault_health": VAULT_HEALTH,
    }
    assert not [n for n in body["result"]["notices"] if STACK in n and "Warning" not in n and "network" not in n]


def test_status_names_an_unreachable_stack_with_the_next_step(cli: Cli, cdd: Path, ca: Path) -> None:
    environment(cli, cdd)
    discovery(cli)
    bind_remote(cli, ca)
    cli.probe.health_replies.clear()
    no_container(cli)

    code, body = cli.json("status", "default")

    assert code == 0, body
    notices = body["result"]["notices"]
    assert any(DISCOVERY in n and "does not answer" in n and "8080" in n for n in notices)
    assert any(VAULT_HEALTH in n and "does not answer" in n and "8200" in n for n in notices)


def test_status_names_a_stack_host_that_does_not_resolve(cli: Cli, cdd: Path, ca: Path) -> None:
    environment(cli, cdd)
    discovery(cli)
    bind_remote(cli, ca)
    cli.probe.unresolvable.add(STACK)
    no_container(cli)

    code, body = cli.json("status", "default")

    assert code == 0, body
    assert any(f"{STACK} does not resolve" in n and "server's container" in n for n in body["result"]["notices"])


def test_status_names_a_stack_now_serving_another_issuer(cli: Cli, cdd: Path, ca: Path) -> None:
    environment(cli, cdd)
    discovery(cli)
    bind_remote(cli, ca)
    cli.probe.health_replies[VAULT_HEALTH] = HealthReply(200, "{}")
    discovery(cli, issuer=f"http://{STACK}.example.com:8080/realms/connx-mcp")
    no_container(cli)

    code, body = cli.json("status", "default")

    assert any(
        f"{STACK}.example.com" in n and f"--stack-host {STACK}.example.com" in n for n in body["result"]["notices"]
    )


def test_a_sealed_vault_is_a_notice(cli: Cli, cdd: Path, ca: Path) -> None:
    environment(cli, cdd)
    discovery(cli)
    bind_remote(cli, ca)
    cli.probe.health_replies[VAULT_HEALTH] = HealthReply(503, "{}")
    no_container(cli)

    code, body = cli.json("status", "default")

    assert any(VAULT_HEALTH in n and "503" in n for n in body["result"]["notices"])


def test_status_human_output_names_the_remote_stack(cli: Cli, cdd: Path, ca: Path) -> None:
    environment(cli, cdd)
    discovery(cli)
    bind_remote(cli, ca)
    no_container(cli)

    result = cli("status", "default")

    assert f"Demo stack:    remote at {STACK} (keycloak 8080, dex 5556, vault 8200)" in result.output


def test_guide_warns_about_an_unreachable_remote_stack(cli: Cli, cdd: Path, ca: Path) -> None:
    environment(cli, cdd)
    discovery(cli)
    bind_remote(cli, ca)
    cli.probe.health_replies.clear()
    image_found(cli)
    no_container(cli)

    code, body = cli.json("guide", "default")

    assert code == 0, body
    assert any(DISCOVERY in w and "does not answer" in w for w in body["result"]["warnings"])


# --- remove and stop-demo leave a remote binding alone -----------------------------------------------------


def test_remove_of_a_remote_bound_environment_needs_no_force(cli: Cli, machine: Machine, cdd: Path) -> None:
    environment(cli, cdd)
    cli.json("configure", "--demo", "keycloak", "--stack-host", STACK)
    started(machine)
    cli.docker.reply(down_args(machine))
    cli.docker.reply(volume_ls_args(), stdout="")

    code, body = cli.json("remove", "default", "--yes")

    assert code == 0, body
    assert not (machine.base / "default").exists()


def test_the_status_list_names_the_remote_stack_host(cli: Cli, cdd: Path) -> None:
    environment(cli, cdd)
    cli.json("configure", "--demo", "keycloak", "--stack-host", STACK)
    no_container(cli)

    code, body = cli.json("status", "--all")

    assert code == 0, body
    assert body["result"]["environments"][0]["demo_stack_host"] == STACK
    assert body["result"]["environments"][0]["demo_bound"] is False


def test_start_refuses_a_remote_bound_environment_with_self_signed_tls(cli: Cli, machine: Machine, cdd: Path) -> None:
    environment(cli, cdd)
    cli.json("configure", "--demo", "keycloak", "--stack-host", STACK)
    server_env = machine.base / "default" / "server.env"
    server_env.write_text(
        server_env.read_text(encoding="utf-8") + "MCP_TLS_ENABLED=true\nMCP_TLS_HOSTS=localhost\n", encoding="utf-8"
    )

    code, body = cli.json("start")

    assert code == 2
    assert "self-signed" in body["error"]["message"]
    assert not [call for call in cli.docker.calls if "up" in call]


# --- review fixes: the stack's answer is data, never trusted -------------------------------------------------

MALFORMED_ISSUERS = [
    "http://stack01:99999/realms/connx-mcp",  # port out of range
    "http://stack01:abc/realms/connx-mcp",  # port not a number
    "http://stack01:8080:9090/realms/connx-mcp",  # two ports
    "http:///realms/connx-mcp",  # no host
    "http://:8080/realms/connx-mcp",  # no host before the port
    "http://[::1/realms/connx-mcp",  # broken IPv6 literal
    "ftp://stack01:8080/realms/connx-mcp",  # not http
    "javascript:alert(1)",
    "not a url",
    "",
    "http://a$(id)|sh;b:8080/realms/connx-mcp",  # a shell command as host
    "http://user@stack01:8080/realms/connx-mcp",  # user info
    "http://localhost:8080/realms/connx-mcp",  # loopback: --stack-host refuses it
    "http://stack01:8080/realms/other",  # another realm: no stack name renders it
    "http://stack01:8080/realms/connx-mcp\u001b[2J‮",  # terminal escapes
    "http://stack01:8080/" + "x" * 5000,  # very long
]


def no_traceback(result: Any) -> Any:
    """Return the parsed JSON of a run that ended without an uncaught exception."""
    assert result.exception is None or isinstance(result.exception, SystemExit), result.exception
    return json.loads(result.stdout)


def printable(text: str) -> bool:
    """Whether text is free of control characters and bounded, as a terminal should get it."""
    return text.isprintable() and len(text) < 1500


@pytest.mark.parametrize("issuer", MALFORMED_ISSUERS)
def test_configure_refuses_an_unusable_issuer_without_a_traceback(
    cli: Cli, machine: Machine, cdd: Path, issuer: str
) -> None:
    environment(cli, cdd)
    discovery(cli, issuer=issuer)

    result = cli("configure", "--demo", "keycloak", "--stack-host", STACK, "--json")

    body = no_traceback(result)
    assert result.exit_code == 190
    assert printable(body["error"]["message"])
    assert body["error"]["next"] is None
    assert "demo_stack_host" not in stored(machine)[1]


@pytest.mark.parametrize("issuer", MALFORMED_ISSUERS)
def test_status_names_an_unusable_issuer_as_a_notice(cli: Cli, cdd: Path, ca: Path, issuer: str) -> None:
    environment(cli, cdd)
    discovery(cli)
    bind_remote(cli, ca)
    cli.probe.health_replies[VAULT_HEALTH] = HealthReply(200, "{}")
    discovery(cli, issuer=issuer)
    no_container(cli)

    result = cli("status", "default", "--json")

    body = no_traceback(result)
    assert result.exit_code == 0
    notices = [n for n in body["result"]["notices"] if "issuer" in n and DISCOVERY in n]
    assert len(notices) == 1
    assert printable(notices[0])
    assert "--stack-host" not in notices[0]


@pytest.mark.parametrize("issuer", MALFORMED_ISSUERS)
def test_the_status_list_survives_an_unusable_issuer(cli: Cli, cdd: Path, ca: Path, issuer: str) -> None:
    environment(cli, cdd)
    discovery(cli)
    bind_remote(cli, ca)
    discovery(cli, issuer=issuer)
    no_container(cli)

    result = cli("status", "--all", "--json")

    body = no_traceback(result)
    assert result.exit_code == 0
    assert body["result"]["environments"][0]["demo_stack_host"] == STACK


def follow_suggestion(cli: Cli, suggestion: str, input: str | None = None) -> tuple[int, Any]:
    """Run the command a refusal suggests, as the administrator would paste it."""
    argv = shlex.split(suggestion)
    assert argv[0] == "connx-mcp-ce"
    return cli.json(*argv[1:], input=input)


@pytest.mark.parametrize("started_as", ["RudCnx01", "STACK01", "stack01.Example.com"])
def test_the_suggested_stack_host_keeps_its_case_and_then_passes_the_check(
    cli: Cli, machine: Machine, cdd: Path, started_as: str
) -> None:
    environment(cli, cdd)
    issuer = f"http://{started_as}:8080/realms/connx-mcp"
    given = started_as.lower()
    discovery(cli, f"http://{given}:8080/realms/connx-mcp/.well-known/openid-configuration", issuer)
    discovery(cli, f"{issuer}/.well-known/openid-configuration", issuer)

    code, body = cli.json("configure", "--demo", "keycloak", "--stack-host", given)

    assert code == 190
    assert f"--stack-host {started_as}" in body["error"]["next"]
    code, body = follow_suggestion(cli, body["error"]["next"])
    assert code == 0, body
    server, host = stored(machine)
    assert host["demo_stack_host"] == started_as
    assert server["MCP_OAUTH_REALM_URL"] == issuer


def test_the_suggestion_keeps_vault_its_ca_and_every_port_and_then_passes_the_check(
    cli: Cli, machine: Machine, cdd: Path, tmp_path: Path
) -> None:
    environment(cli, cdd)
    ca = tmp_path / "copied ca.pem"
    ca.write_text("-----BEGIN CERTIFICATE-----\n", encoding="utf-8")
    issuer = "http://RudCnx01:18080/realms/connx-mcp"
    discovery(cli, "http://rudcnx01:18080/realms/connx-mcp/.well-known/openid-configuration", issuer)
    discovery(cli, f"{issuer}/.well-known/openid-configuration", issuer)
    cli.probe.health_replies["https://RudCnx01:18200/v1/sys/health"] = HealthReply(200, "{}")

    code, body = cli.json(
        "configure", "--demo", "keycloak", "--demo-vault", "--stack-host", "rudcnx01", "--stack-port",
        "keycloak=18080,vault=18200", "--vault-ca", str(ca),
    )  # fmt: skip

    assert code == 190
    suggestion = body["error"]["next"]
    assert "--demo-vault" in suggestion
    assert "--vault-ca" in suggestion
    code, body = follow_suggestion(cli, suggestion)
    assert code == 0, body
    server, host = stored(machine)
    assert server["MCP_OAUTH_REALM_URL"] == issuer
    assert server["VAULT_ADDR"] == "https://RudCnx01:18200"
    assert host["vault_ca_file"] == str(ca)
    assert host["demo_stack_ports"] == "keycloak=18080,dex=5556,vault=18200"


def test_the_suggestion_for_a_stack_on_another_port_then_passes_the_check(
    cli: Cli, machine: Machine, cdd: Path
) -> None:
    environment(cli, cdd)
    issuer = f"http://{STACK}:8080/realms/connx-mcp"
    discovery(cli, f"http://{STACK}:9090/realms/connx-mcp/.well-known/openid-configuration", issuer)
    discovery(cli)

    code, body = cli.json("configure", "--demo", "keycloak", "--stack-host", STACK, "--stack-port", "keycloak=9090")

    assert code == 190
    code, body = follow_suggestion(cli, body["error"]["next"])
    assert code == 0, body
    assert stored(machine)[0]["MCP_OAUTH_REALM_URL"] == issuer


def test_a_discovery_document_naming_no_issuer_is_a_warning(cli: Cli, cdd: Path) -> None:
    environment(cli, cdd)
    cli.probe.health_replies[DISCOVERY] = HealthReply(200, "<html>not json</html>")

    code, body = cli.json("configure", "--demo", "keycloak", "--stack-host", STACK)

    assert code == 0, body
    assert any("names no issuer" in w and DISCOVERY in w for w in body["result"]["warnings"])


def test_the_unresolved_notice_names_the_environment_and_its_provider(cli: Cli, cdd: Path) -> None:
    environment(cli, cdd, "sales")
    cli.probe.unresolvable.add(STACK)

    code, body = cli.json("configure", "sales", "--demo", "dex", "--stack-host", STACK)

    assert code == 0, body
    assert any("connx-mcp-ce configure sales --demo dex --stack-host <name>" in w for w in body["result"]["warnings"])


def test_a_later_stack_port_keeps_the_other_stored_ports(cli: Cli, machine: Machine, cdd: Path) -> None:
    environment(cli, cdd)
    cli.json("configure", "--demo", "keycloak", "--stack-host", STACK, "--stack-port", "keycloak=9090")

    code, body = cli.json("configure", "--demo", "keycloak", "--stack-host", STACK, "--stack-port", "vault=8201")

    assert code == 0, body
    assert stored(machine)[1]["demo_stack_ports"] == "keycloak=9090,dex=5556,vault=8201"


def test_vault_health_is_checked_against_the_given_ca_copy(cli: Cli, cdd: Path, ca: Path) -> None:
    environment(cli, cdd)
    discovery(cli)
    cli.probe.health_replies[VAULT_HEALTH] = HealthReply(200, "{}")

    bind_remote(cli, ca)

    assert cli.probe.ca_files[VAULT_HEALTH] == str(ca)


def test_a_vault_certificate_the_ca_copy_does_not_verify_is_a_notice(cli: Cli, cdd: Path, ca: Path) -> None:
    environment(cli, cdd)
    discovery(cli)
    bind_remote(cli, ca)
    cli.probe.health_replies[VAULT_HEALTH] = HealthReply(
        None, "", error="certificate verify failed", certificate_refused=True
    )
    no_container(cli)

    code, body = cli.json("status", "default")

    assert code == 0, body
    assert any(VAULT_HEALTH in n and "--vault-ca" in n for n in body["result"]["notices"])


def test_ending_the_remote_binding_returns_the_environment_to_its_settings_before_it(
    cli: Cli, machine: Machine, cdd: Path, ca: Path
) -> None:
    environment(cli, cdd)
    before = stored(machine)
    discovery(cli)
    bind_remote(cli, ca, token=None)

    code, body = cli.json("configure", "--stack-host", "")

    assert code == 0, body
    assert stored(machine) == before


def test_ending_the_remote_binding_drops_a_read_token_given_for_its_vault(
    cli: Cli, machine: Machine, cdd: Path, ca: Path
) -> None:
    environment(cli, cdd)
    before = stored(machine)
    discovery(cli)
    bind_remote(cli, ca)

    code, body = cli.json("configure", "--stack-host", "")

    assert code == 0, body
    assert stored(machine) == before


def test_ending_the_remote_binding_keeps_the_values_an_imported_file_gives(
    cli: Cli, machine: Machine, cdd: Path, ca: Path, tmp_path: Path
) -> None:
    environment(cli, cdd)
    discovery(cli)
    bind_remote(cli, ca)
    sent = tmp_path / "corp.env"
    sent.write_text(f"VAULT_ADDR={CORP_VAULT}\n", encoding="utf-8")

    code, body = cli.json("configure", "--from-env-file", str(sent), "--stack-host", "")

    assert code == 0, body
    server, host = stored(machine)
    assert server["VAULT_ADDR"] == CORP_VAULT
    assert "VAULT_TOKEN" not in server  # issued for the stack's Vault, which the environment no longer uses
    assert "demo_stack_host" not in host


def test_ending_the_remote_binding_drops_the_read_token_when_a_flag_names_another_vault(
    cli: Cli, machine: Machine, cdd: Path, ca: Path
) -> None:
    environment(cli, cdd)
    discovery(cli)
    bind_remote(cli, ca)

    code, body = cli.json("configure", "--stack-host", "", "--set", f"VAULT_ADDR={CORP_VAULT}")

    assert code == 0, body
    server = stored(machine)[0]
    assert server["VAULT_ADDR"] == CORP_VAULT
    assert "VAULT_TOKEN" not in server


def test_ending_the_remote_binding_keeps_the_read_token_while_a_flag_keeps_its_vault(
    cli: Cli, machine: Machine, cdd: Path, ca: Path
) -> None:
    environment(cli, cdd)
    discovery(cli)
    bind_remote(cli, ca)

    code, body = cli.json("configure", "--stack-host", "", "--set", f"VAULT_ADDR=https://{STACK}:8200")

    assert code == 0, body
    server = stored(machine)[0]
    assert server["VAULT_ADDR"] == f"https://{STACK}:8200"
    assert server["VAULT_TOKEN"] in (READ_TOKEN, f"'{READ_TOKEN}'")


def test_a_rebind_to_another_stack_never_carries_the_read_token_to_its_vault(
    cli: Cli, machine: Machine, cdd: Path, ca: Path
) -> None:
    environment(cli, cdd)
    discovery(cli)
    bind_remote(cli, ca)
    other = "stack02"

    code, body = cli.json(
        "configure", "--demo", "keycloak", "--demo-vault", "--stack-host", other,
        "--set", f"VAULT_ADDR=https://{other}:8200",
    )  # fmt: skip

    assert code == 0, body
    server = stored(machine)[0]
    assert server["VAULT_ADDR"] == f"https://{other}:8200"
    assert server["VAULT_TOKEN"] == "connx-mcp-dev-read"
    assert any("default read token" in w for w in body["result"]["warnings"])


def test_rebinding_to_the_local_stack_on_another_port_binds_it_to_the_new_public_url(
    cli: Cli, machine: Machine, cdd: Path
) -> None:
    environment(cli, cdd)
    assert cli.json("configure-demo", "--keycloak")[0] == 0
    discovery(cli)
    assert cli.json("configure", "--demo", "keycloak", "--stack-host", STACK)[0] == 0

    code, body = cli.json("configure", "--port", "9100", "--stack-host", "", "--demo", "keycloak")

    assert code == 0, body
    server, host = stored(machine)
    assert server["MCP_PUBLIC_BASE_URL"] == "http://localhost:9100"
    assert server["MCP_OAUTH_AUDIENCE"] == "http://localhost:9100/mcp"
    assert "demo_stack_host" not in host
    demo = json.loads((machine.base / ".demo" / "demo.json").read_text(encoding="utf-8"))
    assert demo["bound_environment"] == "default"
    assert demo["public_base_url"] == "http://localhost:9100"


def test_a_remote_binding_neither_reads_nor_writes_the_local_stack_settings(
    cli: Cli, machine: Machine, cdd: Path
) -> None:
    environment(cli, cdd)
    demo_json = machine.base / ".demo" / "demo.json"
    demo_json.parent.mkdir(parents=True)
    demo_json.write_text("{not json", encoding="utf-8")

    code, body = cli.json("configure", "--demo", "keycloak", "--stack-host", STACK)

    assert code == 0, body
    assert demo_json.read_text(encoding="utf-8") == "{not json"


def test_the_real_probe_never_falls_back_to_no_verification_when_the_ca_copy_cannot_be_read(tmp_path: Path) -> None:
    reply = UrllibProbe().health("https://127.0.0.1:9/v1/sys/health", ca_file=str(tmp_path / "missing-ca.pem"))

    assert reply.status is None
    assert reply.certificate_refused
