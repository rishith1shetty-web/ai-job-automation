# Windows build (PyInstaller `--onedir`)

How to produce `dist\RisingPathRecruiters\` — a portable folder that runs the whole
application on a Windows computer with **no Python, pip, virtualenv, Node.js, npm, Vite or
source checkout**.

This document covers the *build* — the PyInstaller step only. Wrapping the result in a setup
program is the next step and is documented separately in
[WINDOWS_INSTALLER.md](WINDOWS_INSTALLER.md); run `scripts\build_installer.ps1` after this.

**PostgreSQL is not part of either step.** The build never connects to a database, the
packaged application never installs one, and the installer neither bundles, downloads,
verifies nor installs one. PostgreSQL 16 is installed by hand by the person setting the
computer up, before setup runs.

---

## Build prerequisites

These are needed on the **build** machine only. None of them is required to run the result.

| Requirement | Why | Check |
|---|---|---|
| Windows 10/11 x64 | The build is not cross-platform; the bootloader and Chromium are Windows binaries. | — |
| Python virtual environment at `.venv\` | Supplies the interpreter and every dependency that gets frozen. | `.venv\Scripts\python.exe --version` |
| Project dependencies installed | `pip install -r requirements.txt` | `.venv\Scripts\python.exe -c "import fastapi"` |
| PyInstaller in that environment | Does the freezing. | `.venv\Scripts\python.exe -c "import PyInstaller"` |
| Node.js + npm on `PATH` | Builds the React bundle. **Build-time only** — the packaged app never runs Node. | `npm --version` |
| Internet access on the first build | Playwright downloads Chromium into `build\ms-playwright`. Later builds reuse it. | — |
| ~4 GB free disk | `build\` + `dist\` together. | — |

PostgreSQL is **not** required to build. The build never connects to a database.

## Build command

From the project root:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\build_windows.ps1
```

Options:

| Flag | Effect |
|---|---|
| `-SkipFrontend` | Reuse the existing `frontend\dist`. For iterating on the Python side. |
| `-SkipBrowsers` | Do not stage Chromium. Produces a much smaller folder that **cannot run automation**. |

Exit codes:

| Code | Meaning |
|---|---|
| 0 | Build succeeded and passed verification |
| 2 | Prerequisite missing (venv, npm, spec file) |
| 3 | Frontend build failed or produced no usable `dist` |
| 4 | Playwright browser staging failed |
| 5 | PyInstaller failed |
| 6 | Expected output files missing |
| 7 | **Secrets or user data found in `dist\` — build rejected** |

What the script does, in order:

1. verify the toolchain;
2. clean **only** `dist\` and `build\pyinstaller\` (`build\ms-playwright` survives, so Chromium
   is not re-downloaded on every rebuild);
3. `npm ci` then `npm run build`, and verify `frontend\dist` really has `index.html` + assets;
4. stage Chromium into `build\ms-playwright` — never the developer's global
   `%LOCALAPPDATA%\ms-playwright` cache;
5. run PyInstaller against `RisingPathRecruiters.spec`;
6. verify every expected file landed in `dist\`;
7. scan `dist\` for secrets and user data, and **fail the build** if any is found.

## Output structure

```
dist\RisingPathRecruiters\
├── RisingPathRecruiters.exe          the entry point (app/windows_launcher.py)
└── _internal\
    ├── python3xx.dll, base_library.zip, *.pyd   frozen runtime + dependencies
    ├── frontend\dist\                 built React app (index.html + fingerprinted assets)
    ├── alembic.ini                    Alembic configuration
    ├── alembic\versions\              migration scripts, loaded BY PATH at runtime
    ├── ms-playwright\chromium-*\      bundled Chromium
    ├── playwright\driver\             Playwright's own node driver
    └── certifi\cacert.pem             public CA bundle for outbound HTTPS
