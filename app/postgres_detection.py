"""Find out whether PostgreSQL is installed, running, and reachable on a Windows computer.

Deliberately version-agnostic: nothing here assumes ``C:\\Program Files\\PostgreSQL\\16``.
Installations are discovered from the registry keys the official EnterpriseDB installer
writes (``HKLM\\SOFTWARE\\PostgreSQL\\Installations\\*``), which carry the base directory,
data directory, version and service name for every installed major version. Service state
comes from the service registry plus ``sc query``; the port is read from ``postgresql.conf``
when the data directory is readable, and confirmed by probing.

Distinguishing PostgreSQL from an unrelated program on port 5432 is done at the protocol
level rather than by guessing from an error message: :func:`probe_port` opens a socket and
sends a PostgreSQL SSLRequest packet. A PostgreSQL server answers with exactly one byte,
``S`` or ``N``. Anything else on that port answers differently, or not at all -- so the
result is a fact, not a heuristic.
"""

from __future__ import annotations

import logging
import re
import socket
import struct
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_HOST = "localhost"
DEFAULT_PORT = 5432
PROBE_TIMEOUT_SECONDS = 3.0

REGISTRY_ROOTS = (
    r"SOFTWARE\PostgreSQL\Installations",
    r"SOFTWARE\WOW6432Node\PostgreSQL\Installations",
)

# The PostgreSQL SSLRequest packet: int32 length (8) + int32 request code (80877103).
SSL_REQUEST_PACKET = struct.pack("!ii", 8, 80877103)
SSL_REPLIES = {b"S", b"N"}


@dataclass(frozen=True)
class PostgresInstallation:
    """One installed PostgreSQL, as recorded by its installer."""

    version: str = ""
    base_directory: str = ""
    data_directory: str = ""
    service_name: str = ""
    port: int | None = None

    @property
    def psql_path(self) -> str:
        if not self.base_directory:
            return ""
        candidate = Path(self.base_directory) / "bin" / "psql.exe"
        return str(candidate) if candidate.is_file() else ""

    @property
    def major_version(self) -> int | None:
        match = re.match(r"(\d+)", self.version or "")
        return int(match.group(1)) if match else None


@dataclass(frozen=True)
class ServiceState:
    name: str
    state: str  # RUNNING | STOPPED | UNKNOWN
    start_type: str = ""

    @property
    def running(self) -> bool:
        return self.state.upper() == "RUNNING"


@dataclass
class ProbeResult:
    """What is actually listening on a host/port."""

    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    reachable: bool = False
    is_postgres: bool = False
    detail: str = ""

    @property
    def occupied_by_other(self) -> bool:
        """Something is listening, but it is NOT PostgreSQL."""
        return self.reachable and not self.is_postgres


@dataclass
class DetectionReport:
    """Everything discovered, with no secrets anywhere in it."""

    installations: list[PostgresInstallation] = field(default_factory=list)
    services: list[ServiceState] = field(default_factory=list)
    probe: ProbeResult = field(default_factory=ProbeResult)
    platform: str = sys.platform

    @property
    def installed(self) -> bool:
        return bool(self.installations) or bool(self.services)

    @property
    def running(self) -> bool:
        return self.probe.is_postgres or any(s.running for s in self.services)

    def to_dict(self) -> dict:
        return {
            "platform": self.platform,
            "installed": self.installed,
            "running": self.running,
            "installations": [
                {
                    "version": i.version,
                    "base_directory": i.base_directory,
                    "data_directory": i.data_directory,
                    "service_name": i.service_name,
                    "port": i.port,
                }
                for i in self.installations
            ],
            "services": [
                {"name": s.name, "state": s.state, "start_type": s.start_type}
                for s in self.services
            ],
            "probe": {
                "host": self.probe.host,
                "port": self.probe.port,
                "reachable": self.probe.reachable,
                "is_postgres": self.probe.is_postgres,
                "detail": self.probe.detail,
            },
        }


# --------------------------------------------------------------------------------------
# Probing
# --------------------------------------------------------------------------------------

