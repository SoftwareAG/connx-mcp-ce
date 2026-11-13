#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_release_build.py - The maintainer's release build: binaries, SHA-256 sums, notices and start-up time.

Description:
    Drives build_release.py through its command line, with the process runner and the clock injected, so
    Nuitka and the built binary never run here. Installed distributions are real dist-info directories in a
    temporary path, read through importlib.metadata as the real build reads them.

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
import subprocess
import sys
import tomllib
from collections.abc import Sequence
from pathlib import Path

import pytest

from connx_mcp import version
from scripts.release import build_release
from scripts.release.build_release import Interpreter, main

# sha256(b"abc"), the FIPS 180-2 test vector.
ABC_SHA256 = "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"


class FakeRunner:
    """Records each command; a Nuitka call writes the binary it was asked for, unless told to fail."""

    def __init__(self, nuitka_exit: int = 0, binary_content: bytes = b"abc") -> None:
        self.calls: list[list[str]] = []
        self.nuitka_exit = nuitka_exit
        self.binary_content = binary_content

    def __call__(self, args: Sequence[str]) -> int:
        self.calls.append(list(args))
        if "nuitka" in args and self.nuitka_exit == 0:
            out_dir = next(a.split("=", 1)[1] for a in args if a.startswith("--output-dir="))
            name = next(a.split("=", 1)[1] for a in args if a.startswith("--output-filename="))
            Path(out_dir, name).write_bytes(self.binary_content)
        return self.nuitka_exit if "nuitka" in args else 0


class FakeClock:
    """Returns the next of a fixed series of readings, in seconds."""

    def __init__(self, readings: Sequence[float]) -> None:
        self.readings = list(readings)

    def __call__(self) -> float:
        return self.readings.pop(0)


def write_dist(
    site: Path,
    name: str,
    version: str,
    requires: Sequence[str] = (),
    licence: str | None = "MIT",
    licence_text: str | None = "MIT licence text",
) -> None:
    """Write an installed distribution's dist-info: METADATA, RECORD and an optional licence file."""
    info = site / f"{name.replace('-', '_')}-{version}.dist-info"
    info.mkdir(parents=True)
    lines = ["Metadata-Version: 2.4", f"Name: {name}", f"Version: {version}"]
    if licence:
        lines.append(f"License-Expression: {licence}")
    lines += [f"Requires-Dist: {r}" for r in requires]
    (info / "METADATA").write_text("\n".join(lines) + "\n", encoding="utf-8")
    record = [f"{info.name}/METADATA,,"]
    if licence_text is not None:
        (info / "licenses").mkdir()
        (info / "licenses" / "LICENSE").write_text(licence_text, encoding="utf-8")
        record.append(f"{info.name}/licenses/LICENSE,,")
    (info / "RECORD").write_text("\n".join(record) + "\n", encoding="utf-8")


@pytest.fixture
def interpreter(tmp_path: Path) -> Interpreter:
    python_licence = tmp_path / "python" / "LICENSE.txt"
    python_licence.parent.mkdir()
    python_licence.write_text("PSF licence text", encoding="utf-8")
    return Interpreter(
        python_version="3.14.2",
        openssl_version="OpenSSL 3.5.4 30 Sep 2025",
        licence_file=python_licence,
        linked={
            "Nuitka": "4.3.0",
            "Zstandard": "1.5.7",
            "zlib": "1.3.1",
            "SQLite": "3.50.4",
            "Expat": "2.7.1",
            "mpdecimal": "4.0.1",
        },
    )


@pytest.fixture
def site(tmp_path: Path) -> Path:
    """An installed connx-mcp-ce with one direct and one transitive dependency."""
    site = tmp_path / "site"
    # connx-mcp-ce's wheel carries its own LICENSE (pyproject `license-files`), as the real build environment's does.
    write_dist(
        site, "connx-mcp-ce", "0.1.0", ["typer>=0.16"], licence="Apache-2.0", licence_text="connx-mcp-ce own text"
    )
    write_dist(site, "typer", "0.27.3", ["rich>=13", "pytest>=8; extra == 'test'"])
    write_dist(site, "rich", "15.0.0", ["colorama; sys_platform == 'win32'"], licence_text="rich MIT text")
    return site