```

The whole folder is **read-only at runtime**. Copying it to another computer is a complete
install; there is nothing to register and nothing else to place.

### Command-line modes

`RisingPathRecruiters.exe` normally takes no arguments — a double-click starts the app and
opens the browser. The remaining modes exist for verification and support:

| Flag | Purpose |
|---|---|
| *(none)* | Start the application and open the browser once it answers. |
| `--no-browser` | Start the application but do not open a browser. |
| `--check` | Run the startup checks (credential, database, migrations) and exit. |
| `--selftest` | Import the whole packaged module graph and report the first failure. Finds packaging gaps without starting anything. |
| `--browser-check` | Launch the bundled Chromium, open `about:blank`, close it. Opens no portal and loads no saved session. |

`--check`, `--selftest` and `--browser-check` report to stderr instead of a modal dialog, so
an automated check can never be blocked by a message box.

## Runtime data paths

Nothing is ever written into the program folder. Every writable location resolves through
`app/paths.py` to one machine-local root:

```
%LOCALAPPDATA%\AIJobAutomation\
├── logs\launcher.log        startup log (the error dialogs point here)
├── work\launcher.lock       single-instance lock
├── outputs\                 diagnostics, screenshots, debug HTML
├── cache\, screenshots\, html_diagnostics\
├── uploads\                 uploaded resumes
├── browser_sessions\        linkedin_state.json, dice_state.json
├── storage\
└── config\database_credential.dat    DPAPI-protected database credential
```

`%LOCALAPPDATA%` — **not** `%APPDATA%` — is deliberate. Roaming application data can
synchronise between computers in a managed Windows environment, which would carry resumes,
application history, configuration and signed-in LinkedIn/Dice sessions onto another machine.
Every computer, and every Windows account on a computer, keeps entirely separate data.

Overrides (both honoured by the packaged build):

| Variable | Effect |
|---|---|
| `AI_JOB_AUTOMATION_DATA_DIR` | Put **everything** under one directory. Used by the tests and by a portable install. |
| `AI_JOB_AUTOMATION_LOCAL_DATA_DIR` | Move only the machine-local root. |

## Bundled Chromium strategy

The build stages its own copy rather than depending on whatever the machine happens to have:

* `build_windows.ps1` sets `PLAYWRIGHT_BROWSERS_PATH` **for its own process only** and runs
  `python -m playwright install chromium`, so the browser lands in `build\ms-playwright`
  (owned by the build) instead of the developer's global cache. The variable is removed again
  immediately; the caller's environment is never modified.
* The spec copies that directory into the build as `_internal\ms-playwright`.
* `scripts\pyinstaller_runtime_hook.py` runs **before any application code** and points
  `PLAYWRIGHT_BROWSERS_PATH` at the bundled directory — so any code path that reaches
  Playwright is already correct, including one that bypasses the launcher.
* An explicit `PLAYWRIGHT_BROWSERS_PATH` from the environment still wins, so a support
  engineer can redirect a packaged build without rebuilding it.

Verify it on a built folder:

```powershell
dist\RisingPathRecruiters\RisingPathRecruiters.exe --browser-check
```

which prints the executable Playwright actually resolved, whether it is inside the
installation, and whether the global cache was used.

`-SkipBrowsers` produces a build with no browser. It starts and serves the interface, but any
automation fails — use it only for quick iteration.

## Database expectations

The packaged build **does not install, download or configure PostgreSQL**, and contains no
database, no credential and no `.env`.

At startup the launcher:

1. resolves the database URL — `DATABASE_URL` → the DPAPI credential store → an explicitly
   enabled development fallback (`app/db_credentials.py`);
2. opens one connection and classifies any failure into an actionable message;
3. runs `alembic upgrade head` in-process, using the bundled `alembic.ini` and revision
   scripts (so no password ever appears on a command line).

| Situation | Exit code | What the user sees |
|---|---|---|
| Everything fine | 0 | The app starts |
| Already running | 3 | "Already running" |
| PostgreSQL missing or stopped | 10 | Install / start instructions |
| Port 5432 held by something else | 11 | "Database port is in use" |
| Migration failed | 14 | "Database update failed" (existing data unchanged) |
| No credential stored yet | 17 | "Database not set up yet" → run provisioning |
| Password rejected | 18 | "Database sign-in failed" → re-run provisioning |
| Anything else | 19 | "Could not start", pointing at `launcher.log` |

No message, log line or dialog ever contains a password — `app/db_credentials.py` supplies the
redaction used everywhere, and `tests/test_packaging.py` asserts it.

Provisioning (creating the database, the least-privilege role and the stored credential) is
covered by [POSTGRES_PROVISIONING.md](POSTGRES_PROVISIONING.md); migrations by
[DATABASE_MIGRATIONS.md](DATABASE_MIGRATIONS.md); installing, upgrading and removing the
product, and the manual PostgreSQL 16 prerequisite, by
[WINDOWS_INSTALLER.md](WINDOWS_INSTALLER.md). What shipped in each version is in
[RELEASE_NOTES.md](RELEASE_NOTES.md).

## Troubleshooting

| Symptom | Likely cause | What to do |
|---|---|---|
| Dialog: *Unexpected error — ImportError* | A module reached only at runtime was not frozen. | Run `RisingPathRecruiters.exe --selftest`; it prints the full traceback and the exact missing module. Add it to `hiddenimports` in the spec (for numpy/scipy/sklearn, `scripts\package_modules.py` already collects everything from disk). |
| `launcher.log` is missing | Logging failed before it started, or nothing has run yet. | Logging is configured first, before any risky import. Check `%LOCALAPPDATA%\AIJobAutomation\logs`. If the folder does not exist at all, `%LOCALAPPDATA%` itself is not resolvable for that account. |
| The log stops partway through | Something replaced the root log handlers. | `alembic/env.py` only calls `fileConfig` when the host has not configured logging — do not remove that guard. |
| Build fails with exit 7 | The build contains a secret or user data. | Read the scanner output; it names the file. Never suppress this. |
| Build fails with exit 3 | `npm ci` / `npm run build` failed. | Build the frontend manually in `frontend\` and read the errors. |
| Build fails with exit 4 | Playwright could not download Chromium. | Check internet access and proxy settings, then rerun. |
| "Database not set up yet" | No credential is stored for this Windows account. | Run provisioning. A DPAPI blob from another account or computer cannot be decrypted, by design. |
| Automation cannot start a browser | The build was made with `-SkipBrowsers`. | Rebuild without the flag; verify with `--browser-check`. |
| Port 8000 busy | Something else is listening. | The launcher automatically uses the next free port (8000-8039) and logs which. |
| A second double-click does nothing | The single-instance lock refused it (exit 3). | Look for the already-open browser tab. A crashed run leaves a stale lock, which is detected and removed. |

## Clean-machine checklist

Run through this on a computer that has never had the project on it.

**Before copying anything**

- [ ] No Python, no `.venv`, no Node.js/npm needed — confirm none is installed or on `PATH`
- [ ] **PostgreSQL 16 is already installed by hand and running on port 5432** — nothing in
      this product installs it, so a machine without it is not "clean", it is incomplete
- [ ] The user account is the one that will run the app (DPAPI is per-account)

**Copy and start**

- [ ] Copy `dist\RisingPathRecruiters\` anywhere (Program Files, a USB stick, a user folder)
- [ ] Run provisioning so a credential exists for this account
- [ ] Double-click `RisingPathRecruiters.exe`
- [ ] The browser opens on `http://127.0.0.1:8000` and the React interface loads
- [ ] Refreshing a deep route (e.g. `/matched-jobs`) still shows the app, not a 404

