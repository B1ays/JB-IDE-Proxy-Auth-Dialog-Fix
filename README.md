# JetBrains Proxy Auth Patcher

**English** | [Русский](README.RU.md)

A TUI and CLI utility that changes `IdeProxyAuthenticator` behavior in compatible JetBrains-based IDE installations. The patch prevents the proxy authentication dialog from appearing automatically when stored credentials are missing or contain no password.

> This project is not affiliated with JetBrains. It modifies IDE installation files and may be overwritten by an IDE update. Close the IDE before applying or restoring a patch.

## Features

- Automatic IDE discovery in standard Windows, macOS, and Linux locations, including JetBrains Toolbox.
- Full-screen TUI with multi-selection, confirmation dialogs, and an operation log.
- CLI commands for patching, status inspection, and restoration.
- Backup creation and atomic JAR replacement.
- Bytecode structure checks, ZIP integrity validation, and class loading with `-Xverify:all`.
- Automatic migration of earlier patch versions.
- A standalone low-level patcher for an individual JAR.

## Requirements

- Python 3.9 or newer.
- Packages listed in `requirements.txt` for the full-screen TUI.
- The `list`, `status`, `patch`, and `restore` CLI commands do not require Textual.

## Run the TUI

```sh
python -m pip install -r requirements.txt
python jetbrains_proxy.py
```

Controls:

| Key | Action |
|---|---|
| `↑` / `↓` | Navigate the installation list |
| `Space` | Select or deselect an IDE |
| `A` | Select all / clear all selections |
| `P` | Apply the patch |
| `R` | Restore the original JAR |
| `U` | Rescan IDE installations |
| `Q` | Quit |

Patch and restore operations require confirmation in a modal dialog. They run in a background worker so the interface remains responsive.

## CLI

List discovered installations and their states:

```sh
python jetbrains_proxy.py list
python jetbrains_proxy.py status
```

Patch an installation using its ID from the list:

```sh
python jetbrains_proxy.py patch <ide-id>
```

Multiple IDs are supported. A product code, IDE name, or installation path can also be used:

```sh
python jetbrains_proxy.py patch <ide-id-1> <ide-id-2>
python jetbrains_proxy.py patch <product-code>
```

Patch every compatible discovered installation:

```sh
python jetbrains_proxy.py patch --all
```

Restore an original JAR:

```sh
python jetbrains_proxy.py restore <ide-id>
python jetbrains_proxy.py restore --all
```

Add a discovery location:

```sh
python jetbrains_proxy.py --root "<INSTALLATION_PARENT>" list
python jetbrains_proxy.py --root "<IDE_HOME>" patch <ide-id>
```

`--root` may be specified more than once. Close selected IDEs before patch or restore operations. `--force` skips only the running-process check; it does not bypass operating-system file locks.

### States

- `original`: unmodified bytecode.
- `patched`: the proxy authentication patch is installed.
- `broken`: an invalid early patch version was detected.
- `unsupported`: the JAR or class structure is not supported.

Earlier patch versions and internal ZIP metadata are migrated automatically without a separate user-facing state.

## Build an Executable

### Windows

```powershell
powershell -ExecutionPolicy Bypass -File .\build_exe.ps1
```

### Linux and macOS

```sh
chmod +x build.sh
./build.sh
```

The script uses `python3` by default. Set `PYTHON_BIN` to select another interpreter:

```sh
PYTHON_BIN=python3.12 ./build.sh
```

The build scripts create `.venv`, install the required packages and PyInstaller, and build a standalone application in `dist`:

```text
dist/JetBrainsProxyPatcher.exe  (Windows)
dist/JetBrainsProxyPatcher      (Linux/macOS)
```

On Linux and macOS, run the application with:

```sh
./dist/JetBrainsProxyPatcher
./dist/JetBrainsProxyPatcher list
./dist/JetBrainsProxyPatcher patch <ide-id>
```

On Windows, run it without arguments to open the TUI, or use:

```powershell
JetBrainsProxyPatcher.exe list
JetBrainsProxyPatcher.exe patch <ide-id>
```

## Low-Level Usage

Use `patch_ide_proxy.py` to work directly with an `intellij.platform.ide.jar` file.

Create a separate patched copy:

```sh
python patch_ide_proxy.py "<IDE_HOME>/lib/intellij.platform.ide.jar" --output patched.jar
```

Patch a JAR in place:

```sh
python patch_ide_proxy.py "<IDE_HOME>/lib/intellij.platform.ide.jar"
```

In-place patching creates `intellij.platform.ide.jar.bak` next to the original file. Running the patcher on an up-to-date JAR returns `already patched`.

Prefer the main utility's `restore` command over manually copying backup files.

## Behavior Changes

The original `requestPasswordAuthenticationInstance` logic is conceptually similar to:

```java
Credentials credentials = proxyAuth.getKnownAuthentication(hostName, port);
if (credentials == null || credentials.getUserName() == null || credentials.getPassword() == null) {
    credentials = proxyAuth.getPromptedAuthentication(prompt, hostName, port);
}
```

The same method already handles a missing password later:

```java
OneTimeString password = credentials.getPassword();
return new PasswordAuthentication(
    credentials.getUserName(),
    password != null ? password.toCharArray() : new char[0]
);
```

The patch changes two control-flow paths:

1. If `getKnownAuthentication()` returns `null`, the method returns `null` without requesting prompted authentication.
2. The `getPassword() == null` check is bypassed, allowing the existing code to create an empty password array.

The `getUserName() == null` check remains unchanged.

### Limitation

The JDK `Authenticator` dialog is no longer used to collect initially missing credentials automatically. If a proxy requires authentication, enter its username and password in the IDE proxy settings.

## Safety and Implementation

The patcher parses the constant pool, target method instructions, and `StackMapTable`. It modifies bytecode only after matching the expected JVM signatures and instruction structure. Unsupported class versions cause the operation to fail without installing the modified JAR.

Subsequent instruction offsets are preserved. `StackMapTable` is adjusted to match the new control flow. When writing the JAR, the JetBrains IKV ZIP comment is removed because its absolute central-directory offsets become stale after an entry changes size.

Before installation, the utility performs:

- ZIP integrity validation;
- another parse of the modified class;
- class loading with the bundled JetBrains Runtime and `-Xverify:all`, when available;
- atomic JAR replacement while preserving the backup.

Backups are stored next to the JAR with a `.bak` suffix. An IDE update may restore the original JAR and remove the applied patch.