def test_build_compiles_a_onefile_binary_and_writes_its_sha256(
    tmp_path: Path, site: Path, interpreter: Interpreter
) -> None:
    runner = FakeRunner()
    out = tmp_path / "dist"
    # The version comes from the checkout's pyproject.toml, not the repository's (which every bump changes) and
    # not the installed distribution's (0.1.0 in the site fixture).
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    (checkout / "pyproject.toml").write_text('[project]\nname = "connx-mcp-ce"\nversion = "15.0.3"\n', encoding="utf-8")

    code = main(
        ["build", "--out", str(out), "--platform", "linux-x64", "--runs", "3", "--checkout", str(checkout)],
        run=runner,
        clock=FakeClock([0, 1, 1, 1.2, 2, 2.3]),
        site_path=[str(site)],
        interpreter=interpreter,
    )

    assert code == 0
    nuitka = runner.calls[0]
    assert nuitka[1:3] == ["-m", "nuitka"]
    assert "--mode=onefile" in nuitka
    assert "--output-filename=connx-mcp-ce-linux-x64" in nuitka
    assert "--product-name=connx-mcp-ce" in nuitka
    assert "--file-description=CONNX MCP Server Community Edition administration CLI" in nuitka
    [tempdir] = [a for a in nuitka if a.startswith("--onefile-tempdir-spec=")]
    assert tempdir.startswith("--onefile-tempdir-spec={CACHE_DIR}/connx-mcp-ce/15.0.3-")
    assert "--python-flag=-m" in nuitka
    assert "--include-package-data=connx_mcp" in nuitka  # the demo stack's assets and seed users
    assert nuitka[-1].replace("\\", "/").endswith("src/connx_mcp")
    sha256 = (out / "connx-mcp-ce-linux-x64.sha256").read_text(encoding="utf-8")
    assert sha256 == f"{ABC_SHA256}  connx-mcp-ce-linux-x64\n"


def test_python_files_loaded_by_path_ship_as_data_with_their_imports(
    tmp_path: Path, site: Path, interpreter: Interpreter
) -> None:
    package = tmp_path / "src" / "connx_mcp"
    assets = package / "demo" / "assets"
    assets.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "demo" / "__init__.py").write_text("import yaml\n", encoding="utf-8")
    (assets / "loader.py").write_text(
        "import json\nimport yaml\nimport not_installed_anywhere\nfrom sibling import x\n", encoding="utf-8"
    )
    (assets / "sibling.py").write_text("x = 1\n", encoding="utf-8")
    runner = FakeRunner()

    main(
        ["build", "--out", str(tmp_path / "dist"), "--platform", "linux-x64", "--package-dir", str(package)],
        run=runner,
        clock=FakeClock([0, 1] * 10),
        site_path=[str(site)],
        interpreter=interpreter,
    )

    nuitka = runner.calls[0]
    data = sorted(a for a in nuitka if a.startswith("--include-data-files="))
    assert data == [
        f"--include-data-files={assets / 'loader.py'}=connx_mcp/demo/assets/loader.py",
        f"--include-data-files={assets / 'sibling.py'}=connx_mcp/demo/assets/sibling.py",
    ]
    # Only what the compiled code would not otherwise pull in: an installed third-party package.
    assert [a for a in nuitka if a.startswith("--include-package=")] == ["--include-package=yaml"]
    assert nuitka[-1] == str(package)


def test_build_names_the_windows_binary_with_exe(tmp_path: Path, site: Path, interpreter: Interpreter) -> None:
    runner = FakeRunner()
    out = tmp_path / "dist"

    code = main(
        ["build", "--out", str(out), "--platform", "windows-x64", "--runs", "1"],
        run=runner,
        clock=FakeClock([0, 1]),
        site_path=[str(site)],
        interpreter=interpreter,
    )

    assert code == 0
    assert "--output-filename=connx-mcp-ce-windows-x64.exe" in runner.calls[0]
    assert (out / "connx-mcp-ce-windows-x64.exe.sha256").exists()


