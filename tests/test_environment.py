from scripts.check_environment import inspect_environment


def test_environment_report_does_not_expose_user_paths():
    report = inspect_environment()
    assert isinstance(report["contracts_environment_ready"], bool)
    assert report["broker_tls_tested"] is False
    assert "executable" not in report["mosquitto"]
    assert all("version" in data or "error_type" in data for data in report["packages"].values())
