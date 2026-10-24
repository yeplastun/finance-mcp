-- IOUs from chat: money owed with no bank payment behind it ("I owe Kim £15 for Sunday's court", "Sam owes me
-- £50"). Append-only like the other chat tables: the newest row per iou wins, null pence = closed by hand.
-- pence > 0: the person owes the user; < 0: the user owes them. finance_debts adds open IOUs to the balances from shares
-- and loans, so a repayment labelled loan with that person settles one by itself.
create sequence ledger.iou_id;

create table ledger.iou (
    n bigint primary key default nextval('ledger.chat_n'),
    at timestamptz not null default now(),
    xact xid8 not null default pg_current_xact_id(),
    iou bigint not null,
    person text not null check (ledger.clean(person, 60)),
    pence bigint check (pence <> 0 and abs(pence) <= 10000000),
    day date not null,
    note text check (ledger.clean(note, 200))
);
create index on ledger.iou (iou, n);

create view ledger.chat_iou as
select * from (select distinct on (iou) * from ledger.iou order by iou, n desc) i
where pence is not null;
