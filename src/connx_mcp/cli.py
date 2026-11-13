#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""cli.py - The `connx-mcp-ce` command line: commands, layered help, `--json` and `--no-input`.

Description:
    A thin command layer: each command builds a Session (output, prompt rule, ports) and calls into
    the deep modules; a CliError anywhere becomes one error in human text or the JSON envelope, with
    its exit code. Daemon-bound commands check Docker before anything else. Tests inject fakes for
    the two ports through the Typer context object; a real run uses default_ports().

Copyright:
    Copyright 2026 Software GmbH
    Licensed under the Apache License, Version 2.0 (the "License"); you may not use this file except in compliance
    with the License. You may obtain a copy of the License at https://www.apache.org/licenses/LICENSE-2.0
    Unless required by applicable law or agreed to in writing, software distributed under the License is distributed
    on an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the License
    for the specific language governing permissions and limitations under the License.
"""

from __future__ import annotations

import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any

import typer
from typer._click.exceptions import ClickException, UsageError
from typer.core import TyperGroup

from connx_mcp import CLI_NAME, fleet, image, lifecycle, skill, version
from connx_mcp import guide as guidance
from connx_mcp import import_export as transfer
from connx_mcp import login as signin
from connx_mcp import remove as removal
from connx_mcp import update as upgrade
from connx_mcp.configure import Changes
from connx_mcp.configure import configure as run_configure
from connx_mcp.demo import bind
from connx_mcp.demo import stack as demo
from connx_mcp.docker import DockerInfo, check_docker
from connx_mcp.environment import DEFAULT_BASE
from connx_mcp.output import CliError, ExitCode, Output, exit_code_table, exit_codes
from connx_mcp.ports import Ports, Terminal, default_ports
from connx_mcp.start_inputs import StartInputs

ROOT_HELP = f"""Set up, run and maintain CONNX MCP Server Community Edition environments on this machine.

\b
First steps:
  1. {CLI_NAME} configure        create the default environment
  2. {CLI_NAME} login            sign in to the container registry
  3. {CLI_NAME} download         download the image from the registry
  4. {CLI_NAME} start            start it and wait until it is healthy
  5. {CLI_NAME} status           get the environment's state
  6. {CLI_NAME} install-skill    give your harness the connx-sql-expert skill

To find the next command, run `{CLI_NAME} guide`. Every command has its own --help.
"""

EPILOG = f"""Every command takes --json and --no-input. With --json, a command shows one JSON object: the result,
or an error with its name, exit code, message and next command. A command never asks with --json or --no-input,
or when its input does not come from a keyboard (for example a pipe, a file or a CI job). In these cases, a command
that needs an answer exits with exit code 3.

\b
Environment variables:
  CONNX_MCP_ENVIRONMENT    The environment a command uses when none is named.
  MCP_ENVIRONMENT_PATH     The base directory that holds every environment (default {DEFAULT_BASE}).
{image.VARIABLES_HELP}

Exit codes: `{CLI_NAME} exit-codes` lists each exit code and what it means.
"""

DOCKER_PREREQUISITE = (
    "Minimum setup: Docker Engine 24+ with Compose v2, Docker Desktop, or Rancher Desktop with the dockerd (moby) "
    "engine."
)


def _help(summary: str, examples: Sequence[str], prerequisites: Sequence[str]) -> str:
    """Compose a command's help: summary, examples and prerequisites."""
    lines = "\n".join(f"  {e}" for e in examples)
    needs = "\n".join(f"  {p}" for p in prerequisites)
    return f"{summary}\n\n\b\nExamples:\n{lines}\n\n\b\nPrerequisites:\n{needs}\n"


class _Group(TyperGroup):
    """The root group; with `--json`, usage errors also come out as one JSON object."""

    def main(
        self,
        args: Sequence[str] | None = None,
        prog_name: str | None = None,
        complete_var: str | None = None,
        standalone_mode: bool = True,
        windows_expand_args: bool = True,
        **extra: Any,
    ) -> Any:
        """Run the CLI, rendering usage errors in the JSON envelope when `--json` is on the command line."""
        argv = list(sys.argv[1:] if args is None else args)
        if "--json" not in argv:
            return super().main(argv, prog_name, complete_var, standalone_mode, windows_expand_args, **extra)
        try:
            code = super().main(argv, prog_name, complete_var, False, windows_expand_args, **extra)
        except UsageError as error:
            name = error.ctx.info_name if error.ctx is not None and error.ctx.parent is not None else None
            help_command = f"{CLI_NAME} {name} --help" if name else f"{CLI_NAME} --help"
            code = Output(True, name).error(CliError(ExitCode.USAGE, error.format_message(), help_command))
        except ClickException as error:
            code = Output(True, None).error(CliError(ExitCode.FAILURE, error.format_message()))
        except typer.Abort:
            code = Output(True, None).error(CliError(ExitCode.CANCELLED, "Cancelled; nothing was changed."))
        code = code if isinstance(code, int) else 0
        if standalone_mode:
            sys.exit(code)
        return code


app = typer.Typer(
    cls=_Group,
    name=CLI_NAME,
    help=ROOT_HELP,
    epilog=EPILOG,
    rich_markup_mode=None,
    add_completion=False,
    context_settings={"help_option_names": ["-h", "--help"], "max_content_width": 120},
)


def _show_version(shown: bool) -> None:
    """Show the Product version as people write it, then exit (the eager `--version` option)."""
    if shown:
        typer.echo(f"{CLI_NAME} {version.display(version.installed_version())}")
        raise typer.Exit()


@app.callback(invoke_without_command=True)
def _root(
    ctx: typer.Context,
    _version: Annotated[
        bool,
        typer.Option(
            "--version",
            help="Show the Product version, such as 15.0.0 (build 26004), and exit.",
            callback=_show_version,
            is_eager=True,
        ),
    ] = False,
) -> None:
    """Show the help when no command is given."""
    if ctx.invoked_subcommand is None:
        typer.echo(ctx.get_help())


