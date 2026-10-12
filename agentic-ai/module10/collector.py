"""Install and run the checksum-pinned native OpenTelemetry Collector."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import os
from pathlib import Path
import platform
import ipaddress
import shutil
import signal
import socket
import subprocess
import tarfile
import tempfile
import time
from threading import current_thread, main_thread
from urllib.parse import urlsplit
import urllib.request

from .urls import open_url

VERSION = "0.160.0"
ROOT = Path(__file__).parent
CACHE = ROOT / ".cache" / "collector" / VERSION
DEFAULT_ENDPOINT = "http://127.0.0.1:4318"
SETTINGS = ("LOGZIO_REGION", "LOGZIO_LOGS_TOKEN", "LOGZIO_TRACES_TOKEN")


def install() -> Path:
    system = {"Darwin": "darwin", "Linux": "linux"}.get(platform.system())
    machine = {"arm64": "arm64", "aarch64": "arm64", "x86_64": "amd64"}.get(platform.machine())
    if not system or not machine:
        raise RuntimeError("The collector launcher supports macOS and Linux on ARM64 or x86-64.")
    binary = CACHE / f"{system}_{machine}" / "otelcol-contrib"
    if binary.exists():
        return binary
    archive = f"otelcol-contrib_{VERSION}_{system}_{machine}.tar.gz"
    base = f"https://github.com/open-telemetry/opentelemetry-collector-releases/releases/download/v{VERSION}/"
    with tempfile.TemporaryDirectory(prefix="module10-collector-") as directory:
        downloaded = Path(directory) / archive
        with open_url(base + archive, timeout=60) as response, downloaded.open("wb") as out:
            shutil.copyfileobj(response, out)
        with open_url(base + archive + ".sha256", timeout=30) as response:
            digest = response.read().decode().split()[0]
        if hashlib.file_digest(downloaded.open("rb"), "sha256").hexdigest() != digest:
            raise RuntimeError("Collector download did not match the release checksum.")
        with tarfile.open(downloaded) as contents:
            entry = next(m for m in contents.getmembers() if m.name.removeprefix("./") == "otelcol-contrib")
            if not entry.isfile():
                raise RuntimeError("Collector archive has no regular executable.")
            binary.parent.mkdir(parents=True, exist_ok=True)
            temporary = binary.with_suffix(".tmp")
            temporary.write_bytes(contents.extractfile(entry).read())
            temporary.chmod(0o755)
            temporary.replace(binary)
    return binary


def validate_settings(*, check_dns=False):
    missing = [key for key in SETTINGS if not os.getenv(key)]
    if missing:
        raise ValueError("Set local environment variables: " + ", ".join(missing))
    if os.environ["LOGZIO_REGION"] not in {"us", "au", "ca", "eu", "uk"}:
        raise ValueError("LOGZIO_REGION must match the account: us, au, ca, eu or uk.")
    if check_dns:
        region = os.environ["LOGZIO_REGION"]
        listener = "otlp-listener" + ("" if region == "us" else "-" + region) + ".logz.io"
        try:
            addresses = {r[4][0] for r in socket.getaddrinfo(listener, 443)}
        except OSError:
            raise ValueError(f"Cannot resolve {listener}; check DNS and retry the demo.") from None
        if not addresses or all(ipaddress.ip_address(a).is_unspecified or ipaddress.ip_address(a).is_loopback for a in addresses):
            raise ValueError(f"{listener} resolves to a blocked address. Allow the ingestion host in your DNS filter.")


def ready(endpoint):
    """An empty OTLP request checks the receiver without sending a trace."""
    request = urllib.request.Request(endpoint.rstrip("/") + "/v1/traces", data=b"",
        headers={"Content-Type": "application/x-protobuf"})
    try:
        # MODULE10_OTLP_ENDPOINT is an HTTP(S) collector base URL (managed_collector and observability check it),
        # so plain HTTP to a configured remote collector stays allowed; other schemes are refused.
        with open_url(request, timeout=1, any_http_host=True) as response:
            return response.status == 200
    except OSError:
        return False


def stop(process):
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


@contextmanager
def shutdown_signals():
    """Unwind owned resources when the terminal/session is terminated."""
    previous = {}

    def exit_session(number, _frame):
        raise SystemExit(128 + number)

    try:
        if current_thread() is main_thread():
            for number in (signal.SIGTERM, signal.SIGHUP):
                previous[number] = signal.signal(number, exit_session)
        yield
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler)


@contextmanager
def managed_collector(*, required=False, startup_timeout=20):
    """Keep an owned collector alive until the demo has flushed and closed."""
    previous = os.environ.get("MODULE10_OTLP_ENDPOINT")
    if not required and not previous and not any(os.getenv(key) for key in SETTINGS):
        yield None
        return
    endpoint = previous or DEFAULT_ENDPOINT
    parsed = urlsplit(endpoint)
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname
            or parsed.username or parsed.password or parsed.query or parsed.fragment):
        raise ValueError("MODULE10_OTLP_ENDPOINT must be an HTTP(S) collector base URL.")
    os.environ["MODULE10_OTLP_ENDPOINT"] = endpoint
    try:
        if ready(endpoint):
            yield None  # A pre-existing process belongs to its original owner.
            return
        if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost"} or parsed.path not in {"", "/"}:
            raise ValueError("The configured collector is unavailable. Unset MODULE10_OTLP_ENDPOINT "
                             "to let the demo manage a local collector.")
        validate_settings(check_dns=True)
        binary = install()
        with shutdown_signals(), tempfile.TemporaryDirectory(prefix="module10-collector-") as directory, tempfile.TemporaryFile() as logs:
            config = Path(directory) / "collector.yaml"
            config.write_text((ROOT / "collector.yaml").read_text()
                .replace("endpoint: 127.0.0.1:4318", f"endpoint: 127.0.0.1:{parsed.port or 80}")
                .replace("endpoint: 127.0.0.1:13133", "endpoint: 127.0.0.1:0"))
            try:
                # nosemgrep: dangerous-subprocess-use-audit - list args, no shell; the checksum-verified collector from install(), a config written here
                subprocess.run([str(binary), "validate", "--config", str(config)], check=True,
                    timeout=15, stdout=logs, stderr=logs)
            except (subprocess.SubprocessError, OSError):
                raise ValueError("Collector configuration validation failed; check the Logz.io settings.") from None
            # nosemgrep: dangerous-subprocess-use-audit - list args, no shell; the checksum-verified collector from install(), a config written here
            process = subprocess.Popen([str(binary), "--config", str(config)],
                stdout=logs, stderr=logs, start_new_session=True)
            try:
                deadline = time.monotonic() + startup_timeout
                while True:
                    if process.poll() is not None:
                        raise ValueError("Collector exited during startup; check the Logz.io settings and local port.")
                    if ready(endpoint):
                        break
                    if time.monotonic() >= deadline:
                        raise ValueError("Collector startup timed out; retry the demo.")
                    time.sleep(0.1)
                yield process
            finally:
                stop(process)
    finally:
        if previous is None:
            os.environ.pop("MODULE10_OTLP_ENDPOINT", None)
        else:
            os.environ["MODULE10_OTLP_ENDPOINT"] = previous


def main():
    from .observability import load_environment
    load_environment()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("install", "validate", "run"))
    args = parser.parse_args()
    if args.action == "install":
        print(f"Installed OpenTelemetry Collector Contrib {VERSION}: {install()}")
        return
    try:
        validate_settings(check_dns=args.action == "run")
    except ValueError as error:
        parser.error(str(error))
    binary = install()
    config = str(ROOT / "collector.yaml")
    if args.action == "validate":
        # nosemgrep: dangerous-subprocess-use-audit - list args, no shell; the checksum-verified collector from install(), the packaged config
        subprocess.run([str(binary), "validate", "--config", config], check=True)
    else:
        print("Collector receives logs and traces at http://127.0.0.1:4318", flush=True)
        # nosemgrep: dangerous-os-exec-audit - the checksum-verified collector from install(), the packaged config
        os.execv(str(binary), [str(binary), "--config", config])


if __name__ == "__main__":
    main()
