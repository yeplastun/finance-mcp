-- The finance MCP's database role. Apply as a superuser after the finance migrations:
--   sudo -u postgres psql -d <your database> -f deploy/finance.sql
-- If finmcp is also an OS user, pg_hba's default `local all all peer` rule admits it with no password. It reads the entry view and what
-- describes it, never the raw TrueLayer payloads in ledger.transaction or the personal config in
-- ledger.config, bar its `notes` (finance migration 006). The view runs with its owner's rights, so the
-- tables under it need no grant.
--
-- Its only writes: inserting into the chat tables (finance migrations 003, 005, 008, 009 and 012), and only the label
-- columns, so `n`, `at` and `xact` always come from their defaults and the history can't be
-- backdated. No update or delete anywhere: an undo is a new row. The role stays read-only by
-- default; finance_label opens each write transaction itself.
do $$
begin
    if not exists (select from pg_roles where rolname = 'finmcp') then
        create role finmcp login connection limit 4;
    end if;
end
$$;
alter role finmcp set default_transaction_read_only = on;
alter role finmcp set statement_timeout = '10s';
grant usage on schema ledger to finmcp;
grant select on ledger.entry, ledger.entry_line, ledger.trip, ledger.category_group to finmcp;
grant select (notes) on ledger.config to finmcp;
grant select (id, fetched_at) on ledger.run to finmcp;
grant select (id, provider, kind, name, last_run) on ledger.account to finmcp;
grant select on ledger.entry_edit, ledger.merchant_rule, ledger.merchant_alias, ledger.chat_rule,
    ledger.chat_alias, ledger.entry_share, ledger.chat_share, ledger.entry_part, ledger.chat_part, ledger.iou,
    ledger.chat_iou to finmcp;
grant insert (account_id, id, status, flow, category, person, trip, tags, note, whose) on ledger.entry_edit to finmcp;
grant insert (account_id, id, status, person, pence) on ledger.entry_share to finmcp;
grant insert (account_id, id, status, category, flow, pence) on ledger.entry_part to finmcp;
grant insert (prefix, flow, category, person, whose) on ledger.merchant_rule to finmcp;
grant insert (prefix, name) on ledger.merchant_alias to finmcp;
grant insert (iou, person, pence, day, note) on ledger.iou to finmcp;
grant usage on sequence ledger.chat_n, ledger.iou_id to finmcp;
