# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller specification for the packaged Windows application.

--onedir, deliberately. --onefile would unpack ~600 MB (Chromium included) into a temporary
directory on every launch, which is slow, fills the disk, and breaks Playwright's browser
discovery because the path changes each run.

Nothing here collects the virtual environment wholesale. Every data entry below is something
the application genuinely opens at runtime, and every hidden import is a module that is
reached dynamically -- through a driver name, an Alembic revision file, or a plugin lookup --
so PyInstaller's static analysis cannot see it.

Deliberately NOT bundled: .env, the DPAPI credential file, outputs/, work/, logs/, uploaded
resumes, browser sessions. Those are user data and secrets; scripts/build_windows.ps1 fails
the build if any of them appears in dist/.
"""

import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

PROJECT_ROOT = Path(SPECPATH)
APP_NAME = "RisingPathRecruiters"

# The spec is executed by PyInstaller, not imported, so the project root is not necessarily
# importable yet. It has to be, because the module collector below is shared with the tests.
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.package_modules import collect_package_modules  # noqa: E402

# --------------------------------------------------------------------------------------
# Data files
# --------------------------------------------------------------------------------------

datas = []

# The built React application. app/static_site.py serves it from <install>/frontend/dist.
frontend_dist = PROJECT_ROOT / "frontend" / "dist"
if not (frontend_dist / "index.html").is_file():
    raise SystemExit(
        "frontend/dist/index.html is missing. Run 'npm run build' in frontend/ first "
        "(scripts/build_windows.ps1 does this for you)."
    )
datas.append((str(frontend_dist), "frontend/dist"))

# Alembic configuration and revision scripts. The revisions are loaded BY PATH at runtime,
# so they must ship as data files -- as modules they would never be found.
datas.append((str(PROJECT_ROOT / "alembic.ini"), "."))
datas.append((str(PROJECT_ROOT / "alembic"), "alembic"))

# The pinned prerequisite metadata (version, filename, checksum). The installer support
# actions read it at runtime to decide what to install, so it ships WITH the application --
# an installed computer has no source checkout to read it from. The prerequisite installer
# ITSELF is not bundled here; the setup program embeds that separately.
datas.append((str(PROJECT_ROOT / "installer" / "prerequisites.json"), "installer"))

# Playwright's own package data: its driver (node + the driver JS) and browser registry.
datas += collect_data_files("playwright")

# Certificate bundle used by outbound HTTPS (OpenAI, portals).
datas += collect_data_files("certifi")

# Bundled Chromium. Copied into the build directory by scripts/build_windows.ps1 so the
# build never depends on the developer's global %LOCALAPPDATA%\ms-playwright cache.
bundled_browsers = PROJECT_ROOT / "build" / "ms-playwright"
if bundled_browsers.is_dir():
    datas.append((str(bundled_browsers), "ms-playwright"))
else:
    print("[spec] WARNING: build/ms-playwright is absent; the build will have no browser.")

# --------------------------------------------------------------------------------------
# Hidden imports
# --------------------------------------------------------------------------------------

hiddenimports = [
    # The DBAPI driver is named in the URL string, never imported in source.
    "psycopg",
    "psycopg.pq",
    "psycopg_binary",
    "sqlalchemy.dialects.postgresql",
    "sqlalchemy.dialects.postgresql.psycopg",
    # Alembic loads its environment and revisions dynamically.
    "alembic",
    "alembic.runtime.migration",
    "alembic.autogenerate",
    "alembic.ddl.postgresql",
    # uvicorn resolves its implementation classes from strings.
    "uvicorn.logging",
    "uvicorn.loops.auto",
    "uvicorn.loops.asyncio",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.http.h11_impl",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.lifespan.on",
    # Pydantic v2 resolves validators through its compiled core.
    "pydantic.deprecated.decorator",
    # Resume parsing back-ends, selected at runtime by file type.
    "pdfplumber",
    "docx",
    # scikit-learn's TF-IDF path pulls these through lazy loaders.
    "sklearn.utils._typedefs",
    "sklearn.utils._heap",
    "sklearn.utils._sorting",
    "sklearn.utils._vector_sentinel",
    "sklearn.neighbors._partition_nodes",
    # Windows credential storage and message boxes go through ctypes.
    "ctypes.wintypes",
]

# The application's own package: every module, including those reached only through a
# portal adapter lookup.
hiddenimports += collect_submodules("app")

# The scientific stack reaches its compiled Cython helpers through names that only exist at
# runtime, so PyInstaller's static analysis cannot see them. Each of these was an actual
# packaged-runtime crash, in this order:
#   numpy   -> ModuleNotFoundError: No module named 'numpy._core._exceptions'
#   scipy   -> ModuleNotFoundError: No module named 'scipy._cyutility', surfacing as
#              "The `scipy` install you are using seems to be broken"
#   sklearn -> pulls both in through sklearn.utils._param_validation
# All three are collected in full, deliberately and by name. This is not a blanket sweep of
# the virtual environment; test subpackages are filtered out.
#   scipy   -> ModuleNotFoundError: No module named
#              'scipy._external.array_api_compat.numpy.fft'
#
# That last one is why the modules are enumerated FROM DISK (scripts/package_modules.py)
# instead of with collect_submodules(): collect_submodules walks the package by IMPORTING it,
# and scipy._external.array_api_compat.numpy shadows the real numpy during that walk, so its
# children are silently skipped and never packaged. Reading the installed directory has no
# such blind spot. tests/test_packaging.py pins all three module names.

for _package in ("numpy", "scipy", "sklearn"):
    hiddenimports += collect_package_modules(_package)

# --------------------------------------------------------------------------------------
# Exclusions -- development-only, and safe to drop
# --------------------------------------------------------------------------------------

excludes = [
    "pytest", "unittest2", "nose",
    "PyInstaller",
    "IPython", "jupyter", "notebook", "ipykernel",
    "matplotlib", "tkinter",
    "PyQt5", "PyQt6", "PySide2", "PySide6",
    "sphinx", "docutils",
    # Streamlit is the legacy fallback dashboard; the packaged app serves React and never
    # starts it. Excluding it saves ~40 MB. start_app.ps1 -WithStreamlit still works in
    # development, where Streamlit is installed in the virtual environment.
    "streamlit",
]

# --------------------------------------------------------------------------------------

a = Analysis(
    ["app/windows_launcher.py"],
    pathex=[str(PROJECT_ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=["scripts/pyinstaller_runtime_hook.py"],
    excludes=excludes,
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name=APP_NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    # console=True for this phase: the packaged runtime test reads stdout, and a visible
    # console makes an early failure diagnosable. The installer phase flips this to False
    # once the error dialogs have been exercised on a clean machine.
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name=APP_NAME,
)
