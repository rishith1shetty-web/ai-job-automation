"""Serve the built React application from FastAPI, so a deployed computer needs no Node.js.

Development and packaged mode reach the backend by two different routes, and this module
exists to make them behave identically:

* **Development** -- the browser talks to the Vite dev server on :5173. Vite serves
  ``index.html`` for any navigation it does not recognise (which is what makes a refresh on
  ``/matched-jobs`` work), and proxies ``/api/*`` to FastAPI while STRIPPING the prefix
  (``/api/portal/active`` -> ``/portal/active``, see ``frontend/vite.config.ts``).
* **Packaged** -- there is no Vite. FastAPI itself must do both jobs.

The two rules below are the server-side twins of those Vite behaviours, which is why no
frontend code differs between the modes:

1. ``/api/*`` is the ONLY way to reach the backend from the browser; the prefix is stripped
   exactly as Vite strips it.
2. A browser NAVIGATION (a request that accepts HTML) to anything else gets the SPA shell.

Rule 2 is deliberately based on the navigation itself rather than on the URL, because the
React routes and the API routes genuinely share names: ``/applications``, ``/jobs``,
``/screening`` and ``/performance`` are all both a page and an endpoint. Matching on the URL
would force one of the two to lose. Matching on "is this a browser navigating?" is exactly
what the Vite dev server already does, so both modes agree -- and non-browser clients
(scripts, the Streamlit fallback, the existing tests) keep reaching the root-level API
untouched.

``/health`` and the OpenAPI documentation are exempt from rule 2 so they can always be opened
directly in a browser for diagnosis.

Nothing here changes application behaviour: no existing route is modified or replaced.
"""

from __future__ import annotations

import logging
import mimetypes
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from app.paths import frontend_dist_dir

logger = logging.getLogger(__name__)

# The prefix the browser uses for backend calls in BOTH modes. Identical to the Vite proxy
# rule, so the React code never needs to know which mode it is running in.
API_PREFIX = "/api"

INDEX_FILENAME = "index.html"
ASSETS_DIRNAME = "assets"

# Always answered by the backend, even for a browser navigation, so they stay directly
# openable when something needs diagnosing.
NEVER_SHELL_PATHS = frozenset({
    "/health", "/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect",
})

MISSING_FRONTEND_MESSAGE = (
    "The web interface has not been built. Run 'npm run build' in the frontend folder to "
    "generate frontend/dist, then restart the application."
)


class FrontendNotBuiltError(RuntimeError):
    """Raised when strict mode is requested but the built frontend is absent."""


def accepts_html(headers: list[tuple[bytes, bytes]]) -> bool:
    """True when the caller is a browser navigating to a page rather than calling the API.

    Browsers send ``Accept: text/html,...`` for navigations and ``*/*`` (or JSON) for
    ``fetch``; this is the same signal a history-fallback dev server uses."""
    for name, value in headers:
        if name.lower() == b"accept":
            return b"text/html" in value.lower()
    return False


def is_api_path(path: str) -> bool:
    return path == API_PREFIX or path.startswith(API_PREFIX + "/")


def strip_api_prefix(path: str) -> str:
    return path[len(API_PREFIX):] or "/"


