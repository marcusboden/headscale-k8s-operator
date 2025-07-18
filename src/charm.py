#!/usr/bin/env python3
# Copyright 2025 Marcus Boden
# See LICENSE file for licensing details.

"""Charm the application."""

import logging
import time

import ops

# A standalone module for workload-specific logic (no charming concerns):

import yaml
import dataclasses
from typing import Any, Dict, List, cast
import socket

from charms.traefik_k8s.v0.traefik_route import TraefikRouteRequirer,  TraefikRouteProviderReadyEvent, TraefikRouteProviderDataRemovedEvent

from headscale import HeadscaleConfig

logger = logging.getLogger(__name__)

class HeadscaleCharm(ops.CharmBase):
    """Charm the application."""

    def __init__(self, framework: ops.Framework):
        super().__init__(framework)
        self.container = self.unit.get_container("headscale")
        self.pebble_service_name = 'headscale-server'
        self.ingress = TraefikRouteRequirer(self, self.model.get_relation("traefik-route"), "traefik-route", raw=False)
        framework.observe(self.on["headscale"].pebble_ready, self._on_pebble_ready)
        framework.observe(self.on.config_changed, self._on_config_changed)
        self.framework.observe(self.ingress.on.ready, self._on_ingress_ready)

    def _on_config_changed(self, _: ops.ConfigChangedEvent) -> None:
        self._setup_ingress()
        self._render_config()
        self._update_layer_and_restart()

    def _on_ingress_ready(self, event: TraefikRouteProviderReadyEvent):
        logger.debug("Running _on_ingress_ready")
        self._setup_ingress()

    def _render_config(self) -> None:
        try:
            config = self.load_config(HeadscaleConfig)
            hs_conf = config.generate_config(self._external_name(config.name))
            self.container.push("/etc/headscale/config.yaml", yaml.dump(hs_conf), make_dirs=True)
            self.container.restart(self.pebble_service_name)

            self.unit.set_ports(config.port)
        except ValueError as e:
            logger.error('Configuration error: %s', e)
            self.unit.status = ops.BlockedStatus(str(e))
            return

    def _external_name(self, name) -> str:
        if self.ingress.is_ready and self.ingress.external_host:
            return name+"."+self.ingress.external_host
        return name

    def _ingress_config(self) -> dict:
        try:
            config = self.load_config(HeadscaleConfig)
        except ValueError as e:
            logger.error('Configuration error: %s', e)
            self.unit.status = ops.BlockedStatus(str(e))
            return

        router_name = f"juju-{self.model.name}-{self.model.app.name}-router"
        service_name = f"juju-{self.model.name}-{self.model.app.name}-service"
        middle_name = f"juju-headers-{self.model.name}-{self.model.app.name}-websockets" 
        middlewares = { "juju-sidecar-headscale-headers":{
                "headers": {"customRequestHeaders": {"Connection": "Upgrade"}},
            }
        }
        routers = { 
            router_name: {
                "entryPoints": ["web"],
                "middlewares": list(middlewares.keys()),
                "service": service_name,
                "rule": f"Host(`{self.external_name(config.name)}`)",
            },
        }
        services = { service_name: {
                "loadBalancer": {
                    "servers": [{"url": f"http://{socket.getfqdn()}:{config.port}"}],
                }
            }
        }

        return {"http": {"routers": routers, "services": services, "middlewares": middlewares}}


    def _setup_ingress(self):
        if not self.unit.is_leader():
            return
        if self.ingress.is_ready():
            self.ingress.submit_to_traefik(config=self._ingress_config())
            self._render_config()

    def _update_layer_and_restart(self) -> None:
        self.unit.status = ops.MaintenanceStatus('Assembling Pebble layers')
        try:
            self.container.add_layer('base', self._get_pebble_layer(), combine=True)
            logger.info("Added updated layer base to Pebble plan")

            self.container.replan()
            logger.info(f"Replanned with '{self.pebble_service_name}' service")

            self.unit.status = ops.ActiveStatus()
        except (ops.pebble.APIError, ops.pebble.ConnectionError) as e:
            logger.info('Unable to connect to Pebble: %s', e)
            self.unit.status = ops.MaintenanceStatus('Waiting for Pebble in workload container')

    def _get_pebble_layer(self) -> ops.pebble.Layer:
        """A Pebble layer for the FastAPI demo services."""
        pebble_layer: ops.pebble.LayerDict = {
            'summary': 'Headscale service',
            'description': 'Layer to start headscale',
            "services": {
                self.pebble_service_name: {
                    "override": "replace",
                    "summary": "Start the headscale server",
                    "command": "/usr/bin/headscale serve",
                    "startup": "enabled",
                }
            }
        }
        return ops.pebble.Layer(pebble_layer)

    def _on_pebble_ready(self, event: ops.PebbleReadyEvent):
        """Handle pebble-ready event."""
        self.unit.status = ops.MaintenanceStatus("starting workload")
        self._render_config()
        self._update_layer_and_restart()

        self.wait_for_ready()
        #version = headscale.get_version()
        #if version is not None:
        #    self.unit.set_workload_version(version)
        self.unit.status = ops.ActiveStatus()

    def is_ready(self) -> bool:
        """Check whether the workload is ready to use."""
        # We'll first check whether all Pebble services are running.
        for name, service_info in self.container.get_services().items():
            if not service_info.is_running():
                logger.info("the workload is not ready (service '%s' is not running)", name)
                return False
        # The Pebble services are running, but the workload might not be ready to use.
        # So we'll check whether all Pebble 'ready' checks are passing.
        checks = self.container.get_checks(level=ops.pebble.CheckLevel.READY)
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
        # The runtime error is for you (the charm author) to see, not for the user of the charm.
        # Make sure that this function waits long enough for the workload to be ready.


if __name__ == "__main__":  # pragma: nocover
    ops.main(HeadscaleCharm)
