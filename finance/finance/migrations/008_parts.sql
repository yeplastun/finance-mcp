-- Parts: an entry that is partly one thing and partly another (£30 of opera: £15 a gift, £15 an advance) moves
-- part of its amount to another category, with that category's flow. Append-only like the other chat tables
-- (003, 005): the newest row per (entry, category) wins, null pence = that part removed. Keyed on the entry's
-- first row, like shares, so a pending entry keeps its parts when it books.
create table ledger.entry_part (
    n bigint primary key default nextval('ledger.chat_n'),
    at timestamptz not null default now(),
    xact xid8 not null default pg_current_xact_id(),
    account_id text not null,
    id text not null,
    status text not null check (status in ('booked', 'pending')),
    category text not null check (ledger.clean(category, 60)),
    flow text check (flow in ('spend', 'housing', 'income', 'transfer', 'loan', 'people', 'investment', 'tax',
                              'reimbursement')),
    pence bigint check (pence between 1 and 10000000),  -- the part's size, whatever the entry's sign
    check ((flow is null) = (pence is null)),
    foreign key (account_id, id, status) references ledger.transaction
);
create index on ledger.entry_part (account_id, id, status, category, n);

create view ledger.chat_part as
select * from (select distinct on (account_id, id, status, category) * from ledger.entry_part
               order by account_id, id, status, category, n desc) p
where pence is not null;

-- What totals add up: one line per entry, less its parts, plus one line per part with the entry's other labels.
-- `whole` is what the bank moved. A part leaves the trip unless it is spending; a reversed entry has no parts.
-- ledger.entry stays one row per entry, so labels, debts and the Sheet read it unchanged.
create view ledger.entry_line as
select e.account_id, e.id, e.status, e.num, e.day, e.at, e.account, e.merchant, e.description, e.flow,
       e.category, e.trip, e.tags, e.note, e.person, e.added, e.was_pending_as, e.edit_id, e.edit_status,
       e.pence - sign(e.pence)::bigint * coalesce(m.pence, 0) as pence, e.shared, e.pence as whole
from ledger.entry e
left join (select account_id, id, status, sum(pence)::bigint as pence from ledger.chat_part group by 1, 2, 3) m
    on (m.account_id, m.id, m.status) = (e.account_id, e.edit_id, e.edit_status)
    and e.flow is distinct from 'reversed'
union all
select e.account_id, e.id, e.status, e.num, e.day, e.at, e.account, e.merchant, e.description, p.flow,
       p.category, case when p.flow = 'spend' then e.trip end, e.tags, e.note, e.person, e.added,
       e.was_pending_as, e.edit_id, e.edit_status, sign(e.pence)::bigint * p.pence, 0, e.pence
from ledger.chat_part p
join ledger.entry e on (e.account_id, e.edit_id, e.edit_status) = (p.account_id, p.id, p.status)
where e.flow is distinct from 'reversed';