JsonOption = Annotated[
    bool,
    typer.Option(
        "--json",
        help="Show one JSON object: the result, or an error with its name, exit code, message and next command.",
    ),
]
NoInputOption = Annotated[
    bool,
    typer.Option("--no-input", help="Do not ask for input. If the command needs an answer, it exits with exit code 3."),
]
EnvironmentArg = Annotated[
    str | None,
    typer.Argument(
        metavar="ENVIRONMENT",
        help="The environment name. Without one, the command reads it from environment variable "
        "CONNX_MCP_ENVIRONMENT, else uses `default`.",
        show_default=False,
    ),
]
SecretEnvOption = Annotated[
    list[str] | None,
    typer.Option(
        "--secret-env",
        metavar="KEY=VARIABLE",
        help="Read secret KEY for this run from environment variable VARIABLE. Repeatable.",
        show_default=False,
    ),
]
SecretStdinOption = Annotated[
    str | None,
    typer.Option(
        "--secret-stdin",
        metavar="KEY",
        help="Read secret KEY for this run from stdin (a pipe or a file).",
        show_default=False,
    ),
]


@dataclass
class Session:
    """What a command body works with: its output, whether it may prompt, and the ports."""

    out: Output
    no_input: bool
    ports: Ports

    @property
    def can_prompt(self) -> bool:
        """Whether a prompt is allowed: no `--no-input`, and a terminal (or a test's pipe input) to prompt on."""
        return not self.no_input and self.ports.terminal.attached()

    def require_docker(self) -> DockerInfo:
        """Check Docker before any daemon call."""
        return check_docker(self.ports.docker)


def _run(ctx: typer.Context, json_output: bool, no_input: bool, body: Callable[[Session], None]) -> None:
    """Run a command body, turning a CliError into its output and exit code."""
    out = Output(json_output, ctx.info_name)
    try:
        ports = ctx.obj if isinstance(ctx.obj, Ports) else default_ports()
        body(Session(out=out, no_input=no_input, ports=ports))
    except CliError as error:
        raise typer.Exit(out.error(error)) from None


def _tty(session: Session) -> Terminal | None:
    """Return the terminal to prompt on, or None: prompting is off, or JSON owns stdout."""
    return session.ports.terminal if session.can_prompt and not session.out.json_mode else None


def _daemon(body: Callable[[Session], None]) -> Callable[[Session], None]:
    """Return a command body that checks Docker first."""

    def checked(session: Session) -> None:
        """Check Docker, then run the body."""
        session.require_docker()
        body(session)

    return checked


