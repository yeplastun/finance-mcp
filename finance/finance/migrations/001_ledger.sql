-- The ledger: raw TrueLayer runs normalised (ingest), then labelled from the personal config (enrich).
-- run, account and transaction only grow from data/; label and trip are rebuilt by every enrich.
create schema if not exists ledger;  -- migrate() creates it for its own table

create table ledger.run (
    id text primary key,                    -- data/<id>
    fetched_at timestamptz not null,
    ingested_at timestamptz not null default now()
);

create table ledger.account (
    id text primary key,                    -- TrueLayer account_id
    provider text not null,                 -- ob-barclays, ob-amex, ob-monzo
    kind text not null check (kind in ('accounts', 'cards')),
    name text not null,
    currency text not null,
    raw jsonb not null,
    last_run text not null references ledger.run
);

create table ledger.transaction (
    account_id text not null references ledger.account,
    id text not null,                       -- TrueLayer transaction_id
    status text not null check (status in ('booked', 'pending')),
    at timestamptz not null,                -- provider timestamp
    day date not null,                      -- when it happened: Barclays "ON 10 OCT", else the timestamp
    pence bigint not null,                  -- money out is negative, on cards too
    currency text not null,
    description text not null,
    merchant_raw text,                      -- provider merchant or counterparty name
    provider_ref text,                      -- provider's own ID (Monzo tx_ pairs pot moves)
    fx_currency text,                       -- Barclays "AMOUNT IN EUR 29.00 ... FEE £0.74"
    fx_amount numeric,
    fee_pence bigint,
    raw jsonb not null,
    first_run text not null references ledger.run,
    last_run text not null references ledger.run,
    primary key (account_id, id, status)
);
create index on ledger.transaction (day);

create table ledger.trip (
    name text primary key,
    starts date not null,
    ends date not null,
    purpose text
);

create table ledger.label (
    account_id text not null,
    id text not null,
    status text not null,
    merchant text not null,                 -- canonical merchant
    flow text not null,                     -- spend, income, transfer, reversed, housing, loan, ...
    category text not null,
    rule text not null,                     -- what decided it: rule N, merchant, abroad, override N, default
    trip text references ledger.trip,
    trip_kind text,                         -- Flights & long-distance, Accommodation, ...
    tags text[] not null default '{}',      -- reimbursed, booked-ahead, ...
    note text,
    pair_account_id text,                   -- the other leg of a transfer
    pair_id text,
    primary key (account_id, id, status),
    foreign key (account_id, id, status) references ledger.transaction on delete cascade
);

-- One row per real transaction: booked, plus pending only while the latest run still lists it.
create view ledger.entry as
select t.account_id, t.id, t.status, a.name as account, a.provider, t.day, t.at, t.pence, t.currency,
       t.description, l.merchant, l.flow, l.category, l.trip, l.trip_kind, l.tags, l.note, l.rule,
       t.fx_currency, t.fx_amount, t.fee_pence, l.pair_account_id, l.pair_id
from ledger.transaction t
join ledger.account a on a.id = t.account_id
left join ledger.label l on (l.account_id, l.id, l.status) = (t.account_id, t.id, t.status)
where t.status = 'booked' or t.last_run = a.last_run;
