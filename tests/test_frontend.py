"""Static checks on the single-page shell.

These cannot prove the page looks right. They do prove the things that break
silently: an element id the script reaches for and the markup no longer has, a
responsive rule somebody deleted while tidying, or a table that stopped
carrying the labels its phone layout depends on.

The behavioural tests live in tests/dom/ and need Node and a running server.
"""

import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

PAGE = Path(__file__).resolve().parent.parent / "web" / "static" / "index.html"
HTML = PAGE.read_text(encoding="utf-8")
SCRIPTS = "\n".join(re.findall(r"<script>(.*?)</script>", HTML, re.S))
STYLE = "\n".join(re.findall(r"<style>(.*?)</style>", HTML, re.S))


def test_the_page_is_one_self_contained_file():
    assert HTML.lstrip().startswith("<!DOCTYPE html>")
    assert HTML.rstrip().endswith("</html>")
    assert HTML.count("<script>") == HTML.count("</script>")


def test_every_element_id_the_script_uses_exists_in_the_markup():
    """The commonest way this page breaks: a renamed id in one place only."""
    used = set(re.findall(r'\$\("([A-Za-z0-9_-]+)"\)', SCRIPTS))
    defined = set(re.findall(r'\bid="([A-Za-z0-9_-]+)"', HTML))
    assert used - defined == set(), f"script reaches for missing ids: {used - defined}"


def test_every_view_in_the_router_has_a_section():
    views = re.search(r'view:\s*asOne\(\[(.*?)\]', SCRIPTS, re.S).group(1)
    for name in re.findall(r'"([a-z]+)"', views):
        assert f'id="v-{name}"' in HTML, f"no section for view {name}"


def test_the_page_still_says_it_is_not_the_product():
    assert "never leaves the machine" in HTML
    assert "shown over the web" in HTML


def test_no_competitor_is_named_in_the_navigation_or_page():
    """These labels were removed deliberately. The product stands on its own
    domains rather than advertising what it was modelled on."""
    for name in ("Cin7", "Xero", "Employment Hero"):
        assert name not in HTML, name


# ------------------------------------------------------------- responsive


def test_the_three_responsive_breakpoints_are_present():
    for width in ("1100px", "900px", "700px"):
        assert f"max-width:{width}" in STYLE.replace(" ", ""), width


def test_tables_collapse_to_record_cards_on_a_phone():
    """A twenty-column table cannot shrink onto a 390px screen, so each row
    becomes a labelled record card instead."""
    assert "table.flip" in STYLE
    assert "content:attr(data-label)" in STYLE.replace(" ", "")


def test_every_data_table_opts_into_the_collapsing_layout():
    for table_id in re.findall(r'<table class="([^"]*)" id="(t-[a-z]+)"', HTML):
        assert "flip" in table_id[0], table_id[1]


def test_every_column_definition_supplies_a_label_for_the_phone_layout():
    """data-label is generated from the column's label, so a column without
    one renders as an unlabelled value on a phone."""
    for block in re.findall(r"\{id:\"[a-z_]+\", label:\"([^\"]*)\"", SCRIPTS):
        assert block.strip() != ""


def test_the_navigation_becomes_a_drawer_rather_than_disappearing():
    assert "body.nav .rail" in STYLE
    assert "scrim" in STYLE and 'id="scrim"' in HTML
    assert 'id="hamb"' in HTML


def test_touch_targets_are_enlarged_on_touch_and_shrunk_on_a_pointer():
    assert "min-height:44px" in STYLE.replace(" ", "")
    assert "pointer:fine" in STYLE


def test_sideways_scrolling_does_not_trigger_the_browser_back_gesture():
    assert "overscroll-behavior-x:contain" in STYLE.replace(" ", "")


def test_reduced_motion_and_print_are_handled():
    assert "prefers-reduced-motion" in STYLE
    assert "@media print" in STYLE


# ----------------------------------------------------------- token contract


