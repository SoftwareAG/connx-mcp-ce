#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""stack.py - The demo stack: Keycloak, dex and HashiCorp Vault for trying OAuth and Vault logins.

Description:
    The orchestration of the server team's upstream demo stack (`startContainers.sh`, `stopContainers.sh`,
    `settings.sh`), ported so no bash or Python is needed on the host. The compose file,
    templates, seed and TLS scripts, seed Dockerfile and renderer are embedded verbatim in
    `assets/` and copied into the stack directory before every compose call; rendering and TLS
    generation run inside the seed container.

    State lives in `<base directory>/.demo/`: a leading dot is outside the environment name
    pattern, so it can never clash with an environment. It holds `demo.json` (the chosen services,
    the users file, the stack settings and the binding), the default `users.yaml`, `stack.env`
    (compose's variables) and `stack/` (assets and `generated/`). The compose project is
    `connx-mcp-ce_demo`: environments are `connx-mcp-ce-<name>`, so the underscore keeps them apart.

    One stack per machine, bound to at most one environment (`bound_environment`, set by
    `configure-demo --for` or `configure <env> --demo`), whose public URL is baked into the realm.
    Every issuer and Vault's address name `stack_host`, `host.docker.internal` by default:
    the server container reaches it through its `host-gateway` entry, and the
    browser and harness on this machine through the same name. Demo only, never production.

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
import shutil
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field, fields, replace
from pathlib import Path
from typing import Any, Literal

import typer

from connx_mcp import CLI_NAME, compose, environment
from connx_mcp.demo import users
from connx_mcp.docker import DockerPort, DockerResult, query
from connx_mcp.environment import (
    Environment,
    EnvironmentNotFound,
    base_directory,
    private_directory,
    restrict_to_owner,
    write_private,
)
from connx_mcp.output import CliError, ExitCode, Output
from connx_mcp.settings import HOST_NAME, LOOPBACK_HOSTS, serves_self_signed, tls_on

SERVICES = ("keycloak", "dex", "vault")
PROJECT = f"{CLI_NAME}_demo"
DEMO_DIRECTORY = ".demo"
WAIT_SECONDS = 420
# The name the server container and this machine both reach the stack by.
DEFAULT_STACK_HOST = "host.docker.internal"
# The demo stack's own Vault variables, which its compose file reads; the server does not.
VAULT_PORT_VARIABLE = "VAULT_PORT"
VAULT_ROOT_TOKEN_VARIABLE = "VAULT_ROOT_TOKEN"
VAULT_READ_TOKEN_VARIABLE = "VAULT_READ_TOKEN"
LIMITS = (
    "The demo stack is for demonstration use only, not production certified. A machine has one demo stack, "
    "bound to one environment. The stack's credentials are weak and fixed. Its Vault keeps data in memory only, "
    "so after a restart it holds only the entries from the users file."
)
CONFIGURE_NEXT = f"{CLI_NAME} configure-demo --keycloak --vault"
SEED_IMAGE = f"{PROJECT}-seed:local"  # the compose file's `image:` for the seed; `down --rmi local` skips it
# The OAuth clients the embedded realm and dex templates register (fixed there, not settings).
KEYCLOAK_CLIENT = ("connx-mcp-generic", "connx-mcp-generic-secret")
DEX_CLIENT = ("connx-mcp", "connx-mcp-secret")
# The MCP path the embedded realm template's audience mapper adds to the public URL (fixed there, not a setting).
KEYCLOAK_MCP_PATH = "/mcp"
_ENV_REFERENCE = re.compile(r"[a-z]+_env:\s*([A-Za-z_][A-Za-z0-9_]*)")


def demo_directory() -> Path:
    """Return the demo stack's state directory under the base directory."""
    return base_directory() / DEMO_DIRECTORY


@dataclass(frozen=True)
class DemoSettings:
    """The demo stack's settings (upstream `test-stack.env`), its services, users file and binding.

    Attributes:
        services (tuple[str, ...]): The chosen services, in SERVICES order.
        users_file (str): The users file's absolute path.
        stack_host (str): The name the server, browsers and clients reach the stack by; in every issuer.
        keycloak_port (int): Keycloak's published port.
        dex_port (int): dex's published port.
        vault_port (int): Vault's published port.
        public_base_url (str): The bound server's public URL: token audience and redirect target.
        keycloak_admin_password (str): The Keycloak admin console password (user admin).
        vault_root_token (str): Vault's root token, used by the seed only.
        vault_read_token (str): The read-only token the server uses.
        bound_environment (str | None): The environment bound to the stack, if any.
    """

    services: tuple[str, ...] = ()
    users_file: str = ""
    stack_host: str = DEFAULT_STACK_HOST
    keycloak_port: int = 8080
    dex_port: int = 5556
    vault_port: int = 8200
    public_base_url: str = "http://localhost:8000"
    keycloak_admin_password: str = field(default="admin", repr=False)
    vault_root_token: str = field(default="connx-mcp-dev-root", repr=False)
    vault_read_token: str = field(default="connx-mcp-dev-read", repr=False)
    bound_environment: str | None = None

    @property
    def keycloak_issuer(self) -> str:
        """The Keycloak realm's issuer."""
        return f"http://{self.stack_host}:{self.keycloak_port}/realms/connx-mcp"

    @property
    def dex_issuer(self) -> str:
        """The issuer of dex."""
        return f"http://{self.stack_host}:{self.dex_port}/dex"

    @property
    def vault_address(self) -> str:
        """Vault's TLS address."""
        return f"https://{self.stack_host}:{self.vault_port}"

    @property
    def mcp_url(self) -> str:
        """The public base URL without a trailing slash, as the renderer uses it."""
        return self.public_base_url.rstrip("/")

    @property
    def keycloak_client_id(self) -> str:
        """The realm's generic-OIDC (proxy mode) client id."""
        return KEYCLOAK_CLIENT[0]

    @property
    def keycloak_client_secret(self) -> str:
        """The realm's generic-OIDC (proxy mode) client secret."""
        return KEYCLOAK_CLIENT[1]

    @property
    def keycloak_audience(self) -> str:
        """The audience Keycloak's tokens carry: the bound server's MCP resource URI."""
        return f"{self.mcp_url}{KEYCLOAK_MCP_PATH}"

    @property
    def dex_client_id(self) -> str:
        """The client id dex registers for the server; also its tokens' audience."""
        return DEX_CLIENT[0]

    @property
    def dex_client_secret(self) -> str:
        """The client secret dex registers for the server."""
        return DEX_CLIENT[1]

    def endpoints(self) -> dict[str, str]:
        """Return each chosen service's public URL."""
        urls = {"keycloak": self.keycloak_issuer, "dex": self.dex_issuer, "vault": self.vault_address}
        return {service: urls[service] for service in self.services}

    def server_settings(self, provider: Literal["keycloak", "dex"], vault: bool) -> dict[str, str]:
        """Return the server settings that point a server at this stack, as upstream `mcp-server.env` names them.

        Keycloak runs in resource-server mode, dex as generic OIDC in proxy mode. The proxy mode's
        storage directory is the caller's choice and not included.

        Args:
            provider (Literal["keycloak", "dex"]): The identity provider the server signs in with.
            vault (bool): Also the Credential Vault settings. VAULT_CACERT is not among them: the
                environment's `vault_ca_file` host setting names vault_ca_path(), and `start` mounts it
                and points VAULT_CACERT at it (identity.adjust_spec).

        Returns:
            dict[str, str]: Server setting name to value.
        """
        if provider == "keycloak":
            values = {
                "MCP_OAUTH_PROVIDER": "keycloak",
                "MCP_PUBLIC_BASE_URL": self.mcp_url,
                "MCP_OAUTH_REALM_URL": self.keycloak_issuer,
                "MCP_OAUTH_AUDIENCE": self.keycloak_audience,
            }
        else:
            values = {
                "MCP_OAUTH_PROVIDER": "generic-oidc",
                "MCP_PUBLIC_BASE_URL": self.mcp_url,
                "MCP_OAUTH_CONFIG_URL": f"{self.dex_issuer}/.well-known/openid-configuration",
                "MCP_OAUTH_CLIENT_ID": self.dex_client_id,
                "MCP_OAUTH_CLIENT_SECRET": self.dex_client_secret,
                "MCP_OAUTH_AUDIENCE": self.dex_client_id,
            }
        if vault:
            values |= {
                "MCP_CREDENTIAL_PROVIDER": "hashicorp",
                "VAULT_ADDR": self.vault_address,
                "VAULT_TOKEN": self.vault_read_token,
            }
        return values

    def bind(self, environment: str, public_base_url: str, rebind: bool = False) -> DemoSettings:
        """Return these settings bound to an environment, the realm to be rendered for its public URL.

        Args:
            environment (str): The environment's name.
            public_base_url (str): Its public base URL.
            rebind (bool): Take the binding from another environment.

        Returns:
            DemoSettings: The bound settings; not saved.

        Raises:
            CliError: Another environment is bound and `rebind` is off, or the stack host is loopback.
        """
        if self.bound_environment not in (None, environment) and not rebind:
            raise CliError(
                ExitCode.DEMO_BOUND_ELSEWHERE,
                f"The demo stack is bound to environment {self.bound_environment!r}: one stack serves one "
                f"environment, and binding {environment!r} breaks sign-in on {self.bound_environment!r}. Rebind "
                "the stack, then repeat this command.",
                f"{CLI_NAME} configure-demo --for {environment} --rebind",
            )
        if self.stack_host in LOOPBACK_HOSTS:
            raise CliError(
                ExitCode.USAGE,
                f"The demo stack is reached as {self.stack_host}, which inside the server's container is the "
                f"container itself. Use {DEFAULT_STACK_HOST}, which this machine and the container both reach.",
                f"{CLI_NAME} configure-demo --stack-host {DEFAULT_STACK_HOST}",
            )
        return replace(self, bound_environment=environment, public_base_url=public_base_url.rstrip("/"))

    def unbind(self) -> DemoSettings:
        """Return these settings bound to no environment; not saved."""
        return replace(self, bound_environment=None)


def settings_path() -> Path:
    """Return the demo settings file."""
    return demo_directory() / "demo.json"


def stack_directory() -> Path:
    """Return the directory compose runs the stack from."""
    return demo_directory() / "stack"


def vault_ca_path() -> Path:
    """Return the host path of the demo CA that signs Vault's certificate."""
    return stack_directory() / "generated" / "tls" / "ca.pem"


def load_settings() -> DemoSettings | None:
    """Return the saved demo settings, or None when the demo stack was never configured.

    Returns:
        DemoSettings | None: The settings, unknown keys ignored.

    Raises:
        CliError: The settings file cannot be read or is not valid JSON.
    """
    path = settings_path()
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise CliError(ExitCode.FILE_UNREADABLE, f"{path} is not valid JSON: {error}", CONFIGURE_NEXT) from error
    except OSError as error:
        raise CliError(ExitCode.FILE_UNREADABLE, f"{path} cannot be read: {error.strerror}", CONFIGURE_NEXT) from error
    known = {f.name for f in fields(DemoSettings)}
    values: dict[str, Any] = {key: value for key, value in data.items() if key in known}
    values["services"] = tuple(s for s in SERVICES if s in values.get("services", ()))
    return DemoSettings(**values)


def save_settings(settings: DemoSettings) -> None:
    """Write the demo settings, owner-only (they hold the stack's admin password and tokens).

    Args:
        settings (DemoSettings): The settings.
    """
    path = settings_path()
    private_directory(path.parent)
    data = asdict(settings)
    data["services"] = list(settings.services)
    write_private(path, json.dumps(data, indent=2) + "\n")


def require_settings() -> DemoSettings:
    """Return the demo settings.

    Returns:
        DemoSettings: The settings, with at least one service chosen.

    Raises:
        CliError: The demo stack is not configured.
    """
    settings = load_settings()
    if settings is None or not settings.services:
        raise CliError(ExitCode.DEMO_NOT_CONFIGURED, "The demo stack is not configured yet.", CONFIGURE_NEXT)
    return settings


# configure-demo --------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class DemoOptions:
    """What `configure-demo` asks for besides the services and the users file.

    Attributes:
        stack_host (str | None): The name to reach the stack by; None keeps the current one.
        for_environment (str | None): Bind the stack to this environment and render the realm for its URL.
        rebind (bool): Take the binding from another environment.
        resolves (Callable[[str], bool]): Whether a host name resolves on this machine.
    """

    stack_host: str | None = None
    for_environment: str | None = None
    rebind: bool = False
    resolves: Callable[[str], bool] = field(default=lambda host: True, compare=False)


def public_url(env: Environment) -> str:
    """Return an environment's public base URL: its MCP_PUBLIC_BASE_URL, else localhost on its published port.

    Args:
        env (Environment): The environment.

    Returns:
        str: The URL without a trailing slash; https when its server settings turn TLS on.
    """
    if given := env.server.get("MCP_PUBLIC_BASE_URL"):
        return given.rstrip("/")
    return local_url(env)


def local_url(env: Environment) -> str:
    """Return the URL a public URL defaults to: localhost on the published port, https when TLS is on.

    Args:
        env (Environment): The environment.

    Returns:
        str: `http(s)://localhost:<published port>`.
    """
    return f"{'https' if tls_on(env.server) else 'http'}://localhost:{compose.published_port(env)}"


def self_signed_refusal(env: Environment) -> str | None:
    """Return why an environment cannot be bound: TLS without a provided CA-issued certificate; None when it can.

    Args:
        env (Environment): The environment.

    Returns:
        str | None: The refusal.
    """
    if not tls_on(env.server) or serves_self_signed(env.server, env.host) is False:
        return None
    return (
        f"Environment {env.name!r} uses the demo stack, so it runs on plain-http localhost or over TLS with "
        "a provided CA-issued certificate: the server refuses OAuth with a self-signed one, and generates only "
        "self-signed ones. Set MCP_TLS_ENABLED=false, or give a CA-issued pair with --certs <directory>."
    )


def unresolved_note(host: str, resolves: Callable[[str], bool]) -> str | None:
    """Return how to make the stack host resolve on this machine, or None when it does.

    Args:
        host (str): The stack host.
        resolves (Callable[[str], bool]): Whether a host name resolves on this machine.

    Returns:
        str | None: The note, naming the hosts-file line to add.
    """
    if resolves(host):
        return None
    hosts = r"C:\Windows\System32\drivers\etc\hosts" if os.name == "nt" else "/etc/hosts"
    return (
        f"{host} does not resolve on this machine, so the browser and the harness cannot reach the demo "
        f"sign-in. Add the line `127.0.0.1 {host}` to {hosts}. Docker Desktop can add it itself; Rancher Desktop "
        "and Docker Engine do not. Or run the stack on a machine with a stable name and bind it with "
        "--stack-host <name>."
    )


def configure(
    out: Output, choices: dict[str, bool | None], users_file: Path | None, options: DemoOptions | None = None
) -> None:
    """Choose the demo services and the users file; seed the default users file on first use.

    Args:
        out (Output): Where the result goes.
        choices (dict[str, bool | None]): Per service, True to add, False to drop, None to keep.
        users_file (Path | None): Another users file to use; None keeps the current or default one.
        options (DemoOptions | None): The stack host and the environment to bind.

    Raises:
        CliError: No service chosen, the users file is missing or invalid, the stack host is not a
            host name, or the environment cannot run bound (bind.refusals) or another one is bound.
        EnvironmentNotFound: The environment to bind does not exist.
    """
    options = options or DemoOptions()
    current = load_settings() or DemoSettings()
    if options.stack_host is not None:
        if not HOST_NAME.fullmatch(options.stack_host):
            raise CliError(ExitCode.USAGE, f"--stack-host {options.stack_host!r} is not a host name or address.")
        current = replace(current, stack_host=options.stack_host)
    env: Environment | None = None
    if options.for_environment is not None:
        env = environment.load(environment.resolve_name(options.for_environment, "configure-demo"))
        if not env.exists:
            raise EnvironmentNotFound(env)
        if far := env.host.demo_stack_host:
            raise CliError(
                ExitCode.USAGE,
                f"Environment {env.name!r} uses the demo stack on {far}; binding the local stack to it would leave "
                "its settings pointing there. End the remote binding and bind the local stack in one step.",
                f"{CLI_NAME} configure {env.name} --stack-host '' --demo keycloak",
            )
        bound = current.bind(env.name, public_url(env), options.rebind)
        # bind imports this module, so the one refusal-to-fix mapping is reached at call time.
        from connx_mcp.demo import bind

        if refused := bind.refusals(env, bind.Binding(bound)):
            first = refused[0]
            raise CliError(first.exit_code, first.message, first.next_command)
        current = bound
    if not current.services and all(choice is None for choice in choices.values()):
        raise CliError(
            ExitCode.INPUT_REQUIRED,
            "Choose the demo services with --keycloak, --dex and/or --vault.",
            CONFIGURE_NEXT,
        )
    chosen = tuple(s for s in SERVICES if (choices.get(s) if choices.get(s) is not None else s in current.services))
    if not chosen:
        raise CliError(
            ExitCode.USAGE, "Choose at least one demo service: --keycloak, --dex or --vault.", CONFIGURE_NEXT
        )

    created = False
    if users_file is not None:
        path = users_file.expanduser().resolve()
        summary = users.validate(path)
        restrict_to_owner(path)
    else:
        path = Path(current.users_file) if current.users_file else demo_directory() / "users.yaml"
        created = users.seed_users_file(path) if path == demo_directory() / "users.yaml" else False
        summary = users.validate(path)

    settings = replace(current, services=chosen, users_file=str(path))
    save_settings(settings)

    next_step = f"{CLI_NAME} start-demo"
    note = ""
    if summary.mapped == 0:
        note = (
            f"\nNo user has a CONNX login yet: edit {path}, uncomment a user's `connx:` line and name a login "
            "from your CDD before starting the demo."
        )
    if env is not None:
        provider = "keycloak" if "keycloak" in chosen else "dex"
        note += (
            f"\nBound to environment {env.name!r}: start-demo renders the realm for {settings.mcp_url}. Point "
            f"the environment at the stack with: {CLI_NAME} configure {env.name} --demo {provider}"
            f"{' --demo-vault' if 'vault' in chosen else ''}"
        )
        if unresolved := unresolved_note(settings.stack_host, options.resolves):
            note += f"\nWarning: {unresolved}"
    out.result(
        {
            "services": list(chosen),
            "users_file": str(path),
            "users_file_created": created,
            "identities": summary.identities,
            "mapped": summary.mapped,
            "stack_host": settings.stack_host,
            "bound_environment": settings.bound_environment,
            "public_base_url": settings.mcp_url,
            "next": next_step,
        },
        f"Demo stack configured: {', '.join(chosen)}.\n"
        f"Users file: {path}{' (created with alice, bob, carol and dave)' if created else ''}; "
        f"{summary.identities} identities, {summary.mapped} with a CONNX login.{note}\n"
        f"{LIMITS}\nNext: {next_step}",
    )


# start-demo ------------------------------------------------------------------------------------------------------


def _compose_base(stack: Path) -> list[str]:
    """Return `docker compose` with the demo project, its directory, file and variables."""
    return [
        "compose",
        "--project-name",
        PROJECT,
        "--project-directory",
        str(stack),
        "--file",
        str(stack / "docker-compose.yml"),
        "--env-file",
        str(demo_directory() / "stack.env"),
    ]


def _compose_variables(settings: DemoSettings) -> dict[str, str]:
    """Return the variables the compose file interpolates."""
    return {
        "COMPOSE_PROJECT_NAME": PROJECT,
        "STACK_HOST": settings.stack_host,
        "KEYCLOAK_PORT": str(settings.keycloak_port),
        "DEX_PORT": str(settings.dex_port),
        VAULT_PORT_VARIABLE: str(settings.vault_port),
        "MCP_PUBLIC_BASE_URL": settings.mcp_url,
        "KEYCLOAK_ADMIN_PASSWORD": settings.keycloak_admin_password,
        VAULT_ROOT_TOKEN_VARIABLE: settings.vault_root_token,
        VAULT_READ_TOKEN_VARIABLE: settings.vault_read_token,
    }


def _prepare(settings: DemoSettings) -> Path:
    """Copy the embedded assets into the stack directory and write compose's variables.

    Args:
        settings (DemoSettings): The demo settings.

    Returns:
        Path: The stack directory.

    Raises:
        CliError: A setting holds a character the server's or compose's .env reader cannot keep.
    """
    variables = _compose_variables(settings)
    for name, value in variables.items():
        if "'" in value or "${" in value or "\n" in value:
            raise CliError(
                ExitCode.USAGE,
                f"The demo setting {name} may not contain ', ${{ or a line break: the .env readers cannot keep them.",
                f"Edit {settings_path()}",
            )
    stack = stack_directory()
    # The rendered files hold the demo users' CDD passwords: the demo directory, the stack and generated/ are
    # owner-only, so no other local user reaches them whatever modes the verbatim renderer sets.
    private_directory(stack / "generated")
    restrict_to_owner(stack)
    restrict_to_owner(demo_directory())
    for source in users.ASSETS.rglob("*"):
        if source.is_file() and source.name != "UPSTREAM-ORCHESTRATION.sha256" and "__pycache__" not in source.parts:
            target = stack / source.relative_to(users.ASSETS)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(source.read_bytes())
    # Docker Desktop cannot create a missing bind-mount source, and the seed mounts both.
    (stack / "generated" / "tls").mkdir(parents=True, exist_ok=True)
    (stack / "generated" / "vault").mkdir(parents=True, exist_ok=True)
    write_private(demo_directory() / "stack.env", "".join(f"{name}='{value}'\n" for name, value in variables.items()))
    return stack


def _compose(docker: DockerPort, stack: Path, args: Sequence[str]) -> DockerResult:
    """Run one compose command against the demo project; `ps` gives up on a daemon that does not answer."""
    command = [*_compose_base(stack), *args]
    return query(docker, command) if args[:1] == ["ps"] else docker.run(command)


def _require(result: DockerResult, what: str, extra: str = "") -> None:
    """Fail with Docker's own output when a compose command failed.

    Args:
        result (DockerResult): The command's result.
        what (str): What was being done, for the message.
        extra (str): More output worth showing, such as `docker compose ps`.

    Raises:
        CliError: The command failed.
    """
    if result.returncode != 0:
        said = "\n".join(part.strip() for part in (result.stderr, result.stdout, extra) if part.strip())
        raise CliError(
            ExitCode.DEMO_FAILED,
            f"The demo stack could not {what}:\n{said}",
            f"docker compose --project-name {PROJECT} logs <service>",
        )


def _progress(out: Output, text: str) -> None:
    """Tell a person what is happening during a long step; JSON output stays one object."""
    if not out.json_mode:
        typer.echo(text, err=True)


def _host_path(path: Path) -> str:
    """Return a host path as Docker Desktop and Engine both take it in a bind mount."""
    return path.as_posix()


def start(out: Output, docker: DockerPort) -> None:
    """Start the chosen demo services and wait until they are healthy.

    Args:
        out (Output): Where the result goes.
        docker (DockerPort): The Docker port.

    Not configured, a users file that is invalid or maps nobody, and a failed container each end
    in a CliError from the helpers it calls.
    """
    settings = require_settings()
    users_file = Path(settings.users_file)
    summary = users.validate(users_file)
    users.require_mapped(users_file, summary)
    stack = _prepare(settings)
    want_vault = "vault" in settings.services

    _progress(out, "Building the demo seed image...")
    _require(_compose(docker, stack, ["build", "-q", "seed"]), "build the seed image")

    as_owner: list[str] = []
    tls_owner: list[str] = []
    if hasattr(os, "getuid") and hasattr(os, "getgid"):
        uid, gid = os.getuid(), os.getgid()
        as_owner = ["--user", f"{uid}:{gid}"]
        tls_owner = ["-e", f"HOST_UID={uid}", "-e", f"HOST_GID={gid}"]
    secret_vars = sorted(set(_ENV_REFERENCE.findall(users_file.read_text(encoding="utf-8"))))
    env_args = [
        "-e",
        f"STACK_HOST={settings.stack_host}",
        "-e",
        f"KEYCLOAK_PORT={settings.keycloak_port}",
        "-e",
        f"DEX_PORT={settings.dex_port}",
        "-e",
        f"MCP_PUBLIC_BASE_URL={settings.mcp_url}",
        "-e",
        "PYTHONDONTWRITEBYTECODE=1",
    ]
    for name in secret_vars:  # by name only: Docker reads the value from this process's environment
        env_args += ["-e", name]
    mounted = f"/users/{users_file.name}"
    render = [
        "run",
        "--rm",
        "--no-deps",
        "-T",
        *as_owner,
        "--entrypoint",
        "python3",
        *env_args,
        "-v",
        f"{_host_path(stack)}:/stack:ro",
        "-v",
        f"{_host_path(stack / 'generated')}:/out",
        "-v",
        f"{_host_path(users_file)}:{mounted}:ro",
        "seed",
        "/stack/render.py",
        "--users",
        mounted,
        "--stack",
        "/stack",
        "--out",
        "/out",
    ]
    _progress(out, f"Rendering the realm, dex configuration and Vault entries from {users_file}...")
    _require(_compose(docker, stack, render), f"render its configuration from {users_file}")
    entries = stack / "generated" / "vault" / "entries.json"
    if entries.is_file():
        # render.py leaves the CDD passwords world-readable; the seed reads them as the container's root.
        restrict_to_owner(entries)

    if want_vault:
        tls = [
            "run",
            "--rm",
            "--no-deps",
            "-T",
            "--entrypoint",
            "/seed/make-tls.sh",
            *tls_owner,
            "-e",
            f"STACK_HOST={settings.stack_host}",
            "-v",
            f"{_host_path(stack / 'generated' / 'tls')}:/out",
            "seed",
        ]
        _require(_compose(docker, stack, tls), "create Vault's TLS certificate")

    services = [s for s in ("keycloak", "dex") if s in settings.services] + (["vault", "seed"] if want_vault else [])
    _progress(out, f"Starting {', '.join(services)} and waiting until healthy (Keycloak takes a minute or two)...")
    up = _compose(
        docker,
        stack,
        ["up", "-d", "--force-recreate", "--remove-orphans", "--wait", "--wait-timeout", str(WAIT_SECONDS), *services],
    )
    if up.returncode != 0:
        _require(up, "become healthy", _compose(docker, stack, ["ps"]).stdout)

    if not want_vault and _compose(docker, stack, ["ps", "-q", "seed"]).stdout.strip():
        # A seed left running from an earlier start writes the users file's new entries now.
        _require(_compose(docker, stack, ["restart", "seed"]), "restart the Vault seed")

    if "keycloak" in settings.services and settings.stack_host not in LOOPBACK_HOSTS:
        relax = _compose(
            docker,
            stack,
            [
                "exec",
                "-T",
                "keycloak",
                "/bin/sh",
                "-c",
                "/opt/keycloak/bin/kcadm.sh config credentials --server http://localhost:8080 --realm master "
                '--user admin --password "$KC_BOOTSTRAP_ADMIN_PASSWORD" >/dev/null && '
                "/opt/keycloak/bin/kcadm.sh update realms/master -s sslRequired=NONE",
            ],
        )
        if relax.returncode != 0:
            _progress(out, "Warning: could not relax the master realm's sslRequired; use the admin console locally.")

    endpoints = settings.endpoints()
    data: dict[str, Any] = {
        "services": list(settings.services),
        "endpoints": endpoints,
        "users_file": str(users_file),
        "identities": summary.identities,
        "mapped": summary.mapped,
        "bound_environment": settings.bound_environment,
    }
    lines = ["Demo stack up:"]
    if "keycloak" in endpoints:
        lines.append(
            f"  Keycloak  http://{settings.stack_host}:{settings.keycloak_port}  (realm {endpoints['keycloak']})"
        )
    if "dex" in endpoints:
        lines.append(f"  dex       {endpoints['dex']}  (sign in with a user's email)")
    if want_vault:
        data["vault_ca"] = str(vault_ca_path())
        lines.append(f"  Vault     {endpoints['vault']}  (CA {vault_ca_path()})")
    lines.append(
        f"Users from {users_file}: {summary.identities} identities, {summary.mapped} with a CONNX login "
        "(an OAuth password not given there is the name)."
    )
    lines.append(LIMITS)
    data["warnings"] = [network_warning(settings.services)]
    lines += [f"Warning: {w}" for w in data["warnings"]]
    out.result(data, "\n".join(lines))


def network_warning(services: Sequence[str]) -> str:
    """Return the warning that other computers reach the demo services (published on every interface, by decision).

    Args:
        services (Sequence[str]): The services started.

    Returns:
        str: What the network reaches, and that the stack belongs on a trusted machine only.
    """
    exposed = []
    if "vault" in services:
        exposed.append("Vault holds the demo users' CDD logins under a fixed root token")
    if "keycloak" in services:
        exposed.append("Keycloak's admin console signs in with admin/admin")
    reach = f" {'; '.join(exposed)}." if exposed else ""
    return (
        f"Other computers can reach the demo services.{reach} "
        "Run the demo stack only for a demonstration, on a trusted machine."
    )


# stop-demo -------------------------------------------------------------------------------------------------------


def stop(out: Output, docker: DockerPort, purge: bool) -> None:
    """Stop and remove the demo stack's services; with `purge`, also its generated files, CA, seed image and binding.

    Args:
        out (Output): Where the result goes.
        docker (DockerPort): The Docker port.
        purge (bool): Also remove generated files, the demo CA, the seed image and the binding.

    Not configured and a failed compose command each end in a CliError from the helpers it calls.
    """
    settings = require_settings()
    stack = _prepare(settings)
    _require(_compose(docker, stack, ["down", "--remove-orphans"]), "stop")
    if not purge:
        out.result({"purged": False}, "Demo stack stopped and removed; generated files and the demo CA kept.")
        return
    image_error = _remove_seed_image(docker)
    shutil.rmtree(stack)
    (demo_directory() / "stack.env").unlink(missing_ok=True)
    unbound = settings.bound_environment
    save_settings(settings.unbind())
    note = (
        f" Environment {unbound} is no longer bound to it; bind it again with: {CLI_NAME} configure-demo --for {unbound}."
        if unbound
        else ""
    )
    if image_error is None:
        removed = "Demo stack removed with its generated files, the demo CA and the seed image"
    else:
        removed = (
            f"Demo stack removed with its generated files and the demo CA; the seed image {SEED_IMAGE} was kept "
            f"({image_error}): remove it later with `docker image rm {SEED_IMAGE}`"
        )
    data: dict[str, Any] = {
        "purged": True,
        "unbound_environment": unbound,
        "seed_image_removed": image_error is None,
    }
    if image_error is not None:
        data["seed_image_error"] = image_error
    out.result(
        data,
        f"{removed}. start-demo creates a new demo CA at {vault_ca_path()}: restart the environments that use it."
        f"{note} "
        "Your users file is kept.",
    )


def _remove_seed_image(docker: DockerPort) -> str | None:
    """Remove the seed image by name; an image already gone counts as removed.

    Args:
        docker (DockerPort): The Docker port.

    Returns:
        str | None: None when the image is gone, otherwise Docker's reason it stays.
    """
    result = docker.run(["image", "rm", SEED_IMAGE])
    said = (result.stderr.strip() or result.stdout.strip()) or f"exit code {result.returncode}"
    if result.returncode == 0 or "no such image" in said.lower():
        return None
    return said
