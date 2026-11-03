"""Layer 4 FastAPI dashboard: pages render the demo audit log, releases reach the log, tampering shows."""
import sqlite3
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")
from fastapi.testclient import TestClient  # noqa: E402

from core.response.auditlog import AuditLog  # noqa: E402
from dashboard.app import create_app  # noqa: E402
from scripts.demo_layer4 import build  # noqa: E402


@pytest.fixture(scope="module")
def demo_db(tmp_path_factory):
    return build(tmp_path_factory.mktemp("dash") / "audit.sqlite3")


@pytest.fixture
def copy(demo_db, tmp_path):
    db = tmp_path / "copy.sqlite3"
    db.write_bytes(Path(demo_db).read_bytes())
    return db


def test_pages_render_states_chain_and_scenarios(demo_db):
    client = TestClient(create_app(demo_db))
    home = client.get("/")
    assert home.status_code == 200 and "text/html" in home.headers["content-type"]
    assert "Demo data" in home.text and "verified" in home.text
    assert 'class="badge quarantined">quarantined' in home.text and "wn_grid_y1_x3" in home.text
    detail = client.get("/sensors/wn_grid_y2_x2?quantity=T_zAvgTerr_HHM")
    assert detail.status_code == 200 and detail.text.count("<svg") == 2 and "Layer 3 alert" in detail.text
    assert "bad_signature" in client.get("/provenance").text and "replay" in client.get("/provenance").text
    assert "temperature_c" in client.get("/suspicious").text
    assert "Request release" in client.get("/release").text
    audit = client.get("/audit").text
    assert "previous hash" in audit and "trust_decision" in audit and "provenance_event" in audit
    assert client.get("/sensors/unknown").status_code == 404
    assert client.get("/static/style.css").status_code == 200


def test_json_api(demo_db):
    client = TestClient(create_app(demo_db))
    sensors = client.get("/api/sensors").json()
    assert len(sensors) == 9 and sensors[0]["state"] == "quarantined"          # worst first
    assert client.get("/api/chain").json()["ok"] is True
    released = client.get("/api/released").json()
    assert released and {r["source"] for r in released} <= {"raw", "imputed"}
    later = client.get("/api/released", params={"after": released[-1]["seq"]}).json()
    assert later == []
    assert client.get("/api/sensors/wn_grid_y2_x2/decisions", params={"limit": 5}).json()[0]["sensor_id"] == "wn_grid_y2_x2"


def test_release_request_reaches_the_audit_log(copy):
    client = TestClient(create_app(copy))
    bad = client.post("/release", data={"sensor_id": "wn_grid_y2_x2", "analyst": "a", "reason": "r"})
    assert "not waiting for a release" in bad.text                            # not in provenance quarantine
    missing = client.post("/release", data={"sensor_id": "wn_grid_y1_x3", "analyst": "", "reason": "r"})
    assert "both required" in missing.text
    ok = client.post("/release", data={"sensor_id": "wn_grid_y1_x3", "analyst": "analyst-1",
                                       "reason": "forged messages traced; credentials rotated"}, follow_redirects=False)
    assert ok.status_code == 303
    assert "Release request recorded" in client.get(ok.headers["location"]).text
    with AuditLog(copy) as log:
        (request,) = log.entries("analyst_release")
        assert request["sensor_id"] == "wn_grid_y1_x3" and request["payload"]["analyst"] == "analyst-1"
        assert log.verify()[0]


def test_release_token_is_enforced(copy):
    client = TestClient(create_app(copy, release_token="s3cret"))
    form = {"sensor_id": "wn_grid_y1_x3", "analyst": "analyst-1", "reason": "checked"}
    assert "Wrong release token" in client.post("/release", data={**form, "token": "nope"}).text
    assert client.post("/release", data={**form, "token": "s3cret"}, follow_redirects=False).status_code == 303


def test_output_is_escaped(copy):
    with AuditLog(copy) as log:
        log.append("2024-01-03T00:00:00+00:00", "analyst_release", {"analyst": "<script>x</script>", "reason": "r"},
                   sensor_id="wn_grid_y1_x3")
    text = TestClient(create_app(copy)).get("/release").text
    assert "<script>x</script>" not in text and "&lt;script&gt;" in text


def test_broken_chain_is_reported(copy):
    con = sqlite3.connect(copy)
    con.execute("DROP TRIGGER audit_log_no_update")
    con.execute("UPDATE audit_log SET payload = replace(payload, 'quarantined', 'trusted') WHERE kind = 'trust_decision'")
    con.commit()
    con.close()
    client = TestClient(create_app(copy))
    assert "Audit chain broken" in client.get("/").text
    assert client.get("/api/chain").json()["ok"] is False


def test_dashboard_is_read_only_while_browsing(copy):
    before = copy.read_bytes()
    client = TestClient(create_app(copy))
    for path in ("/", "/sensors/wn_grid_y2_x2", "/provenance", "/suspicious", "/release", "/audit", "/api/chain"):
        assert client.get(path).status_code == 200
    assert copy.read_bytes() == before


def test_missing_database(tmp_path):
    client = TestClient(create_app(tmp_path / "none.sqlite3"))
    assert "Audit log not found" in client.get("/").text
    assert not (tmp_path / "none.sqlite3").exists()
