#!/usr/bin/env python3
"""Discover JetBrains IDEs and patch their proxy authentication from a TUI or CLI.

Run without arguments for the interactive TUI.  Run with --help for CLI commands.
Only Python's standard library is required.
"""

from __future__ import annotations

import argparse
import csv
from dataclasses import dataclass
import hashlib
import io
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tempfile
import zipfile

from patch_ide_proxy import CLASS_NAME, PatchError, patch_class, patch_jar


APP_NAME = "JetBrains Proxy Auth Patcher"
BACKUP_SUFFIX = ".bak"


@dataclass(frozen=True)
class IDEInstallation:
    root: Path
    jar: Path
    name: str
    version: str
    product_code: str
    launcher_names: tuple[str, ...]
    id: str = ""
    data_directory: str = ""

    @property
    def backup(self) -> Path:
        return self.jar.with_suffix(self.jar.suffix + BACKUP_SUFFIX)


@dataclass(frozen=True)
class Inspection:
    state: str
    detail: str
    backup: bool


def _case_key(path: Path) -> str:
    return os.path.normcase(str(path.resolve()))


def _launchers(product: dict) -> tuple[str, ...]:
    names = []
    for item in product.get("launch", []):
        if isinstance(item, dict) and item.get("launcherPath"):
            names.append(Path(item["launcherPath"]).name.lower())
    return tuple(dict.fromkeys(names))


def _read_installation(root: Path) -> IDEInstallation | None:
    jar = root / "lib" / "intellij.platform.ide.jar"
    if not jar.is_file():
        return None
    product_file = root / "product-info.json"
    product = {}
    try:
        product = json.loads(product_file.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError):
        pass
    name = product.get("name") or root.name
    version = product.get("version") or product.get("buildNumber") or "unknown"
    code = product.get("productCode") or "IDE"
    return IDEInstallation(
        root=root.resolve(), jar=jar.resolve(), name=str(name), version=str(version),
        product_code=str(code), launcher_names=_launchers(product),
        data_directory=str(product.get("dataDirectoryName") or ""),
    )


def _walk_for_ides(base: Path, max_depth: int = 6) -> list[IDEInstallation]:
    found = []
    base = base.expanduser()
    if not base.exists():
        return found
    direct = _read_installation(base)
    if direct:
        return [direct]
    if base.is_file() and base.name == CLASS_NAME.rsplit("/", 1)[-1]:
        installation = _read_installation(base.parent.parent)
        return [installation] if installation else []
    try:
        base_depth = len(base.resolve().parts)
        for current, directories, files in os.walk(base):
            current_path = Path(current)
            depth = len(current_path.resolve().parts) - base_depth
            if depth >= max_depth:
                directories[:] = []
            directories[:] = [
                name for name in directories
                if name.lower() not in {"caches", "cache", "logs", "log", ".git", "node_modules"}
            ]
            if "product-info.json" in files:
                installation = _read_installation(current_path)
                if installation:
                    found.append(installation)
                    directories[:] = []
    except OSError:
        pass
    return found


def _default_search_roots() -> list[Path]:
    home = Path.home()
    system = platform.system()
    roots = []
    if system == "Windows":
        local = Path(os.environ.get("LOCALAPPDATA", home / "AppData/Local"))
        program_files = Path(os.environ.get("ProgramFiles", "C:/Program Files"))
        roots.extend([
            local / "Programs",
            local / "JetBrains/Toolbox/apps",
            program_files / "JetBrains",
        ])
        # Toolbox may store installations on another drive; custom --root covers that.
    elif system == "Darwin":
        roots.extend([
            Path("/Applications"), home / "Applications",
            home / "Library/Application Support/JetBrains/Toolbox/apps",
        ])
    else:
        roots.extend([
            home / ".local/share/JetBrains/Toolbox/apps",
            home / ".local/opt", Path("/opt"), Path("/usr/local"),
        ])
    return roots


