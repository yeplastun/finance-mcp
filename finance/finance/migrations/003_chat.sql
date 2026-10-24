-- The chat layer: labels, merchant rules and renames set from claude.ai through the finance MCP
-- (role finmcp, mcp/deploy/finance.sql). enrich never touches these tables, so a sync can't
-- wipe them; the entry view lays them over the config's labels. Append-only: finmcp may only insert,
-- the newest row per target wins, and the rows written by one call share `xact`, which is what an undo
-- reverts.

-- A short number per row, shown as the entry's ref (#1234). Edits key on (account_id, id), never on it.
alter table ledger.transaction add column num bigint generated always as identity unique;

create sequence ledger.chat_n;  -- one numbering across the chat tables, so a change has one number

create function ledger.clean(value text, longest int) returns boolean language sql immutable
return length(value) between 1 and longest and value !~ '[[:cntrl:]]';

-- Each row holds the entry's whole chat label at that moment; every field null hands it back to the config.
create table ledger.entry_edit (
    n bigint primary key default nextval('ledger.chat_n'),
    at timestamptz not null default now(),
    xact xid8 not null default pg_current_xact_id(),
    account_id text not null,
    id text not null,
    status text not null default 'booked' check (status = 'booked'),
    flow text check (flow in ('spend', 'housing', 'income', 'transfer', 'loan', 'people', 'investment', 'tax',
                              'reimbursement')),
    category text check (ledger.clean(category, 60)),
    person text check (ledger.clean(person, 60)),
    trip text check (trip = '' or ledger.clean(trip, 100)),  -- '' = not part of any trip
    tags text[] check (cardinality(tags) <= 5 and array_position(tags, null) is null
                       and array_to_string(tags, ' ') ~ '^([a-z0-9-]{1,30}( |$))*$'),
    note text check (note = '' or ledger.clean(note, 200)),  -- '' = no note
    foreign key (account_id, id, status) references ledger.transaction
);
create index on ledger.entry_edit (account_id, id, n);

-- Every entry whose merchant starts with `prefix` (on a word boundary); all fields null = rule removed.
create table ledger.merchant_rule (
    n bigint primary key default nextval('ledger.chat_n'),
    at timestamptz not null default now(),
    xact xid8 not null default pg_current_xact_id(),
    prefix text not null check (prefix = upper(prefix) and length(prefix) >= 3 and ledger.clean(prefix, 100)),
    flow text check (flow in ('spend', 'housing', 'income', 'transfer', 'loan', 'people', 'investment', 'tax',
                              'reimbursement')),
    category text check (ledger.clean(category, 60)),
    person text check (ledger.clean(person, 60))
);

-- Renames: every merchant starting with `prefix` (word boundary) shows as `name`; null = rename removed.
create table ledger.merchant_alias (
    n bigint primary key default nextval('ledger.chat_n'),
    at timestamptz not null default now(),
    xact xid8 not null default pg_current_xact_id(),
    prefix text not null check (prefix = upper(prefix) and length(prefix) >= 3 and ledger.clean(prefix, 100)),
    name text check (name = upper(name) and length(name) >= 2 and ledger.clean(name, 100))
);

-- PRET matches PRET A MANGER, not PRETTYLITTLETHING; a prefix ending in punctuation (SQ *) needs no boundary.
create function ledger.prefix_of(prefix text, merchant text) returns boolean language sql immutable
return starts_with(merchant, prefix)
    and (length(merchant) = length(prefix) or right(prefix, 1) !~ '[[:alnum:]]'
         or substr(merchant, length(prefix) + 1, 1) !~ '[[:alnum:]]');

create view ledger.chat_label as
select * from (select distinct on (account_id, id) * from ledger.entry_edit order by account_id, id, n desc) e
where num_nonnulls(flow, category, person, trip, tags, note) > 0;

create view ledger.chat_rule as
select * from (select distinct on (prefix) * from ledger.merchant_rule order by prefix, n desc) r
where num_nonnulls(flow, category, person) > 0;

create view ledger.chat_alias as
select * from (select distinct on (prefix) * from ledger.merchant_alias order by prefix, n desc) a
where name is not null;

-- Who wins: the entry's chat edit, then a toml [[override]], then the longest matching chat rule, then the
-- config's rules. A rule never turns a computed reversal into something else, and only sets the person of an
-- overridden entry. Renames apply first, and rules match the renamed merchant. New columns go last so the
-- view's readers and grants survive.
create or replace view ledger.entry as
select t.account_id, t.id, t.status, a.name as account, a.provider, t.day, t.at, t.pence, t.currency,
       t.description, coalesce(al.name, l.merchant) as merchant, f.flow,
       coalesce(e.category, case when not o.overridden then r.category end, l.category) as category,
       f.trip, case when f.trip is not distinct from l.trip then l.trip_kind end as trip_kind,
       coalesce(e.tags, l.tags) as tags,
       case when e.note is null then l.note else nullif(e.note, '') end as note,
       case when e.n is not null then 'chat' when r.n is not null and not o.overridden then 'chat rule'
            else l.rule end as rule,
       t.fx_currency, t.fx_amount, t.fee_pence, l.pair_account_id, l.pair_id,
       t.num, coalesce(e.person, r.person) as person, l.merchant as config_merchant
from ledger.transaction t
join ledger.account a on a.id = t.account_id
left join ledger.label l on (l.account_id, l.id, l.status) = (t.account_id, t.id, t.status)
left join ledger.chat_label e on (e.account_id, e.id) = (t.account_id, t.id) and t.status = 'booked'
left join lateral (
    select x.name from ledger.chat_alias x where ledger.prefix_of(x.prefix, l.merchant)
    order by length(x.prefix) desc limit 1
) al on true
left join lateral (
    select x.n, x.flow, x.category, x.person from ledger.chat_rule x
    where ledger.prefix_of(x.prefix, coalesce(al.name, l.merchant))
    order by length(x.prefix) desc limit 1
) r on true
cross join lateral (select coalesce(l.rule ~ '^override', false) as overridden) o
cross join lateral (
    select coalesce(e.flow, case when not o.overridden and l.flow <> 'reversed' then r.flow end, l.flow) as flow
) f0
cross join lateral (
    select f0.flow,
           case when e.trip is not null then nullif(e.trip, '')
                -- relabelled out of spending: no longer part of the trip enrich gave it by date
                when l.flow = 'spend' and f0.flow <> 'spend' and not o.overridden then null
                else l.trip end as trip
) f
where t.status = 'booked' or t.last_run = a.last_run;
