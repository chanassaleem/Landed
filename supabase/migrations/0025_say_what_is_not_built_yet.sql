-- Two things in the schema that look abandoned and are not.
--
-- An external review read public.field_definitions as a dead table:
-- nothing in engine/, web/ or web/static/ references it, and it is
-- empty in every database. That reading is reasonable from the code
-- and wrong about the intent, which is recorded nowhere a reader of
-- the database can see it.
--
-- It is half of an extension point. The other half is the `custom`
-- jsonb column on items, suppliers, customers, machines and
-- stock_movement -- also unread, also unwritten. Together they are
-- how an importer adds a column of their own: field_definitions
-- holds the per-organisation definition that would drive the form,
-- the grid column, the validation and the import template, and the
-- values live in `custom` on the row they belong to.
--
-- Left in place rather than dropped. Dropping a table that an
-- organisation may already hold rows in is not reversible by a
-- migration, the design is sound, and the alternative -- adding it
-- back later -- costs more than the nothing it costs to keep.
--
-- Said here, in the database, because a comment in a migration file
-- from months ago is not where anybody looks. \d+ shows these.

comment on table public.field_definitions is
  'Per-organisation definitions of an importer''s own extra columns: '
  'the label, type, options and validation that would drive the form, '
  'the grid column and the import template. The values live in the '
  'custom jsonb of the row they describe. NOT YET WIRED UP -- nothing '
  'reads or writes this table, and it is empty everywhere. It is an '
  'extension point waiting for its screens, not a dead table. Drop it '
  'only with the custom columns it is paired with.';

comment on column public.items.custom is
  'An importer''s own extra fields for this item, defined in '
  'public.field_definitions. Not yet wired up: see that table.';

comment on column public.suppliers.custom is
  'An importer''s own extra fields for this supplier, defined in '
  'public.field_definitions. Not yet wired up: see that table.';

comment on column public.customers.custom is
  'An importer''s own extra fields for this customer, defined in '
  'public.field_definitions. Not yet wired up: see that table.';

comment on column public.machines.custom is
  'An importer''s own extra fields for this machine, defined in '
  'public.field_definitions. Not yet wired up: see that table.';

comment on column public.stock_movement.custom is
  'An importer''s own extra fields for this movement, defined in '
  'public.field_definitions. Not yet wired up: see that table.';