def discover_ides(extra_roots: list[Path] | None = None) -> list[IDEInstallation]:
    candidates = []
    for root in [*_default_search_roots(), *(extra_roots or [])]:
        candidates.extend(_walk_for_ides(root))
    unique = {}
    for installation in candidates:
        unique[_case_key(installation.root)] = installation
    ordered = sorted(unique.values(), key=lambda item: (item.name.lower(), str(item.root).lower()))
    result = []
    code_counts = {}
    for installation in ordered:
        base = "".join(c.lower() if c.isalnum() else "-" for c in installation.product_code).strip("-") or "ide"
        code_counts[base] = code_counts.get(base, 0) + 1
        suffix = f"-{code_counts[base]}" if code_counts[base] > 1 else ""
        result.append(IDEInstallation(**{**installation.__dict__, "id": base + suffix}))
    # A duplicate product code also needs an unambiguous first ID.
    counts = {}
    for item in result:
        base = item.id.rsplit("-", 1)[0] if item.id.rsplit("-", 1)[-1].isdigit() else item.id
        counts[base] = counts.get(base, 0) + 1
    if any(count > 1 for count in counts.values()):
        seen = {}
        renamed = []
        for item in result:
            base = item.id.rsplit("-", 1)[0] if item.id.rsplit("-", 1)[-1].isdigit() else item.id
            seen[base] = seen.get(base, 0) + 1
            new_id = f"{base}-{seen[base]}" if counts[base] > 1 else base
            renamed.append(IDEInstallation(**{**item.__dict__, "id": new_id}))
        result = renamed
    return result


def inspect(installation: IDEInstallation) -> Inspection:
    backup = installation.backup.is_file()
    try:
        with zipfile.ZipFile(installation.jar) as archive:
            _, status = patch_class(archive.read(CLASS_NAME))
        if status == "already patched":
            return Inspection("patched", "Current patch installed", backup)
        if status == "repaired broken v1 patch":
            return Inspection("broken", "Broken v1 bytecode patch", backup)
        if status == "patched null credentials handling":
            return Inspection("patched", "Proxy authentication patched", backup)
        return Inspection("original", "Unpatched bytecode", backup)
    except (OSError, zipfile.BadZipFile, KeyError, PatchError) as error:
        return Inspection("unsupported", str(error).splitlines()[0], backup)


def _running_windows(launcher_names: tuple[str, ...]) -> bool:
    if not launcher_names:
        return False
    try:
        process = subprocess.run(
            ["tasklist", "/FO", "CSV", "/NH"], capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=20, check=False,
        )
        rows = csv.reader(io.StringIO(process.stdout))
        running = {row[0].lower() for row in rows if row}
        return any(name in running for name in launcher_names)
    except (OSError, subprocess.SubprocessError):
        return False


def is_running(installation: IDEInstallation) -> bool:
    if platform.system() == "Windows":
        return _running_windows(installation.launcher_names)
    root = str(installation.root)
    proc = Path("/proc")
    if proc.is_dir():
        for entry in proc.iterdir():
            if not entry.name.isdigit():
                continue
            try:
                command = (entry / "cmdline").read_bytes().decode(errors="replace")
                if root in command and "jetbrains_proxy" not in command:
                    return True
            except OSError:
                pass
    return False


def _needs_update(installation: IDEInstallation) -> bool:
    try:
        with zipfile.ZipFile(installation.jar) as archive:
            _, status = patch_class(archive.read(CLASS_NAME))
            return status != "already patched" or bool(archive.comment)
    except (OSError, zipfile.BadZipFile, KeyError, PatchError):
        return True


