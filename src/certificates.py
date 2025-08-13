
from charms.tls_certificates_interface.v4.tls_certificates import (
    Certificate,
    CertificateRequestAttributes,
    Mode,
    PrivateKey,
    TLSCertificatesRequiresV4, ProviderCertificate,
)
import logging
import ops
from typing import Optional

CERTS_DIR_PATH = "/etc/headscale"
PRIVATE_KEY_NAME = "headscale.key"
CERTIFICATE_NAME = "headscale.pem"

logger = logging.getLogger(__name__)

class CertHandler:
    def __init__(self, charm: ops.CharmBase, name: str):
        self.charm = charm
        self.certificates = TLSCertificatesRequiresV4(
                charm=charm,
                relationship_name="certificates",
                certificate_requests=[CertificateRequestAttributes(common_name=name)],
                mode=Mode.UNIT,
            )
        self.container = charm.unit.get_container("headscale")
        self.name = name

    def _get_certificate_request_attributes(self) -> CertificateRequestAttributes:
        return CertificateRequestAttributes(common_name=self.name)

    def configure_certs(self) -> bool:
        if not self.container.can_connect():
            logger.info("Certs say: Cannot connect to container")
            return False
        if not self._relation_created("certificates"):
            logger.info("Certs say: No certificate relation")
            return False
        if not self._certificate_is_available():
            logger.info("Certs say: cert isn't available")
            return False

        logger.info("Certs ready")
        certificate_update_required = self._check_and_update_certificate()
        return True

    def remove_certs(self):
        self._remove_certificate()
        self._remove_private_key()

    def _relation_created(self, relation_name: str) -> bool:
        return bool(self.charm.model.relations.get(relation_name))

    def _certificate_is_available(self) -> bool:
        cert, key = self.certificates.get_assigned_certificate(
            certificate_request=self._get_certificate_request_attributes()
        )
        return bool(cert and key)

    def _check_and_update_certificate(self) -> bool:
        """Check if the certificate or private key needs an update and perform the update.

        This method retrieves the currently assigned certificate and private key associated with
        the charm's TLS relation. It checks whether the certificate or private key has changed
        or needs to be updated. If an update is necessary, the new certificate or private key is
        stored.

        Returns:
            bool: True if either the certificate or the private key was updated, False otherwise.
        """
        provider_certificate, private_key = self.certificates.get_assigned_certificate(
            certificate_request=self._get_certificate_request_attributes()
        )
        if not provider_certificate or not private_key:
            logger.debug("Certificate or private key is not available")
            return False
        if certificate_update_required := self._is_certificate_update_required(provider_certificate.chain):
            self._store_certificate(certificate=provider_certificate)
        if private_key_update_required := self._is_private_key_update_required(private_key):
            self._store_private_key(private_key=private_key)
        return certificate_update_required or private_key_update_required

    def _is_certificate_update_required(self, certs: list[Certificate]) -> bool:
        return self._get_existing_certificate() != self._concat_chain(certs)

    def _is_private_key_update_required(self, private_key: PrivateKey) -> bool:
        return self._get_existing_private_key() != private_key

    def _get_existing_certificate(self) -> Optional[str]:
        return self._get_stored_certificate() if self._certificate_is_stored() else None

    def _get_existing_private_key(self) -> Optional[PrivateKey]:
        return self._get_stored_private_key() if self._private_key_is_stored() else None

    def _certificate_is_stored(self) -> bool:
        return self.container.exists(path=f"{CERTS_DIR_PATH}/{CERTIFICATE_NAME}")

    def _private_key_is_stored(self) -> bool:
        return self.container.exists(path=f"{CERTS_DIR_PATH}/{PRIVATE_KEY_NAME}")

    def _get_stored_certificate(self) -> str:
        cert_string = str(self.container.pull(path=f"{CERTS_DIR_PATH}/{CERTIFICATE_NAME}").read())
        return cert_string

    def _get_stored_private_key(self) -> PrivateKey:
        key_string = str(self.container.pull(path=f"{CERTS_DIR_PATH}/{PRIVATE_KEY_NAME}").read())
        return PrivateKey.from_string(key_string)

    def _store_certificate(self, certificate: ProviderCertificate) -> None:
        """Store certificate in workload."""

        self.container.push(path=f"{CERTS_DIR_PATH}/{CERTIFICATE_NAME}", source=self._concat_chain(certificate.chain))
        logger.info("Pushed certificate pushed to workload")

    @staticmethod
    def _concat_chain(certs: list[Certificate]) -> str:
        return "\n".join([str(c) for c in certs])

    def _remove_certificate(self) -> None:
        """Remove certificate in workload."""
        self.container.exec(["rm", f"{CERTS_DIR_PATH}/{CERTIFICATE_NAME}" ])

    def _store_private_key(self, private_key: PrivateKey) -> None:
        """Store private key in workload."""
        self.container.push(
            path=f"{CERTS_DIR_PATH}/{PRIVATE_KEY_NAME}",
            source=str(private_key),
        )
        logger.info("Pushed private key to workload")

    def _remove_private_key(self) -> None:
        """Remove private key in workload."""
        self.container.exec(["rm", f"{CERTS_DIR_PATH}/{PRIVATE_KEY_NAME}"])
