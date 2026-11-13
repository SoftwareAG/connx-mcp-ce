#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""remote.py - A demo stack that runs on another machine: its settings, its checks from here, its warnings.

Description:
    An environment bound with `configure <env> --demo ... --stack-host <name>` uses a demo stack this
    machine does not run: a remote stack. The binding is the environment's own host settings
    `demo_stack_host` and `demo_stack_ports`: no `demo.json` is read or written, so the local stack's
    rules (one bound environment, `--rebind`, `remove --force`, `stop-demo`) never apply to it. Every
    issuer and Vault's address are rendered from the stack's name and ports as upstream's
    `generated/mcp-server.env` renders them, so the name must be the stack's `STACK_HOST` byte for byte.
    `check` asks the stack from this machine: the name resolves, the issuer's discovery document
    answers and names the issuer the server will check, and Vault's health answers. `configure`
    refuses another issuer and only warns otherwise; `status` and `guide` show every finding as a
    notice with its next step. The stack's answer is data: an issuer that is not a URL is a finding,
    never a crash, it is quoted and shortened before it is shown, and a command is suggested only
    when its binding renders that issuer byte for byte, the host's case kept.

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
from dataclasses import dataclass, field, replace
from typing import Any, Literal
from urllib.parse import urlsplit

from connx_mcp import CLI_NAME, settings
from connx_mcp.demo import stack
from connx_mcp.demo.stack import DemoSettings
from connx_mcp.environment import Environment
from connx_mcp.output import shell_argument
from connx_mcp.probe import EndpointProbe

WELL_KNOWN = "/.well-known/openid-configuration"
VAULT_HEALTH = "/v1/sys/health"
# Vault's health answers for an initialised, unsealed node: active, standby, DR secondary, performance standby.
_VAULT_UP = frozenset({200, 429, 472, 473})
# The read token upstream's README publishes; a stack started with its defaults accepts it.
DEFAULT_READ_TOKEN = DemoSettings().vault_read_token
# How much of a remote stack's text a notice shows.
_SHOWN = 200

Provider = Literal["keycloak", "dex"]
_PROVIDERS: dict[str, Provider] = {"keycloak": "keycloak", "generic-oidc": "dex"}


def stack_settings(env: Environment) -> DemoSettings | None:
    """Return the remote demo stack an environment is bound to, as demo settings; None when it has none.

    Args:
        env (Environment): The environment.

    Returns:
        DemoSettings | None: The stack's name, ports and every service, bound to the environment's public URL.
    """
    host = env.host.demo_stack_host
    if not host:
        return None
    ports = settings.stack_ports(env.host.demo_stack_ports)
    return DemoSettings(
        services=stack.SERVICES,
        stack_host=host,
        keycloak_port=ports["keycloak"],
        dex_port=ports["dex"],
        vault_port=ports["vault"],
        public_base_url=stack.public_url(env),
        bound_environment=env.name,
    )


def discovery_url(env: Environment) -> str | None:
    """Return the discovery document of the issuer the server checks tokens against; None without OAuth.

    Args:
        env (Environment): The environment.

    Returns:
        str | None: The Keycloak realm's or the generic-OIDC provider's discovery URL.
    """
    provider = provider_of(env)
    if provider == "keycloak" and (realm := env.server.get("MCP_OAUTH_REALM_URL")):
        return realm.rstrip("/") + WELL_KNOWN
    if provider == "dex" and (config := env.server.get("MCP_OAUTH_CONFIG_URL")):
        return config
    return None


def vault_health_url(env: Environment) -> str | None:
    """Return Vault's health URL when the environment uses Vault; None otherwise.

    Args:
        env (Environment): The environment.

    Returns:
        str | None: `<VAULT_ADDR>/v1/sys/health`.
    """
    address = env.server.get("VAULT_ADDR")
    return address.rstrip("/") + VAULT_HEALTH if address and settings.vault_on(env.server) else None


def describe(env: Environment) -> dict[str, Any] | None:
    """Return the remote binding for a status report; None when the environment has none.

    Args:
        env (Environment): The environment.

    Returns:
        dict[str, Any] | None: The stack host, its ports and the URLs `check` asks.
    """
    demo = stack_settings(env)
    if demo is None:
        return None
    return {
        "stack_host": demo.stack_host,
        "ports": {"keycloak": demo.keycloak_port, "dex": demo.dex_port, "vault": demo.vault_port},
        "discovery": discovery_url(env),
        "vault_health": vault_health_url(env),
    }


@dataclass
class Check:
    """What asking the remote stack from this machine found.

    Attributes:
        notices (list[str]): Each finding with its next step.
        other_issuer (str | None): The issuer the discovery document names when it is not the one rendered,
            quoted and shortened for a terminal.
        next_command (str | None): The configure command that names the stack as its issuer does; None when
            no stack name renders that issuer.
    """

    notices: list[str] = field(default_factory=list)
    other_issuer: str | None = None
    next_command: str | None = None


