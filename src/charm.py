#!/usr/bin/env python3
# Copyright 2025 Marcus Boden
# See LICENSE file for licensing details.

"""Charm the application."""

import logging
import time

import ops

# A standalone module for workload-specific logic (no charming concerns):

from typing import Optional
import socket

import pydantic

from charms.traefik_k8s.v0.traefik_route import TraefikRouteRequirer,  TraefikRouteProviderReadyEvent #, TraefikRouteProviderDataRemovedEvent
from charms.grafana_agent.v0.cos_agent import COSAgentProvider

from headscale import HeadscaleConfig, Headscale#, HeadscaleCmdResult

logger = logging.getLogger(__name__)

class HeadscaleCharm(ops.CharmBase):
    """Charm the application."""

    def __init__(self, framework: ops.Framework):
        super().__init__(framework)
        self.container = self.unit.get_container("headscale")
        self.headscale = Headscale(self.container, self.load_config(HeadscaleConfig))
        self.pebble_service_name = 'headscale-server'
        self.ingress = TraefikRouteRequirer(self, self.model.get_relation("traefik-route"), "traefik-route", raw=False)
        self.headscale.set_name(self._external_name())
#        self._grafana_agent = COSAgentProvider(
#            self,
#            relation_name="cos-agent",
#            metrics_endpoints=[
#                {"path": "/metrics", "port": 9090},
#                {"path": "/debug", "port": 9090},
#            ]
#        )

        framework.observe(self.on["headscale"].pebble_ready, self._on_pebble_ready)
        framework.observe(self.on.config_changed, self._on_config_changed)
        framework.observe(self.on.install, self._on_install)
        framework.observe(self.ingress.on.ready, self._on_ingress_ready)
        framework.observe(self.on["create-authkey"].action, self._on_create_authkey)
        framework.observe(self.on["expire-authkey"].action, self._on_expire_authkey)
        framework.observe(self.on["list-authkeys"].action, self._on_list_authkeys)

    def _on_config_changed(self, _: ops.ConfigChangedEvent) -> None:
        self.headscale.render_config()
        self._update_layer_and_restart()

    def _on_install(self, _: ops.InstallEvent) -> None:
        self.headscale.setup()

    def _on_ingress_ready(self, event: TraefikRouteProviderReadyEvent):
        logger.debug(f"Running event: {event}")
        self._setup_ingress()

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
        event.log(f"Expiring authkey: {params.authkey}")
        ret = self.headscale.expire_authkey(authkey=params.authkey)
        if ret.exit_code:
            event.fail(f"Failed to expire auth key,\nStderr: {ret.stderr}\nStdout:{ret.stdout}")
            return
        event.set_results({"result": ret.stdout})

    def _on_list_authkeys(self, event: ops.ActionEvent):
        ret = self.headscale.list_authkeys()
        if ret.exit_code:
            event.fail(f"Failed to expire auth key,\nStderr: {ret.stderr}\nStdout:{ret.stdout}")
            return
        event.set_results({"result": ret.stdout})

    def _external_name(self) -> str:
        if self.ingress.is_ready and self.ingress.external_host:
            return self.headscale.config.name+"."+self.ingress.external_host
        return self.headscale.config.name

    def _ingress_config(self) -> dict:
        router_name = f"juju-{self.model.name}-{self.model.app.name}-router"
        service_name = f"juju-{self.model.name}-{self.model.app.name}-service"
        middlewares = { "juju-sidecar-headscale-headers":{
                "headers": {"customRequestHeaders": {"Connection": "Upgrade"}},
            }
        }
        routers = { 
            router_name: {
                "entryPoints": ["web"],
                "middlewares": list(middlewares.keys()),
                "service": service_name,
                "rule": f"Host(`{self._external_name()}`)",
            },
        }
        services = { service_name: {
                "loadBalancer": {
                    "servers": [{"url": f"http://{socket.getfqdn()}:80"}],
                }
            }
        }

        return {"http": {"routers": routers, "services": services, "middlewares": middlewares}}


    def _setup_ingress(self) -> None:
        if not self.unit.is_leader():
            return
        if self.ingress.is_ready():
            self.ingress.submit_to_traefik(config=self._ingress_config())
            self.headscale.render_config()

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
                self.headscale.pebble_service_name: {
                    "override": "replace",
                    "summary": "Start the headscale server",
                    "command": "/usr/bin/headscale serve",
                    "startup": "enabled",
                }
            }
        }
        return ops.pebble.Layer(pebble_layer)

    def _on_pebble_ready(self, _: ops.PebbleReadyEvent):
        """Handle pebble-ready event."""
        self.unit.status = ops.MaintenanceStatus("starting workload")
        self.headscale.render_config()
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


class CreateAuthkeyAction(pydantic.BaseModel):
    """Creates a PreAuthKey"""

    tags: str
    expiry: Optional[str] = "1h"
    ephemeral: Optional[bool] = False
    reusable: Optional[bool] = False

class ExpireAuthkeyAction(pydantic.BaseModel):
    """Expires a PreAuthKey"""
    authkey: str

if __name__ == "__main__":  # pragma: nocover
    ops.main(HeadscaleCharm)