#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""instructions.py - The skill instructions: one marked section in a harness's instructions file.

Description:
    The section is the instructions text between a begin marker, which carries the text's version, and an
    end marker. The text is the image's INSTRUCTIONS.md under the image's version, or the built-in text
    under connx-mcp-ce's; either is made LF, and an image file that is not UTF-8 or holds a marker line is
    refused. A version that could break the begin marker shows as `unknown`. A file without the section
    gets it appended after a blank line; a missing file is created,
    an `AGENTS.md` with its heading. A file holding one well-formed section has it replaced in place, so
    text outside the markers is never touched. Every file is planned (read and its new content computed)
    before any file is written, so a refused file leaves nothing half written. The SHA-256 of the section
    as written goes into the install record, so a later run can tell a section it wrote from one edited by
    hand; an edited section or broken markers are refused unless `--force`, which repairs the markers. A
    file keeps its BOM and its line endings; one that is not UTF-8 or cannot be written is refused.
    `unplan` is the inverse for uninstall-skill: it removes the section and the blank line before it,
    refuses edited sections and broken markers the same way, and deletes a file left with nothing but
    what install created.

Copyright:
    Copyright 2026 Software GmbH
    Licensed under the Apache License, Version 2.0 (the "License"); you may not use this file except in compliance
    with the License. You may obtain a copy of the License at https://www.apache.org/licenses/LICENSE-2.0
    Unless required by applicable law or agreed to in writing, software distributed under the License is distributed
    on an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the License
    for the specific language governing permissions and limitations under the License.