@app.command(
    short_help="Create or change an environment's settings.",
    help=_help(
        "Create or change an environment's settings. With no options, the command asks you for each "
        "setting in turn. With options, it saves the environment only when every value is valid and every required "
        "value is set.",
        [
            f"{CLI_NAME} configure",
            f"{CLI_NAME} configure sales --no-input --set CDD_USERNAME=sa --set CDD_PASSWORD= --cdd /srv/sales.cdd",
            f"printf '%s' \"$CDD_PASSWORD\" | {CLI_NAME} configure sales --no-input --set CDD_PASSWORD=-",
            f"{CLI_NAME} configure sales --unset MCP_PATH",
            f"{CLI_NAME} configure test --port 8001",
            f"{CLI_NAME} configure demo --demo keycloak --demo-vault",
            f"printf '%s' \"$TOKEN\" | {CLI_NAME} configure demo --demo keycloak --demo-vault --stack-host stackhost "
            "--vault-ca ca.pem --set VAULT_TOKEN=-",
            f"{CLI_NAME} configure sales --show",
            f"{CLI_NAME} configure sales --no-input --from-env-file sales.env --cdd /srv/sales.cdd",
        ],
        ["None."],
    ),
)
def configure(
    ctx: typer.Context,
    environment: EnvironmentArg = None,
    set_items: Annotated[
        list[str] | None,
        typer.Option(
            "--set",
            help="Set server setting KEY to VALUE. KEY= sets an explicit empty value, not the server default. "
            "KEY=- reads the value from stdin (a pipe or a file), so a secret stays out of shell history. Repeatable.",
            metavar="KEY=VALUE",
        ),
    ] = None,
    unset_keys: Annotated[
        list[str] | None,
        typer.Option(
            "--unset", help="Remove server setting KEY, so the server default applies. Repeatable.", metavar="KEY"
        ),
    ] = None,
    cdd: Annotated[
        Path | None,
        typer.Option("--cdd", metavar="FILE", help="The CDD file on this machine, which the server mounts read-only."),
    ] = None,
    vault_ca: Annotated[
        str | None,
        typer.Option(
            "--vault-ca",
            metavar="FILE",
            help="The Vault CA file on this machine, mounted read-only where VAULT_CACERT points. '' removes it.",
        ),
    ] = None,
    port: Annotated[
        str | None,
        typer.Option("--port", metavar="PORT", help="The host port for the server (default 8000).", show_default=False),
    ] = None,
    listen: Annotated[
        str | None,
        typer.Option(
            "--listen",
            help="The network interfaces for the server port: localhost for this machine only, or all for every "
            "interface (default all).",
            metavar="localhost|all",
            show_default=False,
        ),
    ] = None,
    accept_open_network: Annotated[
        bool | None,
        typer.Option(
            "--accept-open-network/--no-accept-open-network",
            help="Accept that the network reaches this server without OAuth sign-in, and hide that warning. "
            "--no-accept-open-network shows the warning again.",
            show_default=False,
        ),
    ] = None,
    certs: Annotated[
        Path | None,
        typer.Option(
            "--certs",
            metavar="DIR",
            help="The TLS certificate directory on this machine: one .crt/.key pair, or empty.",
        ),
    ] = None,
    image_ref: Annotated[
        str | None,
        typer.Option(
            "--image",
            metavar="IMAGE",
            help="The server image for this environment, registry first (for example "
            f"registry.example:5000/name:tag). It has priority over {image.VARIABLE_NAMES}. '' removes it. "
            f"A change applies at the first start or at `{CLI_NAME} update`.",
            show_default=False,
        ),
    ] = None,
    ask_at_start: Annotated[
        list[str] | None,
        typer.Option(
            "--ask-at-start",
            help="Never store secret KEY, and remove its stored value. A start or restart asks for it, "
            "or reads it from --secret-env or --secret-stdin. Repeatable.",
            metavar="KEY",
        ),
    ] = None,
    demo_provider: Annotated[
        str | None,
        typer.Option(
            "--demo",
            help="Fill the OAuth settings from the demo stack's Keycloak or dex, and bind the demo stack to this "
            "environment.",
            metavar="keycloak|dex",
        ),
    ] = None,
    demo_vault: Annotated[
        bool,
        typer.Option(
            "--demo-vault",
            help="With --demo, also fill the Vault settings from the demo Vault, and mount its CA where "
            "VAULT_CACERT points.",
        ),
    ] = False,
    stack_host: Annotated[
        str | None,
        typer.Option(
            "--stack-host",
            metavar="HOST",
            help="With --demo, use a demo stack on another machine. HOST is the exact name that stack was set up "
            "with: a short name and the full DNS name do not match. This machine and the server must both reach "
            "it by that name. Give a copy of its Vault CA with --vault-ca. --stack-host '' removes the settings "
            "that the demo stack on the other machine filled in.",
            show_default=False,
        ),
    ] = None,
    stack_port: Annotated[
        list[str] | None,
        typer.Option(
            "--stack-port",
            help="With --stack-host, a demo stack port that differs from the default: keycloak (8080), dex (5556) "
            "or vault (8200). Repeatable.",
            metavar="SERVICE=PORT",
        ),
    ] = None,
    from_env_file: Annotated[
        Path | None,
        typer.Option(
            "--from-env-file",
            metavar="FILE",
            help=f"Import a settings file, as `{CLI_NAME} export` writes it. --demo and the other options have "
            "priority over its values.",
        ),
    ] = None,
    show: Annotated[bool, typer.Option("--show", help="Show the environment, with every secret masked.")] = False,
    json_output: JsonOption = False,
    no_input: NoInputOption = False,
) -> None:
    """Configure an environment."""
    host: dict[str, str] = {}
    if vault_ca is not None:
        host["vault_ca_file"] = vault_ca
    if port is not None:
        host["published_port"] = port
    if listen is not None:
        host["listen_address"] = listen
    if accept_open_network is not None:
        host["open_network_accepted"] = str(accept_open_network).lower()
    if certs is not None:
        host["cert_dir"] = str(certs)
    if image_ref is not None:
        host["image"] = image_ref
    if stack_host is not None:
        host["demo_stack_host"] = stack_host
    if stack_port:
        host["demo_stack_ports"] = ",".join(stack_port)
    changes = Changes(
        sets=set_items or [],
        unsets=unset_keys or [],
        cdd=cdd,
        host=host,
        ask_at_start=ask_at_start or [],
        from_env_file=from_env_file,
    )

    def body(session: Session) -> None:
        """Walk the settings on a terminal in human mode; otherwise apply the flags."""
        terminal = _tty(session)
        if demo_provider is not None or demo_vault:
            changes.demo = bind.request(demo_provider, demo_vault, session.ports.probe)
        run_configure(session.out, environment, changes, show, terminal)

    _run(ctx, json_output, no_input, body)


@app.command(
    short_help="Show an environment's settings, or write them to a file.",
    help=_help(
        "Show an environment's settings, or write them to a file with --output. The command leaves out stored "
        "secrets unless you give --include-secrets with --output. It always leaves out ask-at-start secrets.\n\n"
        f"`{CLI_NAME} configure --from-env-file` imports the file. "
        "It marks each secret left out as ask-at-start, unless the environment already stores a value for it.",
        [f"{CLI_NAME} export > default.env", f"{CLI_NAME} export sales --include-secrets --output sales.env"],
        [f"A configured environment ({CLI_NAME} configure)."],
    ),
)
def export(
    ctx: typer.Context,
    environment: EnvironmentArg = None,
    include_secrets: Annotated[
        bool,
        typer.Option(
            "--include-secrets",
            help="With --output, also write the stored secrets to FILE.",
        ),
    ] = False,
    output: Annotated[
        Path | None,
        typer.Option(
            "--output",
            "-o",
            metavar="FILE",
            help="Write the settings to FILE, readable only by its owner.",
        ),
    ] = None,
    json_output: JsonOption = False,
    no_input: NoInputOption = False,
) -> None:
    """Export an environment."""
    _run(ctx, json_output, no_input, lambda session: transfer.export(session.out, environment, include_secrets, output))


@app.command(
    help=_help(
        "Sign in to the container registry with your credentials. The token or password never shows on the command "
        "line or in the output.",
        [f"{CLI_NAME} login", f"{CLI_NAME} login --username me --password-stdin < token.txt"],
        [DOCKER_PREREQUISITE, f"Registry credentials from {image.CREDENTIALS_SOURCE}."],
    )
)
def login(
    ctx: typer.Context,
    username: Annotated[
        str | None,
        typer.Option(
            "--username", "-u", metavar="USER", help="The registry user name. Without it, the command asks you."
        ),
    ] = None,
    password_stdin: Annotated[
        bool,
        typer.Option(
            "--password-stdin",
            help="Read the token or password from stdin. Without it, the command asks you for it and does not show "
            "what you type.",
        ),
    ] = False,
    registry: Annotated[
        str | None,
        typer.Option(
            "--registry",
            metavar="REGISTRY",
            help="The registry host to sign in to, with no path, user or password. Without it, the command uses "
            f"the host of {image.REGISTRY_VARIABLE}, else the built-in registry. {image.NAME_VARIABLE} and "
            f"{image.TAG_VARIABLE} have no effect here. For an environment with its own image (configure --image) "
            "on another registry, give that registry.",
            show_default=False,
        ),
    ] = None,
    json_output: JsonOption = False,
    no_input: NoInputOption = False,
) -> None:
    """Sign in to the registry."""

    def body(session: Session) -> None:
        """Prompt on a terminal in human mode; otherwise take the flags and stdin."""
        terminal = _tty(session)
        chosen = signin.registry_to_sign_in(registry)
        options = signin.LoginOptions(chosen, username=username, password_stdin=password_stdin)
        signin.login(session.ports, session.out, options, terminal, sys.stdin)

    _run(ctx, json_output, no_input, _daemon(body))


