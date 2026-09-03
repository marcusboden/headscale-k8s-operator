#!/usr/bin/env python3
# Copyright 2025 Marcus Boden
# See LICENSE file for licensing details.

"""Charm the application."""

import logging
import os
import time
import ops
from typing import Dict, Optional
from packaging.version import Version
import pydantic

from charms.traefik_k8s.v0.traefik_route import TraefikRouteRequirer, TraefikRouteProviderReadyEvent
from charms.prometheus_k8s.v0.prometheus_scrape import MetricsEndpointProvider
from charms.loki_k8s.v1.loki_push_api import LogForwarder

from headscale import HeadscaleConfig, Headscale#, HeadscaleCmdResult
from certificates import CertHandler
from upgrade import Upgrader

logger = logging.getLogger(__name__)

class HeadscaleCharm(ops.CharmBase):
    """Charm the application."""

    def __init__(self, framework: ops.Framework):
        super().__init__(framework)
        self.container = self.unit.get_container("headscale")
        self.headscale = Headscale(self.container, self.load_config(HeadscaleConfig))
        self.pebble_service_name = 'headscale-server'
        self.ingress = TraefikRouteRequirer(self, self.model.get_relation("traefik-route"), "traefik-route", raw=True)
        self.headscale.set_name(self._external_name())

        self.metrics_endpoint = MetricsEndpointProvider(self,jobs=[
            {
                "static_configs": [ { "targets": ["*:9090", "*:9091"] } ],
                "job_name": "headscale_scraper",
                "metrics_path": "/metrics",
            }
        ])

        self._log_forwarder = LogForwarder(
            self,
            relation_name="logging"  # optional, defaults to `logging`
        )

        self.certs = CertHandler(self, self._external_name(), [self.on.config_changed, self.ingress.on.ready])
        framework.observe(self.certs.certificates.on.certificate_available, self._on_certs_available)
        framework.observe(self.on["certificates"].relation_departed, self._on_certs_removed)
        framework.observe(self.on["certificates"].relation_changed, self._on_certs_available)

        self.upgrade = Upgrader(self)
        framework.observe(self.on.upgrade_charm, self.upgrade._on_upgrade_charm)
        framework.observe(self.on["proceed-upgrade"].action, self.upgrade._on_proceed_upgrade)

        framework.observe(self.on["headscale"].pebble_ready, self._on_pebble_ready)
        framework.observe(self.on.config_changed, self._on_config_changed)
        framework.observe(self.on.secret_changed, self._on_secret_changed)
        framework.observe(self.on.install, self._on_install)
        framework.observe(self.ingress.on.ready, self._on_ingress_ready)
        framework.observe(self.on["create-authkey"].action, self._on_create_authkey)
        framework.observe(self.on["expire-authkey"].action, self._on_expire_authkey)
        framework.observe(self.on["list-authkeys"].action, self._on_list_authkeys)
        framework.observe(self.on["create-backup"].action, self._on_create_backup)
        framework.observe(self.on["restore-backup"].action, self._on_restore_backup)


    def _on_config_changed(self, _: ops.ConfigChangedEvent) -> None:
        self._configure_and_restart()

    def _on_certs_available(self, _: ops.EventBase) -> None:
        self._configure_and_restart()

    def _on_secret_changed(self, event: ops.SecretChangedEvent) -> None:
        # set secret revision to latest
        event.secret.get_content(refresh=True)
        self._configure_and_restart()

    def _configure_and_restart(self, reassert_certs: bool = True):
        """Render config and restart the workload, unless the unit is blocked.

        If the unit is already in BlockedStatus (e.g. a version mismatch or a
        blocked upgrade), skip touching the workload entirely. Without this
        guard, an unrelated event (config-changed, secret-changed, etc.) could
        silently restart Pebble with whatever image is currently attached even
        though the charm has deliberately decided not to activate it -- which
        can have irreversible side effects (e.g. the headscale binary
        auto-migrating the sqlite schema forward on start), poisoning any
        later attempt to correctly resolve the block.

        reassert_certs: if False, skip re-probing the certificates relation
        for a certificate. Juju only fully clears relation data at
        relation-broken, not relation-departed -- so immediately after
        certificates-relation-departed, self.certs.configure_certs() can
        still see the departing relation's cached certificate as "available"
        and silently flip self.headscale.tls back to True, causing this
        method to render a config that references TLS cert files that were
        just deleted by certs.remove_certs(), crash-looping headscale on
        restart. _on_certs_removed() passes False to avoid this.
        """
        if isinstance(self.unit.status, ops.BlockedStatus):
            logger.warning(
                "Unit is blocked (%s); skipping config/restart.", self.unit.status.message
            )
            return
        self.headscale.set_name(self._external_name())
        if reassert_certs and self.certs.configure_certs():
            self.headscale.tls = True
        self._setup_ingress()
        try:
            self.headscale.render_config(restart=False)
        except RuntimeError as e:
            logger.error(f"Failed to push config: {e}")
            self.unit.status = ops.BlockedStatus("Failed to write config. Check logs.")
            return
        if not self._update_layer_and_restart():
            return
        try:
            self.headscale.reconcile_users(self.headscale.config.get_users())
        except Exception as e:
            logger.warning(f"Couldn't reconcile users: {e}")

    def _on_certs_removed(self, _: ops.EventBase):
        logger.info("Running on_certs_removed")
        self.headscale.tls = False
        self.certs.remove_certs()
        self._configure_and_restart(reassert_certs=False)

    def _on_install(self, _: ops.InstallEvent) -> None:
        try:
            self.headscale.setup()
        except Exception as e:
            logger.warning(f"Couldn't setup headscale: {e}")

    def _on_ingress_ready(self, event: TraefikRouteProviderReadyEvent):
        logger.debug(f"Running event: {event}")
        self._configure_and_restart()

    def _on_create_authkey(self, event: ops.ActionEvent):
        params = event.load_params(CreateAuthkeyAction, errors="fail")
        event.log(f"Generating authkey with {params}")
        ret = self.headscale.create_authkey(
            tags=params.tags, expiry=params.expiry, reusable=params.reusable, ephemeral=params.ephemeral
        )
        if ret.exit_code:
            event.fail(f"Failed to create auth key,\nStderr: {ret.stderr}\nStdout:{ret.stdout}")
            return
        event.set_results({"result": ret.stdout})

    def _on_expire_authkey(self, event: ops.ActionEvent):
        params = event.load_params(ExpireAuthkeyAction, errors="fail")
        event.log(f"Expiring authkey with ID: {params.authkey_id}")
        ret = self.headscale.expire_authkey(authkey_id=params.authkey_id)
        if ret.exit_code:
            event.fail(f"Failed to expire auth key,\nStderr: {ret.stderr}\nStdout:{ret.stdout}")
            return
        event.set_results({"result": ret.stdout})

    def _on_list_authkeys(self, event: ops.ActionEvent):
        ret = self.headscale.list_authkeys()
        if ret.exit_code:
            event.fail(f"Failed to list auth keys,\nStderr: {ret.stderr}\nStdout:{ret.stdout}")
            return
        event.set_results({"result": ret.stdout})

    def _on_create_backup(self, event: ops.ActionEvent):
        try:
            path = self.headscale.create_backup()
        except Exception as e:
            event.fail(f"Failed to create backup:\n{e}")
            return
        event.set_results({"result": f"Download backup with `juju scp {self.unit.name}:{path} {path.name}`",
                           "path": str(path),
                           "filename": path.name
                           })


    def _on_restore_backup(self, event: ops.ActionEvent):
        params = event.load_params(RestoreBackupAction, errors="fail")
        event.log(f"Restoring backup from path: {params.backup_path}")
        try:
            path = self.headscale.restore_backup(backup_path=params.backup_path)
        except Exception as e:
            event.fail(f"Failed to restore backup:\n{e}")
            return

        event.set_results({"result": f"backup restored from {params.backup_path}",
                           "backup": f"Download backup with `juju scp {self.unit.name}:{path} {path.name}`",
                           "path": str(path),
                           "filename": path.name,
                           })

    def _external_name(self) -> str:
        if self.ingress.is_ready() and self.ingress.external_host:
            return self.headscale.config.name+"."+self.ingress.external_host
        return self.headscale.config.name

    def _ingress_config(self) -> dict:
        router_name = f"juju-{self.model.name}-{self.model.app.name}-router"
        service_name = f"juju-{self.model.name}-{self.model.app.name}-service"
        
        # Determine port and entrypoint
        if self.headscale.config.port:
            # Custom port - create custom entrypoint name
            port = self.headscale.config.port
            entrypoint_name = f"custom{port}"
            entrypoints = [entrypoint_name]
        else:
            # Default ports
            if self.headscale.config.tls:
                port = 443
                entrypoints = ["websecure"]
            else:
                port = 80
                entrypoints = ["web"]
        
        routers = {
            router_name: {
                "entryPoints": entrypoints,
                "service": service_name,
                "rule": f"HostSNI(`{self._external_name()}`)",
            },
        }
        
        if self.headscale.config.tls:
            routers[router_name] |= {
                "tls": { "passthrough": True },
            }

        rel = self.model.get_relation("traefik-route")
        ip = self.model.get_binding(rel).network.bind_address
        services = { service_name: {
                "loadBalancer": {
                    "servers": [{"address": f"{ip}:{port}"}],
                }
            }
        }

        return {"tcp": {"routers": routers, "services": services}}


    def _setup_ingress(self) -> None:
        if not self.unit.is_leader():
            return
        if self.ingress.is_ready():
            
            # Separate dynamic and static configs
            dynamic_config = self._ingress_config()
            static_config = None
            
            # If we have custom port, add static entrypoint config
            if self.headscale.config.port:
                port = self.headscale.config.port
                entrypoint_name = f"custom{port}"
                static_config = {
                    "entryPoints": {
                        entrypoint_name: {
                            "address": f":{port}"
                        }
                    }
                }
            
            self.ingress.submit_to_traefik(config=dynamic_config, static=static_config)

    def _update_layer_and_restart(self, set_active: bool = True) -> bool:
        self.unit.status = ops.MaintenanceStatus('Assembling Pebble layers')
        try:
            self.container.add_layer('base', self._get_pebble_layer(), combine=True)
            logger.info("Added updated layer base to Pebble plan")

            self.container.replan()
            logger.info(f"Replanned with '{self.pebble_service_name}' service")

            if set_active:
                self.unit.status = ops.ActiveStatus()
            return True
        except (ops.pebble.APIError, ops.pebble.ConnectionError) as e:
            logger.warning('Unable to connect to Pebble: %s', e)
            self.unit.status = ops.MaintenanceStatus('Waiting for Pebble in workload container')
            return False

    def _get_pebble_layer(self) -> ops.pebble.Layer:
        """Return the Pebble layer for all workload services."""
        pebble_layer: ops.pebble.LayerDict = {
            'summary': 'Headscale service',
            'description': 'Layer to start headscale',
            "services": {
                self.headscale.pebble_service_name: {
                    "override": "replace",
                    "summary": "Start the headscale server",
                    "command": "/usr/bin/headscale serve",
                    "startup": "enabled",
                },
                "exporter": {
                    "override": "replace",
                    "summary": "Start the exporter",
                    "command": "headscale_exporter",
                    "startup": "enabled",
                    "requires": [self.headscale.pebble_service_name]
                }
            }
        }
        proxy = self._get_proxy_settings()
        if proxy:
            pebble_layer["services"][self.headscale.pebble_service_name]["environment"] = proxy
        return ops.pebble.Layer(pebble_layer)

    @staticmethod
    def _get_proxy_settings() -> Dict[str,str]:
        settings = {
            "http_proxy": os.environ.get("JUJU_CHARM_HTTP_PROXY"),
            "https_proxy": os.environ.get("JUJU_CHARM_HTTPS_PROXY"),
            "no_proxy": os.environ.get("JUJU_CHARM_NO_PROXY"),
            "HTTP_PROXY": os.environ.get("JUJU_CHARM_HTTP_PROXY"),
            "HTTPS_PROXY": os.environ.get("JUJU_CHARM_HTTPS_PROXY"),
            "NO_PROXY": os.environ.get("JUJU_CHARM_NO_PROXY"),
        }
        return {k:v for k,v in settings.items() if v}

    def _start_and_activate(self) -> bool:
        """Start headscale via pebble, wait for readiness, run setup, set active.

        Returns True on success. Sets BlockedStatus and returns False on any failure.
        """
        if not self._update_layer_and_restart(set_active=False):
            return False
        try:
            self.wait_for_ready()
        except RuntimeError as e:
            logger.error(f"Workload did not become ready: {e}")
            self.unit.status = ops.BlockedStatus("Workload not ready after start. Check logs.")
            return False
        try:
            self.headscale.setup()
        except Exception as e:
            logger.error(f"Workload setup failed: {e}")
            self.unit.status = ops.BlockedStatus("Workload setup failed. Check logs.")
            return False
        self.unit.status = ops.ActiveStatus()
        return True

    def _on_pebble_ready(self, _: ops.PebbleReadyEvent) -> None:
        self.unit.status = ops.MaintenanceStatus("starting workload")
        if self.certs.configure_certs():
            self.headscale.tls = True
        try:
            self.headscale.render_config(restart=False)
        except RuntimeError as e:
            logger.error(f"Failed to write headscale config at pebble-ready: {e}")
            self.unit.status = ops.MaintenanceStatus("Failed to write config; waiting for pebble.")
            return

        running_v_str = self.headscale.get_version()
        if running_v_str is None:
            logger.warning("Could not determine headscale version; starting without version checks.")
            self._start_and_activate()
            return

        running_v = Version(running_v_str)
        self.unit.set_workload_version(str(running_v))

        self.upgrade.handle_pebble_ready(running_v)

    def is_ready(self) -> bool:
        """Check whether the workload is ready to use."""
        try:
            services = self.container.get_services()
        except (ops.pebble.APIError, ops.pebble.ConnectionError):
            return False
        if not services:
            return False
        for name, service_info in services.items():
            if not service_info.is_running():
                logger.info("the workload is not ready (service '%s' is not running)", name)
                return False
        try:
            checks = self.container.get_checks(level=ops.pebble.CheckLevel.READY)
        except (ops.pebble.APIError, ops.pebble.ConnectionError):
            return False
        for check_info in checks.values():
            if check_info.status != ops.pebble.CheckStatus.UP:
                return False
        return True

    def wait_for_ready(self) -> None:
        """Wait for the workload to be ready to use."""
        for _ in range(10):
            if self.is_ready():
                return
            time.sleep(1)
        logger.error("the workload was not ready within the expected time")
        raise RuntimeError("workload is not ready")

class CreateAuthkeyAction(pydantic.BaseModel):
    """Creates a PreAuthKey"""

    tags: str
    expiry: Optional[str] = "1h"
    ephemeral: Optional[bool] = False
    reusable: Optional[bool] = False

class ExpireAuthkeyAction(pydantic.BaseModel):
    """Expires a PreAuthKey"""
    authkey_id: int

class RestoreBackupAction(pydantic.BaseModel):
    """Restores a previously created backup."""
    backup_path: str

if __name__ == "__main__":  # pragma: nocover
    ops.main(HeadscaleCharm)