#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""image.py - The image lifecycle: which image an environment runs, pulling it, and pinning it to a digest.

Description:
    An environment names an image reference: its host settings' image, else the default
    `<registry>/<name>:<tag>`, whose parts CONNX_MCP_REGISTRY, CONNX_MCP_IMAGE and CONNX_MCP_IMAGE_TAG
    each replace at each call (set_variables, the one place they are read). The choice (ImageChoice)
    holds the registry, name and tag, each with its own source. An environment's first start resolves
    that reference to the local image's repo digest and records the digest and the image's version
    label in host settings; every later start renders
    the digest, so a tag moved by a pull for another environment never upgrades this one.
    `update` re-pins deliberately. A pull the registry refuses names `connx-mcp-ce login` and where
    credentials come from (REGISTRY_HINT, the one place that text lives); every image failure adds a
    source line naming each part and where it came from (ImageChoice.line). Each CLI release declares
    the oldest image version it supports and the newest series it knows: `start` and `update`
    refuse an older image and warn about a newer one. `status` shows the pinned version and digest
    and flags a pulled image the environment does not run, or a container not running the pin.
    Variables an environment's own image ignores are named by `start`, `download`, `update` and
    `status`; `start` says when they name another image than a pinned environment runs.

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
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any

from connx_mcp import CLI_NAME
from connx_mcp.docker import DockerPort, query
from connx_mcp.environment import Environment
from connx_mcp.output import CliError, ExitCode, Output
from connx_mcp.settings import IMAGE_HOST, IMAGE_NAME, IMAGE_REFERENCE, IMAGE_REGISTRY, IMAGE_TAG, REGISTRY_FIRST

if TYPE_CHECKING:
    from connx_mcp.lifecycle import StartRun, StatusReport
    from connx_mcp.ports import Ports

# The built-in default image: only the fallback of the variables below; everything else asks default_choice().
DEFAULT_REGISTRY = "softwareagcr.azurecr.io"
DEFAULT_NAME = "connx-mcp-server-ce"
DEFAULT_TAG = "15.0"
DEFAULT_IMAGE = f"{DEFAULT_REGISTRY}/{DEFAULT_NAME}:{DEFAULT_TAG}"
# The variables that each change one part of the default image, read at each call. An environment's own image
# (`configure --image`) beats them; a pinned digest is never changed by them.
REGISTRY_VARIABLE = "CONNX_MCP_REGISTRY"
NAME_VARIABLE = "CONNX_MCP_IMAGE"
TAG_VARIABLE = "CONNX_MCP_IMAGE_TAG"
# The tag variable's old name: refused when set, so a stale CI variable fails loudly instead of being ignored.
RENAMED_TAG_VARIABLE = "CONNX_MCP_IMAGE_VERSION"
IMAGE_VARIABLES = (REGISTRY_VARIABLE, NAME_VARIABLE, TAG_VARIABLE)
VARIABLE_NAMES = f"{REGISTRY_VARIABLE}, {NAME_VARIABLE} and {TAG_VARIABLE}"
SOURCE_ENVIRONMENT = "environment (configure --image)"
SOURCE_BUILT_IN = "built-in default"
SOURCE_REGISTRY_FLAG = "--registry"
VARIABLES_HELP = (
    f"  {REGISTRY_VARIABLE:<24} The registry of the default image: a host, with an optional path\n"
    f"  {'':<24} (default {DEFAULT_REGISTRY}; for example registry.example:5000/connx).\n"
    f"  {NAME_VARIABLE:<24} The name of the default image, with an optional path\n"
    f"  {'':<24} (default {DEFAULT_NAME}; for example connx/connx-mcp-server-ce).\n"
    f"  {TAG_VARIABLE:<24} The tag of the default image (default {DEFAULT_TAG}; for example 15.0.1-rc.2).\n"
    "  The default image is <registry>/<name>:<tag>. Each variable changes only its own part.\n"
    "  An environment's own image (configure --image) has priority over them.\n"
    f"  A change to these variables applies to an environment at its first start or at `{CLI_NAME} update`.\n"
    "  Until then, the environment keeps its pinned image."
)
VERSION_LABEL = "org.opencontainers.image.version"
IMAGE_FORMAT = '{"id":{{json .Id}},"digests":{{json .RepoDigests}},"labels":{{json .Config.Labels}}}'
RUNNING_IMAGE_FORMAT = "{{.Image}}"

# The oldest image (major, minor, fix) this CLI release supports, and the newest (major, minor) series it knows.
MIN_SUPPORTED_VERSION = (15, 0, 0)
NEWEST_KNOWN_SERIES = (15, 0)

