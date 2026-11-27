-- A rate belongs to the currency it was quoted for.
--
-- A receipt against a purchase order inherits the order's currency and
-- rate, which is right and is the reason neither has to be typed. The
-- inheritance did not check that the two still went together.
--
-- Reproduced on a clean database. An order in euro at 305. A
-- consignment on that order invoiced in dollars, with no rate stated:
--
--   line says 60.0000 USD at 305.000000  ->  ledger 18,300 a unit
--   60 USD at a real rate of ~280 is     ->            16,800 a unit
--
-- Nine per cent, on this pair. On a pair further apart it is not nine
-- per cent: a yen price at a euro rate is wrong by a factor of three
-- hundred, and the figure that comes out of it is the unit cost the
-- price list is built on.
--
-- The rule, and all four cases of it:
--
--   no order, foreign currency          an explicit rate is required
--   order, currency not stated          inherit the currency and rate
--   order, same currency stated         inherit the rate
--   order, a different currency stated  an explicit rate is required
--
-- Enforced here rather than only in the application, for the same
-- reason the receipt posts its movement here: an import, a phone and
-- a script cannot each remember it in their own way. The importer
-- reads a currency and a rate straight off a sheet and writes the
-- line itself, so it had the hole too.

create or replace function app.grn_line_inherits_rate()
returns trigger
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
  po   public.purchase_orders%rowtype;
  base text;
begin
  if new.po_line_id is not null then
    select o.* into po
      from public.purchase_orders o
      join public.po_lines l on l.po_id = o.id
     where l.id = new.po_line_id;

    if found then
      -- No currency stated: the order's, and its rate with it.
      if new.currency is null then
        new.currency := po.currency;
        if new.fx_rate = 1 then
          new.fx_rate := po.fx_rate;
        end if;

      -- The order's own currency: its rate still applies, unless the
      -- receipt states one of its own. Holding a rate per receipt is
      -- the point -- the second container clears at a different rate,
      -- and that difference is the importer's margin.
      elsif new.currency is not distinct from po.currency then
        if new.fx_rate = 1 then
          new.fx_rate := po.fx_rate;
        end if;

      -- A different currency. The order's rate was quoted for the
      -- order's currency and means nothing here.
      else
        select currency into base from public.organisations
         where id = new.org_id;
        if new.fx_rate = 1 and new.currency is distinct from base then
          raise exception using
            errcode = 'check_violation',
            message = format(
              'A receipt in %s on an order priced in %s needs its own '
              'exchange rate. The order''s rate of %s was quoted for '
              '%s and valuing %s at it would be wrong by the whole '
              'difference between the two currencies.',
              new.currency, po.currency, po.fx_rate, po.currency,
              new.currency);
        end if;
      end if;
    end if;
  end if;
  return new;
end;
$$;

comment on function app.grn_line_inherits_rate() is
  'Fills a receipt line''s currency and rate from its order, where '
  'they belong together. A rate is inherited only for the currency it '
  'was quoted for: a dollar receipt on a euro order inherited the euro '
  'rate and valued the stock by it.';
