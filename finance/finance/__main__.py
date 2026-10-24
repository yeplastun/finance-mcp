import argparse
import fcntl
import getpass
import json
import os
import re
import secrets
import sys
import tempfile
import uuid
import webbrowser
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import psycopg
from dotenv import load_dotenv

from . import ledger, sheet
from .truelayer import BankingError, NotSupported, TrueLayerClient

ROOT = Path(__file__).resolve().parents[1]
# Private runtime state (gitignored, 0700): env, connections.json, ledger.toml, status.json, data/.
STATE = Path(os.environ.get("FINANCE_STATE") or ROOT / "state")
CONNECTIONS = STATE / "connections.json"
STATUS = STATE / "status.json"
SHEET = STATE / "sheet.json"  # the Google Sheet's ID
STALE = timedelta(hours=13)  # three runs a day; the longest normal gap is 23:00 -> 08:00
RENEW = timedelta(days=14)
REDIRECT_URL = "https://127.0.0.1:8765/callback"


def write_private(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as file:
        temporary = Path(file.name)
        try:
            json.dump(value, file, indent=2)
            file.write("\n")
            file.flush()
            os.fsync(file.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)


def connections() -> dict:
    return json.loads(CONNECTIONS.read_text()) if CONNECTIONS.exists() else {}


def record(name: str, **fields) -> None:
    """Merge fields into status.json under a bank's provider ID or "ledger"; `health` reads it."""
    status = json.loads(STATUS.read_text()) if STATUS.exists() else {}
    status.setdefault(name, {}).update(fields)
    write_private(STATUS, status)


def now() -> str:
    return datetime.now(UTC).isoformat()


def today() -> date:
    return datetime.now(UTC).date()


SAFE_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")


def fetch_booked(client: TrueLayerClient, access_token: str, path: str, params: dict) -> list[dict]:
    if params:
        return client.results(access_token, path, **params)
    # Omitting from/to silently means 88 days, so backfill asks for long ranges and steps down if refused.
    for days in (2555, 730, 365):
        try:
            return client.results(
                access_token,
                path,
                **{"from": (today() - timedelta(days=days)).isoformat(), "to": today().isoformat()},
            )
        except BankingError as error:
            if "invalid_date_range" not in str(error):
                raise
    return client.results(access_token, path)


def collect(client: TrueLayerClient, bank: str, tokens: dict, *, backfill: bool) -> bool:
    access_token = tokens["access_token"]
    run = STATE / "data" / (datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8])
    run.mkdir(parents=True, mode=0o700)
    me = client.results(access_token, "/me")
    write_private(run / "me.json", {"results": me})
    expires = me[0].get("consent_expires_at") if me else None
    print(f"{bank}: consent expires {expires or 'unknown'}")
    record(bank, consent_expires=expires)
    params = {} if backfill else {"from": (today() - timedelta(days=89)).isoformat(), "to": today().isoformat()}
    failed, index = False, 0
    for kind in [k for k in ("accounts", "cards") if k in tokens.get("scope", "").split()]:
        try:
            items = client.results(access_token, f"/{kind}")
        except NotSupported:
            continue
        for item in items:
            index += 1
            folder = run / f"account-{index}"
            try:
                item_id = item.get("account_id")
                if not isinstance(item_id, str) or not SAFE_ID.fullmatch(item_id):
                    raise BankingError("Account has no usable ID.")
                write_private(folder / "account.json", {"kind": kind, **item})
                booked = fetch_booked(client, access_token, f"/{kind}/{item_id}/transactions", params)
                write_private(folder / "transactions.json", {"results": booked})
                try:
                    pending = client.results(access_token, f"/{kind}/{item_id}/transactions/pending")
                    write_private(folder / "pending.json", {"results": pending})
                    pending_count = len(pending)
                except NotSupported:
                    pending_count = "not offered (HTTP 501)"
                dates = [str(tx["timestamp"])[:10] for tx in booked if tx.get("timestamp")]
                report = {
                    "bank": bank,
                    "kind": kind,
                    "fetched_at": datetime.now(UTC).isoformat(),
                    "backfill": backfill,
                    "booked": len(booked),
                    "pending": pending_count,
                    "earliest_booked": min(dates, default=None),
                    "latest_booked": max(dates, default=None),
                }
                write_private(folder / "complete.json", report)
                print(f"  {kind[:-1].capitalize()} {index}: booked={len(booked)}, pending={pending_count}")
                print(f"  Observed booked dates: {report['earliest_booked']} to {report['latest_booked']}")
            except BankingError as error:
                failed = True
                print(f"  {kind[:-1].capitalize()} {index} incomplete: {error}", file=sys.stderr)
    if not index:
        failed = True
        print(f"  {bank}: no accounts or cards returned.", file=sys.stderr)
    print(f"  Raw files: {run.relative_to(STATE)}")
    record(bank, **({"error_at": now(), "last_error": "incomplete run"} if failed else {"last_ok": now()}))
    return not failed


