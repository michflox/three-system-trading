from pathlib import Path


def test_systemd_service_restarts_and_runs_backfill_first() -> None:
    root = Path(__file__).parents[2]
    service = (root / "ops" / "systemd" / "trading-data-recorder.service").read_text(
        encoding="utf-8"
    )
    assert "ExecStartPre=/opt/trading-bot/.venv/bin/python -m data.recorder backfill" in service
    assert "ExecStart=/opt/trading-bot/.venv/bin/python -m data.recorder run" in service
    assert "Restart=on-failure" in service
    assert "NoNewPrivileges=true" in service


def test_funding_status_and_disk_alerting_are_wired() -> None:
    root = Path(__file__).parents[2]
    recorder = (root / "data" / "recorder.py").read_text(encoding="utf-8")
    assert "funding-recorder.json" in recorder
    assert "disk_space_low" in recorder
    assert "MINIMUM_DISK_FREE_PERCENT" in recorder
