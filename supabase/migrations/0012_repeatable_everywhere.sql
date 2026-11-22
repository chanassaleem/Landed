-- Natural keys for the tables the workbook fills that had none.
--
-- The Read me tells people that importing the same file twice is
-- safe, and for commitments, marketing spend, dealings and contacts it
-- was not: none had a key, so a second import added every row again.
--
-- For commitments that is not a cosmetic duplicate. The thirteen-week
-- cash view sums them, so a re-imported workbook would show the
-- salaries going out twice a month and the letter of credit settling
-- twice, and the answer to "can I fund this order" would be wrong in
-- the direction that stops you ordering.
--
-- Each key is the one the sheet already has. Two committed payments
-- with the same label on the same day, two conference stands with the
-- same label on the same day, or the same sentence recorded against
-- the same customer on the same day are all far likelier to be one
-- thing imported twice than two things that happened.

begin;

-- Duplicates already written go first, newest kept: a re-import is a
-- correction, so the later row is the one somebody meant.
delete from public.commitments a
 where exists (select 1 from public.commitments b
                where b.org_id = a.org_id and b.label = a.label
                  and b.due_date = a.due_date
                  and (b.created_at, b.id) > (a.created_at, a.id));

create unique index commitments_natural_idx
  on public.commitments (org_id, label, due_date);

delete from public.marketing_spend a
 where exists (select 1 from public.marketing_spend b
                where b.org_id = a.org_id and b.label = a.label
                  and b.spent_on = a.spent_on
                  and (b.created_at, b.id) > (a.created_at, a.id));

create unique index marketing_spend_natural_idx
  on public.marketing_spend (org_id, label, spent_on);

-- A contact is keyed on the name and whoever they belong to. The two
-- owner columns are each nullable and exactly one is ever set, so the
-- index coalesces them: a plain two-column key would let one name sit
-- twice under the same customer, because null never equals null.
delete from public.contacts a
 where exists (select 1 from public.contacts b
                where b.org_id = a.org_id and b.name = a.name
                  and coalesce(b.customer_id, b.supplier_id)
                      = coalesce(a.customer_id, a.supplier_id)
                  and (b.created_at, b.id) > (a.created_at, a.id));

create unique index contacts_natural_idx
  on public.contacts (org_id, coalesce(customer_id, supplier_id), name);

-- The summary is hashed because a note can run past what a btree key
-- will hold, and a key that fails on a long note would refuse the
-- import rather than the duplicate.
delete from public.interactions a
 where exists (select 1 from public.interactions b
                where b.org_id = a.org_id
                  and coalesce(b.customer_id, b.supplier_id)
                      is not distinct from
                      coalesce(a.customer_id, a.supplier_id)
                  and b.happened_on = a.happened_on
                  and md5(b.summary) = md5(a.summary)
                  and (b.created_at, b.id) > (a.created_at, a.id));

create unique index interactions_natural_idx
  on public.interactions (org_id, coalesce(customer_id, supplier_id),
                          happened_on, md5(summary));

-- A dividend too, and for the sharpest reason of the five. The books
-- module checks that reserves moved by profit after tax less the
-- dividend; a re-imported workbook that counted the dividend twice
-- would fail that check and report a correct set of books as broken.
delete from public.equity_movements a
 where exists (select 1 from public.equity_movements b
                where b.org_id = a.org_id and b.kind = a.kind
                  and b.moved_on = a.moved_on and b.amount = a.amount
                  and (b.created_at, b.id) > (a.created_at, a.id));

create unique index equity_movements_natural_idx
  on public.equity_movements (org_id, kind, moved_on, amount);

commit;
