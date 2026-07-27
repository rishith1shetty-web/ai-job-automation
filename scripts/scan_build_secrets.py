"""Fail a build that contains secrets or user data.

A packaged folder is copied to other computers. Anything private that reaches ``dist/`` is
published, so this runs as a mandatory build step rather than as advice: ``build_windows.ps1``
exits 7 when it reports anything.

Two kinds of finding:

* **Forbidden files** -- a name or location that must never be packaged (``.env``, the DPAPI
  credential blob, browser session state, uploaded resumes, logs, database dumps).
* **Forbidden content** -- a credential-shaped string inside a bundled file, which catches a
  password accidentally hardcoded into a module that was then compiled in.

Only text-like bundled files are scanned for content; compiled dependencies and browser
binaries are matched by path rules alone, since scanning 500 MB of Chromium for password
patterns produces noise, not safety.
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

# Files that must never appear anywhere in the build.
FORBIDDEN_NAMES = {
    ".env", ".env.local", ".env.production",
    "database_credential.dat",
    "linkedin_state.json", "dice_state.json",
    ".pgpass", "pgpass.conf",
    "id_rsa", "id_ed25519",
}

# ".pem" is deliberately NOT here. certifi ships cacert.pem -- a bundle of PUBLIC certificate
# authorities that outbound HTTPS genuinely needs. Private keys also use the .pem extension,
# so .pem files are read instead and rejected on CONTENT ("BEGIN ... PRIVATE KEY").
FORBIDDEN_SUFFIXES = {".dump", ".bak", ".pfx", ".key", ".sqlite", ".sqlite3", ".db"}

# Directories that are runtime state or user data, never build output.
FORBIDDEN_DIR_NAMES = {
    "outputs", "work", "logs", "uploads", "storage",
    "browser_sessions", "screenshots", "html_diagnostics",
    "__pycache__", ".pytest_cache", ".git",
}

# Extensions worth reading. Everything else is matched by path rules only.
SCANNABLE_SUFFIXES = {
    ".py", ".txt", ".json", ".ini", ".cfg", ".toml", ".yaml", ".yml",
    ".js", ".mjs", ".css", ".html", ".md", ".mako", ".ps1", ".env", ".pem", ".crt",
}
# certifi's bundle is ~300 KB; a private key is far smaller. 8 MB covers both comfortably
# while still skipping compiled artefacts.
MAX_SCAN_BYTES = 8_000_000

CONTENT_PATTERNS = (
    # A connection URL carrying a real password.
    (re.compile(r"postgres(?:ql)?(?:\+\w+)?://[^\s\"'<>]*:[^\s\"'<>@/]{3,}@"), "database URL with a password"),
    # Provider API keys.
    (re.compile(r"\bsk-[A-Za-z0-9]{20,}"), "OpenAI API key"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "AWS access key id"),
    (re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"), "private key"),
    # PGPASSWORD=... with a real value.
    (re.compile(r"PGPASSWORD\s*=\s*[^\s\"'#]{3,}"), "PGPASSWORD assignment"),
)

# Placeholders that legitimately appear in shipped documentation and examples.
ALLOWED_LITERALS = ("***", "yourpassword", "somepassword", "changeme", "<password>",
                    "your-password", "PGPASSWORD (and")


@dataclass
class ScanReport:
    root: Path
    forbidden_files: list[str] = field(default_factory=list)
    content_findings: list[str] = field(default_factory=list)
    files_scanned: int = 0
    files_seen: int = 0

    @property
    def clean(self) -> bool:
        return not self.forbidden_files and not self.content_findings

    def render(self) -> str:
        lines = [
            f"Scanned : {self.root}",
            f"Files    : {self.files_seen} seen, {self.files_scanned} inspected for content",
        ]
        if self.clean:
            lines.append("RESULT: clean -- no secrets or user data found.")
            return "\n".join(lines)
        lines.append(f"RESULT: {len(self.forbidden_files) + len(self.content_findings)} problems.")
        lines += [f"  forbidden file: {item}" for item in self.forbidden_files]
        lines += [f"  secret content: {item}" for item in self.content_findings]
        return "\n".join(lines)


def _is_forbidden_path(path: Path, root: Path) -> str | None:
    relative = path.relative_to(root)
    if path.name.lower() in FORBIDDEN_NAMES:
        return str(relative)
    if path.suffix.lower() in FORBIDDEN_SUFFIXES:
        return str(relative)
    # A forbidden directory name only matters when it is OUR data directory, not a name
    # that happens to occur inside a third-party package (e.g. some library's "logs" module).
    parts = [part.lower() for part in relative.parts[:-1]]
    for index, part in enumerate(parts):
        if part in FORBIDDEN_DIR_NAMES and index <= 1:
            return str(relative)
    return None


def scan(root: Path) -> ScanReport:
    report = ScanReport(root=root)
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        report.files_seen += 1

        forbidden = _is_forbidden_path(path, root)
        if forbidden:
            report.forbidden_files.append(forbidden)
            continue

        if path.suffix.lower() not in SCANNABLE_SUFFIXES:
            continue
        try:
            if path.stat().st_size > MAX_SCAN_BYTES:
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        report.files_scanned += 1

        for pattern, label in CONTENT_PATTERNS:
            for match in pattern.finditer(text):
                snippet = match.group(0)
                if any(allowed in snippet for allowed in ALLOWED_LITERALS):
                    continue
                report.content_findings.append(
                    f"{path.relative_to(root)}: {label}"
                )
                break
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Fail the build if it contains secrets.")
    parser.add_argument("--path", required=True, help="Directory to scan (the build output).")
    args = parser.parse_args(argv)

    root = Path(args.path)
    if not root.is_dir():
        print(f"Not a directory: {root}", file=sys.stderr)
        return 2

    report = scan(root)
    print(report.render())
    return 0 if report.clean else 1


if __name__ == "__main__":
    sys.exit(main())
