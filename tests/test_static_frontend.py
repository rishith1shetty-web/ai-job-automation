"""Serving the built React app from FastAPI (app/static_site.py).

Guarantees under test:

* the API is never shadowed: /health and every registered endpoint still answer, and an
  unknown endpoint behind the /api prefix returns a real 404 rather than the HTML shell;
* client-side routes survive a browser refresh (index.html fallback), including the four
  routes whose names collide with real API endpoints;
* JS, CSS and image assets are served with correct content types;
* a missing build is reported clearly instead of failing silently;
* development mode never serves a stale production build;
* packaged mode needs no Node.js and opens the FastAPI URL, development opens Vite.

The static-serving module is exercised against purpose-built FastAPI applications so these
tests need neither a database nor a network listener. A final class inspects the real
application and the launcher to prove the wiring.
"""

import asyncio
import os
import re
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import Settings, reset_settings_cache
from app.paths import (
    MODE_DEVELOPMENT,
    MODE_ENV,
    MODE_PACKAGED,
    frontend_dist_dir,
    install_root,
    project_root,
    reset_runtime_paths_cache,
)
from app.static_site import (
    API_PREFIX,
    MISSING_FRONTEND_MESSAGE,
    NEVER_SHELL_PATHS,
    FrontendMiddleware,
    FrontendNotBuiltError,
    accepts_html,
    mount_frontend,
)

# What a browser sends when NAVIGATING to a page, as opposed to calling fetch(). This is the
# signal the shell fallback keys on, so the tests send it exactly as a browser does.
BROWSER = {"Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"}
# What the frontend's fetch() wrapper sends.
FETCH = {"Accept": "*/*"}

INDEX_HTML = (
    '<!doctype html><html><head><script type="module" src="/assets/index-abc123.js"></script>'
    '<link rel="stylesheet" href="/assets/index-abc123.css"></head>'
    '<body><div id="root"></div></body></html>'
)

# The client-side routes declared in frontend/src/App.tsx. A refresh on any of them must
# return the shell, not a 404 and not JSON.
SPA_ROUTES = (
    "/matched-jobs", "/applications", "/profile", "/employment-history", "/screening",
    "/ai-reasoning", "/form-learning", "/generalized-form", "/performance",
)

# React route names that are ALSO real API endpoints in app/main.py. These are the reason the
# fallback keys on the navigation rather than on the URL.
COLLIDING_ROUTES = ("/applications", "/jobs", "/screening", "/performance")