def provider_of(env: Environment) -> Provider | None:
    """Return the demo identity provider an environment's OAuth settings name; None for neither.

    Args:
        env (Environment): The environment.

    Returns:
        Provider | None: keycloak for Keycloak, dex for generic OIDC.
    """
    return _PROVIDERS.get(env.server.get("MCP_OAUTH_PROVIDER", "").strip().lower())


def check(env: Environment, probe: EndpointProbe) -> Check:
    """Ask the environment's remote demo stack from this machine: name, discovery document and Vault health.

    The server's container must reach the same name; this machine's answer is the closest check of it.
    Vault's certificate is verified against the CA copy the environment mounts, as the server verifies it.

    Args:
        env (Environment): The environment, bound to a remote stack.
        probe (EndpointProbe): The endpoint probe.

    Returns:
        Check: The findings; none when the stack answers as rendered.
    """
    demo = stack_settings(env)
    if demo is None:
        return Check()
    host = demo.stack_host
    if not probe.resolves(host):
        return Check([unresolved_notice(env, demo)])
    found = Check()
    if (url := discovery_url(env)) is not None:
        _check_discovery(env, demo, probe, url, found)
    if (url := vault_health_url(env)) is not None:
        ca_file = env.host.vault_ca_file
        reply = probe.health(url, ca_file=ca_file)
        if reply.certificate_refused:
            found.notices.append(
                f"The demo stack's Vault at {url} serves a certificate the CA copy {ca_file} does not verify "
                f"({_shown(reply.error or 'refused')}), so the server would refuse it too. Copy the stack's "
                f"generated/tls/ca.pem again (its certificate must name {host}): {CLI_NAME} configure {env.name} "
                "--vault-ca <copy of the stack's ca.pem>"
            )
        elif reply.status is None:
            found.notices.append(_unreachable(env, host, url, reply.error))
        elif reply.status not in _VAULT_UP:
            found.notices.append(
                f"The demo stack's Vault answers {reply.status} at {url}: it is sealed or not initialised, so the "
                f"server cannot read the callers' CONNX logins. Restart the stack on {host}, then run "
                f"{CLI_NAME} status {env.name}."
            )
    return found


def _check_discovery(env: Environment, demo: DemoSettings, probe: EndpointProbe, url: str, found: Check) -> None:
    """Ask the issuer's discovery document; record an unreachable stack or another issuer in `found`."""
    expected = url.removesuffix(WELL_KNOWN)
    reply = probe.health(url)
    if reply.status is None:
        found.notices.append(_unreachable(env, demo.stack_host, url, reply.error))
        return
    if reply.status != 200:
        found.notices.append(
            f"The demo stack's discovery document {url} answers {reply.status}: the identity provider is not "
            f"serving on {demo.stack_host}. Start it there, then run {CLI_NAME} status {env.name}."
        )
        return
    try:
        issuer = json.loads(reply.body).get("issuer")
    except json.JSONDecodeError, AttributeError:
        issuer = None
    if not isinstance(issuer, str):
        found.notices.append(
            f"The demo stack's discovery document {url} names no issuer, so whether the server would accept its "
            f"tokens is unknown. Check that the identity provider on {demo.stack_host} serves it, then run "
            f"{CLI_NAME} status {env.name}."
        )
        return
    if issuer.rstrip("/") == expected.rstrip("/"):
        return
    found.other_issuer = shown = _shown(issuer)
    found.next_command = _suggestion(env, demo, issuer)
    if found.next_command is None:
        found.notices.append(
            f"The demo stack's discovery document {url} names its issuer {shown}, which no stack name renders, "
            f"so environment {env.name!r} would refuse every sign-in. Start the stack on {demo.stack_host} with "
            "STACK_HOST set to the name this machine and the server reach it by, then run "
            f"{CLI_NAME} status {env.name}."
        )
        return
    found.notices.append(
        f"The demo stack's discovery document {url} names its issuer {shown}, but environment {env.name!r} would "
        f"check tokens against {expected}: every sign-in would be refused. Give the stack's name exactly as it "
        f"was started with (its STACK_HOST): {found.next_command}"
    )


def _shown(text: str) -> str:
    """Return text from the remote stack quoted for a terminal: control characters escaped, its length capped."""
    return repr(text[:_SHOWN]) + ("..." if len(text) > _SHOWN else "")


def _issuer_name(issuer: str) -> tuple[str, int] | None:
    """Return the host, exactly as written, and the port of an http issuer URL; None when it has neither."""
    try:
        parts = urlsplit(issuer)
        port = parts.port
    except ValueError:
        return None
    if parts.scheme != "http" or port is None:
        return None
    # The netloc keeps the host's case; `hostname` lowercases it, and the server compares issuers byte for byte.
    host = parts.netloc.rpartition(":")[0]
    return (host, port) if host else None


