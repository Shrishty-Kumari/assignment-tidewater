-- 0012 (contract): NOT applied automatically. See docs/MIGRATIONS.md.
--
-- Preconditions, all checked by a human in the release checklist:
--   1. every running version reads/writes only payouts.state (>= 1.9.0),
--   2. the oldest version we could still roll back to is >= 1.9.0
--      (i.e. 1.9.0 has been in production for at least one full release),
--   3. count(settle_build_info{version=~"1\\.[0-8]\\..*"}) == 0 for 7 days
--      (no pre-1.9 pod has run anywhere, including batch jobs).
-- After this, rolling back to 1.7.x is no longer possible - by design, and
-- only once nothing needs it.
SET lock_timeout = '3s';
SET statement_timeout = '30s';

DROP TRIGGER IF EXISTS payouts_sync_state ON payouts;
DROP FUNCTION IF EXISTS payouts_sync_state();

-- uses the validated CHECK from 0011 instead of scanning the table
ALTER TABLE payouts ALTER COLUMN state SET NOT NULL;
ALTER TABLE payouts ALTER COLUMN state SET DEFAULT 'queued';
ALTER TABLE payouts DROP CONSTRAINT IF EXISTS payouts_state_not_null;

ALTER TABLE payouts DROP COLUMN IF EXISTS status;
