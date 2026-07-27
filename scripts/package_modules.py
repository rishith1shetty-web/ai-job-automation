"""Enumerate every importable module of an installed package, FROM THE FILESYSTEM.

PyInstaller's own ``collect_submodules`` walks a package by IMPORTING it. That is normally
fine, but it has a blind spot this project hit three times in a row: ``scipy`` vendors
``scipy._external.array_api_compat.numpy``, which SHADOWS the real ``numpy`` during the walk,
so every child of that subpackage is silently skipped and never packaged. The failure only
appears at runtime, as a ``ModuleNotFoundError`` deep inside SciPy.

Reading the installed directory has no such blind spot: a file on disk is a module, whether or
not it can be imported in the walker's namespace.

Used by RisingPathRecruiters.spec for numpy, scipy and scikit-learn -- the three packages
whose compiled Cython helpers are reached through names that exist only at runtime. This is
deliberately NOT a blanket sweep of the virtual environment: it is applied to named packages
only, and test subpackages are filtered out.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

# Directory names that are never needed at runtime.
DEFAULT_SKIP = ("tests", "testing", "__pycache__")

# Extensions that constitute an importable module on any platform we build for.
MODULE_PATTERNS = ("*.py", "*.pyd", "*.so")


def collect_package_modules(package_name: str,
                            skip: tuple[str, ...] = DEFAULT_SKIP) -> list[str]:
    """Every importable module name inside ``package_name``, sorted.

    Returns an empty list when the package is not installed, so a spec file can ask for an
    optional dependency without a conditional."""
    spec = importlib.util.find_spec(package_name)
    if spec is None or not spec.submodule_search_locations:
        return []

    root = Path(list(spec.submodule_search_locations)[0])
    names: set[str] = set()
    for pattern in MODULE_PATTERNS:
        for path in root.rglob(pattern):
            relative = path.relative_to(root)
            if any(part in skip for part in relative.parts):
                continue
            parts = list(relative.parts)
            if parts[-1] == "__init__.py":
                parts = parts[:-1]
            else:
                # "_ccallback_c.cp312-win_amd64.pyd" -> "_ccallback_c"
                parts[-1] = parts[-1].split(".")[0]
            if not parts:
                continue
            names.add(".".join([package_name, *parts]))
    return sorted(names)