def test_both_themes_define_the_same_token_set():
    """A theme is a drop-in only if it satisfies the whole contract."""
    light = set(re.findall(r"(--[a-z0-9-]+):", STYLE.split('[data-theme="dark"]')[0]))
    dark_block = STYLE.split('[data-theme="dark"]')[1].split("}")[0]
    dark = set(re.findall(r"(--[a-z0-9-]+):", dark_block))
    missing = {t for t in dark if t not in light}
    assert missing == set(), f"dark theme defines tokens light does not: {missing}"
    for token in ("--paper", "--ink", "--signal", "--alarm", "--caution",
                  "--clear", "--watch", "--rule"):
        assert token in dark, f"dark theme is missing {token}"


def test_each_theme_declares_a_colour_scheme():
    """So native scrollbars, form controls and the caret match the page."""
    assert STYLE.count("color-scheme:") >= 3


def test_the_density_tiers_move_row_height_type_and_padding_together():
    for tier in ("compact", "standard", "spacious"):
        block = re.search(rf'\[data-density="{tier}"\]\s*\{{([^}}]*)\}}', STYLE)
        assert block, tier
        for var in ("--row", "--cell", "--tsize"):
            assert var in block.group(1), f"{tier} is missing {var}"


BOUNDARY_AMOUNTS = [
    0, 1, 499, 500, 999, 1_000, 1_500, 30_000, 30_500, 31_499, 999_499,
    999_999, 1_000_000, 1_050_000, 1_250_000, 1_350_000, 2_450_000,
    6_150_000, 11_975_000, 33_579_400, 827_300_000,
    -500, -1_250_000, -30_500,
]


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_money_formatting_agrees_between_python_and_the_browser():
    """A figure in a tile and the same figure inside an engine sentence must
    not disagree about scale. Python rounds half to even and JS toFixed rounds
    half away from zero, so PKR 1,250,000 would read as 1.2M in one place and
    1.3M in the other unless the engine matches the browser deliberately.

    This runs the page's own money() rather than a copy of it, so the two
    cannot drift apart.
    """
    from engine.fmt import money

    script = (
        SCRIPTS[SCRIPTS.index("function money("):SCRIPTS.index("const qty =")]
        + "\nconst vals = " + repr(BOUNDARY_AMOUNTS).replace("'", '"') + ";"
        + "\nconsole.log(JSON.stringify(vals.map(v => money(v))));"
    )
    with tempfile.NamedTemporaryFile("w", suffix=".mjs", delete=False) as fh:
        fh.write(script)
        path = fh.name
    out = subprocess.run(["node", path], capture_output=True, text=True, check=True)
    from_js = json.loads(out.stdout)
    from_py = [money(v) for v in BOUNDARY_AMOUNTS]

    mismatches = [
        (v, p, j) for v, p, j in zip(BOUNDARY_AMOUNTS, from_py, from_js) if p != j
    ]
    assert mismatches == [], f"python and browser disagree: {mismatches}"


def test_the_rounding_is_half_away_from_zero_not_half_to_even():
    from engine.fmt import money

    assert money(1_250_000) == "PKR 1.3M"
    assert money(30_500) == "PKR 31K"
    assert money(-1_250_000) == "-PKR 1.3M"


