-- Importing the same workbook twice must be a no-op that says so, not
-- a database error and not a doubled figure.
--
-- Two holes, found by importing the same file twice against a real
-- database rather than by reading the code.
--
-- The ledger has a unique index on (org_id, client_ref) and the
-- importer's insert had no conflict clause, so a second import raised
-- a UniqueViolation. Nothing was doubled -- the whole transaction
-- aborted -- but what reached the person was the text of a Postgres
-- error, and what reached the logs looked like a bug rather than
-- somebody clicking twice. That half is fixed in engine/importer.py,
-- which now asks the ledger to ignore a replay and counts what it
-- ignored.
--
-- po_lines had neither a constraint nor a conflict clause. It did not
-- double only because the ledger aborted the transaction first, which
-- is luck and not a design: a workbook whose stock sheet is empty, or
-- one imported after the movements already landed, would double every
-- ordered quantity. That figure feeds the stock position and the order
-- sequencing, so the error would show up as the engine recommending
-- against stock that does not exist.

begin;

-- Any duplicates already written, before the index refuses them. The
-- newest row per (po, item) wins: a re-import is a correction, so the
-- later value is the one somebody meant.
delete from public.po_lines l
 where exists (
   select 1 from public.po_lines keep
    where keep.org_id = l.org_id
      and keep.po_id = l.po_id
      and keep.item_id = l.item_id
      and (keep.created_at, keep.id) > (l.created_at, l.id)
 );

-- The workbook's On-order sheet is one row per purchase order per SKU
-- per branch, so this is the natural key it already has. A purchase
-- order that genuinely needs two lines for one item at two prices
-- cannot be expressed in that sheet, and if it ever can, this index is
-- what has to change first -- deliberately, rather than by discovering
-- the duplicates later in a figure nobody can reconcile.
create unique index po_lines_natural_idx
  on public.po_lines (org_id, po_id, item_id);

commit;