def test_require_msvc_asks_nuitka_for_msvc_on_windows(tmp_path: Path, site: Path, interpreter: Interpreter) -> None:
    runner = FakeRunner()

    code = main(
        ["build", "--out", str(tmp_path / "dist"), "--platform", "windows-x64", "--runs", "1", "--require-msvc"],
        run=runner,
        clock=FakeClock([0, 1]),
        site_path=[str(site)],
        interpreter=interpreter,
    )

    assert code == 0
    assert "--msvc=latest" in runner.calls[0]


def test_without_require_msvc_nuitka_chooses_the_compiler(tmp_path: Path, site: Path, interpreter: Interpreter) -> None:
    runner = FakeRunner()

    main(
        ["build", "--out", str(tmp_path / "dist"), "--platform", "windows-x64", "--runs", "1"],
        run=runner,
        clock=FakeClock([0, 1]),
        site_path=[str(site)],
        interpreter=interpreter,
    )

    assert not [a for a in runner.calls[0] if a.startswith("--msvc")]


def test_require_msvc_is_refused_for_a_build_that_is_not_for_windows(
    tmp_path: Path, site: Path, interpreter: Interpreter, capsys: pytest.CaptureFixture[str]
) -> None:
    runner = FakeRunner()

    code = main(
        ["build", "--out", str(tmp_path / "dist"), "--platform", "linux-x64", "--require-msvc"],
        run=runner,
        clock=FakeClock([]),
        site_path=[str(site)],
        interpreter=interpreter,
    )

    assert code == 1
    assert runner.calls == []
    assert "--require-msvc applies to a windows build only" in capsys.readouterr().err


def test_a_failed_nuitka_build_fails_and_writes_no_sum(tmp_path: Path, site: Path, interpreter: Interpreter) -> None:
    out = tmp_path / "dist"

    code = main(
        ["build", "--out", str(out), "--platform", "linux-x64"],
        run=FakeRunner(nuitka_exit=1),
        clock=FakeClock([]),
        site_path=[str(site)],
        interpreter=interpreter,
    )

    assert code == 1
    assert not list(out.glob("*.sha256"))


def test_an_editable_install_is_refused_before_nuitka_runs(
    tmp_path: Path, site: Path, interpreter: Interpreter, capsys: pytest.CaptureFixture[str]
) -> None:
    """An editable connx-mcp-ce lists no package in its RECORD, so Nuitka cannot include its metadata."""
    (site / "connx_mcp_ce-0.1.0.dist-info" / "direct_url.json").write_text(
        '{"url": "file:///src", "dir_info": {"editable": true}}', encoding="utf-8"
    )
    runner = FakeRunner()

    code = main(
        ["build", "--out", str(tmp_path / "dist"), "--platform", "linux-x64"],
        run=runner,
        clock=FakeClock([]),
        site_path=[str(site)],
        interpreter=interpreter,
    )

    assert code == 1
    assert runner.calls == []
    error = capsys.readouterr().err
    assert "installed editable" in error
    assert "uv run --no-editable" in error


@pytest.fixture
def checkout(tmp_path: Path) -> Path:
    """A build checkout whose pyproject.toml and uv.lock declare 15.0.0."""
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    (checkout / "pyproject.toml").write_text('[project]\nname = "connx-mcp-ce"\nversion = "15.0.0"\n', encoding="utf-8")
    (checkout / "uv.lock").write_text(
        'version = 1\n\n[[package]]\nname = "connx-mcp-ce"\nversion = "15.0.0"\nsource = { editable = "." }\n',
        encoding="utf-8",
    )
    return checkout