# Where registry credentials come from; the only place this text lives, so it can change in one edit. The
# registry-refusal hint and `login --help` both use it.
CREDENTIALS_SOURCE = "Empower or Software AG support"
REGISTRY_HINT = f"Sign in with `{CLI_NAME} login`; registry credentials come from {CREDENTIALS_SOURCE}."
_REFUSED = re.compile(
    r"unauthorized|\b401\b|authentication required|requested access to the resource is denied|pull access denied"
    r"|may require 'docker login'|no basic auth credentials",
    re.IGNORECASE,
)
_VERSION = re.compile(r"(\d+)\.(\d+)(?:\.(\d+))?")


@dataclass(frozen=True)
class PinnedImage:
    """A local image resolved to what compose can run unchanged for ever.

    Attributes:
        digest (str): `repository@sha256:...`, or the image ID for an image with no repo digest (built locally).
        version (str | None): The image's version label.
        reference (str): The reference it was resolved from; the pinned notice compares the default image with it.
    """

    digest: str
    version: str | None
    reference: str


@dataclass(frozen=True)
class LocalImage:
    """An image on this machine.

    Attributes:
        id (str): The image ID (`sha256:...`).
        digest (str): The repo digest matching the reference asked for, else the image ID.
        version (str | None): The image's version label.
    """

    id: str
    digest: str
    version: str | None


@dataclass(frozen=True)
class ImagePart:
    """One part of an image reference (its registry, name or tag) and where it came from.

    Attributes:
        value (str): The part; "" when the reference has none (a bare reference's registry, an untagged name).
        source (str): The variable that named it, the environment (configure --image) or the built-in default.
    """

    value: str
    source: str

    def described(self, label: str) -> str:
        """Return the part and its source in words: `<label> <value> (<source>)`."""
        return f"{label} {self.value} ({self.source})"

    def line(self, label: str) -> str:
        """Return the source line an error adds for this part alone: `<label>: <value> (<source>)`."""
        return f"{label}: {self.value} ({self.source})"


@dataclass(frozen=True)
class ImageChoice:
    """An image reference, built from a registry, a name and a tag that each record where they came from.

    Attributes:
        registry (ImagePart): The registry host, with any repository path after it.
        name (ImagePart): The repository name.
        tag (ImagePart): The tag.
        digest (str): The `sha256:...` digest a full reference carries; "" when it has none. It has no source of its
            own: only an environment's own image carries one, and that renders as one reference with one source.
        environment (str): The environment whose own image (configure --image) this is; "" for the default image.
    """

    registry: ImagePart
    name: ImagePart
    tag: ImagePart
    digest: str = ""
    environment: str = ""

    @classmethod
    def whole(cls, ref: str, source: str) -> ImageChoice:
        """Return the choice for a full reference whose three parts share one source.

        A full reference does not say where a registry path ends and the name begins, so its registry is the host
        alone and any path stays in the name; `host/team/name` and the variables' `host/team` + `name` give the same
        reference.

        Args:
            ref (str): A full image reference (settings.IMAGE_REFERENCE); it round-trips exactly through `reference`.
            source (str): Where the whole reference came from.

        Returns:
            ImageChoice: The reference split into its parts.
        """
        rest, _, digest = ref.partition("@")
        head, slash, tail = rest.partition("/")
        host = slash and IMAGE_HOST.fullmatch(head)
        host_part, path = (head, tail) if host else ("", rest)
        name, _, tag = path.partition(":")
        return cls(ImagePart(host_part, source), ImagePart(name, source), ImagePart(tag, source), digest)

    @property
    def reference(self) -> str:
        """The image reference: `<registry>/<name>:<tag>`, each part present only when it has a value."""
        ref = f"{self.registry.value}/{self.name.value}" if self.registry.value else self.name.value
        ref = f"{ref}:{self.tag.value}" if self.tag.value else ref
        return f"{ref}@{self.digest}" if self.digest else ref

    @property
    def sources(self) -> dict[str, str]:
        """Where each part came from: `{"registry": ..., "name": ..., "tag": ...}`."""
        return {"registry": self.registry.source, "name": self.name.source, "tag": self.tag.source}

    @property
    def origin(self) -> str:
        """Each part and its source in words; an environment's own image is one reference with one source."""
        if self.environment:
            return f"{self.reference} (environment {self.environment!r}, configure --image)"
        parts = (("registry", self.registry), ("name", self.name), ("tag", self.tag))
        return ", ".join(part.described(label) for label, part in parts)

    @property
    def line(self) -> str:
        """The source line an image error adds under its message: `image: <origin>`."""
        return f"image: {self.origin}"

    def pinned_line(self, pinned: str, environment: str) -> str:
        """Return the source lines of an image an earlier start or update pinned, and what `update` would pull now.

        The pin was chosen by whatever applied then, so the pin itself is its source; this choice is what applies now.

        Args:
            pinned (str): The pinned image (its digest).
            environment (str): The environment that pinned it.

        Returns:
            str: `image: <pinned> (pinned in environment '<env>' at an earlier start or update)` and
                `update pulls: <origin>`, on two lines.
        """
        return (
            f"image: {pinned} (pinned in environment {environment!r} at an earlier start or update)\n"
            f"update downloads: {self.origin}"
        )


