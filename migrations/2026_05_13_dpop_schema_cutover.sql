-- 2026-05-13: DPoP schema cutover
--
-- Context: PR #6 in sso-sdk-py replaced the legacy AgentID envelope with
-- RFC 9449 DPoP. Two schema implications follow:
--
--   1. The agent's stable identifier changes from the SHA-256 of the
--      serialized Ed25519 envelope key to the RFC 7638 thumbprint of the
--      same key encoded as a JWK. Same key, different hash. The new code
--      writes the latter and uses the column name `jkt` to match the
--      industry term ("JWK Thumbprint", per the access-token `cnf.jkt`
--      claim). Renaming the column makes the value-vs-name mismatch on old
--      rows explicit, and the column name will read correctly for every
--      row inserted from here on.
--
--   2. mcon no longer needs the agent's public key in any form. Under
--      DPoP the SDK does all cryptography against the JWK embedded in the
--      proof header; mcon only needs the `jkt`. `agents.public_key_pem`
--      becomes dead storage and is dropped.
--
-- Pre-cutover rows: the existing `fingerprint` values are PEM-hashes and
-- will not equal the `jkt` any returning agent now presents. After the
-- rename those rows survive (and so do their dashboards/projects/steps via
-- ON DELETE CASCADE on the FK) but no new request will resolve to them.
-- Per the design choice recorded in migrations/README.md we leave them in
-- place rather than wipe — minimal blast radius, no data loss for any
-- pre-cutover dashboard URL that's still in someone's bookmarks.
--
-- Deployment ordering (see runbook): apply this migration AFTER the new
-- code is deployed. Both sides of the rename (`fingerprint` → `jkt`,
-- `agent_fingerprint` → `agent_jkt`) are referenced by name in every
-- query in db.py, so the old code requires the old names and the new code
-- requires the new names. There is no forwards-compatibility window;
-- expect a brief 5xx if a request hits between code restart and migration.
-- Mitigate by sequencing: stop service → migrate → start service. The
-- runbook does exactly that.
--
-- Reversibility: SQLite 3.25+ supports `ALTER TABLE … RENAME COLUMN`;
-- 3.35+ supports `ALTER TABLE … DROP COLUMN`. Once dropped, the column
-- data is gone. Take a `cp -a mcon.sqlite3 mcon.sqlite3.pre-migrate.bak`
-- snapshot before running (the runbook does this for you).
--
-- Safety: BEGIN/COMMIT wraps every step in one transaction; `sqlite3
-- -bail` aborts on the first error so a parse failure or constraint
-- violation leaves the file unchanged. Each step is also independently
-- verifiable via `.schema agents` / `.schema dashboards`.

BEGIN;
ALTER TABLE agents       RENAME COLUMN fingerprint       TO jkt;
ALTER TABLE dashboards   RENAME COLUMN agent_fingerprint TO agent_jkt;
ALTER TABLE agents       DROP   COLUMN public_key_pem;
COMMIT;
