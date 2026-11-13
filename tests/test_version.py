#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_version.py - Every build carries its Product version: `--version` shows it, a build writes it.

Description:
    `connx-mcp-ce --version` shows the Product version as people write it, read from the installed package's
    metadata (patched here): a Release build's Build number, a Development build's `dev<run number>`, or the
    declared major.minor.fix alone from a source checkout. The version helper writes a Product version into a
    build checkout's pyproject.toml, and into its uv.lock so a locked sync still accepts it, and refuses any
    other shape.

Copyright:
    Copyright 2026 Software GmbH
    Licensed under the Apache License, Version 2.0 (the "License"); you may not use this file except in compliance
    with the License. You may obtain a copy of the License at https://www.apache.org/licenses/LICENSE-2.0
    Unless required by applicable law or agreed to in writing, software distributed under the License is distributed
    on an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the License
    for the specific language governing permissions and limitations under the License.
"""

from __future__ import annotations

import tomllib
from importlib import metadata
from pathlib import Path

import pytest

from connx_mcp import version
from tests.conftest import Cli

PYPROJECT = """[project]
name = "connx-mcp-ce"
version = "15.0.0"
requires-python = ">=3.14"

[tool.uv.build-backend]
module-name = "connx_mcp"
"""

LOCK = """version = 1
requires-python = ">=3.14"

[[package]]
name = "colorama"
version = "0.4.6"