# ------------------------------------------------------------------ syntax


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_the_scripts_parse():
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as fh:
        fh.write(SCRIPTS)
        path = fh.name
    result = subprocess.run(["node", "--check", path], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


# ---------------------------------------------------- table capabilities


def test_the_display_control_replaced_the_separate_menus():
    """One control configures the view. Density in one menu and columns in
    another left nowhere sensible to put grouping."""
    assert "function displayMenu(" in SCRIPTS
    for section in ("Density", "Group by", "Sort by", "Columns", "Rows per page"):
        assert f'"{section}"' in SCRIPTS, section
    assert "data-density-set" not in HTML


def test_grouping_paging_and_selection_exist():
    assert "function buildGroups(" in SCRIPTS
    assert "data-collapse" in SCRIPTS
    assert "function pagerBar(" in SCRIPTS
    assert "function selectionBar(" in SCRIPTS
    assert "data-pick" in SCRIPTS


def test_a_sortable_column_says_so_before_it_is_clicked():
    """Showing nothing until a column is sorted means you cannot tell what is
    sortable without trying it."""
    assert "ar idle" in SCRIPTS
    assert ".ar.idle" in STYLE


def test_columns_can_be_resized_and_the_width_is_remembered():
    assert "data-grip" in SCRIPTS
    assert "col-resize" in STYLE
    assert "WIDTH_KEY" in SCRIPTS


def test_filter_counts_are_computed_on_the_scoped_rows():
    """Counts over every row contradict the table as soon as a branch filter
    is on, which is worse than showing no counts."""
    assert "const scoped = all.filter(r => matchScope(r, st))" in SCRIPTS
    assert "countBy(all, r => r.status)" not in SCRIPTS


def test_changing_a_filter_resets_the_page():
    """Narrowing a filter while on page four would otherwise show nothing."""
    assert "RESETS_PAGE" in SCRIPTS


def test_selection_does_not_survive_a_view_change():
    """A Copy button must not export rows the user cannot see."""
    assert "selectionTable !== st.view" in SCRIPTS


def test_the_filter_command_input_offers_every_category():
    assert "function buildSuggestions(" in SCRIPTS
    assert "function applySuggestion(" in SCRIPTS
    assert 'ev.key === "/"' in SCRIPTS
    assert "ArrowDown" in SCRIPTS and "ArrowUp" in SCRIPTS


def test_there_is_an_active_filter_badge():
    assert "function activeFilterCount(" in SCRIPTS
    assert "fcount" in STYLE


def test_a_row_detail_panel_exists_and_does_not_recurse():
    """openDetail changes the URL, which repaints; showDetail only draws.
    One function doing both made paint call openDetail, which called S.set,
    which called paint, for ever."""
    assert 'id="detail"' in HTML
    assert "function openDetail(" in SCRIPTS
    assert "function showDetail(" in SCRIPTS
    assert "if (st.row && CURRENT) showDetail(st.row)" in SCRIPTS


def test_the_status_bar_and_live_region_exist():
    assert 'id="statusbar"' in HTML
    assert 'id="live"' in HTML
    assert 'aria-live="polite"' in HTML
    assert "function announce(" in SCRIPTS


def test_the_selection_actions_are_the_honest_ones():
    """Read-only product: take the rows away, do not pretend to change them."""
    assert "function selectionCsv(" in SCRIPTS
    assert "function downloadSelection(" in SCRIPTS
    assert "data-sel=\"delete\"" not in SCRIPTS


def test_the_data_sanity_view_exists_and_is_in_the_router():
    assert 'id="v-sanity"' in HTML
    assert "function renderSanity(" in SCRIPTS
    assert '"sanity"' in SCRIPTS


def test_the_sanity_view_says_what_silence_means():
    """Nothing found is not the same as the data being right, and the panel
    has to say so or it will be read as a clean bill of health."""
    assert "not the same as being right" in SCRIPTS


def test_no_inline_event_handlers():
    """Everything goes through one delegated listener, so markup rebuilt by a
    render never loses its handlers."""
    assert not re.search(r'\son(click|change|input|submit)=', HTML)


# ---------------------------------------------------------------------------
# Structure
#
# These are static checks on the single page: they prove the arrangement is
# there, not that it behaves. Behaviour is the jsdom harness in tests/dom.
# ---------------------------------------------------------------------------

def test_three_bars_in_order():
    """Scope, then what you can do, then how you are looking at it."""
    org = HTML.index('class="bar bar-org"')
    act = HTML.index('class="bar bar-act"')
    view = HTML.index('class="bar bar-view"')
    assert org < act < view


def test_the_screen_bars_sit_inside_the_content_column():
    """Spanning the window above the navigation made them read as global
    controls, when they are the opposite: they change with every selection."""
    rail = HTML.index('<nav class="rail"')
    main = HTML.index("<main>")
    assert HTML.index('class="bar bar-org"') < rail, "the group bar is global"
    assert main < HTML.index('id="viewbars"'), "the screen bars are in main"
    assert HTML.index('id="viewbars"') > rail
    # and they stay reachable while the table scrolls
    assert ".viewbars{position:sticky" in STYLE


def test_scope_lives_in_the_first_bar_only():
    """A company selector repeated per view is a different control each time."""
    assert HTML.count('id="f_entity"') == 1
    assert HTML.count('id="f_location"') == 1
    bar = HTML[HTML.index('class="bar bar-org"'):HTML.index('class="bar bar-act"')]
    assert 'id="scope"' in bar, "the selectors are rendered into the first bar"
    assert 'id="asof"' in bar
    assert 'id="orgname"' in bar
    # and the script puts them there rather than into a per-view toolbar
    scope = SCRIPTS[SCRIPTS.index("function renderScopeBar("):]
    scope = scope[:scope.index("\n}")]
    assert 'id="f_entity"' in scope
    assert 'id="f_location"' in scope


def test_the_group_name_is_shown_beside_the_scope():
    assert 'id="orgname"' in HTML
    assert '.orgname:empty::before{content:"Unnamed group"' in HTML


def test_settings_is_a_nav_group_not_a_gear():
    for view in ("v-org", "v-thresholds", "v-mapping"):
        assert f'id="{view}"' in HTML, view


def test_each_threshold_explains_its_consequence():
    block = HTML[HTML.index("const THRESHOLDS = ["):]
    block = block[:block.index("\n];")]
    assert block.count("why:") >= 11, "every threshold needs its consequence"
    assert "decides which purchase orders get placed" in block


def test_view_types_are_declared_per_view():
    block = HTML[HTML.index("const VIEW_TYPES = {"):]
    block = block[:block.index("\n};")]
    for view in ("stock", "receivables", "cash", "forecast",
                 "transfers", "machines", "purchasing"):
        assert f"{view}:" in block, view
    # A balance over thirteen weeks is a shape before it is a list.
    assert '"chart", "table"' in block.replace("'", '"')


def test_the_view_type_has_no_default_of_its_own():
    """Defaulting it to "table" meant a view that opens as a chart never did:
    currentVType found "table" in its list and stopped there."""
    assert 'vtype: asOne(["table","chart","board"], "")' in HTML


def test_charts_are_drawn_not_imported():
    assert "function barChart(" in HTML
    assert "function lineChart(" in HTML
    assert "function boardView(" in HTML
    for src in ("unpkg", "jsdelivr", "chart.js", "d3.v"):
        assert src not in HTML.lower(), f"no build step, no {src}"


def test_the_page_fetches_nothing_from_anywhere_else():
    """One file, and everything it needs served beside it. The product is a
    desktop application where data never leaves the machine; a page that
    phones a third party for a typeface has broken that before the first
    figure is drawn."""
    for tag in re.findall(r"<(?:script|link|img)[^>]*>", HTML, re.I):
        for attr in re.findall(r'(?:src|href)="([^"]+)"', tag):
            assert not attr.startswith(("http://", "https://", "//")), tag
    assert "@font-face" in HTML
    assert "/static/fonts/" in HTML


def test_every_chart_carries_a_note_saying_what_it_shows():
    """A plot with no caption leaves the reader to work out what the axis is
    counting. The frame renders the note; every chart passes one."""
    assert 'class="chartnote"' in HTML
    # Every chart goes through renderChart, which frames it and renders the
    # note; four screens draw one.
    drawn = SCRIPTS.count("renderChart(")
    assert drawn >= 5, f"only {drawn} charts drawn through the frame"
    assert SCRIPTS.count("note:") >= 4


def test_charts_have_a_tooltip_of_their_own():
    """A native <title> waits a second, cannot be styled, and cannot carry a
    second line such as the range behind a forecast point."""
    assert 'id="ctip"' in HTML
    assert ".ctip{position:fixed" in STYLE
    assert "function tipFor(" in SCRIPTS
    assert ".chart .hit{fill:transparent}" in STYLE


def test_discovered_rows_are_marked_apart_from_configured_ones():
    assert ".setrow.found{" in HTML
    assert "Seen in your data" in HTML


def test_the_bars_are_derived_from_the_navigation():
    """The sidebar already declares domains and modules. The bars read the
    same structure, so adding a module to a domain in /api/meta puts it in
    that domain's bar with no change to the bar code."""
    assert "function domainOf(" in SCRIPTS
    assert "const DOMAIN_ACTIONS = {" in SCRIPTS
    assert "const MODULE_ACTIONS = {" in SCRIPTS
    block = SCRIPTS[SCRIPTS.index("const DOMAIN_ACTIONS = {"):]
    block = block[:block.index("\n};")]
    for domain in ("overview", "stock", "money", "equipment", "data", "settings"):
        assert f"{domain}:" in block, domain


def test_every_built_module_has_its_own_entry_in_tier_two():
    block = SCRIPTS[SCRIPTS.index("const MODULE_ACTIONS = {"):]
    block = block[:block.index("\n};")]
    for view in ("dashboard", "actions", "stock", "transfers", "purchasing",
                 "forecast", "receivables", "cash", "machines", "service",
                 "pack", "sanity", "history", "audit", "org", "thresholds",
                 "mapping"):
        assert f"{view}:" in block, view


def test_the_search_box_says_what_it_searches():
    block = SCRIPTS[SCRIPTS.index("const SEARCHES = {"):]
    block = block[:block.index("\n};")]
    assert "customers" in block and "items and descriptions" in block
    assert "findings" in block


def test_tier_three_is_cleared_before_each_paint():
    """The sanity screen inherited receivables' search box and Display
    control, which then offered another table's columns."""
    paint = SCRIPTS[SCRIPTS.index("function paint(){"):]
    paint = paint[:paint.index("\n  // Screens that do not depend")]
    assert "renderViewBar(null);" in paint


def test_a_one_click_lens_is_the_same_state_as_a_pill():
    assert 'data-quick=' in SCRIPTS
    assert '[data-quick]' in SCRIPTS
    assert '.btn[aria-pressed="true"]' in STYLE


def test_a_bar_row_can_actually_draw_its_fill():
    """Both the track and the fill are spans, and width does not apply to an
    inline box, so every ageing bar rendered as an empty track whatever the
    figure beside it said."""
    assert ".barrow .fill{display:block" in STYLE
    assert ".barrow .track{display:block" in STYLE


def test_the_header_and_the_bar_rows_are_not_both_called_bars():
    """They were, and the header's rule won: the ageing block was laid out as
    a sticky strip at the top of the page."""
    assert ".topbars{grid-column:1/-1" in STYLE
    assert '<header class="topbars">' in HTML
    assert 'class="bars"' not in HTML
    assert ".barrows{display:grid" in STYLE


def test_charts_are_drawn_at_the_width_they_get():
    """A viewBox stretched to fill the column scales its text with it: a 10px
    axis label on a 560-wide viewBox rendered at 1900px came out at 34px."""
    assert "function drawChart(" in SCRIPTS
    assert "host.clientWidth" in SCRIPTS
    assert "width => barChart(" in SCRIPTS or "width => lineChart(" in SCRIPTS
    # and redrawn when the window changes, or a resized window keeps the old
    assert "function redrawCharts(" in SCRIPTS
    assert 'window.addEventListener("resize"' in SCRIPTS
