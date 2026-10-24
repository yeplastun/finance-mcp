-- The config's `notes` (the user's own notes on people and money arrangements), apart from the rest of it:
-- the finance MCP may read this column and nothing else of ledger.config.
alter table ledger.config add column notes text not null default '';