"""

from __future__ import annotations

import hashlib
import os
import re
import stat
import tempfile
from collections.abc import Collection
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from connx_mcp import CLI_NAME
from connx_mcp.output import CliError, ExitCode

BEGIN_MARKER = f"<!-- {CLI_NAME} begin version={{version}} -->"
# The begin marker up to its version, as a person reads it: the version changes from one install to the next.
BEGIN_PREFIX = BEGIN_MARKER.split(" version=", 1)[0]
END_MARKER = f"<!-- {CLI_NAME} end -->"
# The name is followed by a space, so a marker of a name this one starts with, or that starts with it, never matches.
_BEGIN = re.compile(rf"^{re.escape(BEGIN_PREFIX)}\b[^\n]*?-->[ \t]*(?=\r?$)", re.MULTILINE)
_END = re.compile(rf"^{re.escape(END_MARKER)}[ \t]*(?=\r?$)", re.MULTILINE)
# Where the text came from, and what writing it did to a file.
Source = Literal["built-in", "image"]
Outcome = Literal["created", "appended", "replaced"]
# What removing the section did to a file.
Removed = Literal["removed", "file_deleted"]
# Why a removal needed --force: an edited section, broken markers, or a project file linked outside the project.
Forced = Literal["edited", "broken_markers", "outside_project"]
# What a version may hold to go into the begin marker: a newline or `-->` would break it.
_VERSION = re.compile(r"[A-Za-z0-9._+-]+")
AGENTS_FILE = "AGENTS.md"
AGENTS_HEADING = "# AGENTS.md"

# The text used while the image ships none; the body only, the markers are added around it. Package data, so
# the release build ships it inside the binary.
BUILT_IN_FILE = Path(__file__).parent / "INSTRUCTIONS.md"


@dataclass(frozen=True)
class InstructionsText:
    """The skill instructions to write and where they came from.

    Attributes:
        body (str): The text between the markers, with LF line endings.
        version (str): The version the begin marker shows.
        source (Source): `built-in`, or `image` when the environment's image ships the text.
    """

    body: str
    version: str
    source: Source

    @property
    def section(self) -> str:
        """The section as written, markers included, without a final line ending."""
        begin = BEGIN_MARKER.format(version=self.version)
        body = self.body.rstrip("\n")
        return f"{begin}\n{body}\n{END_MARKER}"

    @property
    def sha256(self) -> str:
        """The SHA-256 of the section as written: what the install record keeps."""
        return section_hash(self.section)


def built_in(version: str) -> InstructionsText:
    """Return the built-in text, shown under the installing connx-mcp-ce's version.

    Args:
        version (str): connx-mcp-ce's version.

    Returns:
        InstructionsText: The built-in text.
    """
    return InstructionsText(_body(BUILT_IN_FILE.read_bytes().decode("utf-8-sig")), _label(version), "built-in")


def from_image(raw: bytes, version: str, image: str, update_command: str) -> InstructionsText:
    """Return the text the image ships, its line endings made LF, shown under the image's version.

    Args:
        raw (bytes): The image's INSTRUCTIONS.md, the body only.
        version (str): The image's version.
        image (str): The image, for the error messages.
        update_command (str): The command that updates the environment to another image, for the errors.

    Returns:
        InstructionsText: The image's text.

    Raises:
        CliError: The file is not UTF-8 text, or it holds a connx-mcp-ce marker line.
    """
    try:
        body = raw.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise CliError(
            ExitCode.FILE_UNREADABLE,
            f"The skill instructions (INSTRUCTIONS.md) in the image {image} are not UTF-8 text ({error}), so "
            f"{CLI_NAME} installs nothing. Update the environment to an image that ships them as UTF-8, or run "
            "install-skill with --no-instructions.",
            update_command,
        ) from error
    body = _body(body)
    if _BEGIN.search(body) or _END.search(body):
        raise CliError(
            ExitCode.FILE_UNREADABLE,
            f"The skill instructions (INSTRUCTIONS.md) in the image {image} hold a {CLI_NAME} marker line, so "
            f"{CLI_NAME} installs nothing. The file must hold the body only; install-skill adds the markers. Update "
            "the environment to an image that ships the body only, or run install-skill with --no-instructions.",
            update_command,
        )
    return InstructionsText(body, _label(version), "image")


def _body(text: str) -> str:
    """Return an instructions text with LF line endings."""
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _label(version: str) -> str:
    """Return the version for the begin marker: `unknown` unless it holds only letters, digits and `._+-`."""
    return version if _VERSION.fullmatch(version) else "unknown"


def section_hash(section: str) -> str:
    """Return the SHA-256 of a section, markers included.

    Args:
        section (str): The section text.

    Returns:
        str: The hex digest.
    """
    return hashlib.sha256(section.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Found:
    """Where the section sits in a file's content.

    Attributes:
        span (tuple[int, int] | None): The start and end of the one well-formed section; None without one.
        broken (bool): The markers are unpaired, out of order or more than one pair.
    """

    span: tuple[int, int] | None
    broken: bool

    def section(self, content: str) -> str | None:
        """Return the well-formed section in the content it was found in, with LF line endings, or None.

        Args:
            content (str): The content `find` read.

        Returns:
            str | None: The section, markers included.
        """
        if self.span is None:
            return None
        return content[self.span[0] : self.span[1]].replace("\r\n", "\n")


def find(content: str) -> Found:
    """Find the section in a file's content: exactly one begin marker followed by one end marker is well formed.

    Args:
        content (str): The file's content.

    Returns:
        Found: The section's span, or that there is none, or that the markers are broken.
    """
    begins = list(_BEGIN.finditer(content))
    ends = list(_END.finditer(content))
    if not begins and not ends:
        return Found(None, broken=False)
    if len(begins) == 1 and len(ends) == 1 and begins[0].start() < ends[0].start():
        return Found((begins[0].start(), ends[0].end()), broken=False)
    return Found(None, broken=True)


def _without_markers(content: str) -> str:
    """Return the content without any marker-bounded fragment, and without the blank line it would leave twice.

    A fragment is a begin-end pair with what it holds, or an unpaired marker line; its line ending goes with
    it. Where the text before a fragment ends in a blank line and the text after it starts with one, one of
    the two blank lines goes too.

    Args:
        content (str): The file's content, BOM removed.

    Returns:
        str: The content left.
    """
    markers = sorted(
        [(m.start(), m.end(), True) for m in _BEGIN.finditer(content)]
        + [(m.start(), m.end(), False) for m in _END.finditer(content)]
    )
    kept = ""
    position = index = 0
    while index < len(markers):
        start, end, begin = markers[index]
        if begin and index + 1 < len(markers) and not markers[index + 1][2]:
            end = markers[index + 1][1]
            index += 1
        index += 1
        kept += content[position:start]
        position = end + _line_ending(content, end)
        if kept.endswith(("\n\n", "\n\r\n")):
            position += _line_ending(content, position)
    return kept + content[position:]


def _line_ending(content: str, index: int) -> int:
    """Return the length of the line ending that starts at the index: 2 for CRLF, 1 for LF, else 0."""
    if content.startswith("\r\n", index):
        return 2
    return 1 if content.startswith("\n", index) else 0


@dataclass(frozen=True)
class Plan:
    """One instructions file's change, computed before anything is written.

    Attributes:
        path (Path): The file.
        harnesses (tuple[str, ...]): The harnesses that read it.
        outcome (Outcome): `created`, `appended` or `replaced`.
        content (str): The file's new content, its BOM and line endings included.
        text (InstructionsText): The text the section holds.
    """

    path: Path
    harnesses: tuple[str, ...]
    outcome: Outcome
    content: str
    text: InstructionsText

    def write(self) -> None:
        """Write the file whole, through a symlink to its target, so a crash leaves the old or the new content.

        Raises:
            CliError: The file could not be written.
        """
        try:
            replace_file(self.path.resolve(), self.content.encode("utf-8"))
        except OSError as error:
            raise CliError(
                ExitCode.FILE_UNREADABLE, f"Could not write the skill instructions to {self.path}: {error}"
            ) from error


def replace_file(path: Path, data: bytes) -> None:
    """Write a file whole: a temporary file in its directory, then a rename over it, so it is never half written.

    An existing file keeps its permission bits; a new one gets the default for this user. An OSError for a
    directory or file that cannot be written passes to the caller.

    Args:
        path (Path): The file, not a symlink.
        data (bytes): Its new content.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else None
    handle, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".partial", dir=path.parent)
    try:
        with os.fdopen(handle, "wb") as file:
            file.write(data)
        if mode is None:
            umask = os.umask(0)
            os.umask(umask)
            mode = 0o666 & ~umask
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        # Gone after the rename; left only by a failure.
        Path(temporary).unlink(missing_ok=True)


