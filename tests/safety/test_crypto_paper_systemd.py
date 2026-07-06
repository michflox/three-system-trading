from pathlib import Path


def test_crypto_paper_service_recovers_restarts_and_never_requests_live() -> None:
    root = Path(__file__).parents[2]
    service = (root / "ops/systemd/trading-crypto-paper.service").read_text(encoding="utf-8")
    environment = (root / "ops/systemd/crypto-paper.env.example").read_text(encoding="utf-8")
    assert (
        "ExecStartPre=/opt/trading-bot/.venv/bin/python -m "
        "engines.crypto_paper_engine recover"
    ) in service
    assert (
        "ExecStart=/opt/trading-bot/.venv/bin/python -m engines.crypto_paper_engine run"
        in service
    )
    assert "Restart=on-failure" in service
    assert "NoNewPrivileges=true" in service
    assert "TRADING_EXECUTION_MODE=DRY_RUN" in environment
    assert "TRADING_LIVE" not in environment


def test_daily_diff_and_quality_timers_are_persistent_and_ordered() -> None:
    root = Path(__file__).parents[2]
    diff_timer = (root / "ops/systemd/trading-crypto-diff.timer").read_text(encoding="utf-8")
    quality_timer = (root / "ops/systemd/trading-crypto-quality.timer").read_text(
        encoding="utf-8"
    )
    diff_service = (root / "ops/systemd/trading-crypto-diff.service").read_text(
        encoding="utf-8"
    )
    assert "OnCalendar=*-*-* 00:15:00 UTC" in diff_timer
    assert "OnCalendar=*-*-* 00:12:00 UTC" in quality_timer
    assert "Persistent=true" in diff_timer
    assert "Persistent=true" in quality_timer
    assert "engines.crypto_paper_engine diff" in diff_service


def test_readme_records_eight_week_gate_and_honest_operational_status() -> None:
    root = Path(__file__).parents[2]
    readme = (root / "README.md").read_text(encoding="utf-8")
    status = (root / "ops/paper_deployment_status.md").read_text(encoding="utf-8")
    assert "2026-09-01" in readme
    assert "at least eight" in readme
    assert "PENDING UBUNTU DEPLOYMENT" in status