def probe_port(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT,
               timeout: float = PROBE_TIMEOUT_SECONDS) -> ProbeResult:
    """Is anything listening, and is it PostgreSQL? Sends no credentials."""
    result = ProbeResult(host=host, port=port)
    try:
        with socket.create_connection((host, port), timeout=timeout) as connection:
            result.reachable = True
            connection.settimeout(timeout)
            connection.sendall(SSL_REQUEST_PACKET)
            reply = connection.recv(1)
    except (OSError, socket.timeout) as error:
        if result.reachable:
            result.detail = f"connected, but the service did not answer a PostgreSQL handshake ({error})"
        else:
            result.detail = f"nothing is listening on {host}:{port} ({error})"
        return result

    if reply in SSL_REPLIES:
        result.is_postgres = True
        result.detail = "PostgreSQL responded to the connection handshake"
    else:
        result.detail = (
            f"port {port} is in use by something that is not PostgreSQL "
            f"(unexpected handshake reply {reply!r})"
        )
    return result


def server_version(host: str, port: int, user: str, password: str,
                   database: str = "postgres") -> str | None:
    """The server version string, or None. Requires valid credentials."""
    try:
        import psycopg

        with psycopg.connect(host=host, port=port, user=user, password=password,
                             dbname=database, connect_timeout=5) as connection:
            with connection.cursor() as cursor:
                cursor.execute("SHOW server_version")
                row = cursor.fetchone()
                return row[0] if row else None
    except Exception as error:  # pragma: no cover - depends on a live server
        logger.debug("Could not read server version: %s", type(error).__name__)
        return None


# --------------------------------------------------------------------------------------
# Windows discovery
# --------------------------------------------------------------------------------------

def _port_from_conf(data_directory: str) -> int | None:
    """Read ``port = NNNN`` from postgresql.conf when the data directory is readable."""
    if not data_directory:
        return None
    conf = Path(data_directory) / "postgresql.conf"
    try:
        for line in conf.read_text(encoding="utf-8", errors="ignore").splitlines():
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            match = re.match(r"port\s*=\s*(\d+)", stripped)
            if match:
                return int(match.group(1))
    except OSError:
        return None
    return None


def find_installations() -> list[PostgresInstallation]:
    """Every installed PostgreSQL recorded in the registry. Empty off Windows."""
    if sys.platform != "win32":
        return []
    try:
        import winreg
    except ImportError:  # pragma: no cover
        return []

    found: list[PostgresInstallation] = []
    for root in REGISTRY_ROOTS:
        try:
            key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, root)
        except OSError:
            continue
        try:
            index = 0
            while True:
                try:
                    name = winreg.EnumKey(key, index)
                except OSError:
                    break
                index += 1
                try:
                    with winreg.OpenKey(key, name) as entry:
                        values = {}
                        for value_index in range(winreg.QueryInfoKey(entry)[1]):
                            value_name, value, _ = winreg.EnumValue(entry, value_index)
                            values[value_name] = value
                except OSError:
                    continue
                data_directory = str(values.get("Data Directory", ""))
                found.append(PostgresInstallation(
                    version=str(values.get("Version", "")),
                    base_directory=str(values.get("Base Directory", "")),
                    data_directory=data_directory,
                    service_name=str(values.get("Service ID", "")),
                    port=_port_from_conf(data_directory),
                ))
        finally:
            key.Close()
    return found


def _query_service(name: str) -> ServiceState:
    try:
        completed = subprocess.run(
            ["sc", "query", name], capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return ServiceState(name=name, state="UNKNOWN")
    match = re.search(r"STATE\s+:\s+\d+\s+(\w+)", completed.stdout or "")
    return ServiceState(name=name, state=match.group(1) if match else "UNKNOWN")


def find_services() -> list[ServiceState]:
    """Installed PostgreSQL Windows services and their state. Empty off Windows."""
    if sys.platform != "win32":
        return []
    try:
        import winreg
    except ImportError:  # pragma: no cover
        return []

    names: list[str] = []
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                            r"SYSTEM\CurrentControlSet\Services") as key:
            index = 0
            while True:
                try:
                    name = winreg.EnumKey(key, index)
                except OSError:
                    break
                index += 1
                if re.match(r"postgresql", name, re.IGNORECASE):
                    names.append(name)
    except OSError:
        return []
    return [_query_service(name) for name in sorted(set(names))]


def detect(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> DetectionReport:
    """The full picture: installations, services and what is on the port."""
    installations = find_installations()
    # A non-default configured port is worth probing instead of blindly using 5432.
    configured_ports = [i.port for i in installations if i.port]
    if port == DEFAULT_PORT and configured_ports and DEFAULT_PORT not in configured_ports:
        port = configured_ports[0]
        logger.info("Probing configured PostgreSQL port %s instead of the default.", port)
    return DetectionReport(
        installations=installations,
        services=find_services(),
        probe=probe_port(host, port),
    )