@app.command(
    short_help="Download an environment's server image.",
    help=_help(
        "Download an environment's server image from the container registry. The image is the environment's own "
        "image (configure --image), else "
        f"the default image that {image.VARIABLE_NAMES} set (see {CLI_NAME} --help). The command does not change a "
        f"pinned image. To move an environment to the new image, run `{CLI_NAME} update`.",
        [f"{CLI_NAME} download", f"{CLI_NAME} download --all"],
        [DOCKER_PREREQUISITE, f"A configured environment and a registry sign-in ({CLI_NAME} login)."],
    ),
)
def download(
    ctx: typer.Context,
    environment: EnvironmentArg = None,
    every: Annotated[
        bool,
        typer.Option(
            "--all",
            help="Download each distinct image of every environment, once. Do not use with an environment name.",
        ),
    ] = False,
    json_output: JsonOption = False,
    no_input: NoInputOption = False,
) -> None:
    """Pull an image."""
    _run(ctx, json_output, no_input, _daemon(lambda s: upgrade.download(s.ports, s.out, environment, every)))


@app.command(
    short_help="Move an environment to the newest image.",
    help=_help(
        "Download the newest image for an environment and pin it. If the server is started, the command offers a "
        "restart on the new image. Without a restart, the new image applies at the next start.\n\n"
        "The image is the environment's own image (configure --image), else the default image that "
        f"{image.VARIABLE_NAMES} set when you run this command.\n\n{image.VERSION_RULE}",
        [
            f"{CLI_NAME} update sales",
            f"{CLI_NAME} update sales --restart",
            f"{CLI_NAME} update sales --restart --no-input --secret-env CDD_PASSWORD=CI_CDD_PASSWORD",
        ],
        [DOCKER_PREREQUISITE, f"A configured environment and a registry sign-in ({CLI_NAME} login)."],
    ),
)
def update(
    ctx: typer.Context,
    environment: EnvironmentArg = None,
    restart: Annotated[
        bool | None,
        typer.Option(
            "--restart/--no-restart",
            help="Restart the server on the new image if it is started. With --no-restart, the server stays on the "
            "old image. Without either, the command asks you. When it cannot ask, it does not restart.",
            show_default=False,
        ),
    ] = None,
    secret_env: SecretEnvOption = None,
    secret_stdin: SecretStdinOption = None,
    json_output: JsonOption = False,
    no_input: NoInputOption = False,
) -> None:
    """Update an environment's image."""
    options = lifecycle.StartOptions(inputs=StartInputs(secret_env=secret_env or [], stdin_key=secret_stdin))

    def body(session: Session) -> None:
        """Offer the restart on a terminal in human mode."""
        terminal = _tty(session)
        upgrade.update(session.ports, session.out, environment, restart, terminal, options)

    _run(ctx, json_output, no_input, _daemon(body))


@app.command(
    help=_help(
        "Start an environment and wait until it is healthy.\n\n"
        "Each start writes the compose file again. Use compose.override.yml in the environment's directory to "
        "customize the environment. The command applies this file over the compose file and never changes it.\n\n"
        "The first start pins the image: later starts use exactly that image, also when its tag moves to a newer "
        f"image. `{CLI_NAME} update` moves the pin. {image.VERSION_RULE}\n\n"
        "The command reads each ask-at-start secret (configure --ask-at-start) from --secret-env or "
        "--secret-stdin. Without them, it asks you and does not show what you type. The one-run overrides --cdd, "
        "--certs and --cdd-username apply to this run only. A stored secret given with --secret-env or "
        "--secret-stdin also applies to this run only.\n\n"
        "The container keeps one-run overrides and ask-at-start secrets until the next start, also across "
        f"reboots. `{CLI_NAME} status` lists the overrides as drift.",
        [
            f"{CLI_NAME} start",
            f"{CLI_NAME} start sales --timeout 600",
            f"{CLI_NAME} start sales --no-input --secret-env CDD_PASSWORD=CI_CDD_PASSWORD",
            f"{CLI_NAME} start test --cdd /srv/try.cdd --cdd-username analyst",
        ],
        [DOCKER_PREREQUISITE, f"A configured environment ({CLI_NAME} configure) and its image ({CLI_NAME} download)."],
    )
)
def start(
    ctx: typer.Context,
    environment: EnvironmentArg = None,
    timeout: Annotated[
        int | None,
        typer.Option(
            "--timeout",
            min=1,
            metavar="SECONDS",
            help="The time to wait until the environment is healthy (default 180, 420 with OAuth or Vault). "
            "Later starts of this environment use it too.",
            show_default=False,
        ),
    ] = None,
    cdd: Annotated[
        Path | None,
        typer.Option(
            "--cdd",
            metavar="FILE",
            help="Mount CDD file FILE for this run only, not the stored one.",
            show_default=False,
        ),
    ] = None,
    certs: Annotated[
        Path | None,
        typer.Option(
            "--certs",
            metavar="DIR",
            help="Mount certificate directory DIR for this run only, not the stored one.",
            show_default=False,
        ),
    ] = None,
    cdd_username: Annotated[
        str | None,
        typer.Option(
            "--cdd-username",
            metavar="LOGIN",
            help="Use CONNX login LOGIN for this run only. Give its password as CDD_PASSWORD with --secret-env or "
            "--secret-stdin. Without them, the command asks you.",
            show_default=False,
        ),
    ] = None,
    secret_env: SecretEnvOption = None,
    secret_stdin: SecretStdinOption = None,
    json_output: JsonOption = False,
    no_input: NoInputOption = False,
) -> None:
    """Start an environment."""
    inputs = StartInputs(cdd, certs, cdd_username, secret_env or [], secret_stdin)
    options = lifecycle.StartOptions(timeout=timeout, inputs=inputs)
    _run(ctx, json_output, no_input, _daemon(lambda s: lifecycle.start(s.ports, s.out, environment, options, _tty(s))))


