# Browser tests

These drive the real page in a real DOM (jsdom) against a running server, and
exercise what the Python tests cannot: navigation, filtering, sorting, column
visibility, density, the back button, the settings round trip, and matching a
dropped file to its slot.

They are not part of the pytest run because they need Node and a live server.

    cd tests/dom && npm install jsdom
    ./run.sh            # 249 checks: every view, the three-tier bar, scope,
                        #   grouping, paging, selection, the detail panel,
                        #   chart and board views, Settings, data sanity
    node match.mjs      # 25 checks: header matching and file staging
                        #   (needs a server already up on 8099)

`run.sh` starts a server of its own on a free port with a database of its own,
runs the harness against it and tears both down. Point it elsewhere with
`BASE=... node render.mjs` if you have a server already.

Both exit non-zero on failure and print any runtime JS error, so they can be
dropped into CI once a Node step is added.

## Two things worth knowing if you extend these

**jsdom's `FormData` is not Node's.** Node's `fetch` serialises its string
fields by luck but turns file entries into garbage, so an upload test silently
falls back to sample data and passes without having uploaded anything. The
shim in both files rebuilds it entry by entry. This bit twice: once losing
every setting, and once making the sanity checks pass against clean data while
appearing to test broken data.

**Do not stub `Blob` or `FormData`.** jsdom provides both. Replacing `Blob`
turns every uploaded file into `[object Object]` on the wire, with the same
silent fallback.

**jsdom has no layout, so it has no `scrollIntoView`.** The page is right to
call it. Stub it on `Element.prototype` rather than guarding the app for the
benefit of a test.

**Give the harness its own server and its own database.** It saves companies,
branches and mappings, so a shared database makes "nothing is configured yet"
pass on the first run and fail on the second. `run.sh` also refuses to start
if the store it finds is not empty: an orphaned server from a previous run
once answered `/health` on the same port, and the harness spent a while
asserting against someone else's data.

**Wait for a condition, not for a number of milliseconds.** A fixed wait after
boot passed most runs and failed some. That is the worst outcome: it reads as
a flake, and the flake was real. `/api/meta` was returning 500 roughly one run
in three, which emptied the navigation rail while the rest of the page came up
fine. Use `waitFor`, and when it times out read what it prints before assuming
the harness is at fault -- twice now it has not been.

**Re-query elements after every interaction.** `renderTable` replaces
`innerHTML`, so a held reference to a pill, header button or checkbox is
detached and its next click goes nowhere. A held pill reference once left a
filter switched on and cascaded into four unrelated failures.

## What this has caught

Worth recording, because in each case the first guess was that the test was
wrong:

- `/api/meta` returning 500 about one boot in three. One DuckDB connection is
  shared by every request and the web server runs synchronous endpoints in a
  threadpool; DuckDB refuses a second call on a busy connection rather than
  waiting. There is now a pytest for it that hammers the endpoints in
  parallel, so it fails as a test rather than as a flake here.
- Settings reading back codes discovered in an upload as companies somebody
  had configured. A snapshot has to reference an entity and a location, so
  those rows are created on upload; they now carry a `configured` flag and the
  screen lists them apart.
- A view whose natural representation is a chart never opening as one, because
  the view-type state defaulted to `table` and `currentVType` found `table` in
  the view's list and stopped there.
- `/api/config/group` answering with only the group it had saved, so the
  client's copy of the configuration went stale and the group name never
  reached the bar until a reload.
- The mapping screen telling you to recalculate with no way to do it.

## What these do NOT cover

jsdom applies no layout and no CSS, so the responsive behaviour is unverified
here: the drawer under 900px, tables collapsing to record cards under 700px,
and touch target sizes all need a real browser or a device. `test_frontend.py`
asserts the rules are present in the stylesheet, which catches deletion but not
whether they look right.