def build_fake_dist(root: Path) -> Path:
    """A minimal stand-in for `npm run build` output, with the same shape Vite produces."""
    dist = root / "frontend" / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text(INDEX_HTML, encoding="utf-8")
    (dist / "assets" / "index-abc123.js").write_text("export const x = 1;", encoding="utf-8")
    (dist / "assets" / "index-abc123.css").write_text(".root{color:red}", encoding="utf-8")
    (dist / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    return dist


def build_api(**kwargs) -> FastAPI:
    """A stand-in backend carrying the same route shapes as the real application, including
    the endpoints whose names collide with React routes."""
    api = FastAPI()

    @api.get("/health")
    def health() -> dict:
        return {"status": "ok"}

    @api.get("/portal/active")
    def portal_active() -> dict:
        return {"portal": "linkedin"}

    @api.post("/applications/apply-selected")
    def apply_selected() -> dict:
        return {"applied": 0}

    for collision in COLLIDING_ROUTES:
        api.get(collision)(lambda: {"api": True})

    mount_frontend(api, **kwargs)
    return api


class _StaticTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_root = Path(tempfile.mkdtemp(prefix="static-test-"))
        self.addCleanup(shutil.rmtree, self.temp_root, ignore_errors=True)
        self.addCleanup(reset_settings_cache)
        self.addCleanup(reset_runtime_paths_cache)
        self.dist = build_fake_dist(self.temp_root)

    def client(self, **kwargs) -> TestClient:
        kwargs.setdefault("dist_dir", self.dist)
        return TestClient(build_api(**kwargs))


class ApiIsNeverShadowedTests(_StaticTestCase):
    def test_health_still_works_with_the_frontend_mounted(self) -> None:
        response = self.client().get("/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})

    def test_health_is_never_replaced_by_the_shell_even_in_a_browser(self) -> None:
        """/health must stay directly openable for diagnosis."""
        response = self.client().get("/health", headers=BROWSER)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})
        self.assertIn("/health", NEVER_SHELL_PATHS)

    def test_health_works_through_the_api_prefix_too(self) -> None:
        response = self.client().get("/api/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})

    def test_registered_api_routes_are_not_intercepted(self) -> None:
        client = self.client()
        for path in ("/portal/active", "/jobs"):
            for url in (path, API_PREFIX + path):
                with self.subTest(url=url):
                    response = client.get(url, headers=FETCH)
                    self.assertEqual(response.status_code, 200)
                    self.assertIn("application/json", response.headers["content-type"])

    def test_colliding_routes_serve_json_to_the_app_and_html_to_the_browser(self) -> None:
        """/applications, /jobs, /screening and /performance are both a page and an endpoint.
        The frontend always calls them through /api, so both can be satisfied."""
        client = self.client()
        for path in COLLIDING_ROUTES:
            with self.subTest(path=path):
                api_call = client.get(API_PREFIX + path, headers=FETCH)
                self.assertEqual(api_call.status_code, 200)
                self.assertEqual(api_call.json(), {"api": True})

                navigation = client.get(path, headers=BROWSER)
                self.assertEqual(navigation.status_code, 200)
                self.assertIn("text/html", navigation.headers["content-type"])

    def test_root_level_api_still_answers_non_browser_clients(self) -> None:
        """Scripts and the Streamlit fallback call the API without the /api prefix."""
        client = self.client()
        for path in COLLIDING_ROUTES:
            with self.subTest(path=path):
                self.assertEqual(client.get(path, headers=FETCH).json(), {"api": True})

    def test_post_routes_are_not_intercepted(self) -> None:
        """The fallback is GET/HEAD-only, so a POST endpoint is never touched."""
        client = self.client()
        for url in ("/applications/apply-selected", "/api/applications/apply-selected"):
            for headers in (FETCH, BROWSER):
                with self.subTest(url=url, accept=headers["Accept"][:9]):
                    response = client.post(url, headers=headers)
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(response.json(), {"applied": 0})

    def test_unknown_api_endpoint_returns_json_404_not_html(self) -> None:
        """A typo in a fetch() must fail loudly instead of receiving the HTML shell."""
        client = self.client()
        for url in ("/api/portal/does-not-exist", "/api/nope", "/api/matched-jobs"):
            for headers in (FETCH, BROWSER):
                with self.subTest(url=url, accept=headers["Accept"][:9]):
                    response = client.get(url, headers=headers)
                    self.assertEqual(response.status_code, 404)
                    self.assertNotIn("text/html", response.headers["content-type"])

    def test_unknown_root_path_is_404_for_non_browser_clients(self) -> None:
        response = self.client().get("/portal/not-a-real-endpoint", headers=FETCH)
        self.assertEqual(response.status_code, 404)
        self.assertNotIn("text/html", response.headers["content-type"])


class FrontendServingTests(_StaticTestCase):
    def test_index_is_served_at_the_root(self) -> None:
        response = self.client().get("/", headers=BROWSER)
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/html", response.headers["content-type"])
        self.assertIn('<div id="root">', response.text)

    def test_non_browser_request_to_the_root_is_not_given_the_shell(self) -> None:
        """A client that did not ask for HTML falls through to routing. Nothing serves "/",
        so it 404s -- which is why the launcher's readiness probe must send Accept: text/html."""
        response = self.client().get("/", headers=FETCH)
        self.assertEqual(response.status_code, 404)
        self.assertNotIn('<div id="root">', response.text)

    def test_index_is_not_cached(self) -> None:
        """The shell names fingerprinted bundles; a cached copy would outlive a rebuild."""
        response = self.client().get("/", headers=BROWSER)
        self.assertIn("no-store", response.headers.get("cache-control", ""))

    def test_javascript_and_css_assets_are_served(self) -> None:
        client = self.client()
        js = client.get("/assets/index-abc123.js")
        self.assertEqual(js.status_code, 200)
        self.assertIn("javascript", js.headers["content-type"])
        self.assertEqual(js.text, "export const x = 1;")
        css = client.get("/assets/index-abc123.css")
        self.assertEqual(css.status_code, 200)
        self.assertIn("text/css", css.headers["content-type"])

    def test_image_assets_at_the_dist_root_are_served(self) -> None:
        response = self.client().get("/logo.png", headers=BROWSER)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["content-type"], "image/png")
        self.assertEqual(response.content, b"\x89PNG\r\n\x1a\n")

    def test_missing_asset_under_assets_is_404_not_the_shell(self) -> None:
        """A stale chunk request must fail, not silently return HTML that cannot execute."""
        response = self.client().get("/assets/deleted-chunk.js", headers=BROWSER)
        self.assertEqual(response.status_code, 404)
        self.assertNotIn('<div id="root">', response.text)

    def test_client_side_routes_survive_a_refresh(self) -> None:
        client = self.client()
        for route in SPA_ROUTES:
            with self.subTest(route=route):
                response = client.get(route, headers=BROWSER)
                self.assertEqual(response.status_code, 200)
                self.assertIn("text/html", response.headers["content-type"])
                self.assertIn('<div id="root">', response.text)

    def test_nested_unknown_route_falls_back_to_the_shell(self) -> None:
        response = self.client().get("/matched-jobs/12345/details", headers=BROWSER)
        self.assertEqual(response.status_code, 200)
        self.assertIn('<div id="root">', response.text)

    def test_directory_traversal_cannot_escape_dist(self) -> None:
        secret = self.temp_root / "secret.txt"
        secret.write_text("do not serve me", encoding="utf-8")
        client = self.client()
        for attack in ("../secret.txt", "..%2Fsecret.txt", "assets/../../secret.txt",
                       "..%5Csecret.txt"):
            with self.subTest(attack=attack):
                response = client.get(f"/{attack}", headers=BROWSER)
                self.assertNotIn("do not serve me", response.text)