class FrontendMiddleware:
    """Applies the two rules above, ahead of routing.

    A pure ASGI middleware rather than a ``BaseHTTPMiddleware`` subclass: it only inspects the
    request line and either rewrites the path or returns a file. Staying out of the
    request/response body path keeps the long-running automation endpoints (which stream for
    minutes) completely untouched.

    ``serve_shell=False`` installs the prefix rewrite ONLY. That is development mode: Vite
    owns the UI, and a ``dist`` folder left over from an earlier build must never be served."""

    def __init__(self, app, dist_dir: Path, serve_shell: bool) -> None:
        self.app = app
        self.dist_dir = dist_dir
        self.serve_shell = serve_shell

    @property
    def index_file(self) -> Path:
        return self.dist_dir / INDEX_FILENAME

    def resolved_asset(self, relative_path: str) -> Path | None:
        """A real file inside the dist directory, or None.

        Resolves before comparing, so ``..`` segments and symlinks cannot escape."""
        if not relative_path:
            return None
        try:
            candidate = (self.dist_dir / relative_path).resolve()
            candidate.relative_to(self.dist_dir.resolve())
        except (ValueError, OSError):
            return None
        return candidate if candidate.is_file() else None

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            return await self.app(scope, receive, send)

        path = scope.get("path", "")

        # Rule 1 -- the API prefix, stripped exactly as the Vite proxy strips it. An unknown
        # endpoint behind the prefix then 404s from FastAPI itself, so a typo in a fetch()
        # fails loudly instead of receiving the HTML shell.
        if is_api_path(path):
            scope = dict(scope)
            scope["path"] = strip_api_prefix(path)
            raw_path = scope.get("raw_path")
            if isinstance(raw_path, bytes) and raw_path.startswith(API_PREFIX.encode()):
                scope["raw_path"] = raw_path[len(API_PREFIX):] or b"/"
            return await self.app(scope, receive, send)

        if not self.serve_shell:
            return await self.app(scope, receive, send)

        if scope.get("method") not in {"GET", "HEAD"} or path in NEVER_SHELL_PATHS:
            return await self.app(scope, receive, send)

        # Fingerprinted bundles are owned by the StaticFiles mount. A missing chunk MUST 404
        # there: answering it with the HTML shell would hand the browser a document where it
        # expects JavaScript, turning a stale-cache problem into a blank page with a
        # confusing MIME-type error.
        if path.startswith(f"/{ASSETS_DIRNAME}/"):
            return await self.app(scope, receive, send)

        # A real built file (favicon, image, or anything else Vite copied from public/).
        asset = self.resolved_asset(path.lstrip("/"))
        if asset is not None:
            media_type = mimetypes.guess_type(asset.name)[0]
            return await FileResponse(asset, media_type=media_type)(scope, receive, send)

        # Rule 2 -- a browser navigating to a client-side route. Anything else falls through
        # to normal routing, so programmatic clients keep reaching the root-level API.
        if not accepts_html(scope.get("headers") or []):
            return await self.app(scope, receive, send)

        return await self.shell_response()(scope, receive, send)

    def shell_response(self) -> Response:
        if not self.index_file.is_file():
            return JSONResponse({"detail": MISSING_FRONTEND_MESSAGE}, status_code=503)
        # Never cache the shell: it names the fingerprinted bundles, so a stale copy would
        # keep pointing at assets a rebuild has already replaced.
        return FileResponse(
            self.index_file,
            media_type="text/html",
            headers={"Cache-Control": "no-store, must-revalidate"},
        )


def mount_frontend(
    app: FastAPI,
    *,
    dist_dir: Path | None = None,
    enabled: bool = True,
    strict: bool = False,
) -> dict:
    """Serve the built React app from ``app``.

    ``enabled=False`` installs only the ``/api`` prefix rewrite, so development mode never
    serves a stale ``dist`` left over from an earlier build.

    ``strict=True`` raises :class:`FrontendNotBuiltError` when the build is missing. The
    default is False on purpose: a packaged app with a missing bundle should still start and
    answer ``/health`` so the problem is diagnosable, and report the failure as a clear 503
    on the page itself. ``start_app.ps1 -Packaged`` performs the hard pre-flight check."""
    dist = Path(dist_dir) if dist_dir is not None else frontend_dist_dir()
    index_file = dist / INDEX_FILENAME
    summary = {
        "enabled": enabled,
        "dist_dir": str(dist),
        "index_present": index_file.is_file(),
        "assets_mounted": False,
    }

    if enabled:
        if not index_file.is_file():
            if strict:
                raise FrontendNotBuiltError(f"{MISSING_FRONTEND_MESSAGE} (looked in {dist})")
            logger.error("Built frontend not found at %s. %s", dist, MISSING_FRONTEND_MESSAGE)
        else:
            logger.info("Serving built frontend from %s", dist)

        assets_dir = dist / ASSETS_DIRNAME
        if assets_dir.is_dir():
            # Vite fingerprints these filenames, so a missing chunk must 404 rather than fall
            # back to the shell -- the mount guarantees that.
            app.mount(f"/{ASSETS_DIRNAME}", StaticFiles(directory=assets_dir),
                      name="frontend-assets")
            summary["assets_mounted"] = True
    else:
        logger.info("Frontend static serving disabled (development mode); Vite serves the UI.")

    # Added last so it wraps the finished application, including the assets mount.
    app.add_middleware(FrontendMiddleware, dist_dir=dist, serve_shell=enabled)
    return summary
