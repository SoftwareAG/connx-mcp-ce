#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_oauth_vault.py - The administrator connects the server to their identity provider and HashiCorp Vault.

Description:
    Through the CLI seam with the fake Docker port, endpoint probe and clock: choosing an
    OAuth provider asks for and accepts only that provider's fields, by flag and on a terminal;
    `configure` enforces the server's cross-field rules (Vault requires OAuth, Keycloak requires an
    audience, Azure requires scopes, OAuth with a self-signed certificate is refused); a Vault CA
    file on this machine is mounted at the in-container path VAULT_CACERT names; OAuth or Vault
    raise the default start timeout to 420 s and the health start period to 300 s; the client
    secret and the Vault token are secrets. The walk drops the fields a switch made inactive when
    asked to, and re-asks a group whose answers break a cross-field rule.

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
from pathlib import Path

import pytest

from connx_mcp import settings
from connx_mcp.ports import default_ports
from tests.conftest import Cli, Machine, bind, mounts
from tests.test_configure_interactive import DOWN, FRESH_WALK, answers, prompt_line
from tests.test_lifecycle import comes_up, env_dir, image_found, serving, state

PASSWORD = "pw-for-tests-only"
CLIENT_SECRET = "client-secret-for-tests-only"
VAULT_TOKEN = "vault-token-for-tests-only"
BASE_URL = "https://mcp.example.com"
REALM = "https://kc.example.com/realms/connx"

KEYCLOAK = [
    "MCP_OAUTH_PROVIDER=keycloak",
    f"MCP_PUBLIC_BASE_URL={BASE_URL}",
    f"MCP_OAUTH_REALM_URL={REALM}",
    f"MCP_OAUTH_AUDIENCE={BASE_URL}/mcp",
]
VAULT = [
    "MCP_CREDENTIAL_PROVIDER=hashicorp",
    "VAULT_ADDR=https://vault.example.com:8200",
    f"VAULT_TOKEN={VAULT_TOKEN}",
]


@pytest.fixture
def cdd(tmp_path: Path) -> Path:
    path = tmp_path / "dictionaries" / "sales.cdd"
    path.parent.mkdir()
    path.write_bytes(b"CDD")
    return path


@pytest.fixture
def vault_ca(tmp_path: Path) -> Path:
    path = tmp_path / "pki" / "vault-ca.pem"
    path.parent.mkdir()
    path.write_text("-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----\n", encoding="utf-8")
    return path


def sets(*items: str) -> list[str]:
    return [arg for item in items for arg in ("--set", item)]


def configure(cli: Cli, cdd: Path, *items: str, extra: tuple[str, ...] = ()) -> tuple[int, dict]:
    """Configure the default environment by flags: the credentials and CDD plus the given --set items."""
    return cli.json(
        "configure", "--no-input", *sets("CDD_USERNAME=mcp", f"CDD_PASSWORD={PASSWORD}", *items),
        "--cdd", str(cdd), *extra,
    )  # fmt: skip


def shown(cli: Cli) -> dict[str, str]:
    code, body = cli.json("configure", "--show")
    assert code == 0, body
    return body["result"]["server_settings"]


def compose_file(machine: Machine) -> str:
    return (env_dir(machine) / "compose.yml").read_text(encoding="utf-8")


# --- One provider's fields, by flag ---------------------------------------------------------------------------


def test_keycloak_is_configured_with_its_own_fields(cli: Cli, cdd: Path) -> None:
    code, body = configure(cli, cdd, *KEYCLOAK)

    assert code == 0, body
    assert not [w for w in body["result"]["warnings"] if "OAUTH" in w or "PUBLIC_BASE_URL" in w]
    assert shown(cli)["MCP_OAUTH_PROVIDER"] == "keycloak"


