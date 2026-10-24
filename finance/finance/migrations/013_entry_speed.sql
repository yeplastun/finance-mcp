-- 011's ledger.entry, with one join written so the planner can count it. "A pending row that booked is hidden" was a
-- left join on booked_from filtered on `bp.id is null`, which PostgreSQL estimated at one row; every join after it
-- then ran as a nested loop, rescanning chat_label once per entry (~2 s over 8,000 entries). As `not exists` it is
-- an anti-join with a true estimate, the rest hash-joins, and the view reads in ~0.1 s. Same rows, same columns.
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
    select coalesce(e.category, case when not o.overridden then r.category end, l.category) as category
) c
cross join lateral (
    select f0.flow,
           case when e.trip is not null then nullif(e.trip, '')
                -- relabelled out of spending: no longer part of the trip enrich gave it by date
                when l.flow = 'spend' and f0.flow <> 'spend' and not o.overridden then null
                -- recategorised from chat into a category that's never on a trip (rent, subscriptions)
                when c.category is distinct from l.category
                     and exists (select from ledger.not_trip n where n.category = c.category) then null
                else l.trip end as trip
) f
left join ledger.trip tj on tj.name = f.trip
left join ledger.category_whose cw on cw.category = c.category
where (t.status = 'booked' or t.last_run = a.last_run)
  and not exists (select from ledger.booked_from bp
                  where t.status = 'pending' and (bp.account_id, bp.pending_id) = (t.account_id, t.id));
