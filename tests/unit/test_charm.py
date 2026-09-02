# Copyright 2025 Marcus Boden
# See LICENSE file for licensing details.
"""Unit tests for HeadscaleCharm event handlers not covered by test_upgrade.py."""

from __future__ import annotations

import dataclasses
from unittest import mock

import ops.testing as testing

from certificates import CertHandler
from headscale import Headscale
from tests.unit.helpers import base_state


class TestCertsRemoved:
    """Regression tests for _on_certs_removed's TLS-reassertion race.

    During certificates-relation-departed, Juju hasn't yet cleared the
    relation's cached data (that only happens at relation-broken), so
    CertHandler.configure_certs() can still report the departing
    certificate as "available". _on_certs_removed must not let that
    silently re-enable TLS after it has just disabled it and deleted the
    cert files -- otherwise headscale restarts with a config pointing at
    TLS cert files that no longer exist on disk, and crash-loops with
    'configuring TLS settings: open /etc/headscale/headscale.pem: no such
    file or directory'.
    """

    def test_does_not_reassert_tls_after_removal(self, ctx):
        relation = testing.Relation(endpoint="certificates", interface="tls-certificates")
        state = base_state()
        state = dataclasses.replace(state, relations=frozenset(state.relations | {relation}))

        with (
            mock.patch.object(
                CertHandler, "configure_certs", return_value=True
            ) as configure_certs,
            mock.patch.object(CertHandler, "remove_certs") as remove_certs,
            mock.patch.object(Headscale, "render_config") as render_config,
        ):
            ctx.run(ctx.on.relation_departed(relation), state)

        remove_certs.assert_called_once()
        render_config.assert_called_once()
        # The regression: configure_certs() must NOT be re-probed here, since
        # doing so (and seeing the still-cached cert as "available") would
        # silently flip tls back to True right after we just disabled it.
        configure_certs.assert_not_called()

    def test_reassert_certs_still_happens_on_other_events(self, ctx):
        """Sanity check: config-changed still re-probes certs as before."""
        state = base_state()

        with (
            mock.patch.object(
                CertHandler, "configure_certs", return_value=True
            ) as configure_certs,
            mock.patch.object(Headscale, "render_config"),
        ):
            ctx.run(ctx.on.config_changed(), state)

        configure_certs.assert_called_once()
