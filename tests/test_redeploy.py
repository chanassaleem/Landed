"""Module 4 tests.

These run modules 1 and 3 per location to build each block, because that is
how the caller does it and because per-location demand is the whole point: the
same item can be smooth at one branch and intermittent at another.
"""

from datetime import date, timedelta

import pytest

from engine.modules.classify import classify
from engine.modules.inventory import evaluate as evaluate_inventory
from engine.modules.redeploy import LocationBlock, suggest
from engine.schema import (
    DemandPeriod,
    ItemSnapshot,
    StockStatus,
    Thresholds,
    TransferUrgency,
)

AS_OF = date(2026, 11, 20)
T = Thresholds()

PERIODS = [
    "2025-12", "2026-01", "2026-02", "2026-03", "2026-04", "2026-05",
    "2026-06", "2026-07", "2026-08", "2026-09", "2026-10", "2026-11",
]


def item(key="RGT-1", on_hand=0.0, **kw):
    base = dict(
        item_key=key,
        description="Reagent pack",
        category="reagent",
        on_hand=on_hand,
        open_po=0.0,
        unit_cost=5_000.0,
        lead_time_days=60.0,
        lead_time_days_std=10.0,
    )
    base.update(kw)
    return ItemSnapshot(**base)


def steady(key="RGT-1", per_month=100.0):
    """Flat demand, which classifies as smooth so policy is well defined."""
    return [DemandPeriod(item_key=key, period=p, quantity=per_month) for p in PERIODS]


def block(entity, location, items, history, thresholds=T):
    classes = classify(items, history, thresholds)
    results = evaluate_inventory(items, classes, thresholds, as_of=AS_OF)
    return LocationBlock(
        entity_id=entity,
        location_id=location,
        items=tuple(items),
        classes=classes,
        results=tuple(results),
    )


# ------------------------------------------------------- the core match


def test_excess_at_one_location_is_offered_to_a_short_one():
    """The headline insight: transfer rather than import."""
    rich = block("E1", "KHI", [item(on_hand=1_200)], steady())   # 12 months cover
    poor = block("E1", "LHR", [item(on_hand=20)], steady())      # runs out

    moves = suggest([rich, poor], T)

    assert len(moves) == 1
    m = moves[0]
    assert (m.from_location, m.to_location) == ("KHI", "LHR")
    assert m.quantity > 0
    assert m.value_moved == pytest.approx(m.quantity * 5_000.0)
    assert m.urgency is TransferUrgency.URGENT
    assert m.receiving_status == StockStatus.CRITICAL.value


def test_a_single_location_produces_nothing_to_redeploy():
    only = block("E1", "KHI", [item(on_hand=1_200)], steady())
    assert suggest([only], T) == ()


def test_nothing_moves_when_no_location_is_short():
    a = block("E1", "KHI", [item(on_hand=1_200)], steady())
    b = block("E1", "LHR", [item(on_hand=1_100)], steady())
    assert suggest([a, b], T) == ()


def test_nothing_moves_when_no_location_has_surplus():
    a = block("E1", "KHI", [item(on_hand=20)], steady())
    b = block("E1", "LHR", [item(on_hand=20)], steady())
    assert suggest([a, b], T) == ()


# ------------------------------------------------------------- the guards


def test_the_sender_is_never_stripped_below_its_cover_floor():
    """Solving a stockout by creating one is not an improvement."""
    rich = block("E1", "KHI", [item(on_hand=1_200)], steady())
    poor = block("E1", "LHR", [item(on_hand=0)], steady())

    moves = suggest([rich, poor], T)
    m = moves[0]

    # Demand is 100/month and the floor is 3 months, so at most 900 may move
    # and the sender must retain at least 3 months of cover.
    assert m.quantity <= 900
    assert m.sending_cover_after >= T.sender_min_cover_months - 1e-9


def test_a_transfer_below_the_value_floor_is_not_suggested():
    """A van and the paperwork cost more than the move saves."""
    cheap = dict(unit_cost=1.0)
    rich = block("E1", "KHI", [item(on_hand=1_200, **cheap)], steady())
    poor = block("E1", "LHR", [item(on_hand=20, **cheap)], steady())
    # Max movable is ~900 units at PKR 1 = PKR 900, far below the 25,000 floor.
    assert suggest([rich, poor], T) == ()


def test_the_value_floor_is_configurable():
    cheap = dict(unit_cost=1.0)
    rich = block("E1", "KHI", [item(on_hand=1_200, **cheap)], steady())
    poor = block("E1", "LHR", [item(on_hand=20, **cheap)], steady())
    loose = Thresholds(min_transfer_value=100.0)
    assert len(suggest([rich, poor], loose)) == 1


# ------------------------------------------------------- expiry as a donor


def test_expiring_stock_is_a_preferred_donor_not_an_excluded_one():
    """Stock that expires unused at one branch may sell comfortably at
    another, which turns a certain write-off into a sale."""
    expiring = item(on_hand=1_000, expiry_date=AS_OF + timedelta(days=30))
    donor = block("E1", "KHI", [expiring], steady())
    assert donor.results[0].status is StockStatus.EXPIRY_RISK

    poor = block("E1", "LHR", [item(on_hand=20)], steady())
    moves = suggest([donor, poor], T)

    assert len(moves) == 1
    assert "expire" in moves[0].reason
    # About 100 of the 1,000 units can be consumed in 30 days at KHI, so
    # roughly 900 are free to move. Here the receiver's need is smaller than
    # that, so need is what binds, and the move is exactly what LHR would
    # otherwise have imported.
    need = poor.results[0].suggested_order_qty
    assert moves[0].quantity == pytest.approx(need)
    assert need < 900


