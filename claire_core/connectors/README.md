# Integrations

How Claire reaches services and apps. Everything here is part of Claire's own
code: tasks can read it but never change it.

## Layout

| File | What it is |
|---|---|
| `__init__.py` | `Connectors`: the one entry point. Setup, sign-in, testing, credentials, calling an action, approval of changes, and what each thread is told about connected services. |
| `google.py` | Google account (Gmail, Calendar) with one OAuth sign-in; the older separate Gmail and Calendar connectors. |
| `notes.py` | Apple Notes, through Notes' own scripting interface. |
| `mac.py`, `mac_control.js`, `mac_overlay.js` | Mac apps through accessibility: read a window, click, type, menus, tabs, screenshots; the blue frame. |
| `browser.py` | A separate Chrome of Claire's own, driven by Playwright MCP. |
| `mcp.py` | Any MCP server, local (sandboxed) or remote. |
| `api.py` | Any HTTPS API that takes a token. |
| `built.py` | Integrations Claire built, once sir approved them. |
| `builder.py` | Building those: manifest rules, checks, install. |
| `kit/claire_kit.py` | The library every Claire-built integration is written with. |
| `http.py`, `secrets.py` | Outbound requests; the keychain-backed secret store. |

## The life of a connection

    set up ──► tested ──► connected ──► failing ──► (re-checked on use) ──► connected
      │                                    │
      └── never connected: a draft, not shown to Claire

- A connection is shown to Claire only once it has connected. One that
  worked and now fails is shown with its problem, and is re-tested when used.
- Actions are read or change. Every change asks sir, in every mode. What
  counts as a change comes from the connector (or, for one Claire built, the
  manifest sir approved), never from the model or a remote server.
- Credentials go from sir to the keychain through the app. The model never
  sees one.

## Integrations Claire builds

    task folder                       <state>/integrations/<name>/
    ───────────                       ─────────────────────────────
    new ──► edit ──► check ──► submit ──[sir approves]──► installed ──► credentials ──► connected
                       │                                     │
             manifest, lint, tests                 old version kept in .history/
             (strict sandbox, offline,
              the real kit)

- Written in the task's folder, where Claire can edit; installed into her
  state, where no task can write. Changing one means `edit`, `check`, `submit`
  and approval again.
- An installed one runs as a sandboxed MCP server per call: it reads only its
  own folder and gets only the credentials it declares.
- Known limit: the macOS sandbox cannot restrict a process to named hosts. The
  kit keeps to the manifest's hosts and `check` refuses code that goes around
  it, but that is not yet enforced at the OS level.

## Skills

How to work with all this is written as skills (`claire_core/skills/`):
`integrations` for deciding what to do when sir asks to connect something, and
`building-integrations` (with `coding`) for writing one. A small model picks
the skills each request needs (`claire_core/skillroute.py`).