@pytest.mark.parametrize(
    ("provider", "required"),
    [
        ("keycloak", {"MCP_PUBLIC_BASE_URL", "MCP_OAUTH_REALM_URL", "MCP_OAUTH_AUDIENCE"}),
        (
            "azure",
            {
                "MCP_PUBLIC_BASE_URL",
                "MCP_OAUTH_CLIENT_ID",
                "MCP_OAUTH_CLIENT_SECRET",
                "MCP_OAUTH_TENANT_ID",
                "MCP_OAUTH_REQUIRED_SCOPES",
            },
        ),
        ("google", {"MCP_PUBLIC_BASE_URL", "MCP_OAUTH_CLIENT_ID", "MCP_OAUTH_CLIENT_SECRET"}),
        (
            "aws",
            {"MCP_PUBLIC_BASE_URL", "MCP_OAUTH_CONFIG_URL", "MCP_OAUTH_CLIENT_ID", "MCP_OAUTH_CLIENT_SECRET"},
        ),
        (
            "generic-oidc",
            {"MCP_PUBLIC_BASE_URL", "MCP_OAUTH_CONFIG_URL", "MCP_OAUTH_CLIENT_ID", "MCP_OAUTH_CLIENT_SECRET"},
        ),
    ],
)
def test_choosing_a_provider_requires_exactly_its_fields(
    cli: Cli, machine: Machine, cdd: Path, provider: str, required: set[str]
) -> None:
    code, body = configure(cli, cdd, f"MCP_OAUTH_PROVIDER={provider}")

    assert code == 3
    message = body["error"]["message"]
    named = {key for key in settings.SCHEMA if key in message}
    assert named == required
    assert not (machine.base / "default").exists()


def test_another_providers_field_is_refused_naming_its_provider(cli: Cli, machine: Machine, cdd: Path) -> None:
    code, body = configure(cli, cdd, *KEYCLOAK, "MCP_OAUTH_TENANT_ID=0000-tenant")

    assert code == 2, body
    message = body["error"]["message"]
    assert "MCP_OAUTH_TENANT_ID is a field of azure" in message
    assert "MCP_OAUTH_PROVIDER is keycloak" in message
    assert "--unset MCP_OAUTH_TENANT_ID" in message
    assert not (machine.base / "default").exists()


def test_a_providers_field_without_a_provider_is_only_warned_about(cli: Cli, cdd: Path) -> None:
    code, body = configure(cli, cdd, "MCP_OAUTH_TENANT_ID=0000-tenant")

    assert code == 0, body
    warning = next(w for w in body["result"]["warnings"] if "MCP_OAUTH_TENANT_ID" in w)
    assert "has no effect unless MCP_OAUTH_PROVIDER is azure" in warning
    assert "--unset MCP_OAUTH_TENANT_ID" in warning


GENERIC_OIDC = [
    "MCP_OAUTH_PROVIDER=generic-oidc",
    f"MCP_PUBLIC_BASE_URL={BASE_URL}",
    "MCP_OAUTH_CONFIG_URL=https://dex.example.com/.well-known/openid-configuration",
    "MCP_OAUTH_CLIENT_ID=connx-mcp",
    f"MCP_OAUTH_CLIENT_SECRET={CLIENT_SECRET}",
]


def test_an_empty_required_scopes_means_no_scope_is_required(cli: Cli, cdd: Path) -> None:
    code, body = configure(cli, cdd, *GENERIC_OIDC, "MCP_OAUTH_REQUIRED_SCOPES=")

    assert code == 0, body
    assert shown(cli)["MCP_OAUTH_REQUIRED_SCOPES"] == ""


def test_the_oauth_state_directory_elsewhere_is_refused_with_the_reason(cli: Cli, machine: Machine, cdd: Path) -> None:
    code, body = configure(cli, cdd, *GENERIC_OIDC, "MCP_OAUTH_STORAGE_DIR=/elsewhere")

    assert code == 2, body
    message = body["error"]["message"]
    assert "/opt/softwareag/connx/mcp/oauth-state" in message
    assert "signed out" in message
    assert not (machine.base / "default").exists()


