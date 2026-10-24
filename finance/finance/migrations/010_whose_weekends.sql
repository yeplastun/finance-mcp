-- A category's whose can differ at weekends ([whose.weekends] in ledger.toml): eating out on a Saturday or Sunday is
-- joint, on a weekday personal. Off trips only: a trip together is joint anyway, one alone stays as the
-- category says. It ranks after the entry's label, chat rules and the trip, before the category's own default.
alter table ledger.category_whose
    alter column whose drop not null,
    add column weekends text check (weekends in ('personal', 'joint', 'hers'));

-- 009's views with the weekend default.
create or replace view ledger.entry as
select t.account_id, t.id, t.status, a.name as account, a.provider, t.day, t.at, t.pence, t.currency,
       t.description, coalesce(al.name, l.merchant) as merchant, f.flow, c.category,
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
       k.renamed as was_pending_as,
       case when f.flow in ('spend', 'housing')
            then coalesce(e.whose, r.whose, case when tj.joint then 'joint' end,
                          case when f.trip is null and extract(isodow from t.day) >= 6 then cw.weekends end, cw.whose,
                          'personal') end as whose,
       coalesce(e.whose, r.whose) as whose_set
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
    select x.n, x.flow, x.category, x.person, x.whose from ledger.chat_rule x
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
cross join lateral (
    select coalesce(e.category, case when not o.overridden then r.category end, l.category) as category
) c
left join ledger.trip tj on tj.name = f.trip
left join ledger.category_whose cw on cw.category = c.category
where (t.status = 'booked' or t.last_run = a.last_run) and bp.id is null;

create or replace view ledger.entry_line as
select e.account_id, e.id, e.status, e.num, e.day, e.at, e.account, e.merchant, e.description, e.flow,
       e.category, e.trip, e.tags, e.note, e.person, e.added, e.was_pending_as, e.edit_id, e.edit_status,
       e.pence - sign(e.pence)::bigint * coalesce(m.pence, 0) as pence, e.shared, e.pence as whole, e.whose
from ledger.entry e
left join (select account_id, id, status, sum(pence)::bigint as pence from ledger.chat_part group by 1, 2, 3) m
    on (m.account_id, m.id, m.status) = (e.account_id, e.edit_id, e.edit_status)
    and e.flow is distinct from 'reversed'
union all
select e.account_id, e.id, e.status, e.num, e.day, e.at, e.account, e.merchant, e.description, p.flow,
       p.category, case when p.flow = 'spend' then e.trip end, e.tags, e.note, e.person, e.added,
       e.was_pending_as, e.edit_id, e.edit_status, sign(e.pence)::bigint * p.pence, 0, e.pence,
       case when p.flow in ('spend', 'housing')
            then coalesce(e.whose_set, case when p.flow = 'spend' and tj.joint then 'joint' end,
                          case when (p.flow <> 'spend' or e.trip is null) and extract(isodow from e.day) >= 6
                               then cw.weekends end, cw.whose, 'personal') end
from ledger.chat_part p
join ledger.entry e on (e.account_id, e.edit_id, e.edit_status) = (p.account_id, p.id, p.status)
left join ledger.trip tj on tj.name = e.trip
left join ledger.category_whose cw on cw.category = p.category
where e.flow is distinct from 'reversed';