@app.command(
    short_help="Stop an environment.",
    help=_help(
        "Stop an environment. The command lets the server shut down cleanly, and forces it to stop after --timeout "
        "seconds.",
        [f"{CLI_NAME} stop", f"{CLI_NAME} stop sales --timeout 30", f"{CLI_NAME} stop --all"],
        [DOCKER_PREREQUISITE],
    ),
)
def stop(
    ctx: typer.Context,
    environment: EnvironmentArg = None,
    timeout: Annotated[
        int,
        typer.Option(
            "--timeout",
            min=0,
            metavar="SECONDS",
            help="The time the server has to shut down before the command forces it to stop "
            f"(default {lifecycle.DEFAULT_STOP_TIMEOUT}).",
            show_default=False,
        ),
    ] = lifecycle.DEFAULT_STOP_TIMEOUT,
    every: Annotated[
        bool, typer.Option("--all", help="Stop every environment. Do not use with an environment name.")
    ] = False,
    json_output: JsonOption = False,
    no_input: NoInputOption = False,
) -> None:
    """Stop an environment."""
    _run(ctx, json_output, no_input, _daemon(lambda s: fleet.stop(s.ports, s.out, environment, timeout, every)))


@app.command(
    help=_help(
        "Restart an environment to apply your changes. The command stops the server and starts it again with the "
        "updated settings (if any). One-run overrides from the previous start are no longer applied. It reads "
        "every ask-at-start secret before the stop. If a secret is not given or is refused, the server stays as it is.",
        [f"{CLI_NAME} restart sales", f"{CLI_NAME} restart sales --if-cert-changed"],
        [DOCKER_PREREQUISITE, f"A configured environment ({CLI_NAME} configure)."],
    )
)
def restart(
    ctx: typer.Context,
    environment: EnvironmentArg = None,
    if_cert_changed: Annotated[
        bool,
        typer.Option(
            "--if-cert-changed",
            help="Restart only when the certificate in the certificate directory differs from the one the server "
            "uses. Use it in the deploy hook of an ACME client.",
        ),
    ] = False,
    secret_env: SecretEnvOption = None,
    secret_stdin: SecretStdinOption = None,
    json_output: JsonOption = False,
    no_input: NoInputOption = False,
) -> None:
    """Restart an environment."""
    options = lifecycle.StartOptions(inputs=StartInputs(secret_env=secret_env or [], stdin_key=secret_stdin))
    _run(
        ctx,
        json_output,
        no_input,
        _daemon(lambda s: lifecycle.restart(s.ports, s.out, environment, if_cert_changed, options, _tty(s))),
    )


@app.command(
    help=_help(
        "Show an environment's server log.",
        [f"{CLI_NAME} log", f"{CLI_NAME} log sales --follow --tail 100", f"{CLI_NAME} log sales --since 10m"],
        [DOCKER_PREREQUISITE, "An environment started at least once."],
    )
)
def log(
    ctx: typer.Context,
    environment: EnvironmentArg = None,
    follow: Annotated[
        bool,
        typer.Option(
            "--follow", "-f", help="Show new lines as they arrive, until you press Ctrl+C. Do not use with --json."
        ),
    ] = False,
    tail: Annotated[
        str | None,
        typer.Option("--tail", metavar="N|all", help="Show only the last N lines (default all).", show_default=False),
    ] = None,
    since: Annotated[
        str | None,
        typer.Option(
            "--since",
            metavar="TIME",
            help="Show only the lines since TIME: a timestamp such as 2026-10-16T09:00:00, or a relative time such "
            "as 10m.",
            show_default=False,
        ),
    ] = None,
    json_output: JsonOption = False,
    no_input: NoInputOption = False,
) -> None:
    """Show the server log."""
    options = lifecycle.LogOptions(follow=follow, tail=tail, since=since)
    _run(ctx, json_output, no_input, _daemon(lambda s: lifecycle.log(s.ports, s.out, environment, options)))


@app.command(
    help=_help(
        "List your environments, or show one in detail.\n\n"
        "The list gives each environment's name, state, port, version, health and drift. The detail view of one "
        "environment adds the server's own health report, its image and its certificate.\n\n"
        "Drift means that the settings changed after the last start, or that the last start used one-run "
        "overrides (see start --help).",
        [f"{CLI_NAME} status", f"{CLI_NAME} status sales --json", f"{CLI_NAME} status --all"],
        [DOCKER_PREREQUISITE],
    )
)
def status(
    ctx: typer.Context,
    environment: Annotated[
        str | None,
        typer.Argument(
            metavar="ENVIRONMENT",
            help="The environment name. Without one, the command reads it from environment variable "
            "CONNX_MCP_ENVIRONMENT, else lists every environment.",
            show_default=False,
        ),
    ] = None,
    every: Annotated[
        bool,
        typer.Option(
            "--all",
            help="List every environment, also when CONNX_MCP_ENVIRONMENT names one. Do not use with an "
            "environment name.",
        ),
    ] = False,
    json_output: JsonOption = False,
    no_input: NoInputOption = False,
) -> None:
    """Show status."""
    _run(ctx, json_output, no_input, _daemon(lambda s: fleet.status(s.ports, s.out, environment, every)))


