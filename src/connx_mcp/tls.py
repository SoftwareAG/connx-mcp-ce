#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tls.py - TLS in the server lifecycle: certificate expiry in status and start, renewals, restarts.

Description:
    The lifecycle registers what lives here: certificate_facts is a status check that reads the
    served certificate through the endpoint probe, rates its expiry (certs.expiry) and compares it
    with the one in the certificate directory, so `status` and `start` warn in time and report a
    renewed certificate not yet served. hint_expired_generated wraps the start's wait: when the
    server exits and its generated self-signed certificate in the environment's certificate volume
    has expired, the failure says how to purge that volume and start again, so the server generates
    a new one. Whether the server refuses an expired generated certificate or refreshes it in place
    at startup is unconfirmed (a live run decides), so the hint says only that purging replaces it,
    never that the expiry caused the exit. certificate_change decides
    `restart --if-cert-changed`, so an ACME client's deploy hook restarts only for a new certificate.

Copyright:
    Copyright 2026 Software GmbH
    Licensed under the Apache License, Version 2.0 (the "License"); you may not use this file except in compliance
    with the License. You may obtain a copy of the License at https://www.apache.org/licenses/LICENSE-2.0
    Unless required by applicable law or agreed to in writing, software distributed under the License is distributed
    on an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the License
    for the specific language governing permissions and limitations under the License.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from connx_mcp import CLI_NAME, certs, compose, settings, start_inputs
from connx_mcp.certs import Certificate, CertificateError, Level
from connx_mcp.environment import Environment
from connx_mcp.output import CliError, ExitCode
from connx_mcp.ports import Ports

if TYPE_CHECKING:
    from connx_mcp.lifecycle import StartRun, StartStep, StatusReport

PROBE_HOST = "localhost"


def now() -> datetime:
    """Return the current time in UTC: certificate expiry is judged on the wall clock."""
    return datetime.now(UTC)


def enabled(env: Environment) -> bool:
    """Return whether the environment's server settings turn TLS on.

    Args:
        env (Environment): The environment.

    Returns:
        bool: True when `MCP_TLS_ENABLED` is true in any spelling the server takes (`settings.tls_on`).
    """
    return settings.tls_on(env.server)


def certificate_volume(env: Environment) -> str:
    """Return the name of the environment's certificate volume.

    Args:
        env (Environment): The environment.

    Returns:
        str: `connx-mcp-ce-<name>-certs`.
    """
    return f"{compose.project_name(env.name)}-certs"


def purge_command(env: Environment) -> str:
    """Return the commands that purge the certificate volume and start again, so the server generates a new one.

    Args:
        env (Environment): The environment.

    Returns:
        str: The commands, joined with `&&`; the container goes first because Docker keeps a volume in use.
    """
    container = compose.project_name(env.name)
    return (
        f"{CLI_NAME} stop {env.name} && docker rm {container} && "
        f"docker volume rm {certificate_volume(env)} && {CLI_NAME} start {env.name}"
    )


def _purge_hint(env: Environment) -> str:
    """Explain how a generated certificate is renewed."""
    return (
        "It is the server's generated self-signed certificate; to replace it, purge the environment's "
        f"certificate volume and start again, and the server generates a new one: {purge_command(env)}"
    )


def _directory(env: Environment) -> Path | None:
    """Return the certificate directory, or None when the named volume is used."""
    return Path(env.host.cert_dir) if env.host.cert_dir else None


def _primary(env: Environment) -> str | None:
    """Return the primary TLS host."""
    return certs.primary_host(env.server.get("MCP_TLS_HOSTS"))


# --- status and start: the served certificate -----------------------------------------------------------------


def _served(env: Environment, ports: Ports) -> Certificate | None:
    """Read the certificate the server serves on its published port, or None."""
    der = ports.probe.served_certificate(PROBE_HOST, compose.published_port(env))
    if der is None:
        return None
    try:
        return certs.from_der(der)
    except CertificateError:
        return None


def _running_directory(env: Environment) -> Path | None:
    """Return the certificate directory the container was started with: a `--certs` override, else the stored one."""
    started = start_inputs.started_cert_dir(env)
    return Path(started) if started else _directory(env)


def _expiry_notice(env: Environment, served: Certificate, moment: datetime) -> str | None:
    """Return the notice for a served certificate near or past its expiry, or None."""
    level = certs.expiry(served, moment)
    if level is Level.OK:
        return None
    when = f"{certs.describe_left(served, moment)} ({served.not_after:%Y-%m-%d %H:%M} UTC)"
    share = {Level.WARNING: "under a third of", Level.CRITICAL: "under a tenth or a day of"}.get(level)
    lifetime = f"its {served.lifetime.days}-day lifetime" if served.lifetime.days else "its lifetime"
    text = f"{level.capitalize()}: the served certificate {when}" + (f", {share} {lifetime} left." if share else ".")
    directory = _running_directory(env)
    if directory is None and served.self_signed:
        return f"{text} {_purge_hint(env)}"
    if directory is not None:
        return f"{text} Renew it in {directory}, then run: {CLI_NAME} restart {env.name} --if-cert-changed"
    return text


