#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""bind.py - One environment bound to the demo stack: its settings, its TLS rule, status and removal.

Description:
    `configure <env> --demo keycloak|dex [--demo-vault]` fills the environment's OAuth (and Vault)
    settings from the demo stack, names the demo CA as its Vault CA file and binds the stack to it,
    so the realm is rendered for its public URL. The binding is saved only after the environment
    is: a refused change binds nothing. A configure of the bound environment keeps the stack's URL
    in step with it. A bound environment runs on plain-http localhost, or over TLS with a provided
    CA-issued certificate: the server refuses OAuth with a self-signed one. `status` shows the
    binding; `remove` refuses the bound environment unless `--force`, which also unbinds.

    With `--stack-host <name>` (stored as the environment's `demo_stack_host`) the binding is to a
    stack on another machine (remote.py): settings rendered from its name and ports, `demo.json`
    neither read nor written, no local binding rules; the TLS and MCP_PATH refusals apply to both.
    `--stack-host ''` removes every value the remote binding rendered. The fill and the end of a remote
    binding are settings edits from the demo stack (edit.py), which `configure` resolves with its flags;
    the flags win.

Copyright:
    Copyright 2026 Software GmbH
    Licensed under the Apache License, Version 2.0 (the "License"); you may not use this file except in compliance
    with the License. You may obtain a copy of the License at https://www.apache.org/licenses/LICENSE-2.0
    Unless required by applicable law or agreed to in writing, software distributed under the License is distributed
    on an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the License
    for the specific language governing permissions and limitations under the License.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from connx_mcp import CLI_NAME, edit, settings
from connx_mcp.demo import remote, stack
from connx_mcp.demo.stack import DemoSettings
from connx_mcp.edit import Origin, OriginKind
from connx_mcp.environment import Environment
from connx_mcp.output import CliError, ExitCode, shell_argument
from connx_mcp.ports import Ports
from connx_mcp.probe import EndpointProbe

if TYPE_CHECKING:
    from connx_mcp.lifecycle import StartRun, StatusReport
    from connx_mcp.remove import RemoveRun

Provider = Literal["keycloak", "dex"]
PROVIDERS: tuple[Provider, ...] = ("keycloak", "dex")
# The shape of a public URL the demo filled from the published port (stack.local_url).
_LOCAL_URL = re.compile(r"https?://localhost:\d+")
# The origins of the demo stack's settings edits: the fill, and the end of a remote binding.
FILLED = Origin(OriginKind.DEMO, "--demo")
ENDED = Origin(OriginKind.DEMO, "--stack-host ''")


@dataclass(frozen=True)
class DemoRequest:
    """What `configure <env> --demo ... [--demo-vault]` asks for.

    Attributes:
        provider (Provider | None): The demo identity provider; None exactly when the request is refused (`--demo`
            names neither keycloak nor dex, or `--demo-vault` comes alone), and `refusals` says why.
        vault (bool): Also the demo Vault.
        probe (EndpointProbe | None): Resolves the stack host and asks a remote stack; None skips both.
        refusals (tuple[edit.Refuse, ...]): Why the request alone is refused, whatever the stack and the settings:
            `configure` reports them with its other edits' refusals, and plans no binding.
    """

    provider: Provider | None
    vault: bool = False
    probe: EndpointProbe | None = field(default=None, compare=False)
    refusals: tuple[edit.Refuse, ...] = ()

    def __post_init__(self) -> None:
        """Reject a request whose provider and refusals disagree: only a refused request names no provider.

        Raises:
            ValueError: A provider with refusals, or neither.
        """
        if (self.provider is None) != bool(self.refusals):
            raise ValueError(f"a demo request names a provider exactly when it is not refused: {self!r}")

    @property
    def chosen(self) -> Provider:
        """The identity provider to bind; only a request that is not refused is ever planned.

        Raises:
            ValueError: The request is refused (`request`): its caller planned what it must report.
        """
        if self.provider is None:
            raise ValueError(f"a refused demo request is never planned: {[r.origin.label for r in self.refusals]}")
        return self.provider

    def resolves(self, host: str) -> bool:
        """Return whether a host name resolves on this machine; True without a probe."""
        return self.probe is None or self.probe.resolves(host)


def request(provider: str | None, vault: bool, probe: EndpointProbe | None) -> DemoRequest:
    """Return the demo request of `--demo` and `--demo-vault`, with the refusals it earns on its own.

    Args:
        provider (str | None): What `--demo` named; None when not given.
        vault (bool): `--demo-vault`.
        probe (EndpointProbe | None): Resolves the stack host and asks a remote stack.

    Returns:
        DemoRequest: The request; refused when `--demo` names something other than keycloak or dex, or
            `--demo-vault` comes without `--demo` (`configure` asks only when one of them is given).
    """
    for known in PROVIDERS:
        if provider is not None and provider.lower() == known:
            return DemoRequest(known, vault, probe)
    if provider is not None:
        flag, reason = f"--demo {shell_argument(provider)}", "not a demo identity provider; give keycloak or dex."
    else:
        flag, reason = (
            "--demo-vault",
            (
                "needs --demo keycloak or --demo dex. The server looks up each signed-in caller's CONNX login in Vault, "
                "and the demo Vault holds logins only for the demo identity providers' users."
            ),
        )
    return DemoRequest(None, vault, probe, (edit.Refuse(reason, Origin(OriginKind.DEMO_FLAG, flag)),))


@dataclass
class Binding:
    """The demo settings to save once the environment is saved, and what to tell the administrator.

    Attributes:
        settings (DemoSettings): The demo settings, bound to the environment.
        notes (list[str]): Lines for the result.
        remote (bool): The stack runs on another machine: the binding is the environment's own host settings.
        warnings (list[str]): Warnings for the result, its JSON included.
    """

    settings: DemoSettings
    notes: list[str] = field(default_factory=list)
    remote: bool = False
    warnings: list[str] = field(default_factory=list)

    def commit(self) -> None:
        """Save the bound demo settings of a local binding.

        A remote binding is the environment's host settings, saved with it: `demo.json` is neither read nor
        written, so a local binding of the same environment stays until `configure-demo --for` moves it.
        """
        if not self.remote:
            stack.save_settings(self.settings)

    def to_json(self) -> dict[str, Any]:
        """Return the binding for the JSON result."""
        data: dict[str, Any] = {
            "bound_environment": self.settings.bound_environment,
            "public_base_url": self.settings.mcp_url,
            "stack_host": self.settings.stack_host,
        }
        if self.remote:
            data["remote"] = True
        return data


@dataclass(frozen=True)
class Plan:
    """A binding planned before its fill is resolved; `bind` gives the binding once the fill is.

    The only way to the planned binding: a remote one is checked against its stack on the way, so no caller
    saves a remote binding to the wrong issuer.

    Attributes:
        planned (Binding): The binding as planned.
        request (DemoRequest): The request; its probe asks a remote stack.
    """

    planned: Binding
    request: DemoRequest

    def bind(self, env: Environment) -> Binding:
        """Return the binding to commit once the environment is saved, a remote stack asked first.

        A remote stack whose discovery document names another issuer ends in `_check_remote`'s CliError.

        Args:
            env (Environment): The environment, the fill resolved.

        Returns:
            Binding: The binding; a remote one carries what its stack's check found as warnings.
        """
        if self.planned.remote:
            _check_remote(env, self.planned, self.request)
        return self.planned


def bound_environment() -> str | None:
    """Return the environment the demo stack is bound to; None when unbound or unconfigured.

    An unreadable demo settings file ends in stack.load_settings' CliError.

    Returns:
        str | None: The bound environment.
    """
    demo = stack.load_settings()
    return demo.bound_environment if demo is not None else None


def status_binding() -> tuple[str | None, str | None]:
    """Return the bound environment for a status report, and a notice when the demo settings cannot be read.

    Status reports rather than fails: the notice says why the binding is unknown.

    Returns:
        tuple[str | None, str | None]: The bound environment, and the notice or None.
    """
    try:
        return bound_environment(), None
    except CliError as error:
        return None, f"The demo stack's binding is unknown: {error.message}"


def _bound_to(name: str) -> DemoSettings | None:
    """Return the demo settings when the stack is bound to this environment; None otherwise.

    Args:
        name (str): The environment's name.

    Returns:
        DemoSettings | None: The demo settings.

    An unreadable demo settings file ends in stack.load_settings' CliError: whether the environment is
    bound is then unknown, so nothing that depends on it is skipped.
    """
    demo = stack.load_settings()
    return demo if demo is not None and demo.bound_environment == name else None


def binding_of(env: Environment) -> Binding | None:
    """Return an environment's binding: to a remote stack (its host settings), else to the local one; None if neither.

    Args:
        env (Environment): The environment.

    Returns:
        Binding | None: The binding.

    An unreadable demo settings file ends in stack.load_settings' CliError, for an environment without a
    remote binding.
    """
    if (remote_stack := remote.stack_settings(env)) is not None:
        return Binding(remote_stack, remote=True)
    demo = _bound_to(env.name)
    return Binding(demo) if demo is not None else None


def filled_keys(request: DemoRequest) -> list[str]:
    """Return the server keys the demo fill replaces: the OAuth group, and with Vault the Vault group.

    Args:
        request (DemoRequest): The demo request.

    Returns:
        list[str]: The keys, in schema order.
    """
    groups = {settings.Group.OAUTH, *([settings.Group.VAULT] if request.vault else [])}
    return [key for key, row in settings.SCHEMA.items() if row.group in groups]


def claims(env: Environment, request: DemoRequest | None) -> list[edit.Claim]:
    """Return the claims on what the demo fill replaces, which change nothing: a file's value for it is not judged.

    The fill replaces the OAuth group (and with Vault the Vault group); a local stack's Vault also names its
    CA file, while a remote stack's is the copy `--vault-ca` names.

    Args:
        env (Environment): The environment with this call's other edits resolved; its stack host tells local
            from remote.
        request (DemoRequest | None): The demo request; None when the call binds no demo stack.

    Returns:
        list[edit.Claim]: A claim per server key, then the Vault CA file's for a local stack with Vault; none
            without a demo request.
    """
    if request is None:
        return []
    found = [edit.Claim(key, FILLED) for key in filled_keys(request)]
    if request.vault and remote.stack_settings(env) is None:
        found.append(edit.Claim("vault_ca_file", FILLED, host=True))
    return found


def plan(env: Environment, stored: Environment, request: DemoRequest) -> tuple[Plan, list[edit.Edit]]:
    """Plan an environment's binding to the demo stack, and the settings edits that fill its settings from it.

    The edits replace the OAuth group (and with Vault the Vault group) by the stack's values; a flag of the
    same call beats them, and a secret marked ask-at-start stays asked for.

    Args:
        env (Environment): The environment with this call's flags resolved and a remote binding it ends ended,
            before the fill.
        stored (Environment): The environment as stored before this call: a remote stack's Vault reuses the read
            token stored for it.
        request (DemoRequest): The demo provider and whether Vault too; never one with refusals.

    Returns:
        tuple[Plan, list[edit.Edit]]: The plan, whose `bind` gives the binding to commit once the environment
            is saved, and the edits.

    Raises:
        CliError: A service the stack does not run, or another environment bound; for a remote stack, Vault
            without a copy of its CA.

    A request with refusals of its own ends in `DemoRequest.chosen`'s ValueError: its caller planned what it
    must report.
    """
    provider = request.chosen
    if (remote_stack := remote.stack_settings(env)) is not None:
        binding, edits = _plan_remote(env, stored, remote_stack, request)
        return Plan(binding, request), edits
    demo = stack.require_settings()
    wanted = [provider, *(["vault"] if request.vault else [])]
    if missing := [service for service in wanted if service not in demo.services]:
        raise CliError(
            ExitCode.DEMO_SERVICE_NOT_CHOSEN,
            f"The demo stack does not run {' or '.join(missing)}; it runs {', '.join(demo.services)}.",
            f"{CLI_NAME} configure-demo " + " ".join(f"--{service}" for service in missing),
        )
    bound = demo.bind(env.name, stack.public_url(env))
    values = bound.server_settings(provider, request.vault)
    edits = _fill(values, request)

    with_vault = " and Vault" if request.vault else ""
    notes = [
        f"Environment {env.name!r} is bound to the demo stack ({provider}{with_vault}); start-demo "
        f"renders the realm for {bound.mcp_url}."
    ]
    if still_asked := sorted(env.asked_at_start & values.keys()):
        notes.append(
            f"{', '.join(still_asked)} stay asked for at start: give the demo stack's values from "
            f"{stack.settings_path()} (or the demo client secret its realm and dex templates name)."
        )
    if request.vault:
        # Stored as given: the CA file exists only once start-demo has created it.
        edits.append(edit.SetHost("vault_ca_file", str(stack.vault_ca_path()), FILLED))
        notes.append(f"Vault CA: {stack.vault_ca_path()} (start-demo creates it), mounted where VAULT_CACERT points.")
    if unresolved := stack.unresolved_note(bound.stack_host, request.resolves):
        notes.append(f"Warning: {unresolved}")
    notes.append(f"Next: {CLI_NAME} start-demo, then {CLI_NAME} start {env.name}")
    return Plan(Binding(bound, notes), request), edits


def _fill(values: dict[str, str], request: DemoRequest) -> list[edit.Edit]:
    """Return the edits that replace the OAuth (and with Vault the Vault) group by the stack's values.

    Args:
        values (dict[str, str]): The stack's server settings.
        request (DemoRequest): The demo request.

    Returns:
        list[edit.Edit]: An unset of each key of the groups the stack gives no value, then a fill of each value.
    """
    unsets: list[edit.Edit] = [edit.Unset(key, FILLED) for key in filled_keys(request) if key not in values]
    return [*unsets, *(edit.Fill(key, value, FILLED) for key, value in values.items())]


def _plan_remote(
    env: Environment, stored: Environment, demo: DemoSettings, request: DemoRequest
) -> tuple[Binding, list[edit.Edit]]:
    """Plan the binding to a demo stack on another machine, named by its host settings, and its fill.

    The stack's CA is the copy `--vault-ca` names. The read token is the one given in this call, asked
    for at start, or stored for the same Vault address; else upstream's published default, with a warning.
    Only the stored address counts: a VAULT_ADDR this call gives never carries a token to another Vault.

    Args:
        env (Environment): The environment, `demo_stack_host` set; this call's flags resolved, before the fill.
        stored (Environment): The environment as stored before this call.
        demo (DemoSettings): The remote stack its host settings name.
        request (DemoRequest): The request; never a refused one.

    Returns:
        tuple[Binding, list[edit.Edit]]: The remote binding, whose commit writes nothing beyond the environment,
            and the edits. `Plan.bind` asks the stack once the fill is resolved.

    Raises:
        CliError: Vault without a copy of the stack's CA.
    """
    ca_file = env.host.vault_ca_file
    if request.vault and (not ca_file or Path(ca_file) == stack.vault_ca_path()):
        raise CliError(
            ExitCode.INPUT_REQUIRED,
            f"The demo Vault on {demo.stack_host} serves a certificate its own demo CA signed: give a copy of the "
            "stack's generated/tls/ca.pem (in its stack directory) with --vault-ca. Nothing was saved.",
            f"{CLI_NAME} configure {env.name} --vault-ca <copy of the stack's ca.pem>",
        )
    provider = request.chosen
    values = demo.server_settings(provider, request.vault)
    stored_token = stored.server.get("VAULT_TOKEN")
    if request.vault and stored_token and stored.server.get("VAULT_ADDR") == demo.vault_address:
        values["VAULT_TOKEN"] = stored_token  # the token given before for this Vault
    with_vault = " and Vault" if request.vault else ""
    notes = [
        f"Environment {env.name!r} is bound to the demo stack on {demo.stack_host} ({provider}{with_vault}); its "
        f"realm must be rendered there for {demo.mcp_url} (MCP_PUBLIC_BASE_URL on the stack machine).",
        f"Next: {CLI_NAME} start {env.name}",
    ]
    return Binding(demo, notes, remote=True), _fill(values, request)


def _check_remote(env: Environment, binding: Binding, request: DemoRequest) -> None:
    """Ask a remote demo stack about the settings its fill produced; add what it finds to the binding's warnings.

    Args:
        env (Environment): The environment, the fill resolved.
        binding (Binding): The remote binding `plan` gave.
        request (DemoRequest): The request; its probe asks the stack.

    Raises:
        CliError: The stack's discovery document names another issuer.
    """
    demo = binding.settings
    found = remote.check(env, request.probe) if request.probe is not None else remote.Check()
    if found.other_issuer is not None:
        fix = (
            "Give the stack's name exactly as it was started with (its STACK_HOST)."
            if found.next_command
            else "No stack name renders that issuer: start the stack with STACK_HOST set to the name this machine "
            "and the server reach it by."
        )
        raise CliError(
            ExitCode.DEMO_STACK_ISSUER_MISMATCH,
            f"The demo stack on {demo.stack_host} names its issuer {found.other_issuer}, not the one this binding "
            f"renders, so the server would refuse every token. {fix} Nothing was saved.",
            found.next_command,
        )
    binding.warnings += [*found.notices, *remote.exposure_warnings(env)]


def end_remote(env: Environment, ended: DemoSettings) -> list[edit.Edit]:
    """Return the edits that remove every value a remote binding rendered, back to the settings before it.

    A value is removed only while it is still the one the binding rendered: one the administrator changed
    since stays, and so does one a flag of this call gives. While VAULT_ADDR is the stack's Vault, the Vault
    CA copy goes; the read token goes by `drop_read_token`, once these edits are resolved.

    Args:
        env (Environment): The environment with this call's flags resolved, its `demo_stack_host` cleared.
        ended (DemoSettings): The remote stack it was bound to.

    Returns:
        list[edit.Edit]: The edits.
    """
    rendered = {(key, value) for provider in PROVIDERS for key, value in ended.server_settings(provider, True).items()}
    edits: list[edit.Edit] = [
        edit.Unset(key, ENDED, only_if=value) for key, value in sorted(rendered) if key != "VAULT_TOKEN"
    ]
    if env.server.get("VAULT_ADDR") == ended.vault_address:
        edits.append(edit.SetHost("vault_ca_file", None, ENDED))
    return edits


def drop_read_token(stored: Environment, after: Environment, ended: DemoSettings) -> list[edit.Edit]:
    """Return the edit that removes the read token stored for a remote stack's Vault the environment no longer uses.

    A credential for a Vault the environment no longer uses goes: the stored token was issued for the stack's
    Vault when the stored VAULT_ADDR names it, and it stays only while VAULT_ADDR still does once the binding
    is ended (a flag of this call may give it again). A token this call gives, or another value, stays.

    Args:
        stored (Environment): The environment as stored before this call.
        after (Environment): The environment with this call's edits and `end_remote`'s resolved.
        ended (DemoSettings): The remote stack it was bound to.

    Returns:
        list[edit.Edit]: The removal of the stored token while it is still that value; none when it stays.
    """
    token, vault = stored.server.get("VAULT_TOKEN"), ended.vault_address
    if token is None or stored.server.get("VAULT_ADDR") != vault or after.server.get("VAULT_ADDR") == vault:
        return []
    return [edit.Unset("VAULT_TOKEN", ENDED, only_if=token)]


def follow(env: Environment) -> Binding | None:
    """Return the binding of an environment the stack is bound to, its URL kept in step; None otherwise.

    A public URL the demo filled from the published port (`http(s)://localhost:<port>`, unchanged by this
    call) moves with the port and TLS, and so does the Keycloak audience the demo filled from it; a URL
    the administrator gave is kept.

    Args:
        env (Environment): The environment, with this call's changes applied; refilled in memory.

    Returns:
        Binding | None: The binding to commit once the environment is saved; a remote binding unchanged,
            since its realm is rendered on the stack machine.

    An unreadable demo settings file ends in stack.load_settings' CliError.
    """
    if (remote_stack := remote.stack_settings(env)) is not None:
        return Binding(remote_stack, remote=True)
    demo = _bound_to(env.name)
    if demo is None:
        return None
    changed: list[str] = []  # named in the note
    stored = env.server.get("MCP_PUBLIC_BASE_URL", "").rstrip("/")
    local = stack.local_url(env)
    if stored == demo.mcp_url and _LOCAL_URL.fullmatch(stored) and stored != local:
        env.server["MCP_PUBLIC_BASE_URL"] = local
        changed.append("MCP_PUBLIC_BASE_URL")
        if env.server.get("MCP_OAUTH_AUDIENCE") == demo.keycloak_audience:
            env.server["MCP_OAUTH_AUDIENCE"] = replace(demo, public_base_url=local).keycloak_audience
            changed.append("MCP_OAUTH_AUDIENCE")
    url = stack.public_url(env)
    if url == demo.mcp_url:
        return Binding(demo)
    moved = f" ({' and '.join(changed)} moved with it)" if changed else ""
    note = f"The demo stack follows {env.name!r} to {url}{moved}: run {CLI_NAME} start-demo to render the realm again."
    return Binding(replace(demo, public_base_url=url), [note])


def refusals(env: Environment, binding: Binding | None) -> list[CliError]:
    """Return why a demo-bound environment cannot run as configured, each with the command that fixes it.

    TLS without a provided CA-issued certificate is refused, and so is Keycloak on another MCP_PATH. This is
    the one place a refusal is paired with its fix: `start` raises the first, `guide` names it, `configure`
    refuses a change that would cause one, and `configure-demo --for` refuses the binding.

    Args:
        env (Environment): The environment.
        binding (Binding | None): Its binding to the demo stack, if any.

    Returns:
        list[CliError]: The refusals, each with its next command; empty when the environment is not bound or may run.
    """
    if binding is None:
        return []
    found: list[CliError] = []
    if refusal := stack.self_signed_refusal(env):
        found.append(CliError(ExitCode.USAGE, refusal, f"{CLI_NAME} configure {env.name} --certs <directory>"))
    if refusal := _path_refusal(env):
        found.append(CliError(ExitCode.USAGE, refusal, f"{CLI_NAME} configure {env.name} --unset MCP_PATH"))
    return found


def _path_refusal(env: Environment) -> str | None:
    """Return why Keycloak tokens would fail the audience check: an MCP_PATH other than the realm's; None if not."""
    path = env.server.get("MCP_PATH") or stack.KEYCLOAK_MCP_PATH
    if env.server.get("MCP_OAUTH_PROVIDER", "").strip().lower() != "keycloak" or (
        path.rstrip("/") == stack.KEYCLOAK_MCP_PATH
    ):
        return None
    return (
        f"Environment {env.name!r} serves MCP on MCP_PATH={path}, but the demo Keycloak realm puts "
        f"{stack.KEYCLOAK_MCP_PATH} in its tokens' audience, so every token would be refused. Unset MCP_PATH, "
        "or bind with --demo dex."
    )


def refuse_self_signed(run: StartRun) -> None:
    """Start step: refuse a demo-bound environment that would serve a self-signed certificate or another MCP_PATH.

    Args:
        run (StartRun): The start in progress.

    Raises:
        CliError: The first of refusals(): the environment is bound and its TLS has no provided CA-issued
            certificate, or Keycloak is on another MCP_PATH; or the demo settings file cannot be read.
    """
    if found := refusals(run.env, binding_of(run.env)):
        first = found[0]
        raise CliError(first.exit_code, first.message, first.next_command)


def status_check(env: Environment, ports: Ports, report: StatusReport) -> None:
    """Status check: record which environment the local stack is bound to, and ask a remote stack from here.

    Args:
        env (Environment): The environment.
        ports (Ports): The ports; the probe asks a remote stack.
        report (StatusReport): The report to fill.
    """
    report.demo_bound_environment, notice = status_binding()
    if notice:
        report.notices.append(notice)
    if (described := remote.describe(env)) is not None:
        report.demo_stack = described
        report.notices += remote.check(env, ports.probe).notices
        # The default read token only: configure already said that the network reaches the stack.
        if (token := remote.default_token_warning(env)) is not None:
            report.notices.append(token)


def refuse_bound_removal(run: RemoveRun) -> None:
    """Pre-remove check: refuse the demo-bound environment unless `--force`, which also unbinds.

    Args:
        run (RemoveRun): The removal in progress.

    Raises:
        CliError: The environment is bound and `--force` was not given.
    """
    if bound_environment() != run.env.name:
        return
    if not run.options.force:
        raise CliError(
            ExitCode.DEMO_ENVIRONMENT_BOUND,
            f"Environment {run.env.name!r} is bound to the demo stack; removing it would leave the stack pointing "
            "at nothing. --force removes it and unbinds the stack.",
            f"{CLI_NAME} remove {run.env.name} --force",
        )
    run.unbinds_demo = True


def unbind_removed(run: RemoveRun) -> None:
    """Remove step: unbind the demo stack from the environment being removed (`--force`).

    Args:
        run (RemoveRun): The removal in progress.
    """
    if not run.unbinds_demo:
        return
    demo = stack.load_settings()
    if demo is not None:
        stack.save_settings(demo.unbind())
    run.notes.append(
        "The demo stack is no longer bound to an environment; bind another with: "
        f"{CLI_NAME} configure-demo --for <environment>"
    )
