#!/usr/bin/env bash
# A throwaway Postgres for the policy tests.
#
# The tests in tests/test_rls.py and tests/test_supabase_link.py need a
# real database -- reading the SQL is not evidence that the policies hold.
# This starts one on a non-standard port so it cannot collide with
# anything you already run, and prints the DSN to export.
#
#   source scripts/dev_postgres.sh      # start it and export the DSN
#   scripts/dev_postgres.sh stop        # stop it
set -euo pipefail

PGPORT="${PGPORT:-5441}"
PGDATA="${PGDATA:-/tmp/landed-pg}"
PGBIN="$(ls -d /usr/lib/postgresql/*/bin 2>/dev/null | tail -1 || true)"
[ -n "$PGBIN" ] || PGBIN="$(dirname "$(command -v postgres)")"

run() { if [ "$(id -u)" = "0" ]; then su postgres -c "$1"; else bash -c "$1"; fi; }

case "${1:-start}" in
  stop)
    run "$PGBIN/pg_ctl -D $PGDATA stop" || true
    exit 0 ;;
esac

if [ ! -s "$PGDATA/PG_VERSION" ]; then
  rm -rf "$PGDATA"; mkdir -p "$PGDATA"
  [ "$(id -u)" = "0" ] && chown -R postgres "$PGDATA"
  run "$PGBIN/initdb -D $PGDATA -A trust -U postgres" >/dev/null
fi

if ! pg_isready -h /tmp -p "$PGPORT" >/dev/null 2>&1; then
  run "$PGBIN/pg_ctl -D $PGDATA -o '-p $PGPORT -k /tmp' -l /tmp/landed-pg.log start" >/dev/null
  sleep 2
fi

psql -h /tmp -p "$PGPORT" -U postgres -tAc \
  "select 1 from pg_database where datname='landed'" | grep -q 1 \
  || createdb -h /tmp -p "$PGPORT" -U postgres landed

export LANDED_TEST_DSN="postgres://postgres@/landed?host=/tmp&port=$PGPORT"
echo "LANDED_TEST_DSN=$LANDED_TEST_DSN"