def save_connection(provider_id: str, tokens: dict) -> None:
    linked = connections()
    linked[provider_id] = {
        "refresh_token": tokens["refresh_token"],
        "scope": tokens.get("scope", ""),
        "updated_at": datetime.now(UTC).isoformat(),
    }
    write_private(CONNECTIONS, linked)


def callback_code(pasted: str, state: str) -> str:
    parsed = urlsplit(pasted.strip())
    query = parse_qs(parsed.query)
    if (
        f"{parsed.scheme}://{parsed.netloc}{parsed.path}" != REDIRECT_URL
        or len(query.get("state", [])) != 1
        or not secrets.compare_digest(query["state"][0], state)
    ):
        raise BankingError("Pasted address is not this authorisation's callback; no connection saved.")
    if "error" in query or len(query.get("code", [])) != 1:
        raise BankingError("Authorisation was declined or returned no code; no connection saved.")
    return query["code"][0]


def connect(client: TrueLayerClient, provider: dict) -> bool:
    state = secrets.token_urlsafe(32)
    url = client.auth_link(provider, REDIRECT_URL, state)
    write_private(STATE / "authorisation.json", {"url": url})
    print("Complete bank authorisation in your browser. It ends on a page that fails to load; that is expected.")
    if not webbrowser.open(url):
        print("Browser did not open. Open the URL in state/authorisation.json yourself; keep it private.")
    # Nothing listens on the redirect URL, so the user pastes the address back.
    code = callback_code(getpass.getpass("Paste the full address of that page (hidden): "), state)
    tokens = client.token(grant_type="authorization_code", code=code, redirect_uri=REDIRECT_URL)
    save_connection(provider["provider_id"], tokens)
    (STATE / "authorisation.json").unlink(missing_ok=True)
    print(f"{provider['display_name']}: connected. Fetching all available history now.")
    return collect(client, provider["provider_id"], tokens, backfill=True)


def update_ledger() -> bool:
    """Load new raw runs into PostgreSQL ($LEDGER_DSN) and relabel everything from state/ledger.toml."""
    dsn, config = os.getenv("LEDGER_DSN"), STATE / "ledger.toml"
    if not dsn or not config.exists():
        print(
            "Ledger: set LEDGER_DSN in state/env and create state/ledger.toml (see ledger.example.toml).",
            file=sys.stderr,
        )
        return False
    try:
        with ledger.connect(dsn) as conn:
            runs, rows = ledger.ingest(conn, STATE / "data")
            report = ledger.enrich(conn, ledger.load_config(config))
            ledger.save_config(conn, config.read_text())
    except psycopg.Error as error:
        message = f"database error {type(error).__name__} ({error.sqlstate})"
        print(f"Ledger: {message}; no details printed.", file=sys.stderr)
        record("ledger", error_at=now(), last_error=message)
        return False
    except (ValueError, KeyError, re.error) as error:  # config mistakes; messages name keys and positions only
        print(f"Ledger: {type(error).__name__}: {error}", file=sys.stderr)
        record("ledger", error_at=now(), last_error=f"{type(error).__name__}: {error}")
        return False
    print(f"Ledger: {runs} new run(s), {rows} transaction rows; " + ", ".join(f"{k}={v}" for k, v in report.items()))
    record("ledger", last_ok=now(), report=report)
    return True


def update_sheet() -> bool:
    """Rewrite the Google Sheet copy of the ledger. Off (and fine) until state/env holds a Google token."""
    google = {k: os.getenv(f"GOOGLE_{k}", "") for k in ("CLIENT_ID", "CLIENT_SECRET", "REFRESH_TOKEN")}
    dsn = os.getenv("LEDGER_DSN")
    if not dsn or not all(google.values()):
        return True
    known = json.loads(SHEET.read_text())["id"] if SHEET.exists() else None
    try:
        with ledger.connect(dsn) as conn, httpx.Client(timeout=120) as http:
            sheet_id, entries = sheet.export(conn, http, google, known)
    except (sheet.SheetError, httpx.HTTPError, psycopg.Error) as error:
        message = str(error) if isinstance(error, sheet.SheetError) else f"{type(error).__name__}; no details printed"
        print(f"Sheet: {message}", file=sys.stderr)
        record("sheet", error_at=now(), last_error=message)
        return False
    if sheet_id != known:
        write_private(SHEET, {"id": sheet_id})
        print(f"Sheet: created https://docs.google.com/spreadsheets/d/{sheet_id}")
    print(f"Sheet: {entries} entries")
    record("sheet", last_ok=now(), entries=entries)
    return True