def build_windows(tmp_path: Path, checkout: Path, site: Path, interpreter: Interpreter) -> list[str]:
    """Build the Windows binary of a checkout; return the Nuitka command."""
    runner = FakeRunner()
    args = ["build", "--out", str(tmp_path / "dist"), "--platform", "windows-x64", "--runs", "1"]
    code = main(
        [*args, "--checkout", str(checkout)],
        run=runner,
        clock=FakeClock([0, 1]),
        site_path=[str(site)],
        interpreter=interpreter,
    )
    assert code == 0
    return runner.calls[0]


@pytest.mark.parametrize(
    ("build", "product_version", "file_version"),
    [("26004", "15.0.0.26004", "15.0.0.26004"), ("dev408", "15.0.0.dev408", "15.0.0.0")],
    ids=["release-build", "development-build"],
)
def test_a_stamped_build_carries_its_product_version_and_a_four_number_windows_file_version(
    tmp_path: Path,
    checkout: Path,
    site: Path,
    interpreter: Interpreter,
    capsys: pytest.CaptureFixture[str],
    build: str,
    product_version: str,
    file_version: str,
) -> None:
    assert main(["stamp", build, "--checkout", str(checkout)]) == 0
    assert capsys.readouterr().out == f"{product_version}\n"

    nuitka = build_windows(tmp_path, checkout, site, interpreter)

    assert f"--file-version={file_version}" in nuitka
    assert f"--product-version={file_version}" in nuitka
    [tempdir] = [a for a in nuitka if a.startswith("--onefile-tempdir-spec=")]
    assert tempdir.startswith(f"--onefile-tempdir-spec={{CACHE_DIR}}/connx-mcp-ce/{product_version}-")
    # The binary reads its Product version from the package metadata, which Nuitka must ship.
    assert "--include-distribution-metadata=connx-mcp-ce" in nuitka


def test_an_unstamped_checkout_builds_with_file_version_major_minor_fix_0(
    tmp_path: Path, checkout: Path, site: Path, interpreter: Interpreter
) -> None:
    nuitka = build_windows(tmp_path, checkout, site, interpreter)

    assert "--file-version=15.0.0.0" in nuitka


def test_a_checkout_version_of_another_shape_is_refused_never_built_as_0_0_0_0(
    tmp_path: Path, checkout: Path, site: Path, interpreter: Interpreter
) -> None:
    pyproject = checkout / "pyproject.toml"
    pyproject.write_text(pyproject.read_text(encoding="utf-8").replace('"15.0.0"', '"15.0rc1"'), encoding="utf-8")

    with pytest.raises(ValueError, match=r"15\.0rc1"):
        build_windows(tmp_path, checkout, site, interpreter)


