# Copyright 2025 Marcus Boden
# See LICENSE file for licensing details.
"""Integration test for the certs-added-but-not-applied restart bug.

Reproduces the reported issue directly: attaching the `certificates`
relation to an *already-active* unit pushes the certificate and rewrites
config.yaml with TLS enabled, but headscale-server's Pebble layer command
never changes, so `container.replan()` alone can't detect the config
content changed and won't restart an already-running service. Without an
explicit restart (see `HeadscaleCharm._configure_and_restart`), the running
process would keep serving plain HTTP forever, never picking up TLS.

This needs a real TLS-certificates provider charm (`self-signed-certificates`
from Charmhub) integrated with an already-deployed, already-active unit --
exactly the sequence that exposed the bug, and one that can't be exercised
by deploying with certificates already attached from the start.
"""

from __future__ import annotations

import pathlib
import time
from typing import Callable

import jubilant
import pytest

from tests.integration.helpers import DERP_MAP_URL_CONFIG

WAIT_TIMEOUT = 600
CERT_POLL_TIMEOUT = 120

BuiltCharm = Callable[[str], pathlib.Path]
BuiltRock = Callable[[str], str]

HEADSCALE_VERSION = "0.29.3"


def _wait_active(juju: jubilant.Juju, app: str) -> None:
    """Wait until `app` (and only `app`) is active. See test_upgrade.py's docstring."""
    juju.wait(lambda status: jubilant.all_active(status, app), timeout=WAIT_TIMEOUT)


def _wait_for_cert(juju: jubilant.Juju, app: str) -> None:
    """Poll until the certificate file actually appears in the workload container.

    `all_active` on both apps doesn't prove the cert exchange has actually
    completed -- both apps can trivially already show "active" (the
    headscale unit was already active before the relation was even added,
    and self-signed-certificates goes active almost immediately on its own,
    independent of whether it's finished issuing anything yet) without the
    certificate having been requested, issued, and pushed to the workload.
    """
    deadline = time.monotonic() + CERT_POLL_TIMEOUT
    while time.monotonic() < deadline:
        try:
            juju.ssh(
                f"{app}/0",
                "test -f /etc/headscale/headscale.pem",
                container="headscale",
            )
            return
        except jubilant.CLIError:
            time.sleep(5)
    raise TimeoutError(
        f"/etc/headscale/headscale.pem never appeared within {CERT_POLL_TIMEOUT}s"
    )


def _wait_for_tls_listener(juju: jubilant.Juju, app: str) -> str:
    """Poll until the running headscale process actually answers on 443 with TLS.

    There can be a short delay between the cert file landing on disk and the
    restarted process finishing startup and binding the new port, so this
    polls rather than checking once immediately after the cert appears.

    Uses python3's ssl module rather than curl: this rock's image is lean
    and doesn't stage curl, but python3 is always present (the exporter
    service depends on it).
    """
    tls_probe = (
        "python3 -c \"\n"
        "import socket, ssl\n"
        "ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)\n"
        "ctx.check_hostname = False\n"
        "ctx.verify_mode = ssl.CERT_NONE\n"
        "with socket.create_connection(('localhost', 443), timeout=5) as sock:\n"
        "    with ctx.wrap_socket(sock) as tls:\n"
        "        print('TLS_OK', tls.version())\n"
        "\" || echo TLS_FAILED"
    )
    deadline = time.monotonic() + CERT_POLL_TIMEOUT
    last_output = ""
    while time.monotonic() < deadline:
        last_output = juju.ssh(f"{app}/0", tls_probe, container="headscale")
        if "TLS_FAILED" not in last_output:
            return last_output
        time.sleep(5)
    return last_output


@pytest.mark.juju_setup
def test_certs_added_to_active_unit_restarts_and_enables_tls(
    juju: jubilant.Juju,
    built_charm: BuiltCharm,
    built_rock: BuiltRock,
) -> None:
    """Attaching certificates to an already-active unit must actually apply TLS."""
    app = "headscale-certs-restart"

    juju.deploy(
        built_charm(HEADSCALE_VERSION),
        app=app,
        resources={"headscale-image": built_rock(HEADSCALE_VERSION)},
        config=DERP_MAP_URL_CONFIG,
    )
    _wait_active(juju, app)

    # Before certs: plain HTTP, port 80, no cert file.
    before = juju.ssh(f"{app}/0", "cat /etc/headscale/config.yaml", container="headscale")
    assert "tls_cert_path" not in before
    assert "listen_addr: 0.0.0.0:80" in before

    juju.deploy("self-signed-certificates", app="self-signed-certificates")
    juju.integrate(app, "self-signed-certificates")
    juju.wait(
        lambda status: jubilant.all_active(status, app, "self-signed-certificates"),
        timeout=WAIT_TIMEOUT,
    )
    _wait_for_cert(juju, app)

    # The rewritten config must reflect TLS.
    after = juju.ssh(f"{app}/0", "cat /etc/headscale/config.yaml", container="headscale")
    assert "tls_cert_path" in after
    assert "listen_addr: 0.0.0.0:443" in after

    # The regression: the *running* process must actually have picked this
    # up -- i.e. it must now genuinely be serving TLS on 443, not just have
    # a config file on disk that nothing ever read. A TLS handshake
    # succeeding (even with a self-signed cert, using -k) is definitive
    # proof the live process is in TLS mode; curl's own exit code failing
    # means the connection/handshake itself failed, not just a non-2xx
    # response.
    tls_check = _wait_for_tls_listener(juju, app)
    assert "TLS_FAILED" not in tls_check, (
        f"TLS handshake to the running headscale process failed -- it never picked up "
        f"the new config after certs were attached. Output: {tls_check}"
    )