@app.command(
    short_help="Delete an environment (images are kept).",
    help=_help(
        "Delete an environment after you confirm. The command stops the server, removes its container and "
        "deletes the environment's directory with its settings and your compose.override.yml. It keeps "
        "the images, because other environments can use them.",
        [f"{CLI_NAME} remove test", f"{CLI_NAME} remove test --yes --purge"],
        [DOCKER_PREREQUISITE],
    ),
)
def remove(
    ctx: typer.Context,
    environment: EnvironmentArg = None,
    yes: Annotated[
        bool, typer.Option("--yes", "-y", help="Delete the environment without a question to confirm.")
    ] = False,
    purge: Annotated[
        bool,
        typer.Option(
            "--purge",
            help="Also delete the certificate and OAuth state volumes. Without --purge, a new environment of the "
            "same name uses them again.",
        ),
    ] = False,
    force: Annotated[
        bool,
        typer.Option(
            "--force",
            help="Also delete an environment that the demo stack is bound to. The demo stack is then bound to no "
            "environment. Without --force, the command refuses to delete it.",
        ),
    ] = False,
    json_output: JsonOption = False,
    no_input: NoInputOption = False,
) -> None:
    """Remove an environment."""
    options = removal.RemoveOptions(yes=yes, purge=purge, force=force)

    def body(session: Session) -> None:
        """Confirm on a terminal in human mode; otherwise only `--yes` removes."""
        terminal = _tty(session)
        removal.remove(session.ports, session.out, environment, options, terminal)

    _run(ctx, json_output, no_input, _daemon(body))


@app.command(
    name="install-skill",
    help=_help(
        "Install the connx-sql-expert skill. The command copies the skill from the environment's image into each "
        "harness that you choose. Then it shows the steps to register the server with each harness. It does not "
        "register the server for you.\n\nThe command also writes the skill instructions into the instructions "
        "file that each harness reads. For yourself, this is ~/.claude/CLAUDE.md for claude-code, AGENTS.md "
        "(or AGENTS.override.md when it exists) in CODEX_HOME (default ~/.codex) for codex, and "
        "~/.pi/agent/AGENTS.md (or the CLAUDE.md there when it is the only file) for pi. hermes and agents have "
        "no instructions file for yourself: use --project for them. With --project, the file is in DIR. For "
        "claude-code, this is CLAUDE.md when CLAUDE.md, .claude/CLAUDE.md or CLAUDE.local.md is in DIR or a "
        "directory above it, otherwise AGENTS.md. Your own ~/.claude/CLAUDE.md does not count. When one of these "
        "files in DIR imports DIR/AGENTS.md, this is AGENTS.md. For codex, this is AGENTS.override.md when it exists, otherwise AGENTS.md. For pi, "
        "hermes and agents, this is AGENTS.md. A file that two harnesses read gets one section. The instructions "
        f"tell the harness to use the skill. They are one section between {CLI_NAME} markers. The "
        "command adds the section at the end of the file, or creates the file. A later run replaces the section "
        "where it is. Edit outside the markers. With --no-instructions, the command writes no instructions "
        "file.\n\nClaude Desktop takes the skill as an upload. With --harness "
        "claude-desktop, the command writes the skill zip and its install notes into the current directory, or "
        "into the --zip directory.",
        [
            f"{CLI_NAME} install-skill --harness claude-code",
            f"{CLI_NAME} install-skill sales --harness codex --harness pi --project ~/work/sales",
            f"{CLI_NAME} install-skill --zip ./out --harness claude-desktop",
        ],
        [DOCKER_PREREQUISITE, f"The environment's image ({CLI_NAME} download)."],
    ),
)
def install_skill(
    ctx: typer.Context,
    environment: EnvironmentArg = None,
    harness: Annotated[
        list[str] | None,
        typer.Option(
            "--harness",
            metavar="HARNESS",
            help=f"Install the skill for this harness ({', '.join(skill.HARNESSES)}). Without it, the command asks "
            "you and ticks the harnesses it detects on this machine. Repeatable.",
            show_default=False,
        ),
    ] = None,
    user: Annotated[
        bool,
        typer.Option(
            "--user",
            help="Install the skill for yourself, under your home directory (default), and write the skill "
            "instructions into the instructions file that each harness reads for you. With --no-instructions, "
            "install only the skill. Do not use with --project.",
        ),
    ] = False,
    project: Annotated[
        Path | None,
        typer.Option(
            "--project",
            metavar="DIR",
            help="Install the skill for the project in directory DIR, and write the skill instructions there.",
            file_okay=False,
            exists=True,
        ),
    ] = None,
    force: Annotated[
        bool,
        typer.Option(
            "--force",
            help=(
                f"Replace a skill or skill zip that {CLI_NAME} did not write, and skill instructions that were edited "
                "or whose markers are broken. Write through a project instructions file that links outside the "
                "project."
            ),
        ),
    ] = False,
    zip_dir: Annotated[
        Path | None,
        typer.Option(
            "--zip",
            metavar="DIR",
            help="Write the skill zip and the install notes for each harness into directory DIR. The notes quote "
            "the skill instructions; with --no-instructions, they leave them out. Install nothing on this machine. "
            "Do not use with --user or --project.",
            file_okay=False,
        ),
    ] = None,
    no_instructions: Annotated[
        bool,
        typer.Option(
            "--no-instructions",
            help="Install only the skill: write no instructions file. A section that an earlier run wrote stays "
            "as it is, and the command warns that it remains. With --zip or --harness claude-desktop, the install "
            "notes leave out the skill instructions.",
        ),
    ] = False,
    json_output: JsonOption = False,
    no_input: NoInputOption = False,
) -> None:
    """Install the skill."""
    if user and project is not None:
        raise typer.BadParameter("give --user or --project, not both", param_hint="--project")
    if zip_dir is not None and (user or project is not None):
        raise typer.BadParameter("give --zip or --user/--project, not both", param_hint="--zip")

    def body(session: Session) -> None:
        """Install the skill."""
        request = skill.InstallRequest(
            harnesses=harness or [],
            project=project,
            force=force,
            terminal=_tty(session),
            zip_dir=zip_dir,
            user=user,
            no_instructions=no_instructions,
        )
        skill.install(session.ports, session.out, environment, request)

    _run(ctx, json_output, no_input, _daemon(body))


