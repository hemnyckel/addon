from __future__ import annotations

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from app.config import Config, Door

# A valid-looking 64-hex device token.
DEVICE_TOKEN = "aabbccddeeff0011" * 4


@pytest.fixture
def apns_key(tmp_path):
    """A throwaway P-256 key in .p8 (PKCS#8 PEM) form, plus its public half."""
    private = ec.generate_private_key(ec.SECP256R1())
    pem = private.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    path = tmp_path / "AuthKey_ABC123DEFG.p8"
    path.write_bytes(pem)
    return str(path), private.public_key()


@pytest.fixture
def cfg(apns_key, tmp_path) -> Config:
    path, _ = apns_key
    return Config(
        apns_key_path=path,
        apns_key_id="ABC123DEFG",
        apns_team_id="TEAM123456",
        bundle_id="se.hemnyckel.app",
        data_dir=str(tmp_path / "data"),
        doors=[Door(id="front", name="Ytterdörren", lock_entity="lock.front")],
    )
