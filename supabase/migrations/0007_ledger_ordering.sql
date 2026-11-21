-- The ledger needs an order, and now() does not give it one.
--
-- now() returns the transaction's start time, so every row written in
-- one transaction carries the same created_at. A transfer is two rows
-- written together and they tie; so do a receipt and a sale recorded
-- in one go. Ordering by created_at then falls back to whatever the
-- planner returns, which means the movement history of a line can show
-- the sale before the receipt that made it possible.
--
-- clock_timestamp() reads the actual clock each time it is called, so
-- rows written microseconds apart sort in the order they happened.
--
-- Existing rows keep the timestamps they have. Their order within a
-- transaction was never recorded and cannot be recovered; this stops
-- it happening again rather than pretending to fix the past.

begin;

alter table public.stock_movement
  alter column created_at set default clock_timestamp();

alter table public.demand_history
  alter column created_at set default clock_timestamp();

comment on column public.stock_movement.created_at is
  'clock_timestamp(), not now(): rows written in the same transaction
   must still sort in the order they were written.';

commit;
