-- One organisation per person, enforced where it cannot be bypassed.
--
-- The policies have always allowed a user to belong to several
-- organisations, and the application was not ready for it. Proven on a
-- clean database: give one user two importers, each with SKU RGT-100
-- at branch KHI, one selling 50 a month and the other 30. The engine
-- reads 80 for both. DEMAND_SQL groups by SKU, branch and month with
-- no organisation in the key, so two businesses' sales are summed into
-- one forecast -- and a forecast drives the reorder point, which
-- drives what the product tells somebody to buy.
--
-- The stock rows do not sum, but they come back as two rows with
-- identical keys and entity_id 'default', which anything keyed on
-- (item, branch) downstream will collapse.
--
-- Two ways to fix it. Carry an active-organisation context through
-- every read, write and calculation, or make the unsafe state
-- impossible until that exists. This is the second. It is the option
-- the product's own roadmap calls acceptable for a first release, and
-- it is the one that cannot be got wrong by a query somebody writes
-- next month.
--
-- Here rather than in the API because the API is not the only way in:
-- an import, a script and a future mobile client all reach the same
-- tables, and a rule that lives in one of them is a rule the others
-- do not have.

begin;

-- Only active memberships count. Somebody whose access to an old
-- employer was revoked has not used up their one organisation.
create unique index memberships_one_active_org_per_user
    on public.memberships (user_id)
 where status = 'active';

comment on index public.memberships_one_active_org_per_user is
  'One active organisation per person. The engine groups demand by '
  'SKU, branch and month without an organisation in the key, so two '
  'businesses under one login have their sales summed into one '
  'forecast. Lift this only with an active-organisation context '
  'carried through every read, write and calculation.';

commit;
