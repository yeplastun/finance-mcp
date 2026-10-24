-- Category groups from ledger.toml's [groups] (Home = Rent + Council tax + ...), rebuilt by every ledger run like
-- ledger.trip. A category in no group stands alone.
create table ledger.category_group (
    category text primary key,
    name text not null
);