class MissingBuildTests(_StaticTestCase):
    def test_missing_dist_returns_an_explicit_503_with_instructions(self) -> None:
        client = self.client(dist_dir=self.temp_root / "never-built")
        response = client.get("/", headers=BROWSER)
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["detail"], MISSING_FRONTEND_MESSAGE)
        self.assertIn("npm run build", response.json()["detail"])

    def test_api_and_health_keep_working_when_the_build_is_missing(self) -> None:
        """A missing UI must stay diagnosable -- the backend still has to answer."""
        client = self.client(dist_dir=self.temp_root / "never-built")
        self.assertEqual(client.get("/health").status_code, 200)
        self.assertEqual(client.get("/api/portal/active").status_code, 200)

    def test_strict_mode_raises_a_named_error(self) -> None:
        with self.assertRaises(FrontendNotBuiltError) as caught:
            build_api(dist_dir=self.temp_root / "never-built", strict=True)
        self.assertIn("npm run build", str(caught.exception))

    def test_mount_summary_reports_what_was_found(self) -> None:
        present = mount_frontend(FastAPI(), dist_dir=self.dist)
        self.assertTrue(present["index_present"])
        self.assertTrue(present["assets_mounted"])
        self.assertEqual(present["dist_dir"], str(self.dist))
        missing = mount_frontend(FastAPI(), dist_dir=self.temp_root / "never-built")
        self.assertFalse(missing["index_present"])
        self.assertFalse(missing["assets_mounted"])


