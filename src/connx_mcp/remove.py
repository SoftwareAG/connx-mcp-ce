#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""remove.py - `remove <env>`: take an environment's server down and delete the environment.

Description:
    A removal runs PRE_REMOVE_CHECKS (refusals before anything is asked or changed: the demo-bound
    environment without `--force`), then REMOVE_STEPS over one RemoveRun: confirm (or `--yes`),
    `docker compose down` (stops the server and removes its container; when `compose.yml` is gone,
    the project's containers are found by their compose label and taken down by project name),
    keep or `--purge` the project's volumes (certificates, OAuth state), unbind the demo stack
    (`--force`), delete the environment's directory. The directory goes last, so a failed Docker
    step leaves everything needed to run `remove` again.
    Images are never removed: other environments may run the same one. Nor are the skill copies,
    skill instructions sections and zips install-skill wrote: before the record goes with the
    directory, the run lists those still in place with the uninstall-skill commands that remove them.

Copyright:
    Copyright 2026 Software GmbH
    Licensed under the Apache License, Version 2.0 (the "License"); you may not use this file except in compliance
    with the License. You may obtain a copy of the License at https://www.apache.org/licenses/LICENSE-2.0
    Unless required by applicable law or agreed to in writing, software distributed under the License is distributed
    on an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the License
    for the specific language governing permissions and limitations under the License.
"""

from __future__ import annotations

import shutil
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import questionary
from prompt_toolkit import print_formatted_text

from connx_mcp import CLI_NAME, compose, skill
from connx_mcp.demo import bind
from connx_mcp.docker import query
from connx_mcp.environment import Environment
from connx_mcp.lifecycle import DEFAULT_STOP_TIMEOUT, container_name, load_configured
from connx_mcp.output import CliError, ExitCode, Output
from connx_mcp.ports import Ports, Terminal


@dataclass
class RemoveOptions:
    """What one `remove` call asks for.

    Attributes:
        yes (bool): Skip the confirmation.
        purge (bool): Also remove the project's volumes (certificates, OAuth state).
        force (bool): Remove the environment the demo stack is bound to, and unbind the stack.
    """

    yes: bool = False
    purge: bool = False
    force: bool = False


@dataclass(frozen=True)
class SharedEntry:
    """A skill copy or section the removed environment installed, with the other environments that also use it.

    Attributes:
        entry (skill.RecordedCopy | skill.RecordedInstructions): The record's entry.
        claimants (list[skill.Claimant]): The other environments whose records claim its path; empty when none.
    """

    entry: skill.RecordedCopy | skill.RecordedInstructions
    claimants: list[skill.Claimant]


@dataclass
class RemoveRun:
    """The state one removal shares between its checks and steps.

    Attributes:
        env (Environment): The environment being removed.
        ports (Ports): The ports.
        out (Output): Where progress goes.
        options (RemoveOptions): The command line's options.
        terminal (Terminal | None): Where to confirm; None when prompting is not allowed.
        volumes (list[str]): The project's volumes, found before confirming.
        volumes_removed (list[str]): The volumes `--purge` removed.
        containers (list[str] | None): Without `compose.yml`, the project's containers found by label;
            None while `compose.yml` is there (compose down finds them itself).
        taken_down (bool): Whether `docker compose down` ran.
        unbinds_demo (bool): The demo stack is bound to the environment and `--force` unbinds it.
        notes (list[str]): Lines for the result.
        skill_kept (skill.InstallRecord): The skill copies, sections and zips still in place, which stay;
            the shared ones are in `skill_shared` instead.
        skill_shared (list[SharedEntry]): The skill copies and sections still in place that another
            environment also uses: its uninstall-skill keeps them, so no command is listed for them.
    """

    env: Environment
    ports: Ports
    out: Output
    options: RemoveOptions
    terminal: Terminal | None = None
    volumes: list[str] = field(default_factory=list)
    volumes_removed: list[str] = field(default_factory=list)
    containers: list[str] | None = None
    taken_down: bool = False
    unbinds_demo: bool = False
    notes: list[str] = field(default_factory=list)
    skill_kept: skill.InstallRecord = field(default_factory=skill.InstallRecord)
    skill_shared: list[SharedEntry] = field(default_factory=list)

    @property
    def retry(self) -> str:
        """The command that repeats this removal without a question."""
        flags = [flag for flag, on in (("--purge", self.options.purge), ("--force", self.options.force)) if on]
        return " ".join([f"{CLI_NAME} remove {self.env.name} --yes", *flags])

    @property
    def has_compose_file(self) -> bool:
        """Whether the environment has the compose file a start writes."""
        return (self.env.directory / compose.COMPOSE_FILE).is_file()


RemoveStep = Callable[[RemoveRun], None]

# Refusals that run before the confirmation and before anything changes; each raises CliError to refuse.
PRE_REMOVE_CHECKS: list[RemoveStep] = [bind.refuse_bound_removal]


def _project_volumes(run: RemoveRun) -> None:
    """Find the volumes compose created for the environment's project."""
    project = compose.project_name(run.env.name)
    result = run.ports.docker.run(
        ["volume", "ls", "--quiet", "--filter", f"label=com.docker.compose.project={project}"]
    )
    if result.returncode != 0:
        raise CliError(ExitCode.DOCKER_FAILED, f"docker volume ls for {project} failed: {result.stderr.strip()}")
    run.volumes = sorted(line.strip() for line in result.stdout.splitlines() if line.strip())


def _project_containers(run: RemoveRun) -> None:
    """Without `compose.yml`, find the project's containers by their compose label, before confirming."""
    if run.has_compose_file:
        return
    project = compose.project_name(run.env.name)
    result = query(
        run.ports.docker, ["ps", "--all", "--quiet", "--filter", f"label=com.docker.compose.project={project}"]
    )
    if result.returncode != 0:
        raise CliError(ExitCode.DOCKER_FAILED, f"docker ps for {project} failed: {result.stderr.strip()}", run.retry)
    run.containers = sorted(line.strip() for line in result.stdout.splitlines() if line.strip())


def _require_confirmable(run: RemoveRun) -> None:
    """Refuse at once when there is neither `--yes` nor a terminal to ask on."""
    if not run.options.yes and run.terminal is None:
        raise CliError(
            ExitCode.INPUT_REQUIRED,
            f"Removing environment {run.env.name!r} needs a confirmation; pass --yes to remove without asking.",
            run.retry,
        )


def _skill_kept(run: RemoveRun) -> None:
    """Read the install record while it exists: what install-skill wrote stays, so the result lists it.

    An unreadable record becomes a note; the removal goes on.
    """
    try:
        record = skill.read_record(run.env)
    except CliError as error:
        run.notes.append(
            f"{error.message} The skill copies and skill instructions it lists are kept and not listed; "
            f"remove them with: {_generic_command(run.env.name)}"
        )
        return
    if record is None:
        return
    kept = skill.still_in_place(record)

    def user_first[T: (skill.RecordedCopy, skill.RecordedInstructions)](entries: tuple[T, ...]) -> tuple[T, ...]:
        claimed = (
            e
            if e.harnesses
            else replace(e, harnesses=skill.harnesses_at(e.path, e.project, copy=isinstance(e, skill.RecordedCopy)))
            for e in entries
        )
        return tuple(sorted(claimed, key=lambda entry: entry.project is not None))

    others = skill.other_records(run.env.name)
    run.notes += others.unreadable
    copies = [SharedEntry(c, skill.claimants(c.path, others.records, copy=True)) for c in user_first(kept.copies)]
    files = [SharedEntry(e, skill.claimants(e.path, others.records, copy=False)) for e in user_first(kept.instructions)]
    run.skill_shared = [s for s in copies + files if s.claimants]
    run.skill_kept = skill.InstallRecord(
        tuple(s.entry for s in copies if not s.claimants and isinstance(s.entry, skill.RecordedCopy)),
        kept.zips,
        tuple(s.entry for s in files if not s.claimants and isinstance(s.entry, skill.RecordedInstructions)),
    )


def _generic_command(name: str) -> str:
    """Return the uninstall-skill command for a copy no harness is known for, to complete by hand."""
    request = skill.UninstallRequest(harnesses=[], project=None, user=True, yes=False, terminal=None)
    return f"{request.command(name, [skill.HARNESS_PLACEHOLDER])} (or --project DIR)"


def skill_commands(name: str, kept: skill.InstallRecord) -> list[str]:
    """Return one uninstall-skill command per scope, naming each harness that installed there.

    Each command names its harnesses and scope, so it works once the environment and its record are gone.
    Claude Desktop, which only a zip reaches, joins the user scope. An entry no harness uses is left out;
    _skill_lines gives it the generic command.

    Args:
        name (str): The environment name.
        kept (skill.InstallRecord): The skill copies, sections and zips still in place.

    Returns:
        list[str]: The commands, the user scope first, then each project in the record's order.
    """
    groups: dict[Path | None, list[str]] = {}
    for entry in [e for e in [*kept.copies, *kept.instructions] if e.harnesses]:
        harnesses = groups.setdefault(entry.project, [])
        harnesses += [h for h in entry.harnesses if h not in harnesses]
    if any("claude-desktop" in z.harnesses for z in kept.zips):
        groups.setdefault(None, []).append("claude-desktop")
    order = sorted(groups, key=lambda project: project is not None)
    return [
        skill.UninstallRequest(harnesses=[], project=project, user=project is None, yes=False, terminal=None).command(
            name, groups[project]
        )
        for project in order
    ]


def _confirm(run: RemoveRun) -> None:
    """Ask before removing, unless `--yes`."""
    if run.options.yes or run.terminal is None:
        return
    with run.terminal.session():
        print_formatted_text("\n".join(_plan(run)))
        try:
            agreed = questionary.confirm(f"Remove environment {run.env.name!r}?", default=False).unsafe_ask()
        except KeyboardInterrupt, EOFError:
            agreed = False
    if not agreed:
        raise CliError(ExitCode.CANCELLED, f"Environment {run.env.name!r} was not removed.")


def _plan(run: RemoveRun) -> list[str]:
    """Describe what the removal does, for the confirmation."""
    if run.containers == []:
        lines = [f"Environment {run.env.name!r} has no container to stop."]
    else:
        lines = [f"Container {container_name(run.env)} is stopped and removed."]
    lines.append(f"Directory {run.env.directory} is deleted with its settings.")
    if run.unbinds_demo:
        lines.append("The demo stack is unbound from it (--force).")
    if run.volumes:
        fate = "deleted (--purge)" if run.options.purge else "kept"
        lines.append(f"Volumes {', '.join(run.volumes)} are {fate}.")
    lines.append("Images are kept.")
    return [*lines, *_skill_lines(run, "are kept", "is"), *run.notes]


def _skill_lines(run: RemoveRun, fate: str, verb: str) -> list[str]:
    """Describe the skill copies, sections and zips the removal keeps, with the commands that remove them.

    Args:
        run (RemoveRun): The removal.
        fate (str): What happens to them, after the heading's subject: `are kept` or `were kept`.
        verb (str): The verb for one kept shared copy, agreeing with `fate`: `is` or `was`.

    Returns:
        list[str]: The heading and one line each; none when nothing is kept.
    """
    kept = run.skill_kept
    if not (kept.copies or kept.instructions or kept.zips or run.skill_shared):
        return []
    lines = [f"The skill copies and skill instructions install-skill wrote for environment {run.env.name} {fate}:"]
    generic = f": no harness reads it; remove it with: {_generic_command(run.env.name)}"

    def claims(entry: skill.RecordedCopy | skill.RecordedInstructions) -> str:
        return f" ({', '.join(entry.harnesses)})" if entry.harnesses else generic

    lines += [f"  Skill copy {c.path}{claims(c)}" for c in kept.copies]
    lines += [f"  Skill instructions section in {e.path}{claims(e)}" for e in kept.instructions]
    lines += [f"  Zip {z.path}: delete it and {skill.NOTES_NAME} beside it yourself" for z in kept.zips]
    for shared in run.skill_shared:
        what = "Skill copy" if isinstance(shared.entry, skill.RecordedCopy) else "Skill instructions section in"
        # Only uninstall-skill --shared removes a shared copy, so no generic command is named for it.
        named = f" ({', '.join(shared.entry.harnesses)})" if shared.entry.harnesses else ""
        kept_phrase = skill.kept_by((), [c.environment for c in shared.claimants])
        lines.append(f"  {what} {shared.entry.path}{named} {verb} {kept_phrase}")
    commands = skill_commands(run.env.name, kept)
    if commands:
        lines.append("Remove them with:")
        lines += [f"  {command}" for command in commands]
    return lines


def _compose_down(run: RemoveRun) -> None:
    """Stop the server and remove its container; volumes and images stay.

    Without `compose.yml` (never started, or the file was lost) compose is addressed by project name
    alone, and only when the project still has containers.
    """
    if run.has_compose_file:
        target = compose.compose_args(run.env)
    elif run.containers:
        target = ["compose", "-p", compose.project_name(run.env.name)]
    else:
        return
    run.out.progress(f"Taking {container_name(run.env)} down ...")
    result = run.ports.docker.run([*target, "down", "--timeout", str(DEFAULT_STOP_TIMEOUT), "--remove-orphans"])
    if result.returncode != 0:
        raise CliError(
            ExitCode.DOCKER_FAILED,
            f"docker compose down for {container_name(run.env)} failed: {result.stderr.strip()}",
            run.retry,
        )
    run.taken_down = True


def _purge_volumes(run: RemoveRun) -> None:
    """With `--purge`, remove the project's volumes."""
    if not run.options.purge or not run.volumes:
        return
    run.out.progress(f"Removing volumes {', '.join(run.volumes)} ...")
    result = run.ports.docker.run(["volume", "rm", *run.volumes])
    if result.returncode != 0:
        raise CliError(ExitCode.DOCKER_FAILED, f"docker volume rm failed: {result.stderr.strip()}", run.retry)
    run.volumes_removed = list(run.volumes)


def _delete_directory(run: RemoveRun) -> None:
    """Delete the environment's directory: settings, compose files and anything else in it."""
    try:
        shutil.rmtree(run.env.directory)
    except OSError as error:
        server = "The server is down" if run.taken_down else "The environment had no container"
        raise CliError(
            ExitCode.REMOVE_FAILED,
            f"{server}, but directory {run.env.directory} could not be deleted: {error}",
            run.retry,
        ) from error


REMOVE_STEPS: list[RemoveStep] = [
    _require_confirmable,
    _skill_kept,
    _project_volumes,
    _project_containers,
    _confirm,
    _compose_down,
    _purge_volumes,
    bind.unbind_removed,
    _delete_directory,
]


def run_remove(
    env: Environment, ports: Ports, out: Output, options: RemoveOptions, terminal: Terminal | None
) -> RemoveRun:
    """Remove an environment: every PRE_REMOVE_CHECKS entry, then every REMOVE_STEPS entry, in order.

    Args:
        env (Environment): A configured environment.
        ports (Ports): The ports.
        out (Output): Where progress goes.
        options (RemoveOptions): The command line's options.
        terminal (Terminal | None): Where to confirm; None when prompting is not allowed.

    Returns:
        RemoveRun: The finished run.
    """
    run = RemoveRun(env=env, ports=ports, out=out, options=options, terminal=terminal)
    for step in [*PRE_REMOVE_CHECKS, *REMOVE_STEPS]:
        step(run)
    return run


def _result(run: RemoveRun) -> tuple[dict[str, Any], str]:
    """Return a finished removal's result and its text."""
    kept = [v for v in run.volumes if v not in run.volumes_removed]
    data = {
        "environment": run.env.name,
        "container": container_name(run.env),
        "container_taken_down": run.taken_down,
        "removed": True,
        "volumes_kept": kept,
        "volumes_removed": run.volumes_removed,
        "notes": run.notes,
        "skill_kept": {
            "copies": [c.to_json() for c in run.skill_kept.copies],
            "instructions": [
                {"path": str(e.path), "harnesses": list(e.harnesses), "project": e.project and str(e.project)}
                for e in run.skill_kept.instructions
            ],
            "zips": [z.to_json() for z in run.skill_kept.zips],
            "shared": [
                {
                    "path": str(s.entry.path),
                    "harnesses": list(s.entry.harnesses),
                    "project": s.entry.project and str(s.entry.project),
                    "claimants": [c.to_json() for c in s.claimants],
                }
                for s in run.skill_shared
            ],
            "commands": skill_commands(run.env.name, run.skill_kept),
        },
    }
    lines = [f"Removed environment {run.env.name!r}; images are kept."]
    if run.volumes_removed:
        lines.append(f"Removed volumes {', '.join(run.volumes_removed)}.")
    if kept:
        lines.append(
            f"Kept volumes {', '.join(kept)}; an environment of the same name reuses them. "
            f"Delete them with: docker volume rm {' '.join(kept)}"
        )
    return data, "\n".join([*lines, *_skill_lines(run, "were kept", "was"), *run.notes])


def remove(
    ports: Ports, out: Output, given_name: str | None, options: RemoveOptions, terminal: Terminal | None
) -> None:
    """Run `remove`.

    Args:
        ports (Ports): The ports.
        out (Output): Where the result goes.
        given_name (str | None): The environment name on the command line.
        options (RemoveOptions): The command line's options.
        terminal (Terminal | None): Where to confirm; None when prompting is not allowed.
    """
    run = run_remove(load_configured(given_name, "remove"), ports, out, options, terminal)
    out.result(*_result(run))
