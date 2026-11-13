#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""version.py - The Product version of connx-mcp-ce: read from the package, written into a build checkout.

Description:
    `pyproject.toml` declares major.minor.fix and nothing else. A build writes its Product version there (and into
    `uv.lock`, so a locked sync accepts it) before it installs the package, and never commits it: a Release build
    adds its Build number (`15.0.0.26004`), a Development build `dev<run number>` (`15.0.0.dev408`, PEP 440). At
    run time the version comes from the installed package's metadata; a source checkout shows the declared
    major.minor.fix alone. `ProductVersion` parses a version once into its parts. People read it as
    `15.0.0 (build 26004)`. A Windows file version takes four numbers: a Release build's own, `major.minor.fix.0`
    otherwise; a version of any other shape has none. Only the standard library is imported, so the release
    scripts can use this module too.

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
import tomllib
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path

DISTRIBUTION = "connx-mcp-ce"

# major.minor.fix: what pyproject.toml declares.
BASE = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+")
# A Build number: two-digit year, then a three-digit count from 001.
BUILD_NUMBER = re.compile(r"[0-9]{2}(?!000)[0-9]{3}")
# A Development build's build: `dev` and the CI run number.
DEV_BUILD = re.compile(r"dev[0-9]+")
# A Product version: major.minor.fix and its build.
PRODUCT_VERSION = re.compile(rf"(?P<base>{BASE.pattern})\.(?P<build>{BUILD_NUMBER.pattern}|{DEV_BUILD.pattern})")
# A Product version, or the major.minor.fix alone of an unstamped checkout.
_VERSION = re.compile(rf"(?P<base>{BASE.pattern})(?:\.(?P<build>{BUILD_NUMBER.pattern}|{DEV_BUILD.pattern}))?")

# The `version = "…"` line of a TOML table's body: pyproject's [project] table, a uv.lock package entry.
_VERSION_LINE = re.compile(r'^version = "[^"\n]*"$', re.MULTILINE)


@dataclass(frozen=True)
class ProductVersion:
    """A version split once into its parts: major.minor.fix, then the build, None in an unstamped checkout.

    Attributes:
        base (str): major.minor.fix, such as `15.0.0`.
        build (str | None): A Build number such as `26004`, `dev<run number>`, or None when unstamped.
    """

    base: str
    build: str | None = None

    @classmethod
    def parse(cls, text: str) -> ProductVersion:
        """Split a Product version, or an unstamped major.minor.fix, into its parts.

        Args:
            text (str): The version, such as `15.0.0.26004`, `15.0.0.dev408` or `15.0.0`.

        Returns:
            ProductVersion: Its parts.

        Raises:
            ValueError: The text is neither.
        """
        match = _VERSION.fullmatch(text)
        if match is None:
            raise ValueError(
                f"version {text!r} is not major.minor.fix, optionally followed by a build "
                "(a Build number such as 26004, or dev<run number>)"
            )
        return cls(match["base"], match["build"])

    @property
    def release(self) -> str:
        """The CONNX release, major.minor, such as `15.0`: what its Release branch is named after."""
        return self.base.rsplit(".", 1)[0]

    @property
    def is_release_build(self) -> bool:
        """Whether the build is a Build number, which only a Release build carries."""
        return self.build is not None and BUILD_NUMBER.fullmatch(self.build) is not None

    def __str__(self) -> str:
        """Return the version as written: `15.0.0.26004`, `15.0.0.dev408` or `15.0.0`."""
        return self.base if self.build is None else f"{self.base}.{self.build}"

    def display(self) -> str:
        """Return the version as people write it: `15.0.0 (build 26004)`, `15.1.0 (build dev408)` or `15.0.0`."""
        return self.base if self.build is None else f"{self.base} (build {self.build})"

    def file_version(self) -> str:
        """Return the four-number Windows file version: a Release build's Product version, else major.minor.fix.0."""
        return str(self) if self.is_release_build else f"{self.base}.0"


def installed_version() -> str:
    """Return the installed package's version, as its metadata records it; `unknown` when it is not installed."""
    try:
        return metadata.version(DISTRIBUTION)
    except metadata.PackageNotFoundError:
        return "unknown"


def display(version: str) -> str:
    """Return a version as people write it: `15.0.0 (build 26004)`, `15.1.0 (build dev408)`, or as given.

    Args:
        version (str): A Product version, a declared major.minor.fix, or any other string (shown unchanged).

    Returns:
        str: The display form.
    """
    try:
        return ProductVersion.parse(version).display()
    except ValueError:
        return version