@dataclass(frozen=True)
class Refusal:
    """A file install-skill will not write, found before anything is written.

    Attributes:
        path (Path): The file or directory.
        reason (str): Why, as the end of a sentence that starts with the path.
        forceable (bool): `--force` writes it anyway.
    """

    path: Path
    reason: str
    forceable: bool


def _unwritable(path: Path) -> Refusal | None:
    """Return a refusal when the file, or for a new file the nearest existing directory above it, is read-only."""
    if path.exists():
        return None if os.access(path, os.W_OK) else Refusal(path, "is read-only", forceable=False)
    directory = path.parent
    while not directory.exists() and directory != directory.parent:
        directory = directory.parent
    if directory.is_dir() and os.access(directory, os.W_OK | os.X_OK):
        return None
    return Refusal(path, f"cannot be created, because {directory} is read-only", forceable=False)


def plan(
    path: Path,
    harnesses: tuple[str, ...],
    text: InstructionsText,
    *,
    recorded: Collection[str] | None = None,
    force: bool = False,
) -> Plan | Refusal:
    """Read one instructions file and compute its new content: created, appended or replaced in place.

    An existing file keeps its BOM and its line endings (CRLF when its first line ending is CRLF); a new one
    uses the OS default. A section whose hash no install record keeps, in a file this environment's record
    names, or broken markers, are refused without `--force`; with it, broken markers are removed and one
    clean section appended.

    Args:
        path (Path): The file.
        harnesses (tuple[str, ...]): The harnesses that read it.
        text (InstructionsText): The text to write.
        recorded (Collection[str] | None): The section hashes the install records of every environment keep
            for the file; None when this environment's record does not name it.
        force (bool): `--force` was given.

    Returns:
        Plan | Refusal: The change, or why the file is not written.
    """
    if not path.exists():
        # Only a new AGENTS.md gets a heading: a new CLAUDE.md is Claude Code's own file and starts with the section.
        heading = f"{AGENTS_HEADING}\n\n" if path.name == AGENTS_FILE else ""
        content = f"{heading}{text.section}\n".replace("\n", os.linesep)
        return _unwritable(path) or Plan(path, harnesses, "created", content, text)
    try:
        current = path.read_bytes().decode("utf-8")
    except OSError as error:
        return Refusal(path, f"cannot be read: {error}", forceable=False)
    except UnicodeDecodeError as error:
        return Refusal(path, f"is not UTF-8 text ({error}), so it is left as it is", forceable=False)
    refusal = _unwritable(path)
    if refusal is not None:
        return refusal
    bom = "\ufeff" if current.startswith("\ufeff") else ""
    current = current.removeprefix(bom)
    first = current.find("\n")
    newline = "\r\n" if first > 0 and current[first - 1] == "\r" else "\n"
    section = text.section.replace("\n", newline)
    found = find(current)
    if found.broken:
        if not force:
            return Refusal(
                path,
                f"holds {CLI_NAME} markers that do not make one section, so {CLI_NAME} cannot tell which text "
                "to replace",
                forceable=True,
            )
        return Plan(path, harnesses, "replaced", bom + _appended(_without_markers(current), section, newline), text)
    if found.span is not None:
        written = found.section(current)
        if recorded and not force and written is not None and section_hash(written) not in recorded:
            return Refusal(
                path, f"holds skill instructions that were edited since {CLI_NAME} wrote them", forceable=True
            )
        start, end = found.span
        return Plan(path, harnesses, "replaced", bom + current[:start] + section + current[end:], text)
    return Plan(path, harnesses, "appended", bom + _appended(current, section, newline), text)