@app.command(
    name="uninstall-skill",
    help=_help(
        "Uninstall the connx-sql-expert skill. The command deletes the skill copies that install-skill wrote for "
        "each harness that you choose. It also removes the skill instructions section from the instructions file "
        "that each harness reads. It finds them by the rules that install-skill uses, and in the environment's "
        "record of what install-skill wrote.\n\nWithout options, the command removes everything in the record. "
        "Each option limits only its own part: --harness alone removes that harness for yourself and in each "
        "project of the record, "
        "and --user or --project alone removes each harness of the record at that scope.\n\nThe command shows "
        "what it removes and asks you before it removes "
        f"anything. It deletes only a skill copy that {CLI_NAME} installed, and only the text between the "
        f"{CLI_NAME} markers. The one blank line before the section goes with it. When nothing else is left in the "
        "file, the command deletes the file. When a section was edited, its markers are broken, or a project file "
        "links outside the project, the command removes nothing and stops with exit code 123. With --force, it "
        "removes them anyway. When a removal fails, the command stops with exit code 124 and names anything it "
        "removed before. Run the same command again to remove the rest. The environment need not exist when you "
        "give --harness. The command does not use Docker.",
        [
            f"{CLI_NAME} uninstall-skill sales",
            f"{CLI_NAME} uninstall-skill sales --harness codex --harness pi --project ~/work/sales",
            f"{CLI_NAME} uninstall-skill --harness codex --user --yes",
        ],
        ["None."],
    ),
)
def uninstall_skill(
    ctx: typer.Context,
    environment: EnvironmentArg = None,
    harness: Annotated[
        list[str] | None,
        typer.Option(
            "--harness",
            metavar="HARNESS",
            help=f"Uninstall the skill for this harness ({', '.join(skill.HARNESSES)}). Without it, the command "
            "uses each harness of the environment's record; without a record, there is nothing to remove. When the "
            "environment does not exist, it asks you and ticks the harnesses it detects on this machine. Repeatable.",
            show_default=False,
        ),
    ] = None,
    user: Annotated[
        bool,
        typer.Option(
            "--user",
            help="Uninstall the skill for yourself, under your home directory, and remove the skill instructions "
            "from the instructions file that each harness reads for you. Without --user and --project, the command "
            "uses each scope of the environment's record, or your home directory when there is no record. Do not "
            "use with --project.",
        ),
    ] = False,
    project: Annotated[
        Path | None,
        typer.Option(
            "--project",
            metavar="DIR",
            help="Uninstall the skill for the project in directory DIR, and remove the skill instructions there.",
            file_okay=False,
            exists=True,
        ),
    ] = None,
    force: Annotated[
        bool,
        typer.Option(
            "--force",
            help="Remove skill instructions that were edited or whose markers are broken, and remove the section "
            f"from a project instructions file that links outside the project. A skill copy that {CLI_NAME} did not "
            "install stays.",
        ),
    ] = False,
    shared: Annotated[
        bool,
        typer.Option(
            "--shared",
            help="Also remove a shared copy: a skill copy or skill instructions section that another harness or "
            "environment still uses. Without --shared, the command keeps it until the last of them uninstalls. "
            "--force does not remove it.",
        ),
    ] = False,
    yes: Annotated[
        bool, typer.Option("--yes", "-y", help="Uninstall the skill without a question to confirm.")
    ] = False,
    json_output: JsonOption = False,
    no_input: NoInputOption = False,
) -> None:
    """Uninstall the skill."""
    if user and project is not None:
        raise typer.BadParameter("give --user or --project, not both", param_hint="--project")

    def body(session: Session) -> None:
        """Uninstall the skill."""
        request = skill.UninstallRequest(
            harnesses=harness or [],
            project=project,
            user=user,
            force=force,
            yes=yes,
            terminal=_tty(session),
            shared=shared,
        )
        skill.uninstall(session.ports, session.out, environment, request)

    _run(ctx, json_output, no_input, body)


@app.command(
    name="configure-demo",
    help=_help(
        "Configure the demo stack. Choose its services (Keycloak, dex, Vault) and its users file. Each service "
        "option adds its service, and its --no- form (for example --no-dex) removes it. Services that you do not "
        "name stay as they are.\n\n"
        "The users file lists the people who sign in to the stack and the CONNX login of each. On first use, the "
        "command creates a users file with alice, bob, carol and dave. None of them has a CONNX login.\n\n"
        "Before start-demo, map at least one user to a CONNX login. To map a user, uncomment the user's `connx:` "
        "line in the users file. Then fill in a CONNX login from your CDD and its password.\n\n"
        f"{demo.LIMITS}",
        [
            f"{CLI_NAME} configure-demo --keycloak --vault --for demo",
            f"{CLI_NAME} configure-demo --dex --users-file acme.yaml",
            f"{CLI_NAME} configure-demo --for test --rebind",
        ],
        [
            f"With --for: a configured environment of that name ({CLI_NAME} configure).",
        ],
    ),
)
def configure_demo(
    ctx: typer.Context,
    keycloak: Annotated[
        bool | None, typer.Option("--keycloak/--no-keycloak", help="Add Keycloak, an OAuth identity provider.")
    ] = None,
    dex: Annotated[bool | None, typer.Option("--dex/--no-dex", help="Add dex, an OAuth identity provider.")] = None,
    vault: Annotated[
        bool | None,
        typer.Option(
            "--vault/--no-vault",
            help="Add HashiCorp Vault and its seed, which loads each user's CONNX login into Vault.",
        ),
    ] = None,
    users_file: Annotated[
        Path | None,
        typer.Option(
            "--users-file",
            metavar="FILE",
            help="Use users file FILE instead of the current one. The command checks FILE and makes it accessible "
            "to its owner only.",
        ),
    ] = None,
    for_environment: Annotated[
        str | None,
        typer.Option(
            "--for",
            help="Bind the demo stack to environment ENVIRONMENT. start-demo then sets up sign-in for the address "
            "of ENVIRONMENT.",
            metavar="ENVIRONMENT",
        ),
    ] = None,
    rebind: Annotated[
        bool,
        typer.Option(
            "--rebind",
            help="With --for, switch the demo stack from its current environment to ENVIRONMENT. Sign-in through "
            "the demo stack then fails for the old environment.",
        ),
    ] = False,
    stack_host: Annotated[
        str | None,
        typer.Option(
            "--stack-host",
            help="The host name or address that this machine and the server use to reach the demo stack "
            f"(default {demo.DEFAULT_STACK_HOST}).",
            metavar="HOST",
            show_default=False,
        ),
    ] = None,
    json_output: JsonOption = False,
    no_input: NoInputOption = False,
) -> None:
    """Configure the demo stack."""
    choices = {"keycloak": keycloak, "dex": dex, "vault": vault}

    def body(session: Session) -> None:
        """Configure, binding the named environment."""
        options = demo.DemoOptions(stack_host, for_environment, rebind, session.ports.probe.resolves)
        demo.configure(session.out, choices, users_file, options)

    _run(ctx, json_output, no_input, body)


