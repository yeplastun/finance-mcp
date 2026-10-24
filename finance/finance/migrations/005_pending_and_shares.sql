-- Pending entries that book, and splits.
--
-- A pending card payment books later as a new row with a new ID. enrich links the two in booked_from (same
-- account and amount, booked within days) and keeps each link once made. The entry view then shows one entry:
-- the booked row, under the pending row's ref and `added`, with what chat set on the pending row. So chat labels
-- and shares key on an entry's first row, `edit_id`/`edit_status`: the pending row if it had one, else itself.

create table ledger.booked_from (  -- written by enrich only; both rows stay in ledger.transaction for good
    account_id text not null,
    id text not null,               -- the booked row
    pending_id text not null,       -- the pending row it was
    primary key (account_id, id),
    unique (account_id, pending_id)
);

-- Labels may go on a pending entry. `reversed` (counts nowhere) marks a hold that won't book, or a refund the
-- same-day rule didn't pair; if a hold marked so books after all, the booked entry ignores the mark.
alter table ledger.entry_edit
    drop constraint entry_edit_status_check,
    add constraint entry_edit_status_check check (status in ('booked', 'pending')),
    drop constraint entry_edit_flow_check,
    add constraint entry_edit_flow_check check (flow in ('spend', 'housing', 'income', 'transfer', 'loan', 'people',
                                                         'investment', 'tax', 'reimbursement', 'reversed'));

create or replace view ledger.chat_label as
select * from (select distinct on (account_id, id, status) * from ledger.entry_edit
               order by account_id, id, status, n desc) e
where num_nonnulls(flow, category, person, trip, tags, note) > 0;

-- Splits: what a person owes the owner of a payment they made. Append-only like the other chat tables: the newest row per
-- (entry, person) wins, null pence = that share removed. Spending counts the payment less its live shares.
create table ledger.entry_share (
    n bigint primary key default nextval('ledger.chat_n'),
    at timestamptz not null default now(),
    xact xid8 not null default pg_current_xact_id(),
    account_id text not null,
    id text not null,
    status text not null check (status in ('booked', 'pending')),
    person text not null check (ledger.clean(person, 60)),
    pence bigint check (pence between 1 and 10000000),
    foreign key (account_id, id, status) references ledger.transaction
);
create index on ledger.entry_share (account_id, id, status, person, n);

create view ledger.chat_share as
select * from (select distinct on (account_id, id, status, person) * from ledger.entry_share
               order by account_id, id, status, person, n desc) s
where pence is not null;

-- 004's view, plus: a pending row that booked is hidden and the booked row takes its ref, `added` and chat state;
-- new columns edit_id, edit_status, shared (pence others owe of this payment) and was_pending_as go last. A link
-- made on the amount alone (the merchant changed, as Square does) is the one that could be wrong, so that booked
-- row names the pending merchant and stays new, for the nightly review to check.
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
       coalesce(p.num, t.num) as num, coalesce(e.person, r.person) as person, l.merchant as config_merchant,
       -- seen already while pending, unless marked as a hold that wouldn't book or linked under another merchant
       case when p.num is not null and not k.unmarked and k.renamed is null then pr.fetched_at
            else fr.fetched_at end as added,
       k.id as edit_id, k.status as edit_status, coalesce(sh.pence, 0)::bigint as shared,
       k.renamed as was_pending_as
from ledger.transaction t
join ledger.account a on a.id = t.account_id
join ledger.run fr on fr.id = t.first_run
left join ledger.booked_from bf on (bf.account_id, bf.id) = (t.account_id, t.id) and t.status = 'booked'
left join ledger.transaction p on (p.account_id, p.id, p.status) = (bf.account_id, bf.pending_id, 'pending')
left join ledger.run pr on pr.id = p.first_run
left join ledger.booked_from bp on (bp.account_id, bp.pending_id) = (t.account_id, t.id) and t.status = 'pending'
left join ledger.label l on (l.account_id, l.id, l.status) = (t.account_id, t.id, t.status)
left join ledger.chat_label e on (e.account_id, e.id, e.status) = (t.account_id, coalesce(p.id, t.id), coalesce(p.status, t.status))
left join (select account_id, id, status, sum(pence) as pence from ledger.chat_share group by 1, 2, 3) sh
    on (sh.account_id, sh.id, sh.status) = (t.account_id, coalesce(p.id, t.id), coalesce(p.status, t.status))
left join lateral (
    select x.name from ledger.chat_alias x where ledger.prefix_of(x.prefix, l.merchant)
    order by length(x.prefix) desc limit 1
) al on true
left join lateral (
    select x.n, x.flow, x.category, x.person from ledger.chat_rule x
    where ledger.prefix_of(x.prefix, coalesce(al.name, l.merchant))
    order by length(x.prefix) desc limit 1
) r on true
cross join lateral (
    select coalesce(p.id, t.id) as id, coalesce(p.status, t.status) as status,
           coalesce(p.num is not null and e.flow = 'reversed', false) as unmarked,
           nullif(coalesce(p.merchant_raw, p.description), coalesce(t.merchant_raw, t.description)) as renamed
) k
cross join lateral (select coalesce(l.rule ~ '^override', false) as overridden) o
cross join lateral (
    select coalesce(case when not k.unmarked then e.flow end,
                    case when not o.overridden and l.flow <> 'reversed' then r.flow end, l.flow) as flow
) f0
cross join lateral (
    select f0.flow,
           case when e.trip is not null then nullif(e.trip, '')
                -- relabelled out of spending: no longer part of the trip enrich gave it by date
                when l.flow = 'spend' and f0.flow <> 'spend' and not o.overridden then null
                else l.trip end as trip
) f
where (t.status = 'booked' or t.last_run = a.last_run) and bp.id is null;