[[package]]
name = "connx-mcp-ce"
version = "15.0.0"
source = { editable = "." }
"""


def installed_as(monkeypatch: pytest.MonkeyPatch, installed: str | None) -> None:
    """Patch the installed package's metadata version; None is a package that is not installed.

    `version.installed_version()` reads it there for `--version` and the skill instructions alike, so this is the one
    patch point for the installed version.
    """

    def read(distribution: str) -> str:
        if installed is None or distribution != "connx-mcp-ce":
            raise metadata.PackageNotFoundError(distribution)
        return installed

    monkeypatch.setattr("connx_mcp.version.metadata.version", read)


@pytest.mark.parametrize(
    ("installed", "shown"),
    [
        ("15.0.0.26004", "connx-mcp-ce 15.0.0 (build 26004)"),
        ("15.1.0.dev408", "connx-mcp-ce 15.1.0 (build dev408)"),
        ("15.0.0", "connx-mcp-ce 15.0.0"),
    ],
    ids=["release-build", "development-build", "source-checkout"],
)
def test_version_shows_the_product_version_as_people_write_it(
    cli: Cli, monkeypatch: pytest.MonkeyPatch, installed: str, shown: str
) -> None:
    installed_as(monkeypatch, installed)

    result = cli("--version")

    assert result.exit_code == 0
    assert result.stdout == f"{shown}\n"


def test_version_without_package_metadata_says_unknown(cli: Cli, monkeypatch: pytest.MonkeyPatch) -> None:
    installed_as(monkeypatch, None)

    result = cli("--version")

    assert (result.exit_code, result.stdout) == (0, "connx-mcp-ce unknown\n")


@pytest.fixture
def checkout(tmp_path: Path) -> Path:
    """A build checkout declaring 15.0.0, with its uv.lock."""
    (tmp_path / "pyproject.toml").write_text(PYPROJECT, encoding="utf-8")
    (tmp_path / "uv.lock").write_text(LOCK, encoding="utf-8")
    return tmp_path


def declared(checkout: Path) -> tuple[str, str]:
    """The version pyproject.toml declares, and the one uv.lock records for connx-mcp-ce."""
    project = tomllib.loads((checkout / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
    lock = tomllib.loads((checkout / "uv.lock").read_text(encoding="utf-8"))
    [locked] = [p["version"] for p in lock["package"] if p["name"] == "connx-mcp-ce"]
    return project, locked


def test_the_declared_version_is_major_minor_fix(checkout: Path) -> None:
    assert version.base_version(checkout / "pyproject.toml") == "15.0.0"


@pytest.mark.parametrize("product_version", ["15.0.0.26004", "15.0.0.dev408"], ids=["release", "development"])
def test_a_build_writes_its_product_version_into_pyproject_and_the_lock(checkout: Path, product_version: str) -> None:
    version.stamp(checkout, product_version)

    assert declared(checkout) == (product_version, product_version)
    # Nothing else in either file changes.
    assert (checkout / "pyproject.toml").read_text(encoding="utf-8") == PYPROJECT.replace(
        'version = "15.0.0"', f'version = "{product_version}"'
    )
    assert "colorama" in (checkout / "uv.lock").read_text(encoding="utf-8")


def test_a_build_names_its_build_and_takes_major_minor_fix_from_pyproject(checkout: Path) -> None:
    assert version.stamp_build(checkout, "dev408") == "15.0.0.dev408"

    assert declared(checkout) == ("15.0.0.dev408", "15.0.0.dev408")


def test_a_checkout_without_a_lock_gets_its_pyproject_written(checkout: Path) -> None:
    (checkout / "uv.lock").unlink()

    version.stamp(checkout, "15.0.0.26004")

    assert (checkout / "pyproject.toml").read_text(encoding="utf-8") == PYPROJECT.replace(
        'version = "15.0.0"', 'version = "15.0.0.26004"'
    )
    assert not (checkout / "uv.lock").exists()


@pytest.mark.parametrize(
    "product_version",
    [
        "15.0.0",  # no build
        "15.0.0.2604",  # four digits: not yy + a three-digit count
        "15.0.0.26000",  # count 000: the first number of a year is 001
        "15.0.0.dev",  # no run number
        "15.0.0.dev-408",  # the server's spelling, not PEP 440
        "15.0.0rc1",
        "15.0.26004",  # two-part major.minor
        "15.1.0.26004",  # not the declared major.minor.fix
    ],
)
def test_a_product_version_of_any_other_shape_is_refused_and_nothing_is_written(
    checkout: Path, product_version: str
) -> None:
    with pytest.raises(ValueError, match=product_version.replace(".", r"\.")):
        version.stamp(checkout, product_version)

    assert declared(checkout) == ("15.0.0", "15.0.0")


def test_a_declared_version_that_is_not_major_minor_fix_is_refused(checkout: Path) -> None:
    pyproject = checkout / "pyproject.toml"
    pyproject.write_text(PYPROJECT.replace('"15.0.0"', '"15.0.0.26004"'), encoding="utf-8")

    with pytest.raises(ValueError, match="not major.minor.fix"):
        version.stamp_build(checkout, "26005")


@pytest.mark.parametrize(
    ("text", "base", "build", "release_build"),
    [
        ("15.0.0.26004", "15.0.0", "26004", True),
        ("15.1.0.dev408", "15.1.0", "dev408", False),
        ("15.0.0", "15.0.0", None, False),
    ],
    ids=["release-build", "development-build", "unstamped"],
)
def test_a_version_is_parsed_once_into_its_parts(text: str, base: str, build: str | None, release_build: bool) -> None:
    parsed = version.ProductVersion.parse(text)

    assert (parsed.base, parsed.build, parsed.is_release_build) == (base, build, release_build)
    assert str(parsed) == text


@pytest.mark.parametrize(
    ("text", "numeric"),
    [("15.0.0.26004", "15.0.0.26004"), ("15.1.0.dev408", "15.1.0.0"), ("15.0.0", "15.0.0.0")],
    ids=["release-build", "development-build", "unstamped"],
)
def test_the_windows_file_version_is_four_numbers(text: str, numeric: str) -> None:
    assert version.file_version(text) == numeric


@pytest.mark.parametrize("text", ["unknown", "15.0", "15.0.0rc1", "15.0.0.26000"])
def test_an_unparsable_version_has_no_file_version(text: str) -> None:
    with pytest.raises(ValueError, match=text.replace(".", r"\.")):
        version.file_version(text)


def test_a_checkout_reads_its_stamped_product_version(checkout: Path) -> None:
    version.stamp(checkout, "15.0.0.dev408")

    assert version.read_version(checkout / "pyproject.toml") == version.ProductVersion("15.0.0", "dev408")


@pytest.mark.parametrize("text", ["15.0.3.26004", "15.0.3.dev408", "15.0.3"])
def test_a_versions_release_is_its_major_minor(text: str) -> None:
    assert version.ProductVersion.parse(text).release == "15.0"
