#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""edit.py - Settings edits: resolve every requested change to an environment's settings in one place.

Description:
    A caller describes what it wants as settings edits, each with its origin (a flag, a line of an
    imported file, or the demo stack); `resolve` turns them into a new environment and never changes
    the one it is given. A flag beats the demo stack, which beats an imported file, whatever
    the order of the edits; claims are by side (server or host) and name. Two tables hold the rest:
    `_SOURCES` gives each origin kind its rank, how its host values are stored (the demo's as given, a
    flag's or a file's through a parser) and how a message names it; `_TIES` settles two edits of one
    key at one rank. Every refusal and warning of an edit reads `<origin>: <reason>`. An edit a higher
    source claims is skipped unvalidated. The demo stack's server values are `Fill` edits only.
    Ask-at-start marking runs after every other edit, then the cross-field rules and the missing-value
    check judge the result once.

Copyright:
    Copyright 2026 Software GmbH
    Licensed under the Apache License, Version 2.0 (the "License"); you may not use this file except in compliance
    with the License. You may obtain a copy of the License at https://www.apache.org/licenses/LICENSE-2.0
    Unless required by applicable law or agreed to in writing, software distributed under the License is distributed
    on an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the License
    for the specific language governing permissions and limitations under the License.
"""

from __future__ import annotations

import copy
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from enum import Enum, StrEnum, auto
from typing import Any, TypeGuard

from connx_mcp import CLI_NAME, settings
from connx_mcp.environment import Environment
from connx_mcp.output import shell_argument
from connx_mcp.settings import Missing


class OriginKind(StrEnum):
    """Where a settings edit comes from."""

    SET_FLAG = "set_flag"  # --set and --unset
    HOST_FLAG = "host_flag"  # a host setting's flag (--cdd, --port, --stack-host, ...)
    ASK_FLAG = "ask_flag"  # --ask-at-start
    DEMO_FLAG = "demo_flag"  # the demo request's flags, refused for how they are combined
    IMPORT_LINE = "import_line"  # one line of an imported file
    IMPORT_FILE = "import_file"  # an imported file as a whole (the sweep of missing secrets)
    DEMO = "demo"  # the demo stack's values

    @property
    def rank(self) -> int:
        """How much an edit from this source weighs: a flag beats the demo stack, which beats an imported file."""
        return _SOURCES[self].rank


FLAG_RANK, DEMO_RANK, IMPORT_RANK = 3, 2, 1


@dataclass(frozen=True)
class Origin:
    """The source of one settings edit.

    Attributes:
        kind (OriginKind): The kind of source.
        label (str): How a message names it: the flag, the imported file, or the demo.
        line (int | None): The line of an `IMPORT_LINE`; None for any other source.
    """

    kind: OriginKind
    label: str
    line: int | None = None

    @property
    def where(self) -> str:
        """How a message names the source: the file and line for an imported line, else the label."""
        return f"{self.label}, line {self.line}" if self.line is not None else self.label


@dataclass(frozen=True)
class Set:
    """Store a server setting from a flag or an imported line; the demo stack's server values are `Fill` edits.

    Attributes:
        key (str): The server key, in any letter case.
        value (str | None): The value; None for a `--set` item without `=`, which is refused.
        origin (Origin): Where the edit comes from; never the demo stack.
        note (str | None): A warning the source gives about the value (a file's lone `$`), as a reason; it is
            reported only when the value is stored.
    """

    key: str
    value: str | None
    origin: Origin
    note: str | None = None

    def __post_init__(self) -> None:
        """Reject a demo origin: a `Set` would store a secret the administrator marked ask-at-start.

        Raises:
            ValueError: The origin is the demo stack.
        """
        if self.origin.kind is OriginKind.DEMO:
            raise ValueError(f"{self.key}: a demo server value is a Fill edit, not a Set (a Fill keeps ask-at-start).")


@dataclass(frozen=True)
class Unset:
    """Remove a server setting.

    With `only_if` it claims nothing and runs after the other edits, before ask-at-start: it removes what they
    leave only while it is still that value, so a lower source's other value stays (an imported file's value
    survives the end of a remote binding), and a higher source's claim still skips it.

    Attributes:
        key (str): The server key, in any letter case.
        origin (Origin): Where the edit comes from.
        only_if (str | None): Remove the value only while it is still this one; None removes any value.
    """

    key: str
    origin: Origin
    only_if: str | None = None


@dataclass(frozen=True)
class SetHost:
    """Store a host setting.

    Attributes:
        name (str): The host setting's name (`settings.HOST_SCHEMA`); an imported line may name an unknown one.
        value (Any): A flag's text, an imported line's JSON value, or the demo stack's value to store as it is.
        origin (Origin): Where the edit comes from.
    """

    name: str
    value: Any
    origin: Origin


@dataclass(frozen=True)
class AskAtStart:
    """Mark a secret ask-at-start: never stored, `start` asks for it. Runs after every other edit.

    Attributes:
        key (str): The server key, in any letter case.
        origin (Origin): Where the edit comes from.
        unless_stored (bool): Mark it only while the result stores no value for it (an imported `# secret` line);
            False drops a stored value (`--ask-at-start`).
    """

    key: str
    origin: Origin
    unless_stored: bool = False


@dataclass(frozen=True)
class AskForMissingSecrets:
    """Mark ask-at-start every required secret the result still lacks (an import's sweep). Runs after every other edit.

    A secret a source ranked above the sweep's claims is left as it is.

    Attributes:
        origin (Origin): Where the edit comes from; an imported file's label is the file.
    """

    origin: Origin


@dataclass(frozen=True)
class Fill:
    """Store a server setting as given unless it is marked ask-at-start: the only way the demo stack stores one.

    Attributes:
        key (str): The server key, in any letter case.
        value (str): The value.
        origin (Origin): Where the edit comes from.
    """

    key: str
    value: str
    origin: Origin


@dataclass(frozen=True)
class Claim:
    """Claim a server or host setting at the origin's rank and change nothing: a lower source's edit is skipped.

    The demo's first pass claims the groups (and the host settings) it will fill, so a file value it replaces
    is never judged, while the stored value stays for the binding to read (a remote rebind reuses the stored
    Vault read token).

    Attributes:
        key (str): The server key, in any letter case; with `host`, the host setting's name.
        origin (Origin): Where the edit comes from.
        host (bool): Claim the host setting `key` names instead of a server key.
    """

    key: str
    origin: Origin
    host: bool = False


@dataclass(frozen=True)
class Refuse:
    """Refuse the whole change: always reported, whatever else is edited.

    Attributes:
        message (str): The reason, as the administrator reads it; the refusal names the origin first.
        origin (Origin): Where the edit comes from.
    """

    message: str
    origin: Origin


Edit = Set | Unset | SetHost | AskAtStart | AskForMissingSecrets | Fill | Claim | Refuse


class _Side(StrEnum):
    """Which settings an edit claims: a server key and a host setting of the same name never meet."""

    SERVER = "server"
    HOST = "host"


_Claim = tuple[_Side, str]
# The host setting that holds the ask-at-start marks: `changed` lists the marked keys instead of its name.
_MARKS = "ask_at_start"


def _store_host_as_given(host: settings.HostSettings, name: str, value: Any) -> str | None:
    """Store a host value without a parse (code, not people, produced it); never refused.

    Args:
        host (settings.HostSettings): The host settings to store it in.
        name (str): The host setting's name.
        value (Any): The value.

    Returns:
        str | None: Always None.
    """
    setattr(host, name, value)
    return None


def _store_host_from_line(host: settings.HostSettings, name: str, value: Any) -> str | None:
    """Set one host setting from an imported `# host` line through the parser its flag uses; return why it is refused.

    Args:
        host (settings.HostSettings): The host settings to store it in.
        name (str): The host setting's name, as the line gives it.
        value (Any): The line's JSON value.

    Returns:
        str | None: Why the line is refused, without its name (the message's origin names it) but with the value
            its parser refuses, which the origin does not name; None when it is stored.
    """
    if name not in settings.HOST_SCHEMA:
        known = ", ".join(n for n in settings.HOST_SCHEMA if n not in settings.NOT_TRANSFERRED_HOST_SETTINGS)
        return f"it is not a host setting; the host settings are {known}."
    if name in settings.NOT_TRANSFERRED_HOST_SETTINGS:
        return "it is not imported; mark each secret with a `# secret KEY` line instead."
    if not isinstance(value, str | int):
        return "it must be a JSON string, number or boolean."
    # A boolean is written as JSON `true`/`false`; its parser reads the word.
    given = str(value).lower() if isinstance(value, bool) else str(value)
    if reason := settings.set_host_setting(host, name, given):
        flag = settings.HOST_SCHEMA[name].flag
        hint = f" Give {flag} to choose it on this machine." if flag else ""
        return f"{reason.removesuffix('.')}; the line gives {shell_argument(given)}.{hint}"
    return None


@dataclass(frozen=True)
class _Source:
    """How the edits of one origin kind weigh, how their host values are stored, and how a message names them.

    Every refusal and warning `resolve` writes reads `<origin>: <reason>` (`_worded`); a `Refuse` edit's
    message is the reason. The reason never repeats what the origin names.

    Attributes:
        rank (int): How much an edit weighs: a higher rank claims what it edits from every lower one.
        store_host (Callable[[settings.HostSettings, str, Any], str | None]): Stores a host value; returns why it
            is refused. A flag's text goes through its parser (`settings.set_host_setting`).
        names_operand (bool): A message names the edit's operand in its origin: after a flag, the key of `--set`
            or `--ask-at-start` or the value of a host flag, as typed; after a file's line, in brackets, the key or
            host setting the line gives. A key is named only when it looks like a setting name, so a mistyped
            secret is never shown.
        warns_marked (bool): Marking a secret ask-at-start warns that the source leaves it out.
        unset_unmarks (bool): Removing a value also removes its ask-at-start mark (`--unset` forgets a secret).
    """

    rank: int
    store_host: Callable[[settings.HostSettings, str, Any], str | None]
    names_operand: bool = False
    warns_marked: bool = False
    unset_unmarks: bool = False


_FLAG = _Source(FLAG_RANK, settings.set_host_setting, names_operand=True, unset_unmarks=True)
_IMPORT = _Source(IMPORT_RANK, _store_host_from_line, names_operand=True, warns_marked=True)
_SOURCES = {
    OriginKind.SET_FLAG: _FLAG,
    OriginKind.HOST_FLAG: _FLAG,
    OriginKind.ASK_FLAG: _FLAG,
    OriginKind.DEMO_FLAG: _FLAG,
    OriginKind.DEMO: _Source(DEMO_RANK, _store_host_as_given),
    OriginKind.IMPORT_LINE: _IMPORT,
    OriginKind.IMPORT_FILE: _IMPORT,
}


# The warning of a stored key the schema does not hold (a setting a newer server reads, or a typo).
_UNKNOWN_KEY = f"it is not a setting this {CLI_NAME} knows, so it is stored unvalidated."


class _Repeated(Enum):
    """How two `Set` edits of one server key at one rank are settled."""

    LAST_WINS = auto()  # applied in order, so the later edit holds; each is validated
    LAST_ONLY = auto()  # only the last edit is applied; each earlier one is ignored unvalidated, with a warning


class _Mixed(Enum):
    """How edits of different kinds on one server key at one rank are settled."""

    LAST_WINS = auto()  # applied in order, so the later edit holds
    REFUSED = auto()  # each is refused


@dataclass(frozen=True)
class _Ties:
    """The tie rules of one rank.

    Attributes:
        repeated (_Repeated): Two `Set` edits of one server key. A `# host` line given twice is always last-wins.
        mixed (_Mixed): Edits of different kinds on one server key (set and unset, ask-at-start and set or unset).
    """

    repeated: _Repeated
    mixed: _Mixed


# Each flag is still validated when a later one wins; the command line keeps no order between different options.
# An imported file follows compose's env-file reading: the last line of a key wins, the earlier ones are ignored.
_TIES = {
    FLAG_RANK: _Ties(repeated=_Repeated.LAST_WINS, mixed=_Mixed.REFUSED),
    DEMO_RANK: _Ties(repeated=_Repeated.LAST_WINS, mixed=_Mixed.LAST_WINS),
    IMPORT_RANK: _Ties(repeated=_Repeated.LAST_ONLY, mixed=_Mixed.LAST_WINS),
}


@dataclass
class Edited:
    """What resolving settings edits gives.

    Attributes:
        env (Environment): The new environment.
        warnings (list[str]): What the administrator should know; never a value.
        missing (list[Missing]): The required values the new environment lacks.
        changed (list[str]): Each key whose value differs from the original environment's, once: server keys
            sorted, then host settings in schema order, then secrets newly marked (or no longer marked) ask-at-start.
        edit_refusals (list[str]): The refusals of the edits themselves: a later pass with more edits (the demo
            fill) may still satisfy the cross-field rules, but never these.
        rule_refusals (list[str]): The cross-field rules' refusals of the result.
    """

    env: Environment
    warnings: list[str] = field(default_factory=list)
    missing: list[Missing] = field(default_factory=list)
    changed: list[str] = field(default_factory=list)
    edit_refusals: list[str] = field(default_factory=list)
    rule_refusals: list[str] = field(default_factory=list)

    @property
    def refusals(self) -> list[str]:
        """Every reason the change is refused: the edit refusals, then the cross-field rules'."""
        return [*self.edit_refusals, *self.rule_refusals]


def resolve(env: Environment, edits: Sequence[Edit]) -> Edited:
    """Resolve settings edits into a new environment; `env` is never changed.

    Args:
        env (Environment): The environment as stored.
        edits (Sequence[Edit]): The requested edits.

    Returns:
        Edited: The new environment and what the edits led to.
    """
    return _Resolution(env, edits).run()


def changed_keys(before: Environment, after: Environment) -> list[str]:
    """Return each key whose value differs between two environments, once, in the order `Edited.changed` gives.

    Args:
        before (Environment): The environment as stored.
        after (Environment): The environment the edits produced.

    Returns:
        list[str]: Server keys sorted, then host settings in schema order, then secrets newly marked (or no longer
            marked) ask-at-start.
    """
    server = sorted(k for k in {*before.server, *after.server} if before.server.get(k) != after.server.get(k))
    host_before, host_after = before.host.to_json(), after.host.to_json()
    host = [n for n in settings.HOST_SCHEMA if n != _MARKS and host_before.get(n) != host_after.get(n)]
    marked_before, marked_after = list(before.host.ask_at_start or ()), list(after.host.ask_at_start or ())
    marks = [k for k in marked_after if k not in marked_before] + [k for k in marked_before if k not in marked_after]
    return [*server, *host, *(k for k in marks if k not in server)]


class _Resolution:
    """One run of `resolve`: the working copy, the claims and what is reported.

    Args:
        env (Environment): The environment as stored; never changed.
        edits (Sequence[Edit]): The requested edits.
    """

    def __init__(self, env: Environment, edits: Sequence[Edit]) -> None:
        self.before = env
        self.work = copy.deepcopy(env)
        self.edits = edits
        self.marks = list(self.work.host.ask_at_start or ())
        self.refusals: list[str] = []
        self.warnings: list[str] = []
        self.claims: dict[_Claim, int] = {}
        for edit in edits:
            if not _conditional(edit) and (claim := _claim(edit)) is not None:
                self.claims[claim] = max(self.claims.get(claim, 0), edit.origin.kind.rank)

    def run(self) -> Edited:
        """Apply every edit that wins its claim, the ask-at-start edits last, and return the result."""
        repeats = _repeats(self.edits)
        # Each later edit warns which one it replaces, even when a higher source claims the key: the source
        # repeats itself.
        self.warnings += [
            _worded(later, f"it is given again ({_ignored(earlier)} is ignored).") for earlier, later in repeats
        ]
        replaced = {id(earlier) for earlier, _ in repeats}
        ask_at_start_edits = [edit for edit in self.edits if isinstance(edit, AskAtStart | AskForMissingSecrets)]
        conditional = [edit for edit in self.edits if _conditional(edit)]
        for edit in self.edits:
            if _conditional(edit):
                continue  # judges what the other edits leave, so it runs after them
            if id(edit) in replaced:
                continue  # a later edit of its key replaces it: ignored without being validated
            if not self._wins(edit):
                continue  # a higher source claims it: skipped without being validated
            match edit:
                case Set():
                    self._set(edit)
                case Unset():
                    self._unset(edit)
                case SetHost():
                    self._set_host(edit)
                case Fill():
                    self._fill(edit)
                case Refuse():
                    self.refusals.append(_worded(edit, edit.message))
        for edit in conditional:
            if self._wins(edit):
                self._unset(edit)
        for edit in ask_at_start_edits:
            if isinstance(edit, AskAtStart):
                self._ask_at_start(edit)
            else:
                self._ask_for_missing_secrets(edit)
        self.work.host.ask_at_start = sorted(self.marks) or None
        self._drop_stale_pinned_reference()
        problems = settings.check(self.work.server, self.work.host)  # an unknown key is warned of where it is set
        self.warnings += [p.message for p in problems if not p.error]
        return Edited(
            self.work,
            self.warnings,
            missing=self.work.missing(self.work.asked_at_start),
            changed=changed_keys(self.before, self.work),
            edit_refusals=list(self.refusals),
            rule_refusals=[p.message for p in problems if p.error],
        )

    def _drop_stale_pinned_reference(self) -> None:
        """Drop the pinned reference when the edits replace the pin without one: it named the replaced image."""
        before, after = self.before.host, self.work.host
        if after.image_digest != before.image_digest and after.image_pinned_reference == before.image_pinned_reference:
            after.image_pinned_reference = None

    def _wins(self, edit: Edit) -> bool:
        """Return whether no source ranked above the edit's claims what it edits."""
        claim = _claim(edit)
        return claim is None or edit.origin.kind.rank >= self.claims.get(claim, 0)

    def _clashes(self, edit: Edit, *kinds: type) -> bool:
        """Return whether the edit's rank refuses mixed kinds and an edit of one of `kinds` there edits the same key."""
        claim, rank = _claim(edit), edit.origin.kind.rank
        return _TIES[rank].mixed is _Mixed.REFUSED and any(
            isinstance(other, kinds) and _claim(other) == claim and other.origin.kind.rank == rank
            for other in self.edits
        )

    def _store(self, key: str, value: str) -> None:
        """Store a server value; a stored secret is no longer asked for at start."""
        self.work.server[key] = value
        if key in self.marks:
            self.marks.remove(key)

    def _set(self, edit: Set) -> None:
        """Store a server value once it holds."""
        key = settings.canonical_key(edit.key)
        if edit.value is None:
            # A bare item may be a mistyped secret: the reason never names it, `_where` only when it looks like a key.
            self.refusals.append(
                _worded(edit, "the item has no =; give it as KEY=VALUE (KEY= for an explicit empty value).")
            )
        elif self._clashes(edit, Unset):
            self.refusals.append(_worded(edit, "it is both set and unset; give one of them."))
        elif problem := settings.server_setting_problem(key, edit.value):
            self.refusals.append(_worded(edit, problem))
        else:
            self._store(key, edit.value)
            if key not in settings.SCHEMA:
                self.warnings.append(_worded(edit, _UNKNOWN_KEY))
            if edit.note is not None:
                self.warnings.append(_worded(edit, edit.note))

    def _unset(self, edit: Unset) -> None:
        """Remove a server value; with `only_if`, only while it is still that one. A flag also drops the mark."""
        # No refusal by name: removing a retired, fixed or host key from a hand-edited file is the fix.
        key = settings.canonical_key(edit.key)
        if edit.only_if is None or self.work.server.get(key) == edit.only_if:
            self.work.server.pop(key, None)
        if _SOURCES[edit.origin.kind].unset_unmarks and key in self.marks:
            self.marks.remove(key)

    def _fill(self, edit: Fill) -> None:
        """Store a value unless its key is marked ask-at-start, which stays asked for."""
        key = settings.canonical_key(edit.key)
        if key not in self.marks:
            self._store(key, edit.value)

    def _ask_at_start(self, edit: AskAtStart) -> None:
        """Mark a secret ask-at-start; a name that cannot be is refused even when a higher source claims it."""
        key = settings.canonical_key(edit.key)
        if problem := settings.ask_at_start_problem(key):
            self.refusals.append(_worded(edit, problem))
        elif not self._wins(edit):
            return
        elif self._clashes(edit, Set, Unset):
            self.refusals.append(_worded(edit, "it is both asked for at start and set or unset; give one of them."))
        elif not (edit.unless_stored and key in self.work.server):
            self.work.server.pop(key, None)
            self._mark(key, edit)

    def _ask_for_missing_secrets(self, edit: AskForMissingSecrets) -> None:
        """Mark every required secret still missing, except one a higher source claims."""
        for key in settings.missing(self.work.server, self.marks):
            if settings.is_secret(key) and self.claims.get((_Side.SERVER, key), 0) <= edit.origin.kind.rank:
                self._mark(key, edit)

    def _mark(self, key: str, edit: AskAtStart | AskForMissingSecrets) -> None:
        """Mark a key ask-at-start, with the warning its source gives (an imported file says it leaves it out).

        A `# secret` line's origin names its key; the sweep's names only the file, so its reason names the key.
        """
        if key in self.marks:
            return
        self.marks.append(key)
        if not _SOURCES[edit.origin.kind].warns_marked:
            return
        if isinstance(edit, AskAtStart):
            reason = "its value is not in the file; start asks for it (or store it with configure --set)."
        else:
            reason = f"{key} is not in the file; start asks for it (or store it with configure --set {key}=...)."
        self.warnings.append(_worded(edit, reason))

    def _set_host(self, edit: SetHost) -> None:
        """Store a host value the way its source's host values are stored: as given, or through a parser."""
        if reason := _SOURCES[edit.origin.kind].store_host(self.work.host, edit.name, edit.value):
            self.refusals.append(_worded(edit, reason))


def _claim(edit: Edit) -> _Claim | None:
    """Return what an edit claims: its side (server or host) and name; a server key and a host name never meet.

    A refusal and the sweep claim nothing; a `Claim` claims and changes nothing.
    """
    match edit:
        case SetHost():
            return (_Side.HOST, edit.name)
        case Claim(host=True):
            return (_Side.HOST, edit.key)
        case Set() | Unset() | Fill() | AskAtStart() | Claim():
            return (_Side.SERVER, settings.canonical_key(edit.key))
    return None


def _repeats(edits: Sequence[Edit]) -> list[tuple[Set, Set]]:
    """Return each `Set` a later one of its key replaces at a rank whose tie row applies only the last, with it.

    Args:
        edits (Sequence[Edit]): The requested edits.

    Returns:
        list[tuple[Set, Set]]: (the replaced edit, the one after it), in edit order; the replaced edit is never
            validated.
    """
    last: dict[tuple[int, str], Set] = {}
    found: list[tuple[Set, Set]] = []
    for edit in edits:
        if not isinstance(edit, Set) or _TIES[rank := edit.origin.kind.rank].repeated is not _Repeated.LAST_ONLY:
            continue
        key = settings.canonical_key(edit.key)
        if (earlier := last.get((rank, key))) is not None:
            found.append((earlier, edit))
        last[(rank, key)] = edit
    return found


def _ignored(edit: Edit) -> str:
    """Return how a warning names an edit a later one replaces: its line, else its origin."""
    return f"line {edit.origin.line}" if edit.origin.line is not None else f"the earlier {edit.origin.label}"


def _conditional(edit: Edit) -> TypeGuard[Unset]:
    """Return whether an edit is an `Unset` with `only_if`: it claims nothing and runs after the other edits."""
    return isinstance(edit, Unset) and edit.only_if is not None


def _worded(edit: Edit, reason: str) -> str:
    """Return a refusal or warning about an edit in the one wording every source shares: `<origin>: <reason>`."""
    return settings.worded(_where(edit), reason)


def _where(edit: Edit) -> str:
    """Return how a message names an edit's origin: a flag with its operand as typed, or a file's line with its key."""
    origin = edit.origin
    if not _SOURCES[origin.kind].names_operand:
        return origin.where
    match edit:
        case SetHost() if origin.line is None:
            return f"{origin.label} {shell_argument(str(edit.value))}"
        case SetHost() if _names_key(edit.name):
            return f"{origin.where} ({edit.name})"
        case Set() | Unset() | AskAtStart() if _names_key(key := settings.canonical_key(edit.key)):
            return f"{origin.label} {key}" if origin.line is None else f"{origin.where} ({key})"
    return origin.where


def _names_key(key: str) -> bool:
    """Return whether an origin may name a key or host setting: only one that looks like a name (never a secret)."""
    return settings.KEY_PATTERN.fullmatch(key) is not None