@app.command(
    short_help="Start the demo stack.",
    name="start-demo",
    help=_help(
        "Start the demo stack's services and wait until they are healthy. The command then shows the address of "
        "each service. After a change to the demo stack's configuration or users file, run start-demo again.\n\n"
        f"Other computers can reach the services, so run the stack only on a trusted machine.\n\n{demo.LIMITS}",
        [f"{CLI_NAME} start-demo"],
        [
            DOCKER_PREREQUISITE,
            f"A configured demo stack ({CLI_NAME} configure-demo) with at least one user mapped to a CONNX login.",
        ],
    ),
)
def start_demo(ctx: typer.Context, json_output: JsonOption = False, no_input: NoInputOption = False) -> None:
    """Start the demo stack."""
    _run(ctx, json_output, no_input, _daemon(lambda s: demo.start(s.out, s.ports.docker)))


@app.command(
    short_help="Stop the demo stack.",
    name="stop-demo",
    help=_help(
        "Stop the demo stack and remove its containers. Without --purge, the generated files and the demo CA "
        "stay. After --purge, the next start-demo creates a new demo CA. Then restart each environment that uses "
        "the demo Vault.\n\n"
        f"{demo.LIMITS}",
        [f"{CLI_NAME} stop-demo", f"{CLI_NAME} stop-demo --purge"],
        [DOCKER_PREREQUISITE, f"A configured demo stack ({CLI_NAME} configure-demo)."],
    ),
)
def stop_demo(
    ctx: typer.Context,
    purge: Annotated[
        bool,
        typer.Option(
            "--purge",
            help="Delete the stack's generated files and the demo CA. Remove the seed image. The demo "
            "stack is then bound to no environment. The users file stays.",
        ),
    ] = False,
    json_output: JsonOption = False,
    no_input: NoInputOption = False,
) -> None:
    """Stop the demo stack."""
    _run(ctx, json_output, no_input, _daemon(lambda s: demo.stop(s.out, s.ports.docker, purge)))


@app.command(
    short_help="Tell you the next command to run.",
    help=_help(
        "Tell you the next command to run. The command checks these steps in order and names the first step that "
        "is not done, with any warnings:\n\n"
        "\b\n"
        "  1. Docker\n"
        "  2. The environment, its settings and its demo stack binding\n"
        "  3. The image\n"
        "  4. The server: started and healthy\n"
        "  5. The server's certificate\n"
        "  6. The demo stack's sign-in setup\n"
        "  7. The connx-sql-expert skill\n\n"
        "With several environments, it names the step of the environment that is furthest behind, also of an "
        "environment that you stopped on purpose. It never asks and changes nothing.\n\n"
        "Replace each <...> part of the next command with your own value. Run that command. Then run guide again, "
        "until it says that every step is done.\n\n"
        "The command cannot see a deleted pinned image, a stopped demo stack or an expired registry sign-in. For "
        "these problems, the start, start-demo or download command fails and names its own fix.\n\n"
        "\b\nWith --json:\n"
        '  "next"     the command to run next, or null when "done" is true\n'
        '  "fill_in"  the <...> parts of "next"',
        [f"{CLI_NAME} guide", f"{CLI_NAME} guide sales --json"],
        ["None."],
    ),
)
def guide(
    ctx: typer.Context,
    environment: Annotated[
        str | None,
        typer.Argument(
            metavar="ENVIRONMENT",
            help="The environment name. Without one, the command reads it from environment variable "
            "CONNX_MCP_ENVIRONMENT, else checks every environment (`default` when none exists).",
            show_default=False,
        ),
    ] = None,
    json_output: JsonOption = False,
    no_input: NoInputOption = False,
) -> None:
    """Name the next step."""
    _run(ctx, json_output, no_input, lambda s: guidance.guide(s.ports, s.out, environment))


@app.command(
    "exit-codes",
    short_help="List the exit codes and what they mean.",
    help=_help(
        "List the exit codes, each with its name and what it means. When a command with --json fails, its "
        "error already shows the name and the exit code. A script needs this list only to handle an exit code by "
        "number.",
        [f"{CLI_NAME} exit-codes", f"{CLI_NAME} exit-codes --json"],
        ["None."],
    ),
)
def exit_codes_command(
    ctx: typer.Context,
    json_output: JsonOption = False,
    no_input: NoInputOption = False,
) -> None:
    """List the exit codes."""
    _run(
        ctx,
        json_output,
        no_input,
        lambda s: s.out.result({"exit_codes": exit_codes()}, f"Exit codes:\n{exit_code_table()}"),
    )