**Verify it is really self-contained**

- [ ] `RisingPathRecruiters.exe --selftest` prints `SELFTEST OK`
- [ ] `RisingPathRecruiters.exe --browser-check` prints `BROWSER OK` and an executable path
      inside the copied folder
- [ ] No `python.exe` or `node.exe` process appears while it runs
- [ ] Nothing is listening on the Vite port (5173)
- [ ] `netstat` shows the listener on `127.0.0.1` only, never `0.0.0.0`

**Verify the data boundary**

- [ ] `%LOCALAPPDATA%\AIJobAutomation\logs\launcher.log` exists and has startup entries
- [ ] `%LOCALAPPDATA%\AIJobAutomation\work\launcher.lock` exists while running, and is gone
      after a clean exit
- [ ] Nothing was written into the copied program folder
- [ ] `%APPDATA%` (Roaming) contains no `AIJobAutomation` folder
- [ ] A second double-click is refused instead of starting a competing server

The scripted equivalent of the last three sections is:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\test_packaged_build.ps1 -DatabaseUrl "<url>"
```

It runs the executable in a clean-room child process — the project and Node directories
stripped from its `PATH`, working directory outside the checkout — and checks all of the
above (30 checks). It performs **no** LinkedIn or Dice action and submits **no** application,
and it issues no `POST`, `PUT` or `DELETE`: every check is read-only.

`-DatabaseUrl` is passed to the child process for that run only and is never stored. Without
it the child has no credential of its own and the database checks fail with exit 17 — which
is the script working correctly, not a defect in the build.

## Limitations of the raw build folder

These apply to `dist\RisingPathRecruiters\` **copied by hand**. Most of them are answered by
the setup program built on top of it — see [WINDOWS_INSTALLER.md](WINDOWS_INSTALLER.md).

* **No shortcuts, no uninstaller, no upgrade path.** A hand-copied folder has none of them.
  The installer provides all three.
* **Provisioning is a separate step** for a hand-copied folder: run
  `scripts\provision_postgres.ps1` before the first start. The installer does this for you,
  asking for the PostgreSQL administrator password on a masked page.
* **Per-account credential.** The DPAPI blob belongs to one Windows account on one computer.
  Another account on the same machine must provision its own.
* **Large folder (~920 MB)**, dominated by Chromium. See the size notes below.
* **x64 Windows only**, built from a Windows machine; there is no cross-compilation.

And these apply to the shipped product as well:

* **PostgreSQL 16 is a manual prerequisite.** Neither the build, nor the launcher, nor the
  installer installs, downloads or bundles a database server. The installer verifies that a
  suitable one is already running on `localhost:5432` and refuses to install anything until
  it is.
* **No code signing.** SmartScreen warns on first run on a machine that has not seen the
  executable before.
* **No auto-update.** Updates are manual: a newer installer is run **over** the existing
  installation, without uninstalling first.

### The shipped executable is windowed

`RisingPathRecruiters.spec` sets `console=CONSOLE_BUILD`, and `CONSOLE_BUILD` is false unless
`RPR_CONSOLE_BUILD=1` is exported. The release build therefore has **no console window**
(PE subsystem 2), and neither `scripts\build_windows.ps1` nor `scripts\build_installer.ps1`
sets that variable — a console build is opt-in, for support only.

Losing the console means `sys.stdout` and `sys.stderr` are `None` at run time, so **nothing
in the startup path may assume a stream exists**. This is not theoretical: uvicorn's default
logging configuration calls `sys.stdout.isatty()` while building its formatter, which raised
`AttributeError` on a real windowed installation and stopped the application from starting.
The launcher passes `log_config=None` for exactly that reason, and `app/logging_config.py`
falls back to a `NullHandler` when there is no stream. `tests/test_runtime_hardening.py`
covers both with `sys.stdout` and `sys.stderr` set to `None`.

### Size

Roughly: bundled Chromium ~55 %, frozen Python runtime and dependencies ~44 %, built React app
well under 1 %. Safe future reductions, in order of value:

1. Drop unused scientific dependencies. `scikit-learn` pulls in `scipy` and `numpy` for the
   TF-IDF fallback path; replacing that one code path would remove all three.
2. Trim Playwright to Chromium's headless shell if no visible browser is ever needed — but the
   automation currently runs headed on purpose, so this is not a free win.
3. Exclude more development-only packages in the spec's `excludes` list, one at a time, with a
   `--selftest` run after each.
4. Enable UPX compression for the frozen binaries. Left off deliberately: it slows startup and
   is a frequent false positive for antivirus software.

Do **not** shrink the build by removing bundled Chromium, the Alembic revision scripts or the
built React app — each is loaded by path at runtime and its absence is a runtime failure, not a
smaller build.
