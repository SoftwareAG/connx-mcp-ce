#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""build_release.py - Builds a connx-mcp-ce release binary with its SHA-256 sum, notices and start-up time.

Description:
    The maintainer's release build, one subcommand per step so CI and a person run the same thing.
    `build` compiles this OS's single-file binary with Nuitka into --out, writes <binary>.sha256, collects the
    third-party notices and measures start-up time; it runs in the build environment
    (`uv run --locked --no-dev --no-editable --group build python scripts/release/build_release.py build`);
    it refuses an editable install of connx-mcp-ce, whose metadata Nuitka cannot include.
    `checksums` writes one SHA256SUMS file (`sha256sum -c` format) for the given binaries. `collect-notices`
    records, as JSON, the licences of the runtime dependency closure of connx-mcp-ce as installed in this
    environment, plus fixed entries for what no distribution metadata describes: CPython, OpenSSL, Nuitka's
    runtime and onefile bootstrap (with its Zstandard decompressor) and the C libraries linked into CPython.
    `render-notices` merges the JSON of each platform into one THIRD-PARTY-NOTICES file. `measure` times
    `<binary> --help`, the first run apart (it unpacks the onefile payload). `check-tag` fails when a release
    tag is not the Release build's Product version that the export stamped into pyproject.toml (the bare tag,
    such as 15.0.0.26004), so a release never carries binaries of another version.
    `stamp <build>` writes the Product version (major.minor.fix plus a Build number or `dev<run number>`) into the
    checkout's pyproject.toml and uv.lock before the build environment installs it; the binary's `--version` reads
    it from the package metadata, and its Windows file version is four numbers (`major.minor.fix.0` unless a
    Release build). CI stamps a checkout and never commits it.

    Package data (the demo stack's assets) is bundled; a `.py` file outside any package is loaded by path at run
    time, so it ships as data and the installed third-party packages it imports are compiled in.

    The binary unpacks into a per-build cache directory ({CACHE_DIR}/connx-mcp-ce/<version>-<build id>) and reuses
    it on later runs, because an LLM driving the CLI starts it dozens of times per session.

Copyright:
    Copyright 2026 Software GmbH
    Licensed under the Apache License, Version 2.0 (the "License"); you may not use this file except in compliance
    with the License. You may obtain a copy of the License at https://www.apache.org/licenses/LICENSE-2.0
    Unless required by applicable law or agreed to in writing, software distributed under the License is distributed
    on an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the License
    for the specific language governing permissions and limitations under the License.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import importlib
import importlib.util
import json
import platform
import re
import ssl
import statistics
import subprocess
import sys
import sysconfig
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from importlib import metadata
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
PACKAGE_DIR = REPO / "src" / "connx_mcp"
sys.path.insert(0, str(PACKAGE_DIR.parent))

# The version helper imports only the standard library, so every step can use it, the `--no-project` ones too.
from connx_mcp import version as product  # noqa: E402

LICENCES = Path(__file__).resolve().parent / "licences"
ROOT_DISTRIBUTION = "connx-mcp-ce"
PRODUCT = "connx-mcp-ce"

Runner = Callable[[Sequence[str]], int]
Clock = Callable[[], float]


class ReleaseError(Exception):
    """A release step cannot complete; the message says what is missing."""


def run_process(args: Sequence[str]) -> int:
    """Run a command with its output reaching the terminal, and return its exit code."""
    return subprocess.run(list(args), check=False).returncode


def run_quietly(args: Sequence[str]) -> int:
    """Run a command whose stdout is not wanted (a timed binary); stderr still reaches the terminal."""
    return subprocess.run(list(args), check=False, stdout=subprocess.DEVNULL).returncode


def current_platform() -> str:
    """Name this machine's release platform, such as `linux-x64`, `windows-x64` or `macos-arm64`."""
    system = {"win32": "windows", "darwin": "macos"}.get(sys.platform, sys.platform)
    machine = platform.machine().lower()
    arch = "x64" if machine in ("x86_64", "amd64") else machine
    return f"{system}-{arch}"


def binary_name(platform_tag: str) -> str:
    """The release asset name of the binary for a platform."""
    suffix = ".exe" if platform_tag.startswith("windows") else ""
    return f"{PRODUCT}-{platform_tag}{suffix}"


def sha256_of(path: Path) -> str:
    """The SHA-256 hex digest of a file."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def sum_lines(paths: Sequence[Path]) -> str:
    """Checksum lines in `sha256sum` format, sorted by file name."""
    return "".join(f"{sha256_of(p)}  {p.name}\n" for p in sorted(paths, key=lambda p: p.name))


def sources_loaded_by_path(package_dir: Path) -> list[Path]:
    """Python files in the package tree outside any package: the CLI loads them by path, so they ship as data.

    Args:
        package_dir (Path): The connx_mcp package directory.

    Returns:
        list[Path]: Each `.py` file whose directory has no `__init__.py`, sorted.
    """
    return sorted(p for p in package_dir.rglob("*.py") if not (p.parent / "__init__.py").exists())


def third_party_imports(sources: Sequence[Path]) -> list[str]:
    """The installed third-party packages that path-loaded sources import, which Nuitka cannot see.

    Args:
        sources (Sequence[Path]): Python files that ship as data.

    Returns:
        list[str]: Top-level package names, sorted; the standard library, the sources' siblings and anything not
        installed here (a module the file only needs inside a container) are left out.
    """
    siblings = {p.stem for p in sources}
    names: set[str] = set()
    for source in sources:
        for node in ast.walk(ast.parse(source.read_text(encoding="utf-8"), str(source))):
            if isinstance(node, ast.Import):
                names.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names.add(node.module.split(".")[0])
    candidates = names - siblings - set(sys.stdlib_module_names)
    return sorted(n for n in candidates if importlib.util.find_spec(n) is not None)


def nuitka_command(
    out: Path, platform_tag: str, version: str, build_id: str, package_dir: Path, *, require_msvc: bool = False
) -> list[str]:
    """The Nuitka command that compiles the connx_mcp package into one single-file binary.

    Args:
        out (Path): The output directory.
        platform_tag (str): The platform the binary is for.
        version (str): The connx-mcp-ce version: a Product version, or major.minor.fix in an unstamped checkout.
        build_id (str): Makes the onefile cache directory unique to this build.
        package_dir (Path): The connx_mcp package directory to compile.
        require_msvc (bool): Compile with MSVC or fail. Without it Nuitka falls back to zig where MSVC is missing,
            and a zig binary needs the Windows runtime DLLs installed on the machine that runs it.

    Returns:
        list[str]: The command line.
    """
    numeric = product.file_version(version)
    command = [
        sys.executable,
        "-m",
        "nuitka",
        "--mode=onefile",
        "--python-flag=-m",
        "--include-package-data=connx_mcp",
        "--assume-yes-for-downloads",
        f"--output-dir={out}",
        f"--output-filename={binary_name(platform_tag)}",
        f"--onefile-tempdir-spec={{CACHE_DIR}}/{PRODUCT}/{version}-{build_id}",
        # `--version` reads the Product version from the package metadata.
        f"--include-distribution-metadata={ROOT_DISTRIBUTION}",
        "--company-name=Software GmbH",
        f"--product-name={PRODUCT}",
        f"--file-version={numeric}",
        f"--product-version={numeric}",
        "--file-description=CONNX MCP Server Community Edition administration CLI",
    ]
    if require_msvc:
        command.append("--msvc=latest")
    loaded = sources_loaded_by_path(package_dir)
    command += [f"--include-data-files={p}={package_dir.name}/{p.relative_to(package_dir).as_posix()}" for p in loaded]
    command += [f"--include-package={name}" for name in third_party_imports(loaded)]
    return [*command, str(package_dir)]


# --- Start-up time -------------------------------------------------------------------------------------------------


def measure_startup(binary: Path, platform_tag: str, runs: int, run: Runner, clock: Clock) -> dict[str, object]:
    """Time `binary --help` `runs` times; the first run (unpacking the payload) apart from the median of the rest.

    Args:
        binary (Path): The built binary.
        platform_tag (str): The platform it was built for.
        runs (int): How many times to start it.
        run (Runner): Runs one command and returns its exit code.
        clock (Clock): A monotonic clock in seconds.

    Returns:
        dict[str, object]: The measurement: platform, command, runs, first_ms, and the warm runs' median_ms and max_ms.

    Raises:
        ReleaseError: The binary exited with an error.
    """
    timings: list[float] = []
    for _ in range(runs):
        started = clock()
        code = run([str(binary), "--help"])
        timings.append((clock() - started) * 1000)
        if code != 0:
            raise ReleaseError(f"{binary} --help exited with {code}")
    warm = timings[1:] or timings
    return {
        "platform": platform_tag,
        "command": f"{PRODUCT} --help",
        "runs": runs,
        "first_ms": round(timings[0]),
        "median_ms": round(statistics.median(warm)),
        "max_ms": round(max(warm)),
    }


# --- Third-party notices -------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Interpreter:
    """The CPython build compiled into the binary: its version, its OpenSSL, its licence file and linked libraries.

    Attributes:
        python_version (str): The CPython version.
        openssl_version (str): `ssl.OPENSSL_VERSION`.
        licence_file (Path | None): CPython's LICENSE.txt, None when it was not found.
        linked (dict[str, str]): Native component name to the version this interpreter reports; a component that
            reports none is absent.
    """

    python_version: str
    openssl_version: str
    licence_file: Path | None
    linked: dict[str, str] = field(default_factory=dict)

    @classmethod
    def current(cls) -> Interpreter:
        """Describe the interpreter running this script, which Nuitka compiles into the binary.

        Returns:
            Interpreter: The running interpreter's versions and the LICENSE.txt it ships.
        """
        prefix = Path(sys.base_prefix)
        candidates = [
            Path(sysconfig.get_path("stdlib")) / "LICENSE.txt",
            prefix / "LICENSE.txt",
            prefix / "lib" / f"python{sys.version_info.major}.{sys.version_info.minor}" / "LICENSE.txt",
        ]
        licence = next((c for c in candidates if c.is_file()), None)
        return cls(platform.python_version(), ssl.OPENSSL_VERSION, licence, linked_versions())


def _nuitka_version() -> str:
    """Nuitka's version in this environment (the build environment installs it)."""
    return metadata.version("nuitka")


def _zstd_version() -> str:
    """The Zstandard version CPython links."""
    return ".".join(map(str, importlib.import_module("compression.zstd").zstd_version_info))


def _zlib_version() -> str:
    """The zlib version CPython links."""
    return importlib.import_module("zlib").ZLIB_RUNTIME_VERSION


def _sqlite_version() -> str:
    """The SQLite version CPython links."""
    return importlib.import_module("sqlite3").sqlite_version


def _expat_version() -> str:
    """The Expat version CPython links."""
    return importlib.import_module("pyexpat").EXPAT_VERSION.removeprefix("expat_")


def _mpdecimal_version() -> str:
    """The mpdecimal version CPython links."""
    return importlib.import_module("decimal").__libmpdec_version__


# How this interpreter reports the version of each native component; a library CPython links without reporting
# its version (bzip2, liblzma, libffi, libuuid, ncurses, libedit) has no probe.
VERSION_PROBES: dict[str, Callable[[], str]] = {
    "Nuitka": _nuitka_version,
    "Zstandard": _zstd_version,
    "zlib": _zlib_version,
    "SQLite": _sqlite_version,
    "Expat": _expat_version,
    "mpdecimal": _mpdecimal_version,
}


def linked_versions() -> dict[str, str]:
    """The versions this interpreter reports for the native components it links, and Nuitka's when installed.

    Returns:
        dict[str, str]: Component name to version; a component whose module is missing here is left out.
    """
    versions: dict[str, str] = {}
    for name, probe in VERSION_PROBES.items():
        try:
            versions[name] = str(probe())
        except ImportError, AttributeError, metadata.PackageNotFoundError:
            continue
    return versions


# Values old metadata writes in the License field when the licence is not stated: treated as no licence.
UNKNOWN_LICENCES = frozenset({"UNKNOWN", "NONE", "N/A"})

# Licence classifiers that name a category, not a licence (`License :: OSI Approved` with nothing after it).
LICENCE_CATEGORIES = frozenset({"OSI Approved", "DFSG approved"})


def _canonical(name: str) -> str:
    """Normalise a distribution name (PEP 503)."""
    return re.sub(r"[-_.]+", "-", name).lower()


def _licence_of(dist: metadata.Distribution) -> str | None:
    """A distribution's licence: its SPDX expression, a short License field, or its licence classifier."""
    meta = dist.metadata
    expression = (meta.get("License-Expression") or "").strip()
    if expression and expression.upper() not in UNKNOWN_LICENCES:
        return expression
    short = (meta.get("License") or "").strip()
    if short and short.upper() not in UNKNOWN_LICENCES and "\n" not in short and len(short) <= 80:
        return short
    classifiers = [c.split("::")[-1].strip() for c in meta.get_all("Classifier") or [] if c.startswith("License ::")]
    classifiers = [c for c in classifiers if c not in LICENCE_CATEGORIES]
    return ", ".join(classifiers) or None


def _licence_texts(dist: metadata.Distribution) -> list[dict[str, str]]:
    """The licence and notice files a distribution installs in its dist-info."""
    texts = []
    for file in dist.files or []:
        parts = file.parts
        in_info = len(parts) > 1 and parts[0].endswith(".dist-info")
        named = re.match(r"(LICEN[CS]E|COPYING|NOTICE|AUTHORS)", file.name, re.IGNORECASE) is not None
        if in_info and ("licenses" in parts or "license_files" in parts or named):
            text = Path(str(dist.locate_file(file))).read_text(encoding="utf-8", errors="replace")
            texts.append({"file": file.name, "text": text})
    return texts


def _requirements(dist: metadata.Distribution) -> list[tuple[str, bool]]:
    """A distribution's runtime requirements as (name, has a marker); an extra's requirements are left out."""
    found = []
    for requirement in dist.requires or []:
        spec, _, marker = requirement.partition(";")
        if "extra" in marker:
            continue
        name = re.match(r"\s*([A-Za-z0-9._-]+)", spec)
        if name:
            found.append((_canonical(name.group(1)), bool(marker.strip())))
    return found


def collect_notices(site_path: list[str] | None, interpreter: Interpreter, platform_tag: str) -> dict[str, object]:
    """Record the licences of everything compiled into the binary.

    The closure is walked from connx-mcp-ce's own requirements in this environment. A requirement with a marker that
    is not installed belongs to another platform and is skipped; the other platform's build records it.

    Args:
        site_path (list[str] | None): Where to look for installed distributions; None is sys.path.
        interpreter (Interpreter): The CPython build, for the fixed entries.
        platform_tag (str): The platform the binary is built for; some native libraries are linked only on Linux.

    Returns:
        dict[str, object]: The product version, the fixed entries and the entries: name, version, licence, texts.

    Raises:
        ReleaseError: A distribution or a licence text is missing.
    """
    installed = {
        _canonical(d.metadata["Name"]): d
        for d in metadata.distributions(path=sys.path if site_path is None else site_path)
    }
    if ROOT_DISTRIBUTION not in installed:
        raise ReleaseError(f"{ROOT_DISTRIBUTION} is not installed in this environment")
    root = installed[ROOT_DISTRIBUTION]
    seen: dict[str, metadata.Distribution] = {}
    pending = _requirements(root)
    while pending:
        name, has_marker = pending.pop()
        if name in seen:
            continue
        if name not in installed:
            if has_marker:
                continue
            raise ReleaseError(f"{name} is required but not installed; run in the build environment")
        seen[name] = installed[name]
        pending += _requirements(installed[name])

    entries: list[dict[str, object]] = []
    problems = []
    for name, dist in sorted(seen.items()):
        licence, texts = _licence_of(dist), _licence_texts(dist)
        if licence is None or not texts:
            problems.append(name)
        entries.append({"name": dist.metadata["Name"], "version": dist.version, "licence": licence, "texts": texts})
    if problems:
        raise ReleaseError(f"no licence or licence text recorded for: {', '.join(problems)}")
    return {"product_version": root.version, "fixed": fixed_entries(interpreter, platform_tag), "entries": entries}


@dataclass(frozen=True)
class Native:
    """A native component compiled into the binary that no Python distribution metadata describes.

    Attributes:
        name (str): The component's name, as VERSION_PROBES and the notices name it.
        licence (str): Its SPDX licence identifier.
        text_file (str): Its licence text file in scripts/release/licences.
        note (str): What it is in the binary.
        linux_only (bool): Linked only into the Linux build of CPython.
        in_cpython (bool): Part of CPython, so a version CPython does not report reads "bundled with CPython".
    """

    name: str
    licence: str
    text_file: str
    note: str
    linux_only: bool = False
    in_cpython: bool = True


# Nuitka's runtime and onefile bootstrap, then the C libraries the python-build-standalone CPython (the one uv
# installs and Nuitka compiles in) links statically. Tcl/Tk and the dbm libraries ship only with modules connx-mcp-ce
# never imports. Check this list against the build's Python when the Python or Nuitka version changes.
NATIVE: tuple[Native, ...] = (
    Native(
        "Nuitka",
        "Apache-2.0",
        "Apache-2.0.txt",
        "The compiled-code runtime and the onefile bootstrap.",
        in_cpython=False,
    ),
    Native(
        "Zstandard",
        "BSD-3-Clause",
        "zstd.txt",
        "The onefile bootstrap's payload decompressor, and CPython's compression.zstd.",
    ),
    Native("zlib", "Zlib", "zlib.txt", "Linked into CPython (zlib-ng on Windows)."),
    Native("bzip2", "bzip2-1.0.6", "bzip2.txt", "Linked into CPython."),
    Native("XZ Utils (liblzma)", "0BSD", "xz.txt", "Linked into CPython."),
    Native("libffi", "MIT", "libffi.txt", "Linked into CPython."),
    Native("Expat", "MIT", "expat.txt", "Linked into CPython."),
    Native("mpdecimal", "BSD-2-Clause", "mpdecimal.txt", "Linked into CPython."),
    Native("SQLite", "blessing", "sqlite.txt", "Linked into CPython."),
    # Vendored in CPython's own source tree, but not covered by its LICENSE.txt.
    Native("mimalloc", "MIT", "mimalloc.txt", "CPython's memory allocator (Objects/mimalloc)."),
    Native("HACL*", "MIT", "hacl-star.txt", "CPython's hash and HMAC implementations (Modules/_hacl)."),
    Native("libuuid", "BSD-3-Clause", "libuuid.txt", "Linked into CPython.", linux_only=True),
    Native("ncurses", "X11", "ncurses.txt", "Linked into CPython.", linux_only=True),
    Native("libedit", "BSD-3-Clause", "libedit.txt", "Linked into CPython.", linux_only=True),
)


def fixed_entries(interpreter: Interpreter, platform_tag: str) -> list[dict[str, object]]:
    """The entries no Python distribution metadata describes: CPython, OpenSSL and the NATIVE components.

    Args:
        interpreter (Interpreter): The CPython build compiled into the binary.
        platform_tag (str): The platform the binary is built for.

    Returns:
        list[dict[str, object]]: CPython, OpenSSL, then each NATIVE component linked on this platform.

    Raises:
        ReleaseError: CPython's licence file is missing, or OpenSSL is older than 3 (not Apache-2.0).
    """
    if interpreter.licence_file is None:
        raise ReleaseError("CPython's LICENSE.txt was not found next to this interpreter's standard library")
    match = re.match(r"OpenSSL (\d+)\.(\d+)\.(\d+)", interpreter.openssl_version)
    if match is None or int(match.group(1)) < 3:
        raise ReleaseError(f"expected OpenSSL 3 or later (Apache-2.0), found {interpreter.openssl_version!r}")
    entries: list[dict[str, object]] = [
        {
            "name": "CPython",
            "version": interpreter.python_version,
            "licence": "PSF-2.0",
            "texts": [{"file": "LICENSE.txt", "text": interpreter.licence_file.read_text(encoding="utf-8")}],
        },
        {
            "name": "OpenSSL",
            "version": ".".join(match.groups()),
            "licence": "Apache-2.0",
            "texts": [{"file": "Apache-2.0.txt", "text": (LICENCES / "Apache-2.0.txt").read_text(encoding="utf-8")}],
        },
    ]
    for native in NATIVE:
        if native.linux_only and not platform_tag.startswith("linux"):
            continue
        unreported = f"bundled with CPython {interpreter.python_version}" if native.in_cpython else "(not installed)"
        entries.append(
            {
                "name": native.name,
                "version": interpreter.linked.get(native.name, unreported),
                "licence": native.licence,
                "note": native.note,
                "texts": [
                    {"file": native.text_file, "text": (LICENCES / native.text_file).read_text(encoding="utf-8")}
                ],
            }
        )
    return entries


def render_notices(collected: Sequence[dict]) -> str:
    """Merge each platform's collected notices into the THIRD-PARTY-NOTICES text.

    Args:
        collected (Sequence[dict]): The JSON written by collect-notices, one per platform.

    Returns:
        str: The notices: fixed entries first, then the dependencies by name, each name and version once.
    """
    fixed: dict[tuple[str, str], dict] = {}
    entries: dict[tuple[str, str], dict] = {}
    for notices in collected:
        for entry in notices["fixed"]:
            fixed.setdefault((entry["name"], entry["version"]), entry)
        for entry in notices["entries"]:
            entries.setdefault((entry["name"].lower(), entry["version"]), entry)
    version = collected[0]["product_version"]
    parts = [
        f"THIRD-PARTY NOTICES for {PRODUCT} {version}\n",
        f"The {PRODUCT} binaries include the following third-party software, each under its own licence.\n",
    ]
    for entry in [*fixed.values(), *(entries[k] for k in sorted(entries))]:
        parts.append(f"\n## {entry['name']} {entry['version']} ({entry['licence']})\n")
        if entry.get("note"):
            parts.append(f"\n{entry['note']}\n")
        for text in entry["texts"]:
            parts.append(f"\n### {text['file']}\n\n{text['text'].rstrip()}\n")
    return "".join(parts)


# --- Command line --------------------------------------------------------------------------------------------------


def _positive(value: str) -> int:
    """An argparse type for a count of at least one."""
    count = int(value)
    if count < 1:
        raise argparse.ArgumentTypeError(f"expected 1 or more, got {value}")
    return count


def _parser() -> argparse.ArgumentParser:
    """The command-line parser."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0] if __doc__ else None)
    commands = parser.add_subparsers(dest="command", required=True)

    build = commands.add_parser("build", help="Compile this OS's binary, its .sha256, notices and start-up time.")
    build.add_argument("--out", type=Path, default=REPO / "dist", help="Output directory (default: dist).")
    build.add_argument("--platform", default=current_platform(), help="Platform tag (default: this machine's).")
    build.add_argument("--runs", type=_positive, default=10, help="Start-up runs to time (default: 10).")
    build.add_argument("--build-id", default=None, help="Cache directory suffix (default: UTC build time).")
    build.add_argument(
        "--require-msvc",
        action="store_true",
        help="Windows: compile with MSVC or fail, never with Nuitka's zig fallback (the release builds use it).",
    )
    build.add_argument("--package-dir", type=Path, default=PACKAGE_DIR, help=argparse.SUPPRESS)
    build.add_argument("--checkout", type=Path, default=REPO, help=argparse.SUPPRESS)

    stamp = commands.add_parser(
        "stamp", help="Write the Product version, major.minor.fix plus a build, into pyproject.toml and uv.lock."
    )
    stamp.add_argument("build", help="A Build number such as 26004, or dev<run number> for a Development build.")
    stamp.add_argument("--checkout", type=Path, default=REPO, help=argparse.SUPPRESS)

    checksums = commands.add_parser("checksums", help="Write SHA256SUMS for the given binaries.")
    checksums.add_argument("binaries", nargs="+", type=Path)
    checksums.add_argument("--output", type=Path, required=True)

    collect = commands.add_parser("collect-notices", help="Record the shipped licences as JSON.")
    collect.add_argument("output", type=Path)
    collect.add_argument("--platform", default=current_platform(), help="Platform tag (default: this machine's).")

    render = commands.add_parser("render-notices", help="Merge collected notices into THIRD-PARTY-NOTICES.")
    render.add_argument("collected", nargs="+", type=Path)
    render.add_argument("--output", type=Path, required=True)

    measure = commands.add_parser("measure", help="Time `<binary> --help`.")
    measure.add_argument("binary", type=Path)
    measure.add_argument("--platform", default=current_platform())
    measure.add_argument("--runs", type=_positive, default=10)

    tag = commands.add_parser(
        "check-tag", help="Fail when a release tag is not the Release build's Product version in pyproject.toml."
    )
    tag.add_argument("tag", help="The tag, such as 15.0.0.26004 or refs/tags/15.0.0.26004.")
    tag.add_argument("--checkout", type=Path, default=REPO, help=argparse.SUPPRESS)
    return parser


def check_tag(tag: str, checkout: Path = REPO) -> None:
    """Refuse a release tag that is not the Release build's Product version stamped in pyproject.toml.

    The public repository's tag is the bare Product version, such as `15.0.0.26004`, and the export writes that
    version into the staged pyproject.toml: the two must agree, so a release never carries another build's binaries.

    Args:
        tag (str): The tag, with or without `refs/tags/`.
        checkout (Path): The checkout holding pyproject.toml.

    Raises:
        ReleaseError: The tag is not the Product version of a Release build that pyproject.toml holds.
    """
    name = tag.removeprefix("refs/tags/")
    held = product.read_version(checkout / "pyproject.toml")
    if not held.is_release_build:
        raise ReleaseError(
            f"tag {name} is refused: pyproject.toml's version {held} is not a Release build's Product version "
            "(such as 15.0.0.26004); publish only a tree that release.py stage exported"
        )
    if name != str(held):
        raise ReleaseError(f"tag {name} does not match pyproject.toml's version {held}: expected {held}")


def refuse_an_editable_install(site_path: list[str] | None) -> None:
    """Refuse a build environment that installed connx-mcp-ce editable, before Nuitka runs.

    The binary carries the distribution's metadata for `--version`, and Nuitka includes it only when the
    distribution's RECORD lists the package it ships; an editable install lists a `.pth` file instead, and Nuitka
    stops with a FATAL that names neither cause nor fix.

    Args:
        site_path (list[str] | None): Where to look for installed distributions; None is sys.path.

    Raises:
        ReleaseError: connx-mcp-ce is installed editable.
    """
    for dist in metadata.distributions(name=ROOT_DISTRIBUTION, path=sys.path if site_path is None else site_path):
        direct_url = json.loads(dist.read_text("direct_url.json") or "{}")
        if direct_url.get("dir_info", {}).get("editable"):
            raise ReleaseError(
                f"{ROOT_DISTRIBUTION} is installed editable, so Nuitka cannot include its metadata: "
                "run the build with `uv run --no-editable`"
            )


def stamp_checkout(checkout: Path, build: str) -> str:
    """Write a build's Product version into a checkout, before the build environment installs it; see `version.stamp`.

    Args:
        checkout (Path): The checkout holding pyproject.toml and uv.lock.
        build (str): A Build number such as `26004`, or `dev<run number>`.

    Returns:
        str: The Product version written.

    Raises:
        ReleaseError: The build, or the version the checkout declares, has another shape.
    """
    try:
        return product.stamp_build(checkout, build)
    except ValueError as error:
        raise ReleaseError(str(error)) from error


def _write_json(path: Path, value: object) -> None:
    """Write a value as indented JSON."""
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8", newline="\n")


def _build(
    args: argparse.Namespace,
    run: Runner,
    run_timed: Runner,
    clock: Clock,
    site_path: list[str] | None,
    interpreter: Interpreter,
) -> int:
    """Compile the binary, then write its sum, its notices and its start-up time next to it."""
    out: Path = args.out
    out.mkdir(parents=True, exist_ok=True)
    build_id = args.build_id or time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    if args.require_msvc and not args.platform.startswith("windows"):
        raise ReleaseError(f"--require-msvc applies to a windows build only, not {args.platform}")
    refuse_an_editable_install(site_path)
    code = run(
        nuitka_command(
            out,
            args.platform,
            str(product.read_version(args.checkout / "pyproject.toml")),
            build_id,
            args.package_dir,
            require_msvc=args.require_msvc,
        )
    )
    binary = out / binary_name(args.platform)
    if code != 0:
        print(f"Nuitka failed with exit code {code}", file=sys.stderr)
        return 1
    if not binary.is_file():
        raise ReleaseError(f"Nuitka succeeded but {binary} was not written")
    (out / f"{binary.name}.sha256").write_text(sum_lines([binary]), encoding="utf-8", newline="\n")
    _write_json(out / f"notices-{args.platform}.json", collect_notices(site_path, interpreter, args.platform))
    startup = measure_startup(binary, args.platform, args.runs, run_timed, clock)
    _write_json(out / f"startup-{args.platform}.json", startup)
    print(json.dumps(startup))
    return 0


def main(
    argv: Sequence[str] | None = None,
    run: Runner | None = None,
    clock: Clock = time.perf_counter,
    site_path: list[str] | None = None,
    interpreter: Interpreter | None = None,
) -> int:
    """Run one release step.

    Args:
        argv (Sequence[str] | None): The arguments; None reads the command line.
        run (Runner | None): Runs a command and returns its exit code; None runs real processes.
        clock (Clock): A monotonic clock in seconds, for start-up timing.
        site_path (list[str] | None): Where installed distributions are looked up; None is sys.path.
        interpreter (Interpreter | None): The CPython build for the fixed notices; None is this interpreter.

    Returns:
        int: 0 on success, 1 when a step failed.
    """
    args = _parser().parse_args(argv)
    the_interpreter = interpreter or Interpreter.current()
    try:
        if args.command == "build":
            return _build(args, run or run_process, run or run_quietly, clock, site_path, the_interpreter)
        if args.command == "checksums":
            args.output.write_text(sum_lines(args.binaries), encoding="utf-8", newline="\n")
        elif args.command == "collect-notices":
            _write_json(args.output, collect_notices(site_path, the_interpreter, args.platform))
        elif args.command == "render-notices":
            collected = [json.loads(p.read_text(encoding="utf-8")) for p in args.collected]
            args.output.write_text(render_notices(collected), encoding="utf-8", newline="\n")
        elif args.command == "stamp":
            print(stamp_checkout(args.checkout, args.build))
        elif args.command == "check-tag":
            check_tag(args.tag, args.checkout)
        elif args.command == "measure":
            print(json.dumps(measure_startup(args.binary, args.platform, args.runs, run or run_quietly, clock)))
    except ReleaseError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