def health(at: datetime | None = None) -> list[str]:
    """Problems only, one line each (safe to send: names, dates and counts)."""
    at = at or datetime.now(UTC)
    status = json.loads(STATUS.read_text()) if STATUS.exists() else {}
    problems = []
    for bank in connections():
        s = status.get(bank, {})
        ok = s.get("last_ok")
        if not ok or at - datetime.fromisoformat(ok) > STALE:
            line = f"{bank}: no successful sync " + (f"since {ok[:16]} UTC" if ok else "yet")
            if s.get("error_at", "") > (ok or ""):
                line += f" (last error: {s['last_error']})"
            problems.append(line)
        expires = s.get("consent_expires")
        if expires and datetime.fromisoformat(expires) - at < RENEW:
            problems.append(
                f"{bank}: bank consent expires {expires[:10]}; renew with `python -m finance connect {bank}`"
            )
    led = status.get("ledger", {})
    if led.get("error_at", "") > led.get("last_ok", ""):
        problems.append(f"ledger update failed: {led['last_error']}")
    report = led.get("report", {})
    if report.get("overrides_not_matching"):
        problems.append(f"ledger: overrides {report['overrides_not_matching']} match the wrong number of transactions")
    if report.get("vanished_booked"):
        problems.append(f"ledger: {report['vanished_booked']} booked transaction(s) vanished from a later window")
    copy = status.get("sheet", {})
    if copy.get("error_at", "") > copy.get("last_ok", ""):
        problems.append(f"Google Sheet copy not updated: {copy['last_error']}")
    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description="Private TrueLayer connectivity and raw transaction collection")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("check", help="List the UK TrueLayer providers")
    connect_parser = sub.add_parser("connect", help="Connect a bank and immediately fetch all available history")
    connect_parser.add_argument("provider_id", help="Provider ID from check, e.g. ob-barclays")
    sync_parser = sub.add_parser("sync", help="Collect the last 90 calendar days for every connected bank")
    sync_parser.add_argument("--backfill", action="store_true", help="Request all available history again")
    sub.add_parser("ledger", help="Load new raw runs into PostgreSQL and relabel them from state/ledger.toml")
    sub.add_parser("sheet", help="Rewrite the Google Sheet copy of the ledger now (the ledger step does it too)")
    sub.add_parser("migrate", help="Apply schema migrations as the owner ($LEDGER_ADMIN_DSN)")
    sub.add_parser("health", help="Print problems only: stale banks, expiring consent, ledger failures")
    args = parser.parse_args()
    load_dotenv(STATE / "env")
    if args.command == "health":
        if problems := health():
            print("Finance problems:\n" + "\n".join("- " + p for p in problems))
        return 0
    if args.command == "migrate":
        with ledger.connect(os.environ["LEDGER_ADMIN_DSN"]) as conn:
            print(f"Applied migrations: {ledger.migrate(conn) or 'none'}")
        return 0
    client_id = os.getenv("TRUELAYER_CLIENT_ID", "")
    client_secret = os.getenv("TRUELAYER_CLIENT_SECRET", "")
    if args.command not in ("ledger", "sheet") and (not client_id or not client_secret):
        print("Configure TRUELAYER_CLIENT_ID and TRUELAYER_CLIENT_SECRET in state/env; see README.md.", file=sys.stderr)
        return 2
    try:
        STATE.mkdir(mode=0o700, exist_ok=True)
        with (STATE / "collector.lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise BankingError("Another collector command is running.") from None
            if args.command == "ledger":
                return 0 if update_ledger() and update_sheet() else 1
            if args.command == "sheet":
                return 0 if update_sheet() else 1
            with httpx.Client(timeout=60) as http:
                client = TrueLayerClient(client_id, client_secret, http)
                if args.command == "check":
                    providers = [p for p in client.providers() if p.get("country") == "uk"]
                    write_private(STATE / "uk-providers.json", {"providers": providers})
                    print(f"UK providers returned: {len(providers)}")
                    for p in providers:
                        print(f"  {p['provider_id']}: {p.get('display_name')} scopes={','.join(p.get('scopes', []))}")
                    print(
                        "Provider metadata is not a live-account proof. History and pending behaviour remain unverified."
                    )
                    return 0
                if args.command == "connect":
                    found = [p for p in client.providers() if p.get("provider_id") == args.provider_id]
                    if len(found) != 1:
                        raise BankingError("Provider ID not found. Run check; no fallback used.")
                    return 0 if connect(client, found[0]) else 1
                linked = connections()
                if not linked:
                    raise BankingError("No banks connected. Run connect first.")
                success = True
                for provider_id, connection in linked.items():
                    try:
                        tokens = client.token(grant_type="refresh_token", refresh_token=connection["refresh_token"])
                        tokens.setdefault("scope", connection.get("scope", ""))
                        save_connection(provider_id, tokens)  # Refresh tokens rotate; persist before anything else.
                        success = collect(client, provider_id, tokens, backfill=args.backfill) and success
                    except BankingError as error:
                        success = False
                        print(f"{provider_id}: {error}", file=sys.stderr)
                        record(provider_id, error_at=now(), last_error=str(error))
                if os.getenv("LEDGER_DSN"):
                    success = update_ledger() and update_sheet() and success
                return 0 if success else 1
    except BankingError as error:
        print(f"Error: {error}", file=sys.stderr)
    except OSError, ValueError, KeyError, TypeError:
        print(
            "Invalid local configuration, saved state or API response; no sensitive details printed.", file=sys.stderr
        )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