def test_stamp_refuses_a_build_of_another_shape_and_writes_nothing(
    checkout: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    before = (checkout / "pyproject.toml").read_text(encoding="utf-8")

    assert main(["stamp", "dev-408", "--checkout", str(checkout)]) == 1

    assert "15.0.0.dev-408" in capsys.readouterr().err
    assert (checkout / "pyproject.toml").read_text(encoding="utf-8") == before


def test_the_steps_that_need_no_project_run_without_it_installed(tmp_path: Path) -> None:
    # The public release runs check-tag, checksums and render-notices with `uv run --no-project`: no connx_mcp.
    binary = tmp_path / "connx-mcp-ce-linux-x64"
    binary.write_bytes(b"abc")
    script = build_release.REPO / "scripts" / "release" / "build_release.py"

    result = subprocess.run(
        [sys.executable, "-I", "-S", str(script), "checksums", str(binary), "--output", str(tmp_path / "SHA256SUMS")],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert (tmp_path / "SHA256SUMS").read_text(encoding="utf-8") == f"{ABC_SHA256}  connx-mcp-ce-linux-x64\n"


def test_check_tag_reads_the_version_without_the_project_installed(checkout: Path) -> None:
    version.stamp(checkout, "15.0.0.26004")
    script = build_release.REPO / "scripts" / "release" / "build_release.py"

    result = subprocess.run(
        [sys.executable, "-I", "-S", str(script), "check-tag", "15.0.0.26004", "--checkout", str(checkout)],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr


def test_checksums_lists_every_binary_in_sha256sum_format(tmp_path: Path) -> None:
    (tmp_path / "connx-mcp-ce-linux-x64").write_bytes(b"abc")
    (tmp_path / "connx-mcp-ce-windows-x64.exe").write_bytes(b"")
    sums = tmp_path / "SHA256SUMS"

    code = main(
        [
            "checksums",
            str(tmp_path / "connx-mcp-ce-windows-x64.exe"),
            str(tmp_path / "connx-mcp-ce-linux-x64"),
            "--output",
            str(sums),
        ]
    )

    assert code == 0
    assert sums.read_text(encoding="utf-8") == (
        f"{ABC_SHA256}  connx-mcp-ce-linux-x64\n"
        "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855  connx-mcp-ce-windows-x64.exe\n"
    )


def test_build_measures_start_up_time_of_the_built_binary(tmp_path: Path, site: Path, interpreter: Interpreter) -> None:
    runner = FakeRunner()
    out = tmp_path / "dist"

    # Three runs of --help: 1000 ms (first, unpacks the payload), then 200 ms and 300 ms.
    main(
        ["build", "--out", str(out), "--platform", "linux-x64", "--runs", "3"],
        run=runner,
        clock=FakeClock([0, 1, 1, 1.2, 2, 2.3]),
        site_path=[str(site)],
        interpreter=interpreter,
    )

    binary = str(out / "connx-mcp-ce-linux-x64")
    assert runner.calls[1:] == [[binary, "--help"]] * 3
    startup = json.loads((out / "startup-linux-x64.json").read_text(encoding="utf-8"))
    assert startup == {
        "platform": "linux-x64",
        "command": "connx-mcp-ce --help",
        "runs": 3,
        "first_ms": 1000,
        "median_ms": 250,
        "max_ms": 300,
    }


def test_measure_reports_start_up_of_a_given_binary(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    binary = tmp_path / "connx-mcp-ce"
    runner = FakeRunner()

    code = main(
        ["measure", str(binary), "--platform", "linux-x64", "--runs", "2"],
        run=runner,
        clock=FakeClock([0, 0.5, 1, 1.1]),
    )

    assert code == 0
    assert json.loads(capsys.readouterr().out)["median_ms"] == 100


def test_a_failing_binary_fails_the_measurement(tmp_path: Path) -> None:
    def broken(args: Sequence[str]) -> int:
        return 2

    code = main(["measure", str(tmp_path / "connx-mcp-ce"), "--runs", "1"], run=broken, clock=FakeClock([0, 1]))

    assert code == 1


def test_measure_refuses_zero_runs(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exit_info:
        main(["measure", str(tmp_path / "connx-mcp-ce"), "--runs", "0"])

    assert exit_info.value.code == 2
    assert "expected 1 or more" in capsys.readouterr().err


def test_notices_cover_the_runtime_closure_and_the_fixed_entries(
    tmp_path: Path, site: Path, interpreter: Interpreter
) -> None:
    collected = tmp_path / "notices-linux-x64.json"
    rendered = tmp_path / "THIRD-PARTY-NOTICES"

    args = ["collect-notices", str(collected), "--platform", "linux-x64"]
    assert main(args, site_path=[str(site)], interpreter=interpreter) == 0
    assert main(["render-notices", str(collected), "--output", str(rendered)]) == 0

    text = rendered.read_text(encoding="utf-8")
    assert text.startswith("THIRD-PARTY NOTICES for connx-mcp-ce 0.1.0\n")
    headings = [line for line in text.splitlines() if line.startswith("## ")]
    assert headings[:2] == ["## CPython 3.14.2 (PSF-2.0)", "## OpenSSL 3.5.4 (Apache-2.0)"]
    assert headings[-2:] == ["## rich 15.0.0 (MIT)", "## typer 0.27.3 (MIT)"]
    assert "PSF licence text" in text
    assert "Apache License" in text and "Version 2.0, January 2004" in text
    assert "rich MIT text" in text
    assert "pytest" not in text  # an extra's dependency is not shipped
    assert "colorama" not in text  # a dependency for another platform, not installed here
    # PRODUCT is not third-party software: its LICENSE and NOTICE ship beside the notices.
    assert "connx-mcp-ce own text" not in text
    assert not [heading for heading in headings if heading.startswith("## connx-mcp")]


def test_the_distribution_is_named_one_way_by_the_project_the_build_and_the_installed_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with (build_release.REPO / "pyproject.toml").open("rb") as handle:
        project = tomllib.load(handle)["project"]["name"]
    read: list[str] = []

    def metadata_version(distribution: str) -> str:
        read.append(distribution)
        return "1.2.3"

    # installed_version() reads the version through importlib.metadata; record which distribution it asks for.
    monkeypatch.setattr("connx_mcp.version.metadata.version", metadata_version)

    assert version.installed_version() == "1.2.3"
    assert read == [project]
    assert build_release.ROOT_DISTRIBUTION == project


def test_render_merges_both_platforms_without_duplicates(tmp_path: Path, site: Path, interpreter: Interpreter) -> None:
    linux = tmp_path / "notices-linux-x64.json"
    windows = tmp_path / "notices-windows-x64.json"
    write_dist(site, "colorama", "0.4.6", licence="BSD-3-Clause", licence_text="colorama BSD text")
    main(["collect-notices", str(linux)], site_path=[str(site)], interpreter=interpreter)
    main(["collect-notices", str(windows)], site_path=[str(site)], interpreter=interpreter)
    rendered = tmp_path / "THIRD-PARTY-NOTICES"

    assert main(["render-notices", str(linux), str(windows), "--output", str(rendered)]) == 0

    headings = [line for line in rendered.read_text(encoding="utf-8").splitlines() if line.startswith("## ")]
    assert headings.count("## rich 15.0.0 (MIT)") == 1
    assert "## colorama 0.4.6 (BSD-3-Clause)" in headings


def test_a_dependency_without_a_licence_text_fails_the_notices(
    tmp_path: Path, site: Path, interpreter: Interpreter, capsys: pytest.CaptureFixture[str]
) -> None:
    write_dist(site, "shellingham", "1.5.4", licence="ISC", licence_text=None)
    (site / "typer-0.27.3.dist-info" / "METADATA").write_text(
        "Metadata-Version: 2.4\nName: typer\nVersion: 0.27.3\nLicense-Expression: MIT\n"
        "Requires-Dist: rich>=13\nRequires-Dist: shellingham>=1.3\n",
        encoding="utf-8",
    )

    code = main(["collect-notices", str(tmp_path / "n.json")], site_path=[str(site)], interpreter=interpreter)

    assert code == 1
    assert "shellingham" in capsys.readouterr().err


def test_a_missing_python_licence_fails_the_notices(
    tmp_path: Path, site: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    missing = Interpreter("3.14.2", "OpenSSL 3.5.4 30 Sep 2025", None)

    code = main(["collect-notices", str(tmp_path / "n.json")], site_path=[str(site)], interpreter=missing)

    assert code == 1
    assert "CPython" in capsys.readouterr().err


def test_licence_from_a_classifier_when_no_expression(tmp_path: Path, site: Path, interpreter: Interpreter) -> None:
    meta = site / "rich-15.0.0.dist-info" / "METADATA"
    meta.write_text(
        "Metadata-Version: 2.1\nName: rich\nVersion: 15.0.0\nClassifier: License :: OSI Approved :: MIT License\n",
        encoding="utf-8",
    )
    collected = tmp_path / "n.json"
    rendered = tmp_path / "out"

    main(["collect-notices", str(collected)], site_path=[str(site)], interpreter=interpreter)
    main(["render-notices", str(collected), "--output", str(rendered)])

    assert "## rich 15.0.0 (MIT License)" in rendered.read_text(encoding="utf-8")


def test_current_interpreter_finds_its_licence_file() -> None:
    current = build_release.Interpreter.current()

    assert current.licence_file is not None and current.licence_file.is_file()
    assert current.openssl_version.startswith("OpenSSL ")


def native_headings(tmp_path: Path, site: Path, interpreter: Interpreter, platform_tag: str) -> list[str]:
    collected = tmp_path / f"notices-{platform_tag}.json"
    rendered = tmp_path / f"THIRD-PARTY-NOTICES-{platform_tag}"
    args = ["collect-notices", str(collected), "--platform", platform_tag]
    assert main(args, site_path=[str(site)], interpreter=interpreter) == 0
    assert main(["render-notices", str(collected), "--output", str(rendered)]) == 0
    text = rendered.read_text(encoding="utf-8")
    return [line for line in text.splitlines() if line.startswith("## ")]


def test_notices_carry_the_onefile_bootstrap_and_the_c_libraries_linked_into_cpython(
    tmp_path: Path, site: Path, interpreter: Interpreter
) -> None:
    headings = native_headings(tmp_path, site, interpreter, "linux-x64")

    for expected in (
        "## Nuitka 4.3.0 (Apache-2.0)",
        "## Zstandard 1.5.7 (BSD-3-Clause)",
        "## zlib 1.3.1 (Zlib)",
        "## SQLite 3.50.4 (blessing)",
        "## Expat 2.7.1 (MIT)",
        "## mpdecimal 4.0.1 (BSD-2-Clause)",
        "## bzip2 bundled with CPython 3.14.2 (bzip2-1.0.6)",
        "## XZ Utils (liblzma) bundled with CPython 3.14.2 (0BSD)",
        "## libffi bundled with CPython 3.14.2 (MIT)",
        "## libuuid bundled with CPython 3.14.2 (BSD-3-Clause)",
        "## ncurses bundled with CPython 3.14.2 (X11)",
        "## libedit bundled with CPython 3.14.2 (BSD-3-Clause)",
        "## mimalloc bundled with CPython 3.14.2 (MIT)",
        "## HACL* bundled with CPython 3.14.2 (MIT)",
    ):
        assert expected in headings


def test_windows_notices_leave_out_the_linux_only_libraries(
    tmp_path: Path, site: Path, interpreter: Interpreter
) -> None:
    headings = native_headings(tmp_path, site, interpreter, "windows-x64")

    assert "## zlib 1.3.1 (Zlib)" in headings
    assert "## mimalloc bundled with CPython 3.14.2 (MIT)" in headings
    assert "## HACL* bundled with CPython 3.14.2 (MIT)" in headings
    assert not [h for h in headings if h.split()[1] in ("libuuid", "ncurses", "libedit")]


def test_every_native_entry_carries_its_licence_text(tmp_path: Path, site: Path, interpreter: Interpreter) -> None:
    collected = tmp_path / "n.json"
    main(["collect-notices", str(collected), "--platform", "linux-x64"], site_path=[str(site)], interpreter=interpreter)

    fixed = json.loads(collected.read_text(encoding="utf-8"))["fixed"]
    texts = {entry["name"]: entry["texts"][0]["text"] for entry in fixed}
    assert "Jean-loup Gailly and Mark Adler" in texts["zlib"]
    assert "Julian Seward" in texts["bzip2"]
    assert "Stefan Krah" in texts["mpdecimal"]
    assert "Daan Leijen" in texts["mimalloc"]
    assert "HACL* Contributors" in texts["HACL*"]
    assert "Meta Platforms" in texts["Zstandard"] or "Facebook" in texts["Zstandard"]
    assert all(entry["texts"] and entry["texts"][0]["text"].strip() for entry in fixed)


def test_an_unknown_licence_field_fails_the_notices(
    tmp_path: Path, site: Path, interpreter: Interpreter, capsys: pytest.CaptureFixture[str]
) -> None:
    (site / "rich-15.0.0.dist-info" / "METADATA").write_text(
        "Metadata-Version: 2.1\nName: rich\nVersion: 15.0.0\nLicense: UNKNOWN\n", encoding="utf-8"
    )

    code = main(["collect-notices", str(tmp_path / "n.json")], site_path=[str(site)], interpreter=interpreter)

    assert code == 1
    assert "rich" in capsys.readouterr().err


def test_an_unknown_licence_field_falls_back_to_the_classifier(
    tmp_path: Path, site: Path, interpreter: Interpreter
) -> None:
    (site / "rich-15.0.0.dist-info" / "METADATA").write_text(
        "Metadata-Version: 2.1\nName: rich\nVersion: 15.0.0\nLicense: UNKNOWN\n"
        "Classifier: License :: OSI Approved :: MIT License\n",
        encoding="utf-8",
    )

    headings = native_headings(tmp_path, site, interpreter, "linux-x64")

    assert "## rich 15.0.0 (MIT License)" in headings


def test_check_tag_accepts_the_bare_tag_of_the_staged_product_version(checkout: Path) -> None:
    # The public repository's tag is the Release build's Product version, which the export stamps.
    version.stamp(checkout, "15.0.0.26004")

    assert main(["check-tag", "15.0.0.26004", "--checkout", str(checkout)]) == 0
    assert main(["check-tag", "refs/tags/15.0.0.26004", "--checkout", str(checkout)]) == 0


@pytest.mark.parametrize(
    ("tag", "stamped", "says"),
    [
        ("v15.0.0.26004", "15.0.0.26004", "does not match pyproject.toml's version 15.0.0.26004"),
        ("15.0.0.26003", "15.0.0.26004", "does not match pyproject.toml's version 15.0.0.26004"),
        ("15.0.0", None, "is not a Release build's Product version"),
        ("15.0.0.dev408", "15.0.0.dev408", "is not a Release build's Product version"),
    ],
)
def test_check_tag_refuses_a_tag_other_than_the_staged_release_builds(
    checkout: Path, capsys: pytest.CaptureFixture[str], tag: str, stamped: str | None, says: str
) -> None:
    if stamped:
        version.stamp(checkout, stamped)

    assert main(["check-tag", tag, "--checkout", str(checkout)]) == 1

    err = capsys.readouterr().err
    assert f"tag {tag} " in err
    assert says in err


def test_current_interpreter_records_the_linked_library_versions() -> None:
    linked = build_release.Interpreter.current().linked

    assert {"zlib", "SQLite", "Expat", "mpdecimal"} <= set(linked)


@pytest.mark.parametrize(
    "fields",
    [
        "License-Expression: UNKNOWN\n",
        "License: UNKNOWN\nClassifier: License :: OSI Approved\n",
    ],
)
def test_a_licence_that_names_no_licence_fails_the_notices(
    tmp_path: Path, site: Path, interpreter: Interpreter, capsys: pytest.CaptureFixture[str], fields: str
) -> None:
    (site / "rich-15.0.0.dist-info" / "METADATA").write_text(
        f"Metadata-Version: 2.4\nName: rich\nVersion: 15.0.0\n{fields}", encoding="utf-8"
    )

    code = main(["collect-notices", str(tmp_path / "n.json")], site_path=[str(site)], interpreter=interpreter)

    assert code == 1
    assert "rich" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("system", "machine", "tag"),
    [
        ("linux", "x86_64", "linux-x64"),
        ("win32", "AMD64", "windows-x64"),
        ("darwin", "arm64", "macos-arm64"),
    ],
)
def test_this_machine_names_its_release_platform(
    monkeypatch: pytest.MonkeyPatch, system: str, machine: str, tag: str
) -> None:
    monkeypatch.setattr(build_release.sys, "platform", system)
    monkeypatch.setattr(build_release.platform, "machine", lambda: machine)

    assert build_release.current_platform() == tag