def install_patch(installation: IDEInstallation, force: bool = False) -> str:
    if not _needs_update(installation):
        return "already patched"
    if is_running(installation) and not force:
        raise PatchError(f"{installation.name} is running; close it first or use --force")
    source = installation.jar
    backup = installation.backup
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(
            prefix=source.name + ".", suffix=".tmp", dir=source.parent, delete=False
        ) as temporary:
            temporary_path = Path(temporary.name)
        status = patch_jar(source, temporary_path)
        changed = status != "already patched"
        if changed:
            if not backup.exists():
                shutil.copy2(source, backup)
            os.replace(temporary_path, source)
            temporary_path = None
            shutil.copystat(backup, source)
        return status
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def restore(installation: IDEInstallation, force: bool = False) -> str:
    if is_running(installation) and not force:
        raise PatchError(f"{installation.name} is running; close it first or use --force")
    source = installation.jar
    backup = installation.backup
    if not backup.is_file():
        raise PatchError(f"Backup not found: {backup}")
    with zipfile.ZipFile(backup) as archive:
        archive.read(CLASS_NAME)
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(
            prefix=source.name + ".", suffix=".restore.tmp", dir=source.parent, delete=False
        ) as temporary:
            temporary_path = Path(temporary.name)
        shutil.copy2(backup, temporary_path)
        os.replace(temporary_path, source)
        temporary_path = None
        return "restored"
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def select_ides(
    installations: list[IDEInstallation], selectors: list[str], select_all: bool
) -> list[IDEInstallation]:
    if select_all:
        return installations
    if not selectors:
        raise PatchError("Specify IDE IDs or use --all")
    selected = []
    for selector in selectors:
        needle = selector.lower()
        exact = [item for item in installations if item.id.lower() == needle]
        matches = exact or [
            item for item in installations
            if needle in item.name.lower() or needle in item.product_code.lower()
            or needle in str(item.root).lower()
        ]
        if not matches:
            raise PatchError(f"No installed IDE matches: {selector}")
        if len(matches) > 1:
            ids = ", ".join(item.id for item in matches)
            raise PatchError(f"Selector {selector!r} is ambiguous; use one of: {ids}")
        if matches[0] not in selected:
            selected.append(matches[0])
    return selected


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _print_table(installations: list[IDEInstallation], selected: set[str] | None = None) -> None:
    if not installations:
        print("No compatible JetBrains IDE installations found.")
        print("Use --root to specify an IDE directory or an installation parent directory.")
        return
    rows = []
    for number, item in enumerate(installations, 1):
        state = inspect(item)
        mark = "[x]" if selected and item.id in selected else "[ ]" if selected is not None else ""
        rows.append((str(number), mark, item.id, item.name, item.version, state.state, str(item.root), state.backup))
    headers = ("#", "", "ID", "IDE", "VERSION", "STATE", "INSTALLATION", "BACKUP")
    values = [headers, *[
        (number, mark, identifier, name, version, state, root, "yes" if backup else "no")
        for number, mark, identifier, name, version, state, root, backup in rows
    ]]
    widths = [min(max(len(str(row[i])) for row in values), 64) for i in range(len(headers))]
    for row_index, row in enumerate(values):
        cells = []
        for index, value in enumerate(row):
            text = str(value)
            if len(text) > widths[index]:
                text = "…" + text[-(widths[index] - 1):]
            cells.append(text.ljust(widths[index]))
        print("  ".join(cells).rstrip())
        if row_index == 0:
            print("  ".join("-" * width for width in widths))


def _perform(action, installations: list[IDEInstallation], force: bool) -> int:
    failures = 0
    for item in installations:
        print(f"\n{item.name} [{item.id}]\n  {item.jar}")
        try:
            status = action(item, force=force)
            print(f"  OK: {status}")
            print(f"  SHA-256: {_sha256(item.jar)}")
        except (PatchError, OSError, zipfile.BadZipFile, KeyError) as error:
            failures += 1
            print(f"  ERROR: {error}", file=sys.stderr)
    return failures


def run_tui(installations: list[IDEInstallation], extra_roots: list[Path], force: bool) -> int:
    try:
        from jetbrains_proxy_tui import run_textual
    except ImportError as error:
        if error.name and error.name.split(".")[0] in {"textual", "rich"}:
            raise PatchError(
                "The full-screen TUI requires Textual. Install it with:\n"
                "  python -m pip install textual rich\n"
                "CLI commands list/status/patch/restore work without it."
            ) from error
        raise
    return run_textual(installations, extra_roots, force)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root", type=Path, action="append", default=[],
        help="Additional IDE root or parent directory to scan (repeatable)",
    )
    parser.add_argument("--force", action="store_true", help="Skip the running-IDE safety check")
    subparsers = parser.add_subparsers(dest="command")
    for name, help_text in (
        ("list", "List discovered IDE installations"),
        ("status", "Show patch state and backup availability"),
    ):
        subparsers.add_parser(name, help=help_text)
    for name, help_text in (
        ("patch", "Patch selected IDE installations"),
        ("restore", "Restore selected IDE installations from .bak"),
    ):
        command = subparsers.add_parser(name, help=help_text)
        command.add_argument("ides", nargs="*", help="IDE ID, product code, name, or installation path")
        command.add_argument("--all", action="store_true", help="Apply to every discovered IDE")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    installations = discover_ides(args.root)
    try:
        if args.command is None:
            if not sys.stdin.isatty():
                parser.error("No command specified and stdin is not a terminal; use list/status/patch/restore")
            return run_tui(installations, args.root, args.force)
        if args.command in {"list", "status"}:
            _print_table(installations)
            return 0
        selected = select_ides(installations, args.ides, args.all)
        action = install_patch if args.command == "patch" else restore
        return 1 if _perform(action, selected, args.force) else 0
    except PatchError as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