def test_the_oauth_state_directory_at_the_mounted_path_is_accepted(cli: Cli, cdd: Path) -> None:
    code, body = configure(cli, cdd, *GENERIC_OIDC, "MCP_OAUTH_STORAGE_DIR=/opt/softwareag/connx/mcp/oauth-state")

    assert code == 0, body
    assert not [w for w in body["result"]["warnings"] if "STORAGE_DIR" in w]


def test_an_unknown_provider_is_refused_with_the_choices(cli: Cli, cdd: Path) -> None:
    code, body = configure(cli, cdd, "MCP_OAUTH_PROVIDER=okta")

    assert code == 2
    assert "none, keycloak, azure, google, aws, generic-oidc" in body["error"]["message"]


@pytest.mark.parametrize(
    ("item", "reason"),
    [
        ("MCP_PUBLIC_BASE_URL=http://mcp.example.com", "https"),
        ("MCP_PUBLIC_BASE_URL=https://mcp.example.com/mcp", "no path"),
        ("MCP_PUBLIC_BASE_URL=mcp.example.com", "http:// or https://"),
        ("VAULT_ADDR=http://vault.example.com:8200", "https"),
        ("MCP_OAUTH_CONFIG_URL=https://idp.example.com/", "/.well-known/openid-configuration"),
    ],
)
def test_an_identity_url_outside_its_domain_is_refused(cli: Cli, cdd: Path, item: str, reason: str) -> None:
    code, body = configure(cli, cdd, item)

    assert code == 2
    assert reason in body["error"]["message"]


@pytest.mark.parametrize("url", ["http://localhost:8000", "http://127.0.0.1:8000", "http://[::1]:8000"])
def test_plain_http_is_accepted_on_loopback(cli: Cli, cdd: Path, url: str) -> None:
    keycloak = [f"MCP_PUBLIC_BASE_URL={url}" if item.startswith("MCP_PUBLIC") else item for item in KEYCLOAK]

    code, body = configure(cli, cdd, *keycloak)

    assert code == 0, body


# --- Cross-field rules --------------------------------------------------------------------------------------


def test_vault_without_oauth_is_refused(cli: Cli, machine: Machine, cdd: Path) -> None:
    code, body = configure(cli, cdd, *VAULT)

    assert code == 2
    assert "MCP_CREDENTIAL_PROVIDER=hashicorp needs OAuth" in body["error"]["message"]
    assert not (machine.base / "default").exists()


def test_vault_with_oauth_is_accepted(cli: Cli, cdd: Path) -> None:
    code, body = configure(cli, cdd, *KEYCLOAK, *VAULT)

    assert code == 0, body


def test_keycloak_without_an_audience_is_refused_naming_it(cli: Cli, cdd: Path) -> None:
    code, body = configure(cli, cdd, *KEYCLOAK[:3])

    assert code == 3
    assert "MCP_OAUTH_AUDIENCE" in body["error"]["message"]


def azure(scopes: str) -> list[str]:
    return [
        "MCP_OAUTH_PROVIDER=azure",
        f"MCP_PUBLIC_BASE_URL={BASE_URL}",
        "MCP_OAUTH_CLIENT_ID=app-id",
        f"MCP_OAUTH_CLIENT_SECRET={CLIENT_SECRET}",
        "MCP_OAUTH_TENANT_ID=tenant-id",
        f"MCP_OAUTH_REQUIRED_SCOPES={scopes}",
    ]


def test_azure_with_a_custom_scope_is_accepted(cli: Cli, cdd: Path) -> None:
    code, body = configure(cli, cdd, *azure("access_as_user"))

    assert code == 0, body


def test_azure_with_only_openid_scopes_is_refused(cli: Cli, cdd: Path) -> None:
    code, body = configure(cli, cdd, *azure("openid offline_access"))

    assert code == 2
    assert "custom scope" in body["error"]["message"]


