# Copyright 2025 Marcus Boden
# See LICENSE file for licensing details.
"""Unit tests for HeadscaleCharm event handlers not covered by test_upgrade.py."""

from __future__ import annotations

import dataclasses
from unittest import mock

import ops
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


class TestSecretAccessRevokedDuringTeardown:
    """Regression test: secret-access failures during config render must not crash the hook.

    During unit removal, Juju revokes the unit's secret-access grants (e.g.
    for the oidc-secret config option) before every hook in the relation-
    departed/broken cascade has finished running. Since render_config()
    always rebuilds the whole config wholesale -- including re-fetching the
    OIDC secret just to re-emit its unchanged block -- any teardown-time
    event that re-renders config after that revocation (e.g.
    certificates-relation-departed calling this via _on_certs_removed to
    disable TLS) would previously raise an uncaught ops.model.ModelError
    and leave the hook "awaiting error resolution", blocking removal.
    """

    def test_certs_removed_survives_secret_permission_denied(self, ctx):
        relation = testing.Relation(endpoint="certificates", interface="tls-certificates")
        state = base_state()
        state = dataclasses.replace(state, relations=frozenset(state.relations | {relation}))

        with (
            mock.patch.object(CertHandler, "configure_certs", return_value=False),
            mock.patch.object(CertHandler, "remove_certs"),
            # Mock _generate_config (not render_config itself) so the real
            # render_config() body -- including its ModelError -> RuntimeError
            # conversion, which is the actual fix under test -- still runs.
            mock.patch.object(
                Headscale,
                "_generate_config",
                side_effect=ops.model.ModelError("ERROR permission denied"),
            ),
        ):
            out = ctx.run(ctx.on.relation_departed(relation), state)

        assert isinstance(out.unit_status, testing.BlockedStatus)
        assert "Failed to write config" in out.unit_status.message


class TestConfigChangeRestart:
    """Regression test: config changes applied while already active must restart headscale.

    The Pebble layer's command for headscale-server is a static string that
    never varies with config content, so _update_layer_and_restart()'s
    replan() can't detect that config.yaml changed and won't restart an
    already-running service on its own -- it only starts services that
    aren't running yet. Without an explicit restart, any config change
    applied after the unit is already active (certs added/removed,
    magic-dns, policy, DERP map, OIDC/node-expiry, port, users, ...) would
    silently never take effect.
    """

    def test_configure_and_restart_passes_restart_true(self, ctx):
        state = base_state()

        with (
            mock.patch.object(CertHandler, "configure_certs", return_value=False),
            mock.patch.object(Headscale, "render_config") as render_config,
        ):
            ctx.run(ctx.on.config_changed(), state)

        render_config.assert_called_once_with(restart=True)

    def test_certs_available_also_restarts(self, ctx):
        """The certificates-available path (this bug's original report) restarts too."""
        state = base_state()

        with (
            mock.patch.object(CertHandler, "configure_certs", return_value=True),
            mock.patch.object(Headscale, "render_config") as render_config,
        ):
            ctx.run(ctx.on.config_changed(), state)

        render_config.assert_called_once_with(restart=True)

