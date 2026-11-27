-- An owner should be able to see who has access.
--
-- The people screen showed every member's role, branches and status,
-- and for everyone but the person reading it, no name at all. The
-- reason was sound: addresses live in auth.users, which a tenant
-- cannot read, and showing a blank would have been worse than saying
-- so. But an administrator who cannot tell one staff member from
-- another cannot administer access, which is the whole purpose of
-- that screen.
--
-- The address is already in hand at the moment it is needed. An invite
-- carries the email it was sent to, and the trigger that turns an
-- accepted invite into a membership reads it to find the invite in the
-- first place. So the membership keeps its own copy: the organisation
-- owns it, its own policies govern it, and nothing has to read
-- auth.users to show it.
--
-- A copy can go stale if somebody changes their sign-in address, and
-- that is the honest trade. The alternative is a screen that cannot
-- name the people it is listing.

alter table public.memberships
  add column email text;

comment on column public.memberships.email is
  'The address this membership was invited at. A copy the organisation '
  'owns, because auth.users is not readable by a tenant and a people '
  'screen that cannot name anybody is not a people screen. May go '
  'stale if the person changes their sign-in address.';

-- Backfilled from the invites that created them, where they are still
-- on record.
update public.memberships m
   set email = i.email
  from public.invites i
 where i.org_id = m.org_id
   and i.accepted_by = m.user_id
   and m.email is null;

-- And the creator of an organisation, who never had an invite.
update public.memberships m
   set email = u.email
  from auth.users u
 where u.id = m.user_id
   and m.email is null;

create or replace function app.accept_invites()
returns trigger
language plpgsql
security definer
set search_path = public, pg_catalog
as $$
begin
  insert into public.memberships (org_id, user_id, role, location_ids,
                                  invited_by, email)
  select i.org_id, new.id, i.role, i.location_ids, i.invited_by,
         lower(new.email)
    from public.invites i
   where i.email = lower(new.email)
     and i.accepted_at is null
     and i.revoked_at is null
     and i.expires_at > clock_timestamp()
  on conflict (org_id, user_id) do nothing;

  update public.invites
     set accepted_at = clock_timestamp(), accepted_by = new.id
   where email = lower(new.email)
     and accepted_at is null
     and revoked_at is null
     and expires_at > clock_timestamp();

  return new;
end;
$$;

-- The founder's own membership, created by the organisation trigger
-- rather than by an invite, needs the same. Redefined from
-- app.claim_new_organisation as 0011 left it, with only the email
-- added -- the entitlements it grants are unchanged.
create or replace function app.claim_new_organisation()
returns trigger
language plpgsql
security definer
set search_path = public, pg_temp
as $$
begin
  insert into public.memberships (org_id, user_id, role, email)
  values (new.id, new.created_by, 'owner',
          (select lower(email) from auth.users where id = new.created_by));

  insert into public.entitlements (org_id, module, status)
  select new.id, m, 'trial'
    from unnest(array[
      'stock','buy','freight','demand','cash','sell','money',
      'customers','books','people','intelligence','equipment','service',
      'quality'
    ]) as m
  on conflict (org_id, module) do nothing;

  return new;
end;
$$;
