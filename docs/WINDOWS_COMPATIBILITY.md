# Windows compatibility status

Verified locally on 2026-11-17 with Windows, Python 3.12.13 and the existing
Qwen3.8 27B model served by LM Studio. This is a compatibility pass over the
core assistant, not a port of the native Mac integrations.

## What was fixed

- Desktop, Documents, Downloads, Pictures, Music and Videos are discovered
  with Windows `SHGetKnownFolderPath`, including OneDrive redirection and
  localized or relocated folders. Missing legacy `home/Desktop` references
  from older chats resolve to the actual Desktop; existing explicit paths
  retain their meaning. No replacement Desktop directory is created.
- Windows task routing recognizes drive-letter paths, quoted spaces and
  existing project paths outside the profile. Drive roots, system folders,
  private state and credentials remain excluded as task workdirs.
- Windows containment checks normalize case, drive roots and parent traversal.
  Local browser profiles and Windows credential stores are protected from
  bounded file tools. Windows deletion/process/permission commands require
  review; disk and boot administration commands are refused.
- Windows shell commands use PowerShell, preserve paths with spaces, and
  terminate their child process tree on timeout. Background service starts
  and restarts do not open console windows.
- Vault/scout references use portable forward slashes. Windows memory budgets
  read the kernel's physical-memory counters. Windows timezone names resolve
  through tzlocal, with tzdata providing IANA rules.
- Setup creates a local Python environment. A fresh install can select the
  sole downloaded LM Studio language model; existing choices are retained,
  and multiple models remain a user choice. Mac-only setup actions are marked
  unavailable on Windows. Model setup defaults to LM Studio on Windows.

## Verification

The baseline full Windows run had 496 tests: 14 failures, 36 errors and 104
skips. Some were real platform failures, others were Mac-specific test
fixtures or Unix-only path assertions.

The final full run had **512 tests: 402 passed and 110 skipped**, with no
failures or errors. Skips cover unavailable OS sandbox/native Mac features,
POSIX process/permission semantics and symlink creation requiring Windows
privileges. Mac control contracts that mock native I/O now also run on Windows;
these are not skipped as a substitute for preserving their behavior.

The 75 JavaScript frontend checks and Windows launcher syntax checks passed.
The focused 16 host/setup checks were rerun successfully after making their
timezone fixture independent of Windows-only installed packages.

Live acceptance through Claire's actual HTTP service and local model verified
the original Desktop workflow: `local_files` returned a successful listing of
the OS-reported OneDrive Desktop, and Claire reported success. User files were
not changed. Actual Windows shell execution and child-process timeout cleanup
were verified independently without asking the model to judge its own result.

## Mac preservation and remaining release work

The saved configuration and bash launchers were retained. Windows API imports
are lazy and platform-specific. The existing Mac known-folder layout, POSIX
shell path and native Mac integration code remain in place; native `mac.py`,
`mac_ax.py`, Apple Notes and Splash runtime code were not changed by this pass.
Path display normalization leaves existing POSIX paths unchanged.

There was no connected Mac or original repository history. The source snapshot
under `.cache/before-windows-compat-20261117-171802/` records the state before
this audit, after the initial Windows startup work. The review patch compares
against that snapshot and excludes local state and model weights.

`.github/workflows/platform-tests.yml` is prepared to run the suite and frontend
contracts on both `windows-latest` and `macos-latest` after upload to a repository.
It has **not run on GitHub or a real Mac yet**. Before claiming Mac/Windows
release readiness, require both CI jobs and a real Mac acceptance check for
model loading, shell approvals and native app/browser control.

## Explicit Windows limits

Windows currently supports local-model chat, folder/file discovery, project and
task work, approved PowerShell, memory, schedules, and remote API/HTTP MCP.
Listing Desktop files is not visual access to the screen.

The macOS kernel sandbox, native Mac app controls, Apple Notes, local sandboxed
Browser/stdio MCP, workshop/integration-builder code execution, Finder choosers,
Splash runtime and launchd autostart are unavailable on Windows. These features
need separate Windows implementations; the file-location fix does not provide
feature parity or an OS sandbox. Pattern guards and human approvals are retained
but do not replace kernel isolation.

State and notes are ignored by Git (`.claire/`, `claire-vault/`), as are the Python
environment and local backup/test artifacts (`.venv/`, `.cache/`).
