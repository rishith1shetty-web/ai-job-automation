"""A way for the installer to ask the running application to stop -- properly.

The alternative is ``taskkill``, and that is exactly what this exists to avoid. A killed
process abandons its instance lock, leaves its runtime state behind, and -- worse -- can be
killed in the middle of a portal session, leaving a half-written ``linkedin_state.json`` and
an orphaned Chromium. Asking the application to stop lets it finish that work.

Three things guard the endpoint, and all three must hold:

1. **Loopback only.** The peer address must be 127.0.0.1 or ::1. The server never binds
   anything but loopback anyway, so this is defence in depth rather than the only control.
2. **A per-run token.** Generated fresh at every start, stored in a file only this Windows
   user can read, compared with :func:`secrets.compare_digest`, and destroyed on shutdown.
   There is no permanent secret and nothing to leak between runs.
3. **POST only.** A GET cannot trigger it, so no link, redirect or image tag on a page the
   user happens to have open can stop the application.

The token is never logged, never returned in a response, and never placed on a command line;
the installer reads it from the protected file, which it can do because it runs as the same
user (or as an administrator, who could read it regardless).
"""

from __future__ import annotations

import logging
import secrets
import threading

# Imported at module scope, NOT inside install(), and that is load-bearing. This module uses
# postponed annotations, so FastAPI resolves the endpoint's parameter types from the module
# globals at registration time. With these imported inside the function, "Request" is not
# found there and FastAPI silently reclassifies the parameter as a required QUERY string --
# every shutdown request then fails validation with 422 before any guard runs.
#
# It costs nothing: this module is only ever imported from the launcher's serve() path,
# which is loading FastAPI anyway, and always after bootstrap logging is running.
from fastapi import Request
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)

SHUTDOWN_PATH = "/shutdown"
TOKEN_HEADER = "x-shutdown-token"
LOOPBACK_ADDRESSES = frozenset({"127.0.0.1", "::1", "localhost"})

# How long the application is given to finish safe work before the server is stopped.
GRACE_PERIOD_SECONDS = 20.0


class ShutdownCoordinator:
    """Holds the token and the callback that actually stops the server."""

    def __init__(self, token: str) -> None:
        self._token = token
        self._requested = threading.Event()
        self._stop = None

    @property
    def requested(self) -> bool:
        return self._requested.is_set()

    def bind(self, stop) -> None:
        """Supply the callable that stops uvicorn."""
        self._stop = stop

    def authorises(self, presented: str | None) -> bool:
        """Constant-time comparison, so a wrong token leaks nothing by timing."""
        if not presented or not self._token:
            return False
        return secrets.compare_digest(presented, self._token)

    def request(self, reason: str = "local request") -> None:
        if self._requested.is_set():
            return
        logger.info("Graceful shutdown requested (%s).", reason)
        self._requested.set()
        if self._stop is not None:
            # On a worker thread: the HTTP response for the shutdown request must be
            # delivered before the server stops accepting connections.
            threading.Thread(target=self._stop, name="shutdown", daemon=True).start()

    def wait(self, timeout: float | None = None) -> bool:
        return self._requested.wait(timeout)


def client_is_loopback(scope) -> bool:
    """Is the peer on this machine?

    A missing client address is treated as NOT loopback: this is an authorisation decision,
    and unknown must never mean allowed."""
    client = scope.get("client") if isinstance(scope, dict) else None
    if not client:
        return False
    return str(client[0]) in LOOPBACK_ADDRESSES


def install(app, coordinator: ShutdownCoordinator) -> None:
    """Add the shutdown endpoint to a FastAPI application.

    Registered from the launcher rather than declared in ``app/main.py``, so the API module --
    and everything it owns, including the portal automation -- is untouched by this feature.
    """

    async def shutdown_endpoint(request: Request) -> JSONResponse:
        if not client_is_loopback(request.scope):
            logger.warning("Rejected a shutdown request from a non-loopback address.")
            return JSONResponse({"detail": "Not available."}, status_code=404)

        if not coordinator.authorises(request.headers.get(TOKEN_HEADER)):
            # 403 with no detail: a caller that guessed the path learns nothing more.
            logger.warning("Rejected a shutdown request with an invalid token.")
            return JSONResponse({"detail": "Forbidden."}, status_code=403)

        coordinator.request("installer or local tool")
        return JSONResponse({"status": "stopping"}, status_code=202)

    app.add_api_route(SHUTDOWN_PATH, shutdown_endpoint, methods=["POST"],
                      include_in_schema=False, name="local-shutdown")
    logger.info("Local shutdown endpoint is available on loopback.")


def request_shutdown(host: str, port: int, token: str, timeout: float = 10.0) -> bool:
    """Ask a running instance to stop. Used by ``--shutdown`` and by the installer."""
    import urllib.error
    import urllib.request

    request = urllib.request.Request(
        f"http://{host}:{port}{SHUTDOWN_PATH}", method="POST", data=b"",
        headers={TOKEN_HEADER: token},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as reply:
            return 200 <= reply.status < 300
    except urllib.error.HTTPError as error:
        logger.warning("The running instance refused the shutdown request (HTTP %s).",
                       error.code)
        return False
    except (urllib.error.URLError, OSError) as error:
        logger.warning("Could not reach the running instance: %s", type(error).__name__)
        return False
