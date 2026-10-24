-- state/ledger.toml names people, so it never enters git; this copy rides the encrypted database backup.
-- Restore: psql -At -d finance -c 'select text from ledger.config' > state/ledger.toml
create table ledger.config (
    id boolean primary key default true check (id),  -- one row
    text text not null,
    saved_at timestamptz not null default now()
);
