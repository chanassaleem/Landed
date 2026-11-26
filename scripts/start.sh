#!/bin/sh
# Check the schema, then serve. Do not migrate here.
#
# Applying migrations when a worker boots means every worker races every
# other to run the same DDL, and a platform that starts three workers
# runs each migration three times. Verifying instead means a deploy
# whose migrations have not been applied fails as a deploy, rather than
# coming up and returning errors that look like application bugs.
#
# Release order is: apply migrations, then deploy. New code against an
# old schema fails on the first query that needs the new column; old
# code against a new schema almost always works, which is what makes a
# rollback safe.
#
# LANDED_SKIP_MIGRATION_CHECK=1 starts without the check, for a smoke
# test of the image itself where no database is reachable.
set -e

if [ -z "$LANDED_SKIP_MIGRATION_CHECK" ]; then
  python tools/migrate.py verify
fi

exec uvicorn web.app:app --host 0.0.0.0 --port "${PORT:-8080}" --workers 1