def test_an_azure_scope_with_its_api_prefix_is_warned_about(cli: Cli, cdd: Path) -> None:
    code, body = configure(cli, cdd, *azure("api://app-id/access_as_user"))

    assert code == 0, body
    assert any("access_as_user" in w and "api://" in w for w in body["result"]["warnings"])


def test_oauth_with_tls_warns_that_a_self_signed_certificate_is_refused(cli: Cli, cdd: Path) -> None:
    code, body = configure(cli, cdd, *KEYCLOAK, "MCP_TLS_ENABLED=true", "MCP_TLS_HOSTS=mcp.example.com")

    assert code == 0, body
    assert any("self-signed" in w for w in body["result"]["warnings"])


@pytest.mark.parametrize(("self_signed", "refused"), [(True, True), (False, False)])
def test_oauth_with_a_self_signed_certificate_is_refused(self_signed: bool, refused: bool) -> None:
    server = dict(item.split("=", 1) for item in [*KEYCLOAK, "MCP_TLS_ENABLED=true"])
    facts = settings.Facts(server, serves_self_signed=self_signed)

    errors = [p for rule in settings.RULES for p in rule(facts) if p.error]

    assert bool([p for p in errors if "self-signed" in p.message]) is refused


def test_oauth_without_tls_says_nothing_about_certificates(cli: Cli, cdd: Path) -> None:
    code, body = configure(cli, cdd, *KEYCLOAK)

    assert code == 0, body
    assert not any("self-signed" in w for w in body["result"]["warnings"])


# --- Secrets ------------------------------------------------------------------------------------------------


def test_the_client_secret_and_vault_token_are_masked(cli: Cli, cdd: Path) -> None:
    code, body = configure(cli, cdd, *azure("access_as_user"), *VAULT)
    assert code == 0, body

    result = cli("configure", "--show")

    values = shown(cli)
    assert values["MCP_OAUTH_CLIENT_SECRET"] == settings.HIDDEN
    assert values["VAULT_TOKEN"] == settings.HIDDEN
    for secret in (CLIENT_SECRET, VAULT_TOKEN):
        assert secret not in result.output
        assert secret not in json.dumps(body)


def test_the_client_secret_and_vault_token_can_be_asked_at_start() -> None:
    server = dict(item.split("=", 1) for item in [*azure("access_as_user"), *VAULT])
    server.update(CDD_USERNAME="mcp", CDD_PASSWORD="")
    del server["MCP_OAUTH_CLIENT_SECRET"], server["VAULT_TOKEN"]

    assert set(settings.missing(server)) == {"MCP_OAUTH_CLIENT_SECRET", "VAULT_TOKEN"}
    assert settings.missing(server, supplied_at_start={"MCP_OAUTH_CLIENT_SECRET", "VAULT_TOKEN"}) == []


def test_start_hands_ask_at_start_client_secret_and_vault_token_to_the_server_only(
    cli: Cli, machine: Machine, cdd: Path
) -> None:
    items = [
        i for i in [*azure("access_as_user"), *VAULT] if not i.startswith(("MCP_OAUTH_CLIENT_SECRET", "VAULT_TOKEN"))
    ]
    asked = ("--ask-at-start", "MCP_OAUTH_CLIENT_SECRET", "--ask-at-start", "VAULT_TOKEN")
    code, body = configure(cli, cdd, *items, extra=asked)
    assert code == 0, body
    image_found(cli)
    comes_up(cli, machine, state())
    serving(cli)

    code, body = cli.json(
        "start", "--secret-env", "MCP_OAUTH_CLIENT_SECRET=CI_CLIENT_SECRET", "--secret-env", "VAULT_TOKEN=CI_TOKEN",
        env={"CI_CLIENT_SECRET": CLIENT_SECRET, "CI_TOKEN": VAULT_TOKEN},
    )  # fmt: skip

    assert code == 0, body
    up = next(i for i, call in enumerate(cli.docker.calls) if call[0] == "compose" and "up" in call)
    assert cli.docker.environments[up] == {
        "CONNX_MCP_START_MCP_OAUTH_CLIENT_SECRET": CLIENT_SECRET,
        "CONNX_MCP_START_VAULT_TOKEN": VAULT_TOKEN,
    }
    stored = [p.read_text(encoding="utf-8") for p in env_dir(machine).rglob("*") if p.is_file()]
    assert not any(CLIENT_SECRET in text or VAULT_TOKEN in text for text in stored)