# A registry host with a port pasted into CONNX_MCP_IMAGE: dotted or localhost, so `name:5000` stays a name and tag.
_PASTED_HOST = re.compile(r"(?:[A-Za-z0-9][A-Za-z0-9-]*(?:\.[A-Za-z0-9-]+)+|localhost):[0-9]+")
# An `@` that does not start a name's digest: user information (`user:password@host`) that may hold a credential.
_USERINFO = re.compile(r"@(?!sha256:[0-9a-f]{64}$)")


def set_variables(environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """Return the image variables that are set, and the renamed CONNX_MCP_IMAGE_VERSION: the one place they are read.

    They are read at each call, so a change applies at the next command. An empty variable counts as unset.

    Args:
        environ (Mapping[str, str] | None): The process environment; `os.environ` when None.

    Returns:
        dict[str, str]: Each set variable and its value, stripped.
    """
    environ = os.environ if environ is None else environ
    names = (*IMAGE_VARIABLES, RENAMED_TAG_VARIABLE)
    return {name: value for name in names if (value := environ.get(name, "").strip())}


def default_choice(environ: Mapping[str, str] | None = None) -> ImageChoice:
    """Return the default image reference, from the image variables (set_variables) at each call.

    CONNX_MCP_REGISTRY, CONNX_MCP_IMAGE and CONNX_MCP_IMAGE_TAG each replace only their own part of the built-in
    default; the reference is `<registry>/<name>:<tag>`. An empty variable counts as unset.

    Args:
        environ (Mapping[str, str] | None): The process environment; `os.environ` when None.

    Returns:
        ImageChoice: The reference, each part with the variable (or the built-in default) it came from.

    Raises:
        CliError: The renamed CONNX_MCP_IMAGE_VERSION is set, or a variable holds no valid registry, name or tag.
    """
    values = set_variables(environ)
    _refuse_renamed(values)
    for name, pattern, wanted, diagnose in _VARIABLE_CHECKS:
        if name in values and not pattern.fullmatch(values[name]):
            message, next_command = _refusal(name, values[name], wanted, diagnose)
            raise CliError(ExitCode.IMAGE_VARIABLE_INVALID, message, next_command)
    return ImageChoice(
        _part(values, REGISTRY_VARIABLE, DEFAULT_REGISTRY),
        _part(values, NAME_VARIABLE, DEFAULT_NAME),
        _part(values, TAG_VARIABLE, DEFAULT_TAG),
    )


def _refuse_renamed(values: Mapping[str, str]) -> None:
    """Refuse a set CONNX_MCP_IMAGE_VERSION, so a stale variable fails loudly wherever the image is read.

    Args:
        values (Mapping[str, str]): The set image variables (set_variables).

    Raises:
        CliError: The renamed variable is set.
    """
    if RENAMED_TAG_VARIABLE in values:
        raise CliError(
            ExitCode.IMAGE_VARIABLE_INVALID,
            f"{RENAMED_TAG_VARIABLE} was renamed to `{TAG_VARIABLE}`; set {TAG_VARIABLE} to its value instead.",
            f"unset {RENAMED_TAG_VARIABLE}",
        )


# Why a refused value is refused, and the command that fixes it: (message so far, value) -> (message, next command).
type _Diagnoser = Callable[[str, str], tuple[str, str]]


def _refusal(variable: str, value: str, wanted: str, diagnose: _Diagnoser) -> tuple[str, str]:
    """Return why an image variable's value is refused, and the command that fixes it.

    A value with user information (`user:password@host`) is never repeated, since it may hold a credential; any
    other value goes to its variable's diagnoser.

    Args:
        variable (str): The variable.
        value (str): Its value, which does not match its pattern.
        wanted (str): What the variable holds, in words.
        diagnose (_Diagnoser): The variable's diagnoser.

    Returns:
        tuple[str, str]: The message and the next command; neither repeats a value that may hold a credential.
    """
    if _USERINFO.search(value):
        return (
            f"{variable} is not {wanted}; its value is not repeated because it may hold a credential "
            f"(user:password@host). Set it without a user or password: `{CLI_NAME} login` holds those.",
            f"unset {variable}",
        )
    return diagnose(f"{variable}={value!r} is not {wanted}", value)


def _registry_refusal(message: str, value: str) -> tuple[str, str]:
    """Return why a registry is refused: a scheme or trailing `/` (the fix shows the corrected value), or a bad path.

    Args:
        message (str): The start of the message.
        value (str): The refused registry.

    Returns:
        tuple[str, str]: The message and the next command.
    """
    corrected = re.sub(r"^[A-Za-z][A-Za-z0-9+.-]*://", "", value).rstrip("/")
    if corrected != value and IMAGE_REGISTRY.fullmatch(corrected):
        return f"{message}: give it without a scheme or a trailing /.", f"export {REGISTRY_VARIABLE}={corrected}"
    host, _, path = value.partition("/")
    if path and IMAGE_HOST.fullmatch(host):
        return (
            f"{message}: its path {path} after the host {host} is not a repository path (lowercase letters "
            "and digits, separated by /, ., _ or -).",
            f"unset {REGISTRY_VARIABLE}",
        )
    return f"{message}: it names no registry host (a dotted name, localhost or a name with a port).", (
        f"unset {REGISTRY_VARIABLE}"
    )


def _name_refusal(message: str, value: str) -> tuple[str, str]:
    """Return why an image name is refused, naming the variable each misplaced part belongs in.

    A registry, tag or digest in the name is diagnosed; a pasted `host:port` is a registry, not a name and tag.

    Args:
        message (str): The start of the message.
        value (str): The refused name.

    Returns:
        tuple[str, str]: The message and the next command.
    """
    if _PASTED_HOST.fullmatch(value):
        return f"{message}: its registry {value} belongs in {REGISTRY_VARIABLE}, and it names no image.", (
            f"export {REGISTRY_VARIABLE}={value} && unset {NAME_VARIABLE}"
        )
    parts = ImageChoice.whole(value, "")
    host, name, tag = parts.registry.value, parts.name.value, parts.tag.value
    reasons = [
        f"its registry {host} belongs in {REGISTRY_VARIABLE}" if host else "",
        f"its tag {tag} belongs in {TAG_VARIABLE}" if tag else "",
        f"a digest is never set: `{CLI_NAME} start` and `update` pin one" if parts.digest else "",
    ]
    found = [reason for reason in reasons if reason]
    if not found:
        return f"{message}; correct or unset it.", f"unset {NAME_VARIABLE}"
    fixes = [
        (REGISTRY_VARIABLE, host, IMAGE_REGISTRY),
        (NAME_VARIABLE, name, IMAGE_NAME),
        (TAG_VARIABLE, tag, IMAGE_TAG),
    ]
    if name and all(not part or pattern.fullmatch(part) for _, part, pattern in fixes):
        next_command = "export " + " ".join(f"{variable}={part}" for variable, part, _ in fixes if part)
    else:
        next_command = f"unset {NAME_VARIABLE}"
    return f"{message}: {'; '.join(found)}.", next_command


def _tag_refusal(message: str, value: str) -> tuple[str, str]:  # noqa: ARG001
    """Return why a tag is refused: a tag has no common wrong shape to diagnose.

    Args:
        message (str): The start of the message.
        value (str): The refused tag (unused: the message already names it).

    Returns:
        tuple[str, str]: The message and the next command.
    """
    return f"{message}; correct or unset it.", f"unset {TAG_VARIABLE}"


# Each image variable, the pattern its value must match, what that is in words, and why a value is refused.
_VARIABLE_CHECKS: tuple[tuple[str, re.Pattern[str], str, _Diagnoser], ...] = (
    (
        REGISTRY_VARIABLE,
        IMAGE_REGISTRY,
        "a registry host such as registry.example:5000, optionally with a path",
        _registry_refusal,
    ),
    (
        NAME_VARIABLE,
        IMAGE_NAME,
        "an image name such as connx-mcp-server-ce or connx/connx-mcp-server-ce",
        _name_refusal,
    ),
    (TAG_VARIABLE, IMAGE_TAG, "an image tag such as 15.0, 15.0.1-rc.2 or main", _tag_refusal),
)


def _part(values: Mapping[str, str], variable: str, default: str) -> ImagePart:
    """Return a variable's value as an image part, else the built-in default."""
    return ImagePart(values[variable], variable) if variable in values else ImagePart(default, SOURCE_BUILT_IN)


def default_reference(environ: Mapping[str, str] | None = None) -> str:
    """Return the default image reference (see default_choice).

    Args:
        environ (Mapping[str, str] | None): The process environment; `os.environ` when None.

    Returns:
        str: The reference.
    """
    return default_choice(environ).reference


def default_registry(environ: Mapping[str, str] | None = None) -> ImagePart:
    """Return the registry host of the default image, where `login` signs in unless told otherwise.

    Args:
        environ (Mapping[str, str] | None): The process environment; `os.environ` when None.

    Returns:
        ImagePart: The registry host, without any repository path CONNX_MCP_REGISTRY adds after it, and its source.
    """
    chosen = default_choice(environ)
    return ImagePart(registry(chosen.reference), chosen.registry.source)


def choice(env: Environment) -> ImageChoice:
    """Return the image reference an environment pins and where it came from.

    Args:
        env (Environment): The environment.

    Returns:
        ImageChoice: Its host settings' image, else the default (default_choice).

    Raises:
        CliError: The renamed CONNX_MCP_IMAGE_VERSION is set (even for an environment with its own image), or the
            stored image names no registry (configure accepted that before references had to name one).
    """
    _refuse_renamed(set_variables())
    stored = env.host.image
    if not stored:
        return default_choice()
    if not IMAGE_REFERENCE.fullmatch(stored):
        fix = f"{CLI_NAME} configure {env.name} --image"
        raise CliError(
            ExitCode.IMAGE_REFERENCE_INVALID,
            f"Environment {env.name!r} stores the image {stored}, which names no registry, and {CLI_NAME} never "
            f"reads that as Docker Hub: {REGISTRY_FIRST}. Repair it with `{fix} <registry>/{stored}`, or remove "
            f"it with `{fix} ''` so the default image applies. The pinned image is unchanged.",
            f"{fix} <registry>/{stored}",
        )
    return replace(ImageChoice.whole(stored, SOURCE_ENVIRONMENT), environment=env.name)


def refused_reference(env: Environment) -> CliError | None:
    """Return the refusal of an environment whose stored image names no registry, so a list reports it and goes on.

    Args:
        env (Environment): The environment.

    Returns:
        CliError | None: The image_reference_invalid refusal with its repair, or None when the image is usable.

    Raises:
        CliError: Any other refusal of the image choice (the renamed CONNX_MCP_IMAGE_VERSION is set).
    """
    try:
        choice(env)
    except CliError as error:
        if error.exit_code is not ExitCode.IMAGE_REFERENCE_INVALID:
            raise
        return error
    return None


def ignored_variables(chosen: ImageChoice, environ: Mapping[str, str] | None = None) -> list[str]:
    """Return the image variables that are set but play no part, because the environment has its own image.

    Args:
        chosen (ImageChoice): The environment's image choice.
        environ (Mapping[str, str] | None): The process environment; `os.environ` when None.

    Returns:
        list[str]: The set variables, in IMAGE_VARIABLES order; empty for the default image.
    """
    if not chosen.environment:
        return []
    values = set_variables(environ)
    return [name for name in IMAGE_VARIABLES if name in values]


def ignored_notice(chosen: ImageChoice, environ: Mapping[str, str] | None = None) -> str | None:
    """Return the one notice naming each image variable an environment with its own image ignores, if any.

    Args:
        chosen (ImageChoice): The environment's image choice.
        environ (Mapping[str, str] | None): The process environment; `os.environ` when None.

    Returns:
        str | None: The notice, or None when no variable is ignored.
    """
    names = ignored_variables(chosen, environ)
    if not names:
        return None
    env = chosen.environment
    return (
        f"{_are_set(names)} but ignored: {env!r} has its own image {chosen.reference}; "
        f"`{CLI_NAME} configure {env} --image ''` makes the variables apply."
    )


def _are_set(names: list[str]) -> str:
    """Return `A is set` or `A, B and C are set`."""
    if len(names) == 1:
        return f"{names[0]} is set"
    return f"{', '.join(names[:-1])} and {names[-1]} are set"


def notice_ignored_variables(run: StartRun) -> None:
    """Start step: name the image variables an environment with its own image ignores, before anything can fail.

    The notice is shown at once, so a start that fails later still shows it, and the result's warnings carry it.

    Args:
        run (StartRun): The start; its warnings gain the notice.
    """
    if notice := ignored_notice(choice(run.env)):
        run.out.progress(f"Warning: {notice}")
        run.warnings.append(notice)


def notice_variables_moved(run: StartRun) -> None:
    """Start step: say when the variables name another image than a pinned environment runs, and that update moves it.

    The default reference is compared with the reference recorded with the pin; an environment pinned before
    connx-mcp-ce recorded it gets no notice until its next `update`.

    Args:
        run (StartRun): The start; its warnings gain the notice.
    """
    env, chosen = run.env, choice(run.env)
    values = set_variables()
    names = [name for name in IMAGE_VARIABLES if name in values]
    pinned_from = env.host.image_pinned_reference
    if run.pinned or chosen.environment or not names or not pinned_from or pinned_from == chosen.reference:
        return
    run.warnings.append(
        f"{_are_set(names)}: the image variables now name {chosen.reference}, but {env.name!r} stays on its pinned "
        f"image (version {env.host.image_version or 'unknown'}) until `{CLI_NAME} update {env.name}`."
    )


def check_reference(run: StartRun) -> None:
    """Start step: refuse a stored image that names no registry before anything is asked or changed.

    Args:
        run (StartRun): The start.
    """
    choice(run.env)


def status_reference(env: Environment, ports: Ports, report: StatusReport) -> None:  # noqa: ARG001
    """Status check: refuse a stored image that names no registry before Docker is asked anything.

    Args:
        env (Environment): The environment.
        ports (Ports): The ports (unused; every status check takes them).
        report (StatusReport): The report (unused; every status check takes it).
    """
    choice(env)


def reference(env: Environment) -> str:
    """Return the image reference an environment pins.

    Args:
        env (Environment): The environment.

    Returns:
        str: Its host settings' image, else the default (default_choice).
    """
    return choice(env).reference


def repository(ref: str) -> str:
    """Return a reference without its tag or digest.

    Args:
        ref (str): An image reference such as `registry:5000/name:tag`.

    Returns:
        str: `registry:5000/name`.
    """
    name = ref.split("@", 1)[0]
    head, _, last = name.rpartition("/")
    return f"{head}/{last.split(':', 1)[0]}" if head else last.split(":", 1)[0]


def registry(ref: str) -> str:
    """Return the registry an image reference is pulled from.

    Args:
        ref (str): An image reference such as `registry:5000/name:tag`.

    Returns:
        str: Its first path part when that names a host (a dot, a port or `localhost`), else "": a reference with
            no registry never means Docker Hub, and every entry point refuses one before it gets here.
    """
    return ImageChoice.whole(ref, "").registry.value


def inspect(docker: DockerPort, ref: str) -> LocalImage | None:
    """Look an image up on this machine.

    Args:
        docker (DockerPort): The Docker port.
        ref (str): A reference, digest reference or image ID.

    Returns:
        LocalImage | None: The image, or None when it is not on this machine.

    Raises:
        CliError: Docker failed to inspect it for another reason.
    """
    result = query(docker, ["image", "inspect", "--format", IMAGE_FORMAT, ref])
    if result.returncode != 0:
        if "no such image" in result.stderr.lower():
            return None
        raise CliError(ExitCode.DOCKER_FAILED, f"docker image inspect {ref} failed: {result.stderr.strip()}")
    facts = json.loads(result.stdout)
    wanted = repository(ref)
    digests = [d for d in facts.get("digests") or [] if isinstance(d, str)]
    digest = next((d for d in digests if repository(d) == wanted), digests[0] if digests else facts["id"])
    labels = facts.get("labels") or {}
    return LocalImage(id=facts["id"], digest=digest, version=labels.get(VERSION_LABEL))


def resolve(docker: DockerPort, chosen: ImageChoice, env_name: str) -> PinnedImage:
    """Resolve an environment's image reference to the local image's digest and version.

    Args:
        docker (DockerPort): The Docker port.
        chosen (ImageChoice): The environment's image choice; a missing image names where each part came from.
        env_name (str): The environment, for the `download` the refusal names.

    Returns:
        PinnedImage: The digest to render and the version label.

    Raises:
        CliError: The image is not on this machine, or Docker failed to inspect it.
    """
    ref = chosen.reference
    local = inspect(docker, ref)
    if local is None:
        raise CliError(
            ExitCode.IMAGE_NOT_FOUND,
            f"The image {ref} is not on this machine; download it first.\n{chosen.line}",
            f"{CLI_NAME} download {env_name}",
        )
    return PinnedImage(digest=local.digest, version=local.version, reference=ref)


def pin_if_unpinned(docker: DockerPort, env: Environment) -> bool:
    """Pin an environment's image on its first start; a pinned environment keeps its digest.

    Args:
        docker (DockerPort): The Docker port.
        env (Environment): The environment; its host settings are updated in memory.

    Returns:
        bool: True when this call pinned the image.
    """
    if env.host.image_digest:
        return False
    record_pin(env, resolve(docker, choice(env), env.name))
    return True


def record_pin(env: Environment, pinned: PinnedImage) -> None:
    """Record a pin in an environment's host settings: its digest, version and the reference it was resolved from.

    Args:
        env (Environment): The environment; its host settings are updated in memory.
        pinned (PinnedImage): The resolved image.
    """
    env.host.image_digest = pinned.digest
    env.host.image_version = pinned.version
    env.host.image_pinned_reference = pinned.reference


# --- registry -------------------------------------------------------------------------------------------


def refuse_if_unauthorized(what: str, docker_says: str, source_line: str) -> None:
    """Fail as registry_unauthorized when Docker says the registry refused the credentials.

    Args:
        what (str): What was refused, e.g. `The registry refused the pull of <ref>`.
        docker_says (str): Docker's message, already free of any secret.
        source_line (str): What was refused and where it came from (ImageChoice.line, ImagePart.line); no secret.

    Raises:
        CliError: The refusal, naming `connx-mcp-ce login`, where credentials come from and the source line.
    """
    if _REFUSED.search(docker_says):
        message = f"{what}: {docker_says}\n{REGISTRY_HINT}\n{source_line}"
        raise CliError(ExitCode.REGISTRY_UNAUTHORIZED, message, f"{CLI_NAME} login")


def pull(docker: DockerPort, out: Output, chosen: ImageChoice) -> None:
    """Pull an image from its registry.

    Args:
        docker (DockerPort): The Docker port.
        out (Output): Where progress goes.
        chosen (ImageChoice): The image; a failure names where each of its parts came from.

    Raises:
        CliError: The registry refused the credentials, or the pull failed otherwise.
    """
    ref = chosen.reference
    out.progress(f"Downloading {ref} ...")
    result = docker.run(["pull", ref])
    if result.returncode == 0:
        return
    says = result.stderr.strip()
    refuse_if_unauthorized(f"The registry refused the download of {ref}", says, chosen.line)
    raise CliError(ExitCode.PULL_FAILED, f"docker pull {ref} failed: {says}\n{chosen.line}")


# --- versions -------------------------------------------------------------------------------------------


def parse_version(version: str | None) -> tuple[int, int, int] | None:
    """Return the (major, minor, fix) of a version label, or None when it has none.

    Args:
        version (str | None): A label such as `15.0.3.26012` or `15.0.0.dev-559`.

    Returns:
        tuple[int, int, int] | None: The numbers; fix 0 when the label has only major and minor.
    """
    match = _VERSION.match(version or "")
    return (int(match[1]), int(match[2]), int(match[3] or 0)) if match else None


def _dotted(numbers: tuple[int, ...]) -> str:
    """Write version numbers with dots."""
    return ".".join(str(n) for n in numbers)


# The version rule in words, for `start --help` and `update --help`.
VERSION_RULE = (
    "If the command cannot find a release version in the image, it warns that it cannot check that the image is "
    "supported."
)


def refuse_unsupported(
    version: str | None, image: str, next_command: str | None, chosen: ImageChoice, pinned_in: str = ""
) -> None:
    """Refuse an image older than this CLI release's minimum supported version.

    Args:
        version (str | None): The image's version label; an unknown version is warned about, not refused.
        image (str): The image, for the message.
        next_command (str | None): The command to run next.
        chosen (ImageChoice): The image choice now; the refusal names where each part came from.
        pinned_in (str): The environment whose earlier pin is the image; "" when `chosen` named it just now. An
            earlier pin was chosen by whatever applied then, so the refusal names the pin as the source and shows
            `chosen` as what `update` would pull.

    Raises:
        CliError: The image is too old.
    """
    numbers = parse_version(version)
    if numbers is not None and numbers < MIN_SUPPORTED_VERSION:
        source = chosen.pinned_line(image, pinned_in) if pinned_in else chosen.line
        raise CliError(
            ExitCode.IMAGE_TOO_OLD,
            f"The image {image} is version {version}; this {CLI_NAME} supports "
            f"{_dotted(MIN_SUPPORTED_VERSION)} or later and may write settings it does not know.\n{source}",
            next_command,
        )


def version_warning(version: str | None, image: str) -> str | None:
    """Return a warning for an image newer than this CLI knows, or one whose version is unknown.

    Args:
        version (str | None): The image's version label.
        image (str): The image, for the message.

    Returns:
        str | None: The warning, or None for a version this CLI knows.
    """
    numbers = parse_version(version)
    if numbers is None:
        return f"The image {image} has no readable version label ({version or 'none'}); its support is not checked."
    if numbers[:2] > NEWEST_KNOWN_SERIES:
        return (
            f"The image {image} is version {version}, newer than this {CLI_NAME} knows "
            f"({_dotted(NEWEST_KNOWN_SERIES)}); update {CLI_NAME} to configure its new settings."
        )
    return None


def warnings(version: str | None, image: str, *notices: str | None) -> list[str]:
    """Return the warnings a pulled image carries: the given notices, then its version warning, each only if any.

    Args:
        version (str | None): The image's version label.
        image (str): The image, for the version warning.
        *notices (str | None): Notices that come first, such as ignored_notice's; None for none.

    Returns:
        list[str]: The warnings.
    """
    return [warning for warning in (*notices, version_warning(version, image)) if warning]


def check_start_version(run: StartRun) -> None:
    """Start step: refuse a pinned image older than supported; warn about a newer or unlabelled one.

    Args:
        run (StartRun): The start; its warnings gain the version warning.
    """
    host = run.env.host
    chosen = choice(run.env)
    image = host.image_digest or chosen.reference
    pinned_in = "" if run.pinned else run.env.name
    refuse_unsupported(host.image_version, image, f"{CLI_NAME} update {run.env.name}", chosen, pinned_in)
    if warning := version_warning(host.image_version, image):
        run.warnings.append(warning)


# --- status ---------------------------------------------------------------------------------------------


@dataclass
class ImageStatus:
    """What `status` shows about an environment's image.

    Attributes:
        chosen (ImageChoice): The image reference the environment pins at first start or `update`, each part with
            where it came from: the environment, an image variable or the built-in default.
        digest (str | None): The pinned digest; None before the first start.
        version (str | None): The pinned version label.
        running (str | None): The image ID the container runs; None without a container.
        pulled (LocalImage | None): The local image the reference now names, when it is not the pinned one.
        ignored_variables (list[str]): The image variables set but ignored for the environment's own image.
    """

    chosen: ImageChoice
    digest: str | None = None
    version: str | None = None
    running: str | None = None
    pulled: LocalImage | None = None
    ignored_variables: list[str] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        """Return the image facts as a JSON object."""
        pulled = {"digest": self.pulled.digest, "version": self.pulled.version} if self.pulled else None
        return {
            "reference": self.chosen.reference,
            "source": self.chosen.sources,
            "digest": self.digest,
            "version": self.version,
            "running": self.running,
            "pulled": pulled,
            "ignored_variables": self.ignored_variables,
        }

    def human(self) -> list[str]:
        """Return the image facts as status lines."""
        origin = f"                 from {self.chosen.origin}"
        if not self.digest:
            return [f"  Image:         {self.chosen.reference} (pinned at first start)", origin]
        return [
            f"  Image:         version {self.version or 'unknown'}",
            f"                 {self.digest}",
            f"                 update downloads {self.chosen.reference}",
            origin,
        ]


def _running_image(docker: DockerPort, container: str) -> str | None:
    """Return the image ID a container was created from, or None."""
    result = query(docker, ["container", "inspect", "--format", RUNNING_IMAGE_FORMAT, container])
    return (result.stdout.strip() or None) if result.returncode == 0 else None


def status_check(env: Environment, ports: Ports, report: StatusReport) -> None:
    """Status check: the pinned version and digest, a newer pulled image, a container not running the pin.

    Args:
        env (Environment): The environment.
        ports (Ports): The ports.
        report (StatusReport): The report; gains its image facts and notices.
    """
    chosen = choice(env)
    ref = chosen.reference
    facts = ImageStatus(
        chosen=chosen,
        digest=env.host.image_digest,
        version=env.host.image_version,
        ignored_variables=ignored_variables(chosen),
    )
    report.image = facts
    if notice := ignored_notice(chosen):
        report.notices.append(notice)
    if not facts.digest:
        return
    local = inspect(ports.docker, ref)
    if local is not None and local.digest != facts.digest:
        facts.pulled = local
        report.notices.append(
            f"{ref} on this machine is now version {local.version or 'unknown'} ({local.digest}); "
            f"{env.name!r} stays pinned to version {facts.version or 'unknown'} until `{CLI_NAME} update {env.name}`."
        )
    if report.state == "not created":
        return
    facts.running = _running_image(ports.docker, report.container)
    pinned = local if local is not None and local.digest == facts.digest else inspect(ports.docker, facts.digest)
    if facts.running and pinned is not None and facts.running != pinned.id:
        report.notices.append(
            f"{report.container} runs image {facts.running}, not the pinned {facts.digest}; "
            f"`{CLI_NAME} restart {env.name}` applies the pinned image."
        )
