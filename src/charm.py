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

from charms.traefik_k8s.v0.traefik_route import TraefikRouteRequirer

logger = logging.getLogger(__name__)

@dataclasses.dataclass(frozen=True, kw_only=True)
class HeadscaleConfig:
    """Configuration for the Headscale server."""

    port: int = 8080
    external_url: str = "headscale"

    def generate_config(self) -> Dict[str, Any]:
        """Generates config file str.

        Returns:
            A dict of the config to be rendered as yaml.
        """
        config = {
                "server_url": f"http://{external_url}",
                "listen_addr": f"0.0.0.0:{self.port}",
                "metrics_listen_addr": "0.0.0.0:9090",
                "noise": {
                    "private_key_path": "/var/lib/headscale/noise_private.key"
                },
                "prefixes": {
                    "v4": "100.64.0.0/10",
                    "v6": "fd7a:115c:a1e0::/48",
                    "allocation": "sequential",
                },
                "derp": {
                    "server": {
                        "enabled": False
                    },
                    "urls": [
                        "https://controlplane.tailscale.com/derpmap/default"
                    ],
                    "auto_update_enabled": True,
                    "update_frequency": "24h"
                },
                "disable_check_updates": False,
                "ephemeral_node_inactivity_timeout": "30m",
                "database": {
                    "type": "sqlite",
                    "debug": "false",
                    "sqlite": {
                        "path": "/var/lib/headscale/db.sqlite",
                        "write_ahead_log": True,
                        "wal_autocheckpoint": 1000
                    },
                },
                "log": {
                    "format": "text",
                    "level": "debug"
                },
                "policy": {
                    "mode": "database"
                },
                "dns": {
                    "magic_dns": True,
                    "base_domain": "mytest.com",
                    "override_local_dns": False,
                    "nameservers": {
                        "global": [
                            "1.1.1.1", 
                            "1.0.0.1"
                        ]
                    }
                },
                "unix_socket": "/var/run/headscale/headscale.sock",
                "unix_socket_permission": "0770"
        }
        return config


    def __post_init__(self):
        """Validate the configuration."""
        if self.port == 22:
            raise ValueError('Invalid port number, 22 is reserved for SSH')

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
        self._render_config()
        self._setup_ingress()
        self._update_layer_and_restart()

    def _on_ingress_ready(self, event: IngressPerAppReadyEvent):
        self._setup_ingress()

    def _render_config(self) -> None:
        try:
            config = self.load_config(HeadscaleConfig)
            self.container.push("/etc/headscale/config.yaml", yaml.dump(config.generate_config()), make_dirs=True)
            self.container.restart(self.pebble_service_name)

            self.unit.set_ports(config.port)
        except ValueError as e:
            logger.error('Configuration error: %s', e)
            self.unit.status = ops.BlockedStatus(str(e))
            return

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
                "rule": f"Host(`{config.external_url}`)",
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