class DevelopmentModeTests(_StaticTestCase):
    """Development must not serve a production build left over from an earlier packaging
    run -- that would silently show stale UI while the developer edits source."""

    def test_disabled_mount_does_not_serve_stale_assets(self) -> None:
        client = self.client(enabled=False)
        for url in ("/", "/assets/index-abc123.js", "/matched-jobs", "/logo.png"):
            with self.subTest(url=url):
                self.assertEqual(client.get(url, headers=BROWSER).status_code, 404)

    def test_disabled_mount_keeps_the_api_and_the_prefix_rewrite(self) -> None:
        client = self.client(enabled=False)
        self.assertEqual(client.get("/health").status_code, 200)
        self.assertEqual(client.get("/api/health").status_code, 200)
        self.assertEqual(client.get("/api/portal/active").json(), {"portal": "linkedin"})

    def test_no_assets_mount_is_registered_when_disabled(self) -> None:
        api = build_api(dist_dir=self.dist, enabled=False)
        mounts = [r for r in api.routes if getattr(r, "name", "") == "frontend-assets"]
        self.assertEqual(mounts, [])

    def test_summary_reports_disabled(self) -> None:
        summary = mount_frontend(FastAPI(), dist_dir=self.dist, enabled=False)
        self.assertFalse(summary["enabled"])
        self.assertFalse(summary["assets_mounted"])


class MiddlewareUnitTests(unittest.TestCase):
    """The prefix rewrite is the exact server-side twin of the Vite proxy rule."""

    def run_middleware(self, path: str) -> str:
        seen: list[str] = []

        async def app(scope, receive, send):
            seen.append(scope["path"])

        middleware = FrontendMiddleware(app, dist_dir=Path("nowhere"), serve_shell=False)
        asyncio.run(middleware({"type": "http", "method": "GET", "path": path,
                                "headers": []}, None, None))
        return seen[0]

    def test_prefix_is_stripped_exactly_like_the_vite_proxy(self) -> None:
        for sent, expected in (("/api/portal/active", "/portal/active"),
                               ("/api", "/"),
                               ("/api/", "/"),
                               ("/health", "/health"),
                               ("/apibogus", "/apibogus")):
            with self.subTest(sent=sent):
                self.assertEqual(self.run_middleware(sent), expected)

    def test_non_http_scopes_pass_through_untouched(self) -> None:
        seen: list[dict] = []

        async def app(scope, receive, send):
            seen.append(scope)

        scope = {"type": "lifespan", "path": "/api/whatever"}
        middleware = FrontendMiddleware(app, dist_dir=Path("nowhere"), serve_shell=True)
        asyncio.run(middleware(scope, None, None))
        self.assertEqual(seen[0]["path"], "/api/whatever")

    def test_navigation_detection(self) -> None:
        self.assertTrue(accepts_html([(b"accept", b"text/html,*/*;q=0.8")]))
        self.assertTrue(accepts_html([(b"Accept", b"TEXT/HTML")]))
        self.assertFalse(accepts_html([(b"accept", b"*/*")]))
        self.assertFalse(accepts_html([(b"accept", b"application/json")]))
        self.assertFalse(accepts_html([]))

    def test_vite_proxy_rule_and_middleware_agree(self) -> None:
        """If these two ever diverge, dev and packaged mode would disagree about where the
        API lives, and every request would break in exactly one of the two modes."""
        config = (project_root() / "frontend" / "vite.config.ts").read_text(encoding="utf-8")
        self.assertIn('"/api"', config)
        self.assertRegex(config, r"replace\(/\^\\/api/,\s*\"\"\)")
        self.assertEqual(API_PREFIX, "/api")

    def test_production_asset_base_is_absolute(self) -> None:
        """A relative base would 404 every chunk after a refresh on a deep route."""
        config = (project_root() / "frontend" / "vite.config.ts").read_text(encoding="utf-8")
        self.assertRegex(config, r'base:\s*"/"')


