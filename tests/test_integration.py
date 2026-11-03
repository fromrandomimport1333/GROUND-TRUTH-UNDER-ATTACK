"""Contract/dependency integration only. No application pipeline exists yet."""

import json

import cbor2
import pytest
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from paho.mqtt.client import CallbackAPIVersion, Client, MQTTv5

from core.contracts import FeatureWindow, SignedReading

pytestmark = pytest.mark.integration


def test_cbor_contract_roundtrip_and_order_independence(reading):
    payload = reading.model_dump(mode="python")
    original = cbor2.dumps(payload, canonical=True)
    reversed_keys = cbor2.dumps(dict(reversed(list(payload.items()))), canonical=True)
    assert original == reversed_keys
    assert SignedReading.model_validate(cbor2.loads(original)) == reading


def test_crypto_dependency_operates_on_canonical_cbor(reading):
    # Library smoke test, not the future signer implementation or wire protocol.
    key = Ed25519PrivateKey.generate()
    payload = reading.model_dump(exclude={"signature"})
    original = cbor2.dumps(payload, canonical=True)
    signature = key.sign(original)
    key.public_key().verify(signature, original)
    changed = cbor2.dumps({**payload, "value": 99.0}, canonical=True)
    with pytest.raises(InvalidSignature):
        key.public_key().verify(signature, changed)


def test_mqtt_v5_client_can_be_constructed_without_network():
    client = Client(callback_api_version=CallbackAPIVersion.VERSION2, protocol=MQTTv5)
    assert client is not None


def test_l3_consumer_stub_receives_window_and_l4_stub_sees_provenance(window_data):
    window = FeatureWindow(**window_data)

    def l3_contract_stub(value: FeatureWindow):
        return tuple(r.normalized_value for r in value.readings if r.ready_for_analysis)

    def l4_contract_stub(value: FeatureWindow):
        return tuple(r.provenance.status for r in value.readings)

    assert l3_contract_stub(window) == (25.5,)
    assert l4_contract_stub(window) == ("valid",)


@pytest.mark.parametrize("model", [SignedReading, FeatureWindow])
def test_json_schema_export_is_machine_readable(model):
    schema = model.model_json_schema(mode="serialization")
    assert json.loads(json.dumps(schema))["additionalProperties"] is False
