-- An organisation with no creator has no owner, and nobody can
-- administer it.
--
-- The trigger in 0001 makes the creator the owner, so a null created_by
-- made it insert a membership with a null user_id -- which failed, but
-- failed as a NOT NULL violation on memberships, from inside a trigger,
-- naming a table the caller never mentioned. The constraint belongs on
-- the column where the mistake is actually made, so the error names it.
--
-- Written as a separate migration rather than an edit to 0001 because
-- 0001 is already applied: a deployed migration is history.

-- Nothing in production has a null here, but a schema change that
-- assumes that without checking is how a deploy fails at 2am.
do $$
declare orphaned int;
begin
  select count(*) into orphaned from public.organisations where created_by is null;
  if orphaned > 0 then
    raise exception
      'cannot apply: % organisation(s) have no created_by. Set one (the '
      'owner in memberships is the right answer) and re-run.', orphaned;
  end if;
end $$;

alter table public.organisations
  alter column created_by set not null;

comment on column public.organisations.created_by is
  'The user who created it, made its owner by app.claim_new_organisation().
   NOT NULL because an organisation without an owner cannot be administered
   by anyone.';