class RuntimeModeTests(_StaticTestCase):
    """The packaged/development decision must be explicit and must not depend on the
    working directory."""

    MODE_VARS = ("SERVE_FRONTEND", MODE_ENV, "FRONTEND_DIST_DIR")

    def settings(self, **overrides) -> Settings:
        # Remove rather than blank the variables: pydantic rejects "" for a bool field.
        environ = {k: v for k, v in os.environ.items() if k not in self.MODE_VARS}
        environ.update(overrides)
        with patch.dict(os.environ, environ, clear=True):
            reset_runtime_paths_cache()
            reset_settings_cache()
            return Settings()

    def test_packaged_mode_serves_the_frontend_by_default(self) -> None:
        self.assertTrue(self.settings(**{MODE_ENV: MODE_PACKAGED}).should_serve_frontend())

    def test_development_mode_does_not_serve_the_frontend_by_default(self) -> None:
        self.assertFalse(self.settings(**{MODE_ENV: MODE_DEVELOPMENT}).should_serve_frontend())

    def test_explicit_flag_overrides_the_detected_mode(self) -> None:
        forced_on = self.settings(**{MODE_ENV: MODE_DEVELOPMENT, "SERVE_FRONTEND": "true"})
        self.assertTrue(forced_on.should_serve_frontend())
        forced_off = self.settings(**{MODE_ENV: MODE_PACKAGED, "SERVE_FRONTEND": "false"})
        self.assertFalse(forced_off.should_serve_frontend())

    def test_dist_directory_is_anchored_to_the_installation_not_the_cwd(self) -> None:
        settings = self.settings()
        self.assertTrue(settings.frontend_dist_dir.is_absolute())
        self.assertEqual(settings.frontend_dist_dir, install_root() / "frontend" / "dist")
        self.assertEqual(frontend_dist_dir(), settings.frontend_dist_dir)

    def test_a_relative_dist_override_resolves_against_the_installation(self) -> None:
        """Bundled assets anchor to the install directory, not the writable data root."""
        settings = self.settings(**{"FRONTEND_DIST_DIR": "frontend/dist"})
        self.assertEqual(settings.frontend_dist_dir, install_root() / "frontend" / "dist")

    def test_frontend_build_presence_is_reported(self) -> None:
        settings = self.settings(**{"FRONTEND_DIST_DIR": str(self.dist)})
        self.assertTrue(settings.frontend_build_present())
        self.assertEqual(settings.frontend_index_file(), self.dist / "index.html")
        missing = self.settings(**{"FRONTEND_DIST_DIR": str(self.temp_root / "nope")})
        self.assertFalse(missing.frontend_build_present())

    def test_dist_is_not_a_writable_runtime_directory(self) -> None:
        """It ships with the program; it must never land in the per-user data folder."""
        settings = self.settings()
        self.assertNotIn(settings.frontend_dist_dir, settings.writable_directories())