def _suggestion(env: Environment, demo: DemoSettings, issuer: str) -> str | None:
    """Return the configure command whose binding renders `issuer` byte for byte; None when no binding does.

    The command keeps the binding's Vault, its CA copy and every port, so following it passes the check.
    """
    provider = provider_of(env)
    if provider is None or (named := _issuer_name(issuer)) is None:
        return None
    host, port = named
    if settings.set_host_setting(settings.HostSettings(), "demo_stack_host", host) is not None:
        return None  # not a name --stack-host takes (user info, a shell word, loopback)
    ports = {"keycloak": demo.keycloak_port, "dex": demo.dex_port, "vault": demo.vault_port, provider: port}
    candidate = replace(demo, stack_host=host, keycloak_port=ports["keycloak"], dex_port=ports["dex"])
    if (candidate.keycloak_issuer if provider == "keycloak" else candidate.dex_issuer) != issuer.rstrip("/"):
        return None
    return configure_command(env, provider, host, ports)


def configure_command(env: Environment, provider: Provider, host: str, ports: dict[str, int]) -> str:
    """Return the configure command that binds an environment to a remote stack as it is bound now.

    Args:
        env (Environment): The environment; its Vault binding and CA copy are kept.
        provider (Provider): The identity provider.
        host (str): The stack host to name.
        ports (dict[str, int]): The stack's port per service; named in full when any is not upstream's.

    Returns:
        str: The command.
    """
    demo = stack_settings(env)
    vault = (
        demo is not None and vault_health_url(env) is not None and env.server.get("VAULT_ADDR") == demo.vault_address
    )
    command = f"{CLI_NAME} configure {env.name} --demo {provider}"
    command += " --demo-vault" if vault else ""
    command += f" --stack-host {host}"
    if ports != settings.DEMO_STACK_PORTS:
        command += " --stack-port " + ",".join(f"{service}={ports[service]}" for service in settings.DEMO_STACK_PORTS)
    if vault and env.host.vault_ca_file:
        command += f" --vault-ca {shell_argument(env.host.vault_ca_file)}"
    return command


def unresolved_notice(env: Environment, demo: DemoSettings) -> str:
    """Return the notice for a stack host this machine cannot resolve.

    Args:
        env (Environment): The environment bound to the stack.
        demo (DemoSettings): The remote stack.

    Returns:
        str: The notice and its next step.
    """
    ports = {"keycloak": demo.keycloak_port, "dex": demo.dex_port, "vault": demo.vault_port}
    command = configure_command(env, provider_of(env) or "keycloak", "<name>", ports)
    return (
        f"{demo.stack_host} does not resolve on this machine, so neither this machine nor the server's container "
        "can reach the demo stack. Give the stack machine a DNS name both resolve (the server's container resolves "
        "names through Docker's DNS and may not see this machine's hosts file), or bind with the name they already "
        f"resolve: {command}"
    )


def _unreachable(env: Environment, host: str, url: str, error: str | None) -> str:
    """Return the notice for a stack URL that does not answer from this machine."""
    port = urlsplit(url).port
    return (
        f"The demo stack does not answer at {url} from this machine ({error or 'no answer'}). Check that the stack "
        f"runs on {host} and that its firewall lets this machine and the server reach port {port}; then run "
        f"{CLI_NAME} status {env.name}."
    )


def exposure_warnings(env: Environment) -> list[str]:
    """Return what a remote demo stack exposes: always its network reach, and the default read token if stored.

    Args:
        env (Environment): The environment, bound to a remote stack.

    Returns:
        list[str]: The warnings; empty without a remote binding.
    """
    demo = stack_settings(env)
    if demo is None:
        return []
    token = default_token_warning(env)
    return [network_warning(demo), *([token] if token is not None else [])]


def network_warning(demo: DemoSettings) -> str:
    """Return the warning that the network reaches a remote demo stack and every default it publishes.

    Args:
        demo (DemoSettings): The remote stack.

    Returns:
        str: The warning, naming the defaults to change and the ports to firewall.
    """
    return (
        f"The demo stack on {demo.stack_host} is reached over the network, and so is every default its README "
        "publishes: the Keycloak admin login, the Vault tokens and the demo users' passwords. On the stack "
        "machine, set KEYCLOAK_ADMIN_PASSWORD, VAULT_ROOT_TOKEN and VAULT_READ_TOKEN in test-stack.env to values "
        f"of your own, and firewall ports {demo.keycloak_port}, {demo.dex_port} and {demo.vault_port} to the "
        "machines that need them. The demo stack is for demonstration use only, not production certified."
    )


def default_token_warning(env: Environment) -> str | None:
    """Return the warning for the stack's published default Vault read token stored as VAULT_TOKEN.

    Args:
        env (Environment): The environment.

    Returns:
        str | None: The warning and how to give the stack's own token; None without a remote binding or
            with another token.
    """
    demo = stack_settings(env)
    if demo is None or not settings.vault_on(env.server) or env.server.get("VAULT_TOKEN") != DEFAULT_READ_TOKEN:
        return None
    return (
        f"VAULT_TOKEN is the demo stack's published default read token (its VAULT_READ_TOKEN default): anyone "
        f"who reaches {demo.stack_host}:{demo.vault_port} reads the demo users' CONNX logins with it. Set your "
        "own VAULT_READ_TOKEN on the stack, then give it here without the command line: printf '%s' \"$TOKEN\" "
        f"| {CLI_NAME} configure {env.name} --set VAULT_TOKEN=-, or --ask-at-start VAULT_TOKEN."
    )