def certificate_facts(env: Environment, ports: Ports, report: StatusReport) -> None:
    """Status check: the served certificate's expiry, a renewed one not yet served, an expired generated one.

    Args:
        env (Environment): The environment.
        ports (Ports): The ports.
        report (StatusReport): The report to fill; its container state is already known.
    """
    if not enabled(env):
        return
    moment = now()
    served = _served(env, ports) if report.state == "running" else None
    directory = _running_directory(env)
    disk = certs.on_disk(directory, _primary(env)) if directory is not None else None
    renewed = served is not None and disk is not None and disk.der != served.der
    if served is None:
        summary = f"none served (the container is {report.state})"
    else:
        summary = f"{certs.describe_left(served, moment)} ({served.not_after:%Y-%m-%d %H:%M} UTC)"
    report.certificate = {
        "served": certs.to_json(served, moment) if served is not None else None,
        "on_disk": certs.to_json(disk, moment) if disk is not None else None,
        "directory": str(directory) if directory is not None else None,
        "volume": certificate_volume(env) if directory is None else None,
        "renewed_not_served": renewed,
        "summary": summary,
    }
    if served is not None and (notice := _expiry_notice(env, served, moment)):
        report.notices.append(notice)
    if report.state == "exited" and directory is None and (notice := _expired_generated_notice(env, ports, moment)):
        report.notices.append(notice)
    if renewed:
        report.notices.append(
            f"The certificate in {directory} differs from the served one (renewed?); serve it with: "
            f"{CLI_NAME} restart {env.name} --if-cert-changed"
        )


# --- start: an expired generated certificate ------------------------------------------------------------------


def _expired_generated_notice(env: Environment, ports: Ports, moment: datetime) -> str | None:
    """Return the purge notice when the exited server's generated certificate has expired, or None."""
    generated = _generated_certificate(env, ports)
    if generated is None or certs.expiry(generated, moment) is not Level.EXPIRED:
        return None
    return f"The generated certificate expired on {generated.not_after:%Y-%m-%d %H:%M} UTC. {_purge_hint(env)}"


def _generated_certificate(env: Environment, ports: Ports) -> Certificate | None:
    """Read the generated certificate from the environment's certificate volume with the pinned image, or None."""
    if env.host.image_digest is None:
        return None
    result = ports.docker.run(
        [
            "run", "--rm", "--pull", "never", "--platform", "linux/amd64", "--entrypoint", "sh",
            "-v", f"{certificate_volume(env)}:{compose.CERTS_TARGET}:ro",
            env.host.image_digest, "-c", f"cat {compose.CERTS_TARGET}/*.crt",
        ]
    )  # fmt: skip
    if result.returncode != 0:
        return None
    try:
        return certs.from_pem(result.stdout.encode("ascii", "replace"))
    except CertificateError:
        return None


def hint_expired_generated(step: StartStep) -> StartStep:
    """Wrap the start's wait: an exit caused by an expired generated certificate gets the purge hint.

    Args:
        step (StartStep): The wait step.

    Returns:
        StartStep: The same step, whose early exit names the purge when the generated certificate has expired.
    """

    def wait(run: StartRun) -> None:
        """Run the wait; on an early exit with TLS on and no certificate directory, check the generated one."""
        try:
            step(run)
        except CliError as error:
            if error.exit_code is not ExitCode.CONTAINER_EXITED or not enabled(run.env) or run.env.host.cert_dir:
                raise
            generated = _generated_certificate(run.env, run.ports)
            if generated is None or certs.expiry(generated, now()) is not Level.EXPIRED:
                raise
            raise CliError(
                ExitCode.CERTIFICATE_EXPIRED,
                f"{error.message}\nThe generated certificate expired on "
                f"{generated.not_after:%Y-%m-%d %H:%M} UTC. {_purge_hint(run.env)}",
                purge_command(run.env),
            ) from error

    wait.__doc__ = step.__doc__
    return wait


# --- restart --if-cert-changed --------------------------------------------------------------------------------


@dataclass(frozen=True)
class CertificateChange:
    """Whether the certificate on disk differs from the served one.

    Attributes:
        differs (bool): A restart would serve a different certificate.
        reason (str): Why, in words.
    """

    differs: bool
    reason: str


def certificate_change(env: Environment, ports: Ports) -> CertificateChange:
    """Compare the certificate in the certificate directory with the one the server serves.

    Args:
        env (Environment): The environment.
        ports (Ports): The ports.

    Returns:
        CertificateChange: Differs only when the server serves a certificate and the directory holds another.

    Raises:
        CliError: There is no certificate directory, or the server would refuse the one on disk.
    """
    directory = _directory(env)
    if directory is None:
        raise CliError(
            ExitCode.CERTIFICATE_DIRECTORY_NOT_SET,
            f"Environment {env.name!r} has no certificate directory, so there is no certificate on disk to compare: "
            "its server generates its own in the certificate volume.",
            f"{CLI_NAME} configure {env.name} --certs <directory>",
        )
    if reason := certs.directory_problem(directory, _primary(env), now()):
        raise CliError(
            ExitCode.CERTIFICATE_DIRECTORY_INVALID,
            f"Not restarting {compose.project_name(env.name)}: the server would refuse its certificate directory: "
            f"{reason}.",
            f"{CLI_NAME} status {env.name}",
        )
    disk = certs.on_disk(directory, _primary(env))
    served = _served(env, ports)
    if served is None:
        return CertificateChange(False, "The server serves no certificate (not running, or TLS is off): not restarted.")
    if disk is None or disk.der == served.der:
        return CertificateChange(False, "The served certificate is the one on disk: not restarted.")
    return CertificateChange(True, f"The certificate in {directory} differs from the served one.")


def restart_skipped(env: Environment, change: CertificateChange) -> tuple[dict[str, Any], str]:
    """Return the result of a `restart --if-cert-changed` that did not restart.

    Args:
        env (Environment): The environment.
        change (CertificateChange): Why not.

    Returns:
        tuple[dict[str, Any], str]: The JSON result and its text.
    """
    container = compose.project_name(env.name)
    return {"environment": env.name, "container": container, "restarted": False, "reason": change.reason}, change.reason