class LauncherTests(unittest.TestCase):
    """start_app.ps1 decides which URL to open and whether Node.js is needed at all."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.script = (project_root() / "start_app.ps1").read_text(encoding="utf-8")

    def test_packaged_switch_exists(self) -> None:
        self.assertRegex(self.script, r"\[switch\]\$Packaged")

    def test_browser_opens_the_correct_url_in_each_mode(self) -> None:
        self.assertRegex(
            self.script,
            r'\$appUrl\s*=\s*if\s*\(\$Packaged\)\s*\{\s*\$apiUrl\s*\}\s*else\s*\{\s*\$viteUrl\s*\}',
        )
        self.assertIn('$apiUrl = "http://127.0.0.1:8000"', self.script)
        self.assertIn('$viteUrl = "http://127.0.0.1:5173"', self.script)
        self.assertIn("Start-Process $appUrl", self.script)

    def test_only_loopback_urls_are_used(self) -> None:
        """Everything stays local: no remote host may be baked into the launcher."""
        for url in re.findall(r"https?://[^\s\"']+", self.script):
            with self.subTest(url=url):
                self.assertRegex(url, r"^https?://(127\.0\.0\.1|localhost)(:\d+)?")

    def test_packaged_mode_never_requires_node(self) -> None:
        """Node resolution and the Vite launch must both sit behind '-not $Packaged'."""
        self.assertRegex(self.script, r"\$nodeExe = \$null\s*\r?\nif \(-not \$Packaged\) \{")
        self.assertRegex(self.script, r"\$vite = \$null\s*\r?\nif \(-not \$Packaged\) \{")

    def test_readiness_probe_identifies_itself_as_a_browser_navigation(self) -> None:
        """FastAPI serves the shell only for a navigation, so a bare Invoke-WebRequest
        (Accept: */*) correctly 404s and would fail startup. The probe must send the same
        header the real browser sends."""
        self.assertRegex(self.script, r'\$browserAccept\s*=\s*@\{\s*Accept\s*=\s*"text/html')
        self.assertRegex(
            self.script,
            r'Wait-HttpOk \$apiUrl "Web interface" \$apiStdout \$apiStderr -Headers \$browserAccept',
        )

    def test_wait_helper_was_extended_rather_than_duplicated(self) -> None:
        """One polling loop, with optional headers -- not a second copy of the retry logic."""
        self.assertEqual(self.script.count("function Wait-HttpOk"), 1)
        self.assertRegex(self.script, r"\[hashtable\]\$Headers = @\{\}")
        self.assertIn("-TimeoutSec 2 -Headers $Headers", self.script)

    def test_the_probe_header_matches_what_the_middleware_detects(self) -> None:
        """The launcher's Accept value must actually satisfy the navigation test."""
        match = re.search(r'\$browserAccept = @\{ Accept = "([^"]+)" \}', self.script)
        self.assertIsNotNone(match, "no browser Accept header defined in the launcher")
        self.assertTrue(accepts_html([(b"accept", match.group(1).encode())]))

    def test_health_probe_is_not_given_the_browser_header(self) -> None:
        """/health answers everyone; probing it as a browser would hide a real regression."""
        health_probe = re.search(r'Wait-HttpOk "http://[^"]*/health"[^\r\n]*', self.script)
        self.assertIsNotNone(health_probe, "no /health readiness probe found")
        self.assertIn('"FastAPI"', health_probe.group(0))
        self.assertNotIn("-Headers", health_probe.group(0))

    def test_packaged_mode_verifies_the_build_exists(self) -> None:
        self.assertIn('Test-Path (Join-Path $frontendDist "index.html")', self.script)
        self.assertIn("npm run build", self.script)

    def test_packaged_mode_tells_the_backend_to_serve_the_frontend(self) -> None:
        self.assertRegex(self.script, r'SetEnvironmentVariable\("SERVE_FRONTEND"')
        self.assertRegex(self.script, r'if \(\$Packaged\) \{ "true" \} else \{ "false" \}')

    def test_pid_and_log_handling_survives_a_missing_vite_process(self) -> None:
        """Packaged mode records no vite.pid, so the summary must not dereference it."""
        self.assertIn('if ($vite) { Write-Host "    React    (work\\vite.pid):', self.script)
        self.assertIn('Set-Content (Join-Path $work "api.pid") $api.Id', self.script)
        self.assertIn('Stop-PidFileProcess "vite"', self.script)

    def test_stop_script_still_cleans_up_every_pid_file(self) -> None:
        stop = (project_root() / "stop_app.ps1").read_text(encoding="utf-8")
        self.assertIn('"api", "vite", "streamlit"', stop)


class RealApplicationWiringTests(unittest.TestCase):
    def test_frontend_is_mounted_after_every_api_route(self) -> None:
        """The assets mount must not be able to shadow an endpoint declared later."""
        source = (project_root() / "app" / "main.py").read_text(encoding="utf-8")
        mount_at = source.index("mount_frontend(\n")
        last_route_at = max(source.rindex(f'@app.{verb}("')
                            for verb in ("get", "post", "put", "delete", "patch")
                            if f'@app.{verb}("' in source)
        self.assertGreater(mount_at, last_route_at,
                           "mount_frontend must be called after all API routes are declared")

    def test_real_application_exposes_health_and_the_mount_summary(self) -> None:
        from app.main import app as real_app, frontend_mount

        paths = [getattr(route, "path", "") for route in real_app.routes]
        self.assertIn("/health", paths)
        self.assertIn("dist_dir", frontend_mount)
        self.assertTrue(Path(frontend_mount["dist_dir"]).is_absolute())

    def test_health_answers_on_the_real_application(self) -> None:
        from app.main import app as real_app

        # No context manager: the lifespan (and its database initialisation) is not needed
        # for a static route, which keeps this test independent of a running database.
        response = TestClient(real_app).get("/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})


if __name__ == "__main__":
    unittest.main()