def test_start_does_not_ask_for_a_secret_whose_group_is_switched_off(cli: Cli, machine: Machine, cdd: Path) -> None:
    items = [i for i in [*KEYCLOAK, *VAULT] if not i.startswith("VAULT_TOKEN")]
    assert configure(cli, cdd, *items, extra=("--ask-at-start", "VAULT_TOKEN"))[0] == 0
    code, body = cli.json(
        "configure", "--no-input", "--set", "MCP_CREDENTIAL_PROVIDER=none", "--unset", "VAULT_ADDR"
    )  # fmt: skip
    assert code == 0, body
    image_found(cli)
    comes_up(cli, machine, state())
    serving(cli)

    code, body = cli.json("start", "--no-input")

    assert code == 0, body


# --- Start: Vault CA, timeout, health start period ----------------------------------------------------------


def test_a_vault_ca_file_is_mounted_where_vault_cacert_points(
    cli: Cli, machine: Machine, cdd: Path, vault_ca: Path
) -> None:
    code, body = configure(cli, cdd, *KEYCLOAK, *VAULT, extra=("--vault-ca", str(vault_ca)))
    assert code == 0, body
    image_found(cli)
    comes_up(cli, machine, state())
    serving(cli)

    code, body = cli.json("start")

    assert code == 0, body
    rendered = compose_file(machine)
    assert bind(vault_ca, "/opt/softwareag/connx/mcp/vault-ca.pem", read_only=True) in mounts(rendered)
    assert 'VAULT_CACERT: "/opt/softwareag/connx/mcp/vault-ca.pem"' in rendered
    assert "VAULT_CACERT" not in (env_dir(machine) / "server.env").read_text(encoding="utf-8")


