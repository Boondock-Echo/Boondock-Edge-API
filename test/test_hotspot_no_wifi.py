"""Regression coverage for edge devices that have no Wi-Fi hardware."""

from app.services import hotspot_service, hotspot_setup


def _reset_interface_cache(monkeypatch):
    monkeypatch.setattr(hotspot_service, "_wifi_interface_cache", None)
    monkeypatch.setattr(hotspot_service, "_wifi_interface_detected", False)
    monkeypatch.delenv("BOONDOCK_WIFI_INTERFACE", raising=False)


def test_hotspot_status_does_not_query_missing_interface(monkeypatch):
    _reset_interface_cache(monkeypatch)
    monkeypatch.setattr(hotspot_service, "_detect_wifi_interface_nmcli", lambda: None)
    monkeypatch.setattr(hotspot_service, "_detect_wifi_interface_sysfs", lambda: None)

    calls = []
    monkeypatch.setattr(
        hotspot_service,
        "_run_command",
        lambda args: calls.append(args) or {"ok": False, "stdout": "", "stderr": ""},
    )

    status = hotspot_service.get_hotspot_status()

    assert status["supported"] is False
    assert status["interface"] is None
    assert status["external_wifi"] is False
    assert calls == []


def test_initial_setup_skips_device_without_wifi(monkeypatch):
    class Settings:
        def __init__(self):
            self.values = {}

        def get_all_settings(self):
            return self.values.copy()

        def set_setting(self, key, value):
            self.values[key] = value

    settings = Settings()
    monkeypatch.setattr(hotspot_setup, "get_settings_manager", lambda: settings)
    monkeypatch.setattr(
        hotspot_setup,
        "get_hotspot_status",
        lambda: {"external_wifi": False, "supported": False},
    )
    monkeypatch.setattr(hotspot_setup, "get_wifi_interface", lambda: None)
    monkeypatch.setattr(
        hotspot_setup,
        "start_hotspot",
        lambda *_args: (_ for _ in ()).throw(AssertionError("must not start hotspot")),
    )

    result = hotspot_setup.run_initial_hotspot_setup()

    assert result == {
        "skipped": True,
        "reason": "no_wifi_hardware",
        "interface": None,
    }
    assert settings.values[hotspot_setup.HOTSPOT_SETUP_FLAG] == "no_wifi_hardware"
