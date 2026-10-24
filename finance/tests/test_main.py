import json
import stat
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from finance.__main__ import callback_code, collect, connections, fetch_booked, health, record, write_private
from finance.truelayer import BankingError, NotSupported


@pytest.fixture(autouse=True)
def state(tmp_path: Path, monkeypatch):
    """Every test gets its own state/ (raw runs, status.json), never the real one."""
    for name, value in (
        ("STATE", tmp_path),
        ("CONNECTIONS", tmp_path / "connections.json"),
        ("STATUS", tmp_path / "status.json"),
    ):
        monkeypatch.setattr(f"finance.__main__.{name}", value)
    return tmp_path


def test_write_private(tmp_path: Path):
    target = tmp_path / "sub" / "test.json"
    data = {"secret": "private_value"}
    write_private(target, data)

    assert target.exists()
    assert json.loads(target.read_text()) == data

    # Check directory permissions mode is 0o700
    dir_stat = target.parent.stat()
    assert stat.S_IMODE(dir_stat.st_mode) == 0o700


def test_connections_empty(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("finance.__main__.CONNECTIONS", tmp_path / "nonexistent.json")
    assert connections() == {}


def test_connections_existing(tmp_path: Path, monkeypatch):
    conn_file = tmp_path / "connections.json"
    conn_file.write_text(json.dumps({"Barclays": "some-session-id"}))
    monkeypatch.setattr("finance.__main__.CONNECTIONS", conn_file)
    assert connections() == {"Barclays": "some-session-id"}


TOKENS = {"access_token": "tok", "scope": "info accounts cards transactions offline_access"}


def fake_client(pending=None):
    client = MagicMock()

    def results(token, path, **params):
        return (
            {
                "/me": [{"consent_expires_at": "2030-01-01T00:00:00Z"}],
                "/accounts": [{"account_id": "acc1"}],
                "/accounts/acc1/transactions": [
                    {"timestamp": "2026-10-01T00:00:00Z"},
                    {"timestamp": "2026-09-01T00:00:00"},
                ],
                "/accounts/acc1/transactions/pending": pending or [],
            }.get(path)
            if path != "/cards"
            else (_ for _ in ()).throw(NotSupported("HTTP 501"))
        )

    client.results.side_effect = results
    return client


def test_collect_success(tmp_path: Path):
    assert collect(fake_client([{"amount": 1}]), "ob-barclays", TOKENS, backfill=False) is True
    (run,) = (tmp_path / "data").iterdir()
    report = json.loads((run / "account-1" / "complete.json").read_text())
    assert report["booked"] == 2 and report["pending"] == 1 and report["kind"] == "accounts"
    assert (report["earliest_booked"], report["latest_booked"]) == ("2026-09-01", "2026-10-01")
    assert (run / "me.json").exists() and (run / "account-1" / "pending.json").exists()
    status = json.loads((tmp_path / "status.json").read_text())["ob-barclays"]
    assert status["consent_expires"] == "2030-01-01T00:00:00Z" and "last_ok" in status and "last_error" not in status


def test_collect_records_pending_not_offered(tmp_path: Path):
    client = fake_client()
    original = client.results.side_effect
    client.results.side_effect = lambda t, path, **p: (
        (_ for _ in ()).throw(NotSupported("HTTP 501")) if path.endswith("/pending") else original(t, path, **p)
    )
    assert collect(client, "ob-barclays", TOKENS, backfill=True) is True
    (run,) = (tmp_path / "data").iterdir()
    assert json.loads((run / "account-1" / "complete.json").read_text())["pending"] == "not offered (HTTP 501)"


def test_collect_rejects_unsafe_account_id(tmp_path: Path):
    client = fake_client()
    original = client.results.side_effect
    client.results.side_effect = lambda t, path, **p: (
        [{"account_id": "../x"}] if path == "/accounts" else original(t, path, **p)
    )
    assert collect(client, "ob-barclays", TOKENS, backfill=False) is False
    assert json.loads((tmp_path / "status.json").read_text())["ob-barclays"]["last_error"] == "incomplete run"


def test_callback_code():
    assert callback_code(" https://127.0.0.1:8765/callback?state=s1&code=c1\n", "s1") == "c1"
    for pasted in [
        "https://127.0.0.1:8765/callback?state=other&code=c1",
        "https://evil.example/callback?state=s1&code=c1",
        "https://127.0.0.1:8765/callback?state=s1&error=access_denied",
        "https://127.0.0.1:8765/callback?state=s1",
    ]:
        with pytest.raises(BankingError):
            callback_code(pasted, "s1")


def test_fetch_booked_backfill_steps_down_on_invalid_range():
    client = MagicMock()
    calls = []

    def results(token, path, **params):
        calls.append(params)
        if len(calls) < 3:
            raise BankingError("HTTP 400: invalid_date_range.")
        return [{"x": 1}]

    client.results.side_effect = results
    assert fetch_booked(client, "tok", "/accounts/a/transactions", {}) == [{"x": 1}]
    assert [bool(c.get("from") and c.get("to")) for c in calls] == [True, True, True]
    assert calls[0]["from"] < calls[1]["from"] < calls[2]["from"]


AT = datetime(2026, 10, 13, 9, 17, tzinfo=UTC)


def test_health_is_silent_when_every_bank_synced_recently(tmp_path: Path):
    (tmp_path / "connections.json").write_text(json.dumps({"ob-amex": {}, "ob-monzo": {}}))
    for bank in ("ob-amex", "ob-monzo"):
        record(bank, last_ok="2026-10-13T07:00:00+00:00", consent_expires="2030-01-01T12:00:00Z")
    record("ledger", last_ok="2026-10-13T07:01:00+00:00", report={"overrides_not_matching": [], "vanished_booked": 0})
    assert health(AT) == []


def test_health_reports_stale_banks_expiring_consent_and_ledger_problems(tmp_path: Path):
    (tmp_path / "connections.json").write_text(json.dumps({"ob-amex": {}, "ob-barclays": {}, "ob-monzo": {}}))
    record(
        "ob-amex",
        last_ok="2026-10-12T15:00:00+00:00",
        error_at="2026-10-13T07:00:00+00:00",
        last_error="HTTP 429: rate_limited.",
    )
    record("ob-barclays", last_ok="2026-10-13T07:00:00+00:00", consent_expires="2026-10-20T00:00:00Z")
    record(
        "ledger",
        last_ok="2026-10-13T07:01:00+00:00",
        error_at="2026-10-13T08:00:00+00:00",
        last_error="database error OperationalError (08006)",
        report={"overrides_not_matching": [3], "vanished_booked": 2},
    )
    assert health(AT) == [
        "ob-amex: no successful sync since 2026-10-12T15:00 UTC (last error: HTTP 429: rate_limited.)",
        "ob-barclays: bank consent expires 2026-10-20; renew with `python -m finance connect ob-barclays`",
        "ob-monzo: no successful sync yet",
        "ledger update failed: database error OperationalError (08006)",
        "ledger: overrides [3] match the wrong number of transactions",
        "ledger: 2 booked transaction(s) vanished from a later window",
    ]