def test_a_donor_short_of_the_full_need_gives_what_it_can():
    """When availability binds rather than need, the move is the surplus above
    the donor's own order-up-to level."""
    donor = block("E1", "KHI", [item(on_hand=700)], steady())
    poor = block("E1", "LHR", [item(on_hand=0)], steady())

    need = poor.results[0].suggested_order_qty
    order_up_to = donor.results[0].order_up_to
    moves = suggest([donor, poor], T)

    expected = 700 - order_up_to
    assert need > expected
    # Quantities are rounded to two places for the transfer note.
    assert moves[0].quantity == pytest.approx(expected, abs=0.01)
    # The donor is left at its own policy level, not below it.
    assert 700 - moves[0].quantity == pytest.approx(order_up_to, abs=0.01)


def test_an_intermittent_spare_is_not_shipped_down_to_nothing():
    """Mean monthly demand for a spare is near zero, so a cover-months floor
    would happily ship out the last pump nobody can predict needing. The floor
    is the order-up-to level, which carries safety stock for the service
    target."""
    spare = dict(unit_cost=88_000.0, lead_time_days=110.0, lead_time_days_std=28.0)
    # Demand present in only 4 of 12 months: intermittent.
    lumpy = [
        DemandPeriod(item_key="SPR-1", period=p, quantity=(2.0 if n % 3 == 0 else 0.0))
        for n, p in enumerate(PERIODS)
    ]
    donor = block("E1", "ISB", [item("SPR-1", on_hand=6, **spare)], lumpy)
    poor = block("E1", "KHI", [item("SPR-1", on_hand=0, **spare)], lumpy)

    # Six pumps is already at or below this location's own order-up-to level
    # once safety stock for an unpredictable failure is included, so there is
    # no surplus to give and nothing is suggested. A cover-months floor would
    # have called five of the six units surplus.
    assert donor.results[0].order_up_to > 6
    assert suggest([donor, poor], T) == ()


# --------------------------------------------------------- multi-entity


def test_a_cross_entity_move_is_flagged_as_intercompany():
    rich = block("E1", "KHI", [item(on_hand=1_200)], steady())
    poor = block("E2", "LHR", [item(on_hand=20)], steady())
    m = suggest([rich, poor], T)[0]
    assert m.is_intercompany is True
    assert m.from_entity == "E1" and m.to_entity == "E2"
    assert "transfer invoice" in m.reason


def test_a_move_within_one_entity_is_not_intercompany():
    rich = block("E1", "KHI", [item(on_hand=1_200)], steady())
    poor = block("E1", "LHR", [item(on_hand=20)], steady())
    assert suggest([rich, poor], T)[0].is_intercompany is False


# ------------------------------------------------------------- allocation


def test_the_worst_shortage_is_served_first():
    rich = block("E1", "KHI", [item(on_hand=1_200)], steady())
    critical = block("E1", "LHR", [item(on_hand=10)], steady())
    reorder = block("E1", "ISB", [item(on_hand=600)], steady())

    moves = suggest([rich, critical, reorder], T)
    assert moves[0].to_location == "LHR"
    assert moves[0].urgency is TransferUrgency.URGENT


def test_one_donor_is_not_over_allocated_across_several_receivers():
    """The same units must not be promised twice."""
    rich = block("E1", "KHI", [item(on_hand=1_200)], steady())
    a = block("E1", "LHR", [item(on_hand=0)], steady())
    b = block("E1", "ISB", [item(on_hand=0)], steady())

    moves = suggest([rich, a, b], T)
    total = sum(m.quantity for m in moves)
    # Donor can release at most 1,200 - (3 * 100) = 900 units.
    assert total <= 900 + 1e-9


def test_a_location_is_never_told_to_ship_to_itself():
    rich = block("E1", "KHI", [item(on_hand=1_200)], steady())
    poor = block("E1", "LHR", [item(on_hand=20)], steady())
    for m in suggest([rich, poor], T):
        assert (m.from_entity, m.from_location) != (m.to_entity, m.to_location)


def test_items_absent_from_a_block_are_skipped_without_error():
    a = block("E1", "KHI", [item("RGT-1", on_hand=1_200)], steady("RGT-1"))
    b = block("E1", "LHR", [item("RGT-2", on_hand=20)], steady("RGT-2"))
    assert suggest([a, b], T) == ()


def test_import_avoided_is_priced_at_the_receiver_cost():
    """What the receiver would have paid is the saving, not the sender's cost."""
    rich = block("E1", "KHI", [item(on_hand=1_200, unit_cost=5_000)], steady())
    poor = block("E1", "LHR", [item(on_hand=20, unit_cost=6_000)], steady())
    m = suggest([rich, poor], T)[0]
    assert m.unit_cost == 5_000
    assert m.value_moved == pytest.approx(m.quantity * 5_000)
    assert m.import_avoided == pytest.approx(m.quantity * 6_000)


def test_no_blocks_is_safe():
    assert suggest([], T) == ()