@dataclass(frozen=True)
class Removal:
    """One instructions file's section removal, computed before anything is removed.

    Attributes:
        path (Path): The file.
        harnesses (tuple[str, ...]): The harnesses that read it.
        outcome (Removed): `removed`, or `file_deleted` when nothing but what install-skill created is left.
        content (str | None): The file's new content, its BOM and line endings included; None deletes the file.
        target (Path): The file the path led to when it was read: itself, or a symlink's target.
        read (bytes): The file's bytes when it was read, which the content was computed from.
        forced (Forced | None): Why the removal needed `--force`; None when it did not.
    """

    path: Path
    harnesses: tuple[str, ...]
    outcome: Removed
    content: str | None
    target: Path
    read: bytes
    forced: Forced | None = None

    def apply(self) -> None:
        """Write the file whole through a symlink to its target, or delete it, unless it changed since it was read.

        Raises:
            OSError: The file changed, or a symlink now leads elsewhere, since it was read; or it cannot be
                written or deleted.
        """
        if self.path.resolve() != self.target or self.target.read_bytes() != self.read:
            raise OSError(f"it changed after {CLI_NAME} read it, so it was left as it is")
        if self.content is None:
            self.path.unlink()
        else:
            replace_file(self.target, self.content.encode("utf-8"))