def test_the_vault_ca_file_is_stored_absolute_and_can_be_cleared(
    cli: Cli, cdd: Path, vault_ca: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(vault_ca.parent)
    code, body = configure(cli, cdd, *KEYCLOAK, *VAULT, extra=("--vault-ca", vault_ca.name))
    assert code == 0, body
    assert cli.json("configure", "--show")[1]["result"]["host_settings"]["vault_ca_file"] == str(vault_ca)

    code, body = cli.json("configure", "--no-input", "--vault-ca", "")

    assert code == 0, body
    assert "vault_ca_file" not in cli.json("configure", "--show")[1]["result"]["host_settings"]


def test_a_missing_vault_ca_file_is_refused(cli: Cli, cdd: Path, tmp_path: Path) -> None:
    code, body = configure(cli, cdd, *KEYCLOAK, *VAULT, extra=("--vault-ca", str(tmp_path / "nope.pem")))

    assert code == 2
    assert "--vault-ca" in body["error"]["message"]
    assert "no such file" in body["error"]["message"]


def test_a_vault_ca_file_without_vault_is_warned_about(cli: Cli, cdd: Path, vault_ca: Path) -> None:
    code, body = configure(cli, cdd, *KEYCLOAK, extra=("--vault-ca", str(vault_ca)))

    assert code == 0, body
    assert any("--vault-ca" in w and "no effect" in w for w in body["result"]["warnings"])


def test_start_refuses_a_vault_ca_file_that_has_gone(cli: Cli, machine: Machine, cdd: Path, vault_ca: Path) -> None:
    configure(cli, cdd, *KEYCLOAK, *VAULT, extra=("--vault-ca", str(vault_ca)))
    image_found(cli)
    vault_ca.unlink()

    code, body = cli.json("start")

    assert code == 80
    assert body["error"]["code"] == "vault_ca_not_found"
    assert str(vault_ca) in body["error"]["message"]
    assert body["error"]["next"].startswith("connx-mcp-ce configure default --vault-ca")


@pytest.mark.parametrize("items", [KEYCLOAK, [*KEYCLOAK, *VAULT]], ids=["oauth", "oauth-and-vault"])
def test_oauth_or_vault_wait_420_seconds_with_a_300_second_health_start_period(
    cli: Cli, machine: Machine, cdd: Path, items: list[str]
) -> None:
    configure(cli, cdd, *items)
    image_found(cli)
    comes_up(cli, machine, state(health="starting"))

    code, body = cli.json("start")

    assert code == 33
    assert "420 s" in body["error"]["message"]
    assert 420 <= cli.clock.now < 423
    assert "    healthcheck:\n      start_period: 300s\n" in compose_file(machine)


def test_without_oauth_the_image_keeps_its_own_health_start_period(cli: Cli, machine: Machine, cdd: Path) -> None:
    configure(cli, cdd)
    image_found(cli)
    comes_up(cli, machine, state())
    serving(cli)

    code, body = cli.json("start")

    assert code == 0, body
    assert "healthcheck" not in compose_file(machine)


def test_a_given_timeout_still_wins_with_oauth(cli: Cli, machine: Machine, cdd: Path) -> None:
    configure(cli, cdd, *KEYCLOAK)
    image_found(cli)
    comes_up(cli, machine, state(health="starting"))

    code, body = cli.json("start", "--timeout", "60")

    assert code == 33
    assert 60 <= cli.clock.now < 63


# --- The walk ----------------------------------------------------------------------------------------------

# The walk of a fresh environment that chooses Keycloak and no Vault.
KEYCLOAK_WALK = [
    *FRESH_WALK[:-3],
    "MCP_OAUTH_PROVIDER",
    "MCP_PUBLIC_BASE_URL",
    "MCP_OAUTH_REALM_URL",
    "MCP_OAUTH_AUDIENCE",
    "MCP_OAUTH_REQUIRED_SCOPES",
    *FRESH_WALK[-2:],
]
KEYCLOAK_ANSWERS = {
    "CDD_USERNAME": "mcp",
    "CDD_PASSWORD": PASSWORD,
    "MCP_OAUTH_PROVIDER": DOWN,
    "MCP_PUBLIC_BASE_URL": BASE_URL,
    "MCP_OAUTH_REALM_URL": REALM,
    "MCP_OAUTH_AUDIENCE": f"{BASE_URL}/mcp",
}


def test_the_fresh_walk_offers_both_switches_left_at_none(cli: Cli, cdd: Path) -> None:
    keys = answers(FRESH_WALK, {"CDD_USERNAME": "mcp", "CDD_PASSWORD": PASSWORD, "cdd": str(cdd)})

    result, screen = cli.typed("configure", keys=keys)

    assert result.exit_code == 0, result.output + screen
    for title in ("OAuth sign-in", "Credential Vault"):
        assert title in screen
    assert "'none' (not set: server default)" in prompt_line(screen, "MCP_OAUTH_PROVIDER")
    assert "'none' (not set: server default)" in prompt_line(screen, "MCP_CREDENTIAL_PROVIDER")
    for key in ("MCP_PUBLIC_BASE_URL", "VAULT_ADDR", "MCP_OAUTH_CLIENT_SECRET"):
        assert key not in screen


def test_choosing_keycloak_asks_only_its_fields(cli: Cli, cdd: Path) -> None:
    keys = answers(KEYCLOAK_WALK, {**KEYCLOAK_ANSWERS, "cdd": str(cdd)})

    result, screen = cli.typed("configure", keys=keys)

    assert result.exit_code == 0, result.output + screen
    for key in ("MCP_OAUTH_CLIENT_ID", "MCP_OAUTH_CLIENT_SECRET", "MCP_OAUTH_TENANT_ID", "MCP_OAUTH_CONFIG_URL"):
        assert key not in screen
    values = shown(cli)
    assert values["MCP_OAUTH_PROVIDER"] == "keycloak"
    assert values["MCP_OAUTH_AUDIENCE"] == f"{BASE_URL}/mcp"


def test_the_walk_asks_for_the_vault_ca_on_this_machine(cli: Cli, machine: Machine, cdd: Path, vault_ca: Path) -> None:
    walk = [
        *KEYCLOAK_WALK[:-2],
        "MCP_CREDENTIAL_PROVIDER",
        "VAULT_ADDR",
        "VAULT_TOKEN",
        "VAULT_CACERT",
        "VAULT_NAMESPACE",
        "MCP_CREDENTIAL_VAULT_MOUNT",
        "MCP_CREDENTIAL_VAULT_PATH",
        "cdd",
    ]
    given = {
        **KEYCLOAK_ANSWERS,
        "MCP_CREDENTIAL_PROVIDER": DOWN,
        "VAULT_ADDR": "https://vault.example.com:8200",
        "VAULT_TOKEN": VAULT_TOKEN,
        "VAULT_CACERT": str(vault_ca),
        "cdd": str(cdd),
    }

    result, screen = cli.typed("configure", keys=answers(walk, given))

    assert result.exit_code == 0, result.output + screen
    assert "Vault CA file on this machine" in screen
    assert VAULT_TOKEN not in screen + result.output
    assert cli.json("configure", "--show")[1]["result"]["host_settings"]["vault_ca_file"] == str(vault_ca)
    assert "VAULT_CACERT" not in shown(cli)


def test_a_rule_broken_in_a_group_is_explained_and_the_group_asked_again(cli: Cli, cdd: Path) -> None:
    # Vault chosen without OAuth is refused at the end of the Vault group, which is asked again: back to none,
    # and the Vault fields just typed are dropped.
    walk = [*FRESH_WALK[:-1], "VAULT_ADDR", "VAULT_TOKEN", "VAULT_CACERT", "VAULT_NAMESPACE"]
    walk += ["MCP_CREDENTIAL_VAULT_MOUNT", "MCP_CREDENTIAL_VAULT_PATH", "again", "drop", "cdd"]
    given = {
        "CDD_USERNAME": "mcp",
        "CDD_PASSWORD": PASSWORD,
        "MCP_CREDENTIAL_PROVIDER": DOWN,
        "VAULT_ADDR": "https://vault.example.com:8200",
        "VAULT_TOKEN": VAULT_TOKEN,
        "again": "\x1b[A",
        "cdd": str(cdd),
    }

    result, screen = cli.typed("configure", keys=answers(walk, given))

    assert result.exit_code == 0, result.output + screen
    assert "MCP_CREDENTIAL_PROVIDER=hashicorp needs OAuth" in screen
    assert screen.count("MCP_CREDENTIAL_PROVIDER ") >= 2
    values = shown(cli)
    assert values.get("MCP_CREDENTIAL_PROVIDER", "none") == "none"
    assert "VAULT_TOKEN" not in values


def test_switching_a_provider_back_to_none_offers_to_drop_its_fields(cli: Cli, machine: Machine, cdd: Path) -> None:
    code, body = configure(cli, cdd, *azure("access_as_user"))
    assert code == 0, body

    # The menu lists the six providers then the way back to the default; azure is selected, so six up is none.
    walk = [*FRESH_WALK[:-3], "MCP_OAUTH_PROVIDER", "drop", *FRESH_WALK[-2:]]
    result, screen = cli.typed("configure", keys=answers(walk, {"MCP_OAUTH_PROVIDER": "\x1b[A" * 2}))

    assert result.exit_code == 0, result.output + screen
    assert "MCP_OAUTH_CLIENT_SECRET" in screen.split("MCP_OAUTH_PROVIDER", 1)[1]
    values = shown(cli)
    for key in ("MCP_OAUTH_CLIENT_SECRET", "MCP_OAUTH_CLIENT_ID", "MCP_OAUTH_TENANT_ID", "MCP_PUBLIC_BASE_URL"):
        assert key not in values
    assert CLIENT_SECRET not in (env_dir(machine) / "server.env").read_text(encoding="utf-8")


def test_the_stale_fields_can_be_kept(cli: Cli, cdd: Path) -> None:
    code, body = configure(cli, cdd, *azure("access_as_user"))
    assert code == 0, body

    walk = [*FRESH_WALK[:-3], "MCP_OAUTH_PROVIDER", "drop", *FRESH_WALK[-2:]]
    result, screen = cli.typed("configure", keys=answers(walk, {"MCP_OAUTH_PROVIDER": "\x1b[A" * 2, "drop": "n"}))

    assert result.exit_code == 0, result.output + screen
    assert shown(cli)["MCP_OAUTH_CLIENT_SECRET"] == settings.HIDDEN
    assert "--unset MCP_OAUTH_CLIENT_SECRET" in result.output


# --- live: the demo stack -------------------------------------------------------------------------------------


# The licence server the live server reaches; on the Docker host unless the live run names another.
LIVE_LICENSE_HOST = os.environ.get("CONNX_MCP_LIVE_LICENSE_HOST", "host.docker.internal")


@pytest.mark.live
def test_a_server_signs_in_against_the_demo_keycloak_with_vault(cli: Cli, machine: Machine) -> None:
    """Needs a running demo stack with Keycloak and Vault, the CONNX_MCP_LIVE_* values and the pulled image."""
    needed = ["CONNX_MCP_LIVE_CDD", "CONNX_MCP_LIVE_USER", "CONNX_MCP_LIVE_PASSWORD", "CONNX_MCP_LIVE_VAULT_CA"]
    if missing := [n for n in needed if n not in os.environ]:
        pytest.skip(f"set {', '.join(missing)} and start the demo stack to run against the real image")
    real = default_ports()
    cli.docker, cli.probe, cli.clock = real.docker, real.probe, real.clock  # type: ignore[assignment]
    # A remote demo stack (CONNX_MCP_LIVE_STACK_HOST) is reached by its name; a local one as localhost from here
    # and as host.docker.internal from the server.
    stack = os.environ.get("CONNX_MCP_LIVE_STACK_HOST")
    realm_host, vault_host = (stack, stack) if stack else ("localhost", "host.docker.internal")
    token = os.environ.get("CONNX_MCP_LIVE_VAULT_READ_TOKEN") or "connx-mcp-dev-read"
    code, body = cli.json(
        "configure", "live-08", "--no-input",
        *sets(
            f"CDD_USERNAME={os.environ['CONNX_MCP_LIVE_USER']}", f"CDD_PASSWORD={os.environ['CONNX_MCP_LIVE_PASSWORD']}",
            f"CONNX_LICENSE_HOST={LIVE_LICENSE_HOST}", "MCP_OAUTH_PROVIDER=keycloak",
            "MCP_PUBLIC_BASE_URL=http://localhost:8000", f"MCP_OAUTH_REALM_URL=http://{realm_host}:8080/realms/connx-mcp",
            "MCP_OAUTH_AUDIENCE=http://localhost:8000/mcp", "MCP_CREDENTIAL_PROVIDER=hashicorp",
            f"VAULT_ADDR=https://{vault_host}:8200", "VAULT_TOKEN=-",
        ),
        "--cdd", os.environ["CONNX_MCP_LIVE_CDD"], "--vault-ca", os.environ["CONNX_MCP_LIVE_VAULT_CA"], input=token,
    )  # fmt: skip
    assert code == 0, body
    try:
        code, body = cli.json("start", "live-08")
        assert code == 0, body
        assert body["result"]["timeout"] == 420
    finally:
        cli.json("stop", "live-08")
        real.docker.run(["compose", "-p", "connx-mcp-ce-live-08", "down", "--volumes"])