def file_version(version: str) -> str:
    """Return the four-number Windows file version: a Release build's Product version, else major.minor.fix.0.

    A version of any other shape has none: `ProductVersion.parse` refuses it with a ValueError.

    Args:
        version (str): The Product version the binary is built with, or an unstamped major.minor.fix.

    Returns:
        str: Four dot-separated numbers.
    """
    return ProductVersion.parse(version).file_version()


def read_version(pyproject: Path) -> ProductVersion:
    """Return the version a pyproject.toml holds: its declared major.minor.fix, or the Product version stamped there.

    Args:
        pyproject (Path): The pyproject.toml to read.

    Returns:
        ProductVersion: The version.

    Raises:
        ValueError: The version is neither.
    """
    held = tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"]["version"]
    try:
        return ProductVersion.parse(held)
    except ValueError as error:
        raise ValueError(f"{pyproject}: {error}") from error


def base_version(pyproject: Path) -> str:
    """Return the major.minor.fix a pyproject.toml declares.

    Args:
        pyproject (Path): The pyproject.toml to read.

    Returns:
        str: The declared version, such as `15.0.0`.

    Raises:
        ValueError: The declared version is not three numbers.
    """
    declared = read_version(pyproject)
    if declared.build is not None:
        raise ValueError(
            f"{pyproject}: version {str(declared)!r} is not major.minor.fix (three numbers, such as 15.0.0)"
        )
    return declared.base


def check_stamp(pyproject: Path, product_version: str) -> ProductVersion:
    """Return a Product version that `stamp` can write into the checkout of a pyproject.toml; write nothing.

    Args:
        pyproject (Path): The pyproject.toml, declaring major.minor.fix.
        product_version (str): major.minor.fix as pyproject.toml declares it, then a Build number or `dev<n>`.

    Returns:
        ProductVersion: The Product version, parsed.

    Raises:
        ValueError: The Product version has another shape or another major.minor.fix.
    """
    parsed = ProductVersion.parse(product_version)
    if parsed.build is None:
        raise ValueError(
            f"Product version {product_version!r} has no build (a Build number such as 26004, or dev<run number>)"
        )
    declared = base_version(pyproject)
    if parsed.base != declared:
        raise ValueError(f"Product version {product_version!r} is not of {declared}, which {pyproject} declares")
    return parsed


def stamp(checkout: Path, product_version: str) -> None:
    """Write a Product version into a build checkout's pyproject.toml, and its uv.lock when it has one.

    A Product version of another shape or another major.minor.fix is refused by `check_stamp` (ValueError), and
    nothing is written.

    Args:
        checkout (Path): The directory holding pyproject.toml (and uv.lock).
        product_version (str): major.minor.fix as pyproject.toml declares it, then a Build number or `dev<n>`.
    """
    pyproject = checkout / "pyproject.toml"
    check_stamp(pyproject, product_version)
    writes = {pyproject: _replace_version(pyproject, r"^\[project\]\n", product_version)}
    lock = checkout / "uv.lock"
    if lock.is_file():
        writes[lock] = _replace_version(lock, rf'^\[\[package\]\]\nname = "{DISTRIBUTION}"\n', product_version)
    for path, text in writes.items():
        path.write_text(text, encoding="utf-8", newline="\n")


def stamp_build(checkout: Path, build: str) -> str:
    """Write the declared major.minor.fix plus a build into a build checkout; see `stamp`.

    Args:
        checkout (Path): The directory holding pyproject.toml (and uv.lock).
        build (str): A Build number such as `26004`, or `dev<run number>`.

    Returns:
        str: The Product version written.
    """
    product_version = f"{base_version(checkout / 'pyproject.toml')}.{build}"
    stamp(checkout, product_version)
    return product_version


def _replace_version(path: Path, table: str, version: str) -> str:
    """Return a TOML file's text with the version line of one table holding `version`.

    Args:
        path (Path): The TOML file.
        table (str): A multi-line regular expression matching the table's header lines.
        version (str): The version to write.

    Returns:
        str: The new text.

    Raises:
        ValueError: The file has no such table, or the table no version line.
    """
    text = path.read_text(encoding="utf-8")
    header = re.search(table, text, re.MULTILINE)
    line = _VERSION_LINE.search(text, header.end()) if header else None
    if header is None or line is None or "\n[" in text[header.end() : line.start()]:
        raise ValueError(f"{path}: no version line in the table that {table!r} names")
    return f'{text[: line.start()]}version = "{version}"{text[line.end() :]}'