def unplan(
    path: Path, harnesses: tuple[str, ...], *, recorded: Collection[str] | None = None, force: bool = False
) -> Removal | Refusal | None:
    """Read one instructions file and compute its content without the section: the inverse of `plan`.

    The section goes with its line ending and the one blank line install-skill put before it, so a blank line
    is never left twice and the file never ends in one it did not have. Text outside the markers is kept, as
    are the BOM and the line endings. A file left empty, blank, or holding only the heading install-skill
    writes into a new AGENTS.md is deleted, unless it is a symlink: then its target is written. An edited
    section or broken markers are refused without `--force`; with it, the section goes, or every marker-bounded
    fragment and stray marker line as install-skill's `--force` strips them. A file connx-mcp-ce cannot read or
    change is refused even with `--force`.

    Args:
        path (Path): The file.
        harnesses (tuple[str, ...]): The harnesses that read it.
        recorded (Collection[str] | None): The section hashes the install records of every environment keep
            for the file; None or empty when no record names it, so any section is removed.
        force (bool): `--force` was given.

    Returns:
        Removal | Refusal | None: The removal, why the file is not changed, or None when it holds no section.
    """
    if not path.exists():
        return None
    try:
        target = path.resolve()
        read = target.read_bytes()
        current = read.decode("utf-8")
    except OSError as error:
        return Refusal(path, f"cannot be read: {error}", forceable=False)
    except UnicodeDecodeError as error:
        return Refusal(path, f"is not UTF-8 text ({error}), so it is left as it is", forceable=False)
    bom = "\ufeff" if current.startswith("\ufeff") else ""
    current = current.removeprefix(bom)
    found = find(current)
    if found.span is None and not found.broken:
        return None
    was = _Read(path, harnesses, bom, target, read)
    refusal = _unwritable(path) or _undeletable(path)
    if refusal is not None:
        return refusal
    if found.span is None:  # broken markers: nothing else is left after the return above
        if not force:
            return Refusal(
                path,
                f"holds {CLI_NAME} markers that do not make one section, so {CLI_NAME} cannot tell which text "
                "to remove",
                forceable=True,
            )
        return _removal(was, _stripped(current), "broken_markers")
    start, end = found.span
    edited = bool(recorded) and section_hash(found.section(current) or "") not in (recorded or ())
    if edited and not force:
        return Refusal(path, f"holds skill instructions that were edited since {CLI_NAME} wrote them", forceable=True)
    before, after = current[:start], current[end + _line_ending(current, end) :]
    if before.endswith(("\n\n", "\n\r\n")) and (not after or _line_ending(after, 0)):
        before = before[: -2 if before.endswith("\r\n") else -1]
    return _removal(was, before + after, "edited" if edited else None)


def _stripped(content: str) -> str:
    """Return the content without any marker fragment, and without a blank line at its end that it did not have."""
    left = _without_markers(content)
    if left.endswith(("\n\n", "\n\r\n")) and not content.endswith(("\n\n", "\n\r\n")):
        left = left[: -2 if left.endswith("\r\n") else -1]
    return left


@dataclass(frozen=True)
class _Read:
    """An instructions file as `unplan` read it: what a removal from it carries along."""

    path: Path
    harnesses: tuple[str, ...]
    bom: str
    target: Path
    read: bytes


def _removal(was: _Read, left: str, forced: Forced | None) -> Removal:
    """Return the removal that leaves the content: the file is deleted when only install's scaffolding is left."""
    path = was.path
    scaffolding = not left.strip() or (path.name == AGENTS_FILE and left.strip() == AGENTS_HEADING)
    if scaffolding and not path.is_symlink():
        return Removal(path, was.harnesses, "file_deleted", None, was.target, was.read, forced)
    return Removal(path, was.harnesses, "removed", was.bom + left, was.target, was.read, forced)


def _undeletable(path: Path) -> Refusal | None:
    """Return a refusal when the directory holding the file (its target, for a symlink) is read-only."""
    directory = path.resolve().parent
    if os.access(directory, os.W_OK | os.X_OK):
        return None
    return Refusal(path, f"cannot be changed, because {directory} is read-only", forceable=False)


def _appended(content: str, section: str, newline: str) -> str:
    """Return the content with the section appended after one blank line: the file's own, when it ends in one."""
    if not content or content.endswith(("\n\n", "\n\r\n")):
        separator = ""
    else:
        separator = newline if content.endswith("\n") else newline * 2
    return f"{content}{separator}{section}{newline}"
