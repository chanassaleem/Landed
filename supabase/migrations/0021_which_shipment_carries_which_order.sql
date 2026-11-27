-- Which shipment is carrying which order.
--
-- Receiving checked three things: the order exists, it has a line for
-- this item, and the shipment exists. It never checked that the two
-- had anything to do with each other.
--
-- Reproduced on a clean database. PO-1001 from Supplier A, PO-1002
-- from Supplier B, and SHP-01, which is Supplier B's. Receiving
-- PO-1001's goods onto SHP-01 was accepted without complaint, and
-- Supplier B's 280,000 of freight was then allocated to Supplier A's
-- inventory. Both suppliers' costs end up wrong, in opposite
-- directions, and the unit cost that comes out of it is what the
-- price list is built on.
--
-- The relationship is many-to-many and has to be. A consolidated
-- container carries several orders; an order that ships in three
-- parts arrives on three shipments. Modelling it as a column on
-- either side would force somebody to lie about one of those.
--
-- Recorded rather than demanded: the link is written the first time
-- goods are received against the pair, so nobody has to declare a
-- manifest before they can book in a delivery. What is refused is the
-- pair that cannot be true -- a shipment whose supplier is known to
-- be somebody other than the order's.

create table public.shipment_orders (
  org_id      uuid not null references public.organisations (id)
                   on delete cascade,
  shipment_id uuid not null references public.shipments (id)
                   on delete cascade,
  po_id       uuid not null references public.purchase_orders (id)
                   on delete cascade,
  created_at  timestamptz not null default now(),
  created_by  uuid references auth.users (id) default auth.uid(),
  primary key (shipment_id, po_id)
);

create index shipment_orders_po_idx
  on public.shipment_orders (org_id, po_id);

comment on table public.shipment_orders is
  'Which purchase orders a shipment is carrying. Many-to-many because '
  'a consolidated container carries several orders and a part-shipped '
  'order arrives on several shipments. Written when goods are first '
  'received against the pair, so no manifest has to be declared up '
  'front.';

alter table public.shipment_orders enable row level security;

-- Split by command, every time. A FOR ALL policy's USING clause also
-- governs SELECT, and permissive policies are OR'd together.
create policy so_read on public.shipment_orders for select
  using (org_id in (select app.org_ids())
         and (select app.has_module(org_id, 'buy')));
create policy so_insert on public.shipment_orders for insert
  with check ((select app.has_role(org_id, array['owner','manager'])));
create policy so_delete on public.shipment_orders for delete
  using ((select app.has_role(org_id, array['owner','manager'])));

-- The shipments it makes sense to receive an order's goods onto:
-- ones already carrying it, and ones whose supplier does not
-- contradict it. A shipment with no supplier recorded is a forwarder's
-- consolidation and is left open, because that is the honest way to
-- record a container holding two suppliers' goods.
create view public.shipment_for_order with (security_invoker = true) as
  select po.org_id,
         po.id                                   as po_id,
         po.po_number,
         s.id                                    as shipment_id,
         s.reference,
         s.status,
         s.mode,
         s.eta,
         (so.po_id is not null)                  as already_carrying
    from public.purchase_orders po
    join public.shipments s on s.org_id = po.org_id
    left join public.shipment_orders so
           on so.shipment_id = s.id and so.po_id = po.id
   where s.status in ('in_transit', 'arrived', 'cleared')
     and (so.po_id is not null
          or s.supplier_id is null
          or po.supplier_id is null
          or s.supplier_id = po.supplier_id);

comment on view public.shipment_for_order is
  'The shipments an order can sensibly be received onto. Receiving '
  'one supplier''s goods onto another''s shipment allocated the wrong '
  'freight to the wrong inventory, silently.';
