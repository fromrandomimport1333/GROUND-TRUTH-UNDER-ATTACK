import os
from pathlib import Path
import ssl

from cryptography import x509
from cryptography.x509.oid import ExtendedKeyUsageOID
import pytest

from adapters.mqtt_transport import tls_context
from core.local_config import connection_settings, contained_file, load_manifest
from core.provenance.keys import load_private_key
from core.secure_files import new_private_directory, read_private, write_new_private
from scripts.setup_local import setup_local

PASSWORD = b"provisioning-test-password-only"


def test_local_bundle_has_separate_tls_and_sensor_identities(tmp_path):
    root = setup_local(tmp_path / "local bundle", PASSWORD)
    manifest = load_manifest(root)
    assert len(manifest["sensors"]) == 3
    assert len({s["key_id"] for s in manifest["sensors"]}) == 3
    for sensor in manifest["sensors"]:
        load_private_key(root / "sensors" / sensor["private_key"], PASSWORD)
        context = tls_context(connection_settings(root, manifest, sensor["sensor_id"]))
        assert context.check_hostname and context.verify_mode == ssl.CERT_REQUIRED
        assert context.minimum_version == ssl.TLSVersion.TLSv1_2
    server = x509.load_pem_x509_certificate((root / "tls/server.pem").read_bytes())
    client = x509.load_pem_x509_certificate((root / "tls/sensor_01.pem").read_bytes())
    assert ExtendedKeyUsageOID.SERVER_AUTH in server.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
    assert ExtendedKeyUsageOID.CLIENT_AUTH in client.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
    assert (root / "tls/ca.pem").read_bytes() != (root / "tls/server.pem").read_bytes()
    assert server.extensions.get_extension_for_class(x509.AuthorityKeyIdentifier).value.key_identifier
    assert client.extensions.get_extension_for_class(x509.SubjectKeyIdentifier).value.digest


def test_tls_config_has_no_plaintext_listener_or_anonymous_access(tmp_path):
    root = setup_local(tmp_path / "local", PASSWORD)
    config = (root / "mosquitto.conf").read_text()
    assert "listener 8883 127.0.0.1" in config
    assert "require_certificate true" in config
    assert "allow_anonymous false" in config
    assert "use_identity_as_username true" in config
    assert "tls_version tlsv1.2" in config
    acl = (root / "acl.conf").read_text()
    assert "user observer\ntopic read sensors/raw/+" in acl
    assert "user sensor_01\ntopic write sensors/raw/sensor_01" in acl
    assert "topic write #" not in acl


def test_existing_provisioning_is_never_overwritten(tmp_path):
    root = setup_local(tmp_path / "local", PASSWORD)
    original = (root / "sensors/sensor_01.ed25519.pem").read_bytes()
    with pytest.raises(FileExistsError):
        setup_local(root, PASSWORD)
    assert (root / "sensors/sensor_01.ed25519.pem").read_bytes() == original


@pytest.mark.parametrize("sensors", [["server"], ["observer"], ["sensor_01", "sensor_01"], ["../evil"], []])
def test_invalid_provisioning_fails_before_creating_bundle(tmp_path, sensors):
    with pytest.raises(ValueError):
        setup_local(tmp_path / "local", PASSWORD, sensor_ids=sensors)
    assert not (tmp_path / "local").exists()


def test_manifest_paths_cannot_escape(tmp_path):
    outside = tmp_path / "secret"
    outside.write_text("x")
    root = new_private_directory(tmp_path / "local")
    with pytest.raises(ValueError):
        contained_file(root, "../secret")
    with pytest.raises(ValueError):
        contained_file(root, str(outside.absolute()))


def test_exclusive_private_file_creation(tmp_path):
    path = tmp_path / "private.pem"
    write_new_private(path, b"test-only")
    with pytest.raises(FileExistsError):
        write_new_private(path, b"replacement")
    assert read_private(path) == b"test-only"
    if os.name != "nt":
        assert path.stat().st_mode & 0o777 == 0o600
        path.chmod(0o644)
        with pytest.raises(PermissionError):
            read_private(path)


@pytest.mark.skipif(os.name == "nt", reason="symlink creation may require a Windows privilege")
def test_symlinked_private_material_is_rejected(tmp_path):
    actual = tmp_path / "actual"
    write_new_private(actual, b"test-only")
    link = tmp_path / "link"
    link.symlink_to(actual)
    with pytest.raises(ValueError, match="symlink"):
        read_private(link)
