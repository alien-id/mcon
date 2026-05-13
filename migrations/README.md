# mcon db migrations

mcon uses one SQLite file (`mcon.sqlite3`) created lazily by `db.py` on
first launch. There's no migration framework — schema changes are recorded
here as plain `.sql` files named `YYYY_MM_DD_<slug>.sql`, applied manually
in date order against the live file.

## Live environment

| | |
|---|---|
| Host | EC2 `i-02ba2993ce6a8a21c` (tag `bargain.alien.org`) |
| Public | `mcon.alien.org` → EIP `52.71.72.0` |
| Auth | SSH only — no SSM agent (instance has no IAM profile); keypair `alienkey` |
| Service | mcon running on localhost, fronted by nginx 1.28 |
| DB | `mcon.sqlite3` next to `app.py` on the root EBS volume |
| SQLite | Migrations assume **3.35+** (for `ALTER TABLE … DROP COLUMN`). Verify with `sqlite3 --version`. |

## Runbook (apply a migration)

> **Order matters w.r.t. the code deploy.** Each migration's header
> comment says whether it should be applied **before** or **after** the
> new code goes live. Read it before running.

From a workstation with the `alienkey` private key:

```bash
ssh -i ~/.ssh/alienkey ubuntu@52.71.72.0   # or whatever user the AMI uses
cd <mcon-install-dir>                       # where app.py + mcon.sqlite3 live

# 0. Confirm SQLite supports the operations needed.
sqlite3 --version           # need 3.35+

# 1. Stop the service. mcon holds a single sqlite3 connection in WAL
#    mode; trying to ALTER TABLE while it's running will block or fail.
sudo systemctl stop mcon       # if it's a systemd unit
# OR find the process and stop it cleanly:
# pgrep -fa 'python.*app.py'

# 2. Snapshot the file. This is the rollback path — keep it until you've
#    verified the new code is healthy end-to-end.
cp -a mcon.sqlite3 mcon.sqlite3.pre-$(date +%Y%m%d_%H%M%S).bak

# 3. Apply the migration. `-bail` aborts on the first error so the file
#    isn't left half-migrated.
sqlite3 -bail mcon.sqlite3 < migrations/2026_05_13_dpop_schema_cutover.sql

# 4. Verify schema.
sqlite3 mcon.sqlite3 ".schema agents"
# expect: CREATE TABLE agents (jkt        TEXT PRIMARY KEY,
#                              owner      TEXT NOT NULL,
#                              created_at TEXT NOT NULL);
sqlite3 mcon.sqlite3 ".schema dashboards"
# expect: agent_jkt column referencing agents(jkt)

# 5. Start the service back up.
sudo systemctl start mcon

# 6. Smoke test from a workstation:
curl -fsSL https://mcon.alien.org/.well-known/alien-agent-id.json | jq .auth
# expect: {"header": "Authorization", "scheme": "DPoP"}
```

If `sqlite3 -bail` aborts the migration, the `BEGIN/COMMIT` wrapper rolls
back every step and the file is unchanged. Re-running a partially-applied
migration is **not** safe in the general case — always restore from the
snapshot first.

## Rollback

Two scenarios:

- **Migration aborted (BEGIN/COMMIT rolled back).** Nothing to roll back;
  the file is identical to the pre-migrate state. Investigate, fix, retry.
- **Migration committed but new code is broken.** Stop mcon, restore the
  `.pre-*.bak` snapshot (`cp -a mcon.sqlite3.pre-*.bak mcon.sqlite3`),
  redeploy the previous code revision, restart.

There's no in-place "undo" path because the dropped column data is gone.

## Compatibility windows

For this cutover specifically — the old code references `agents.fingerprint`
and `dashboards.agent_fingerprint` by literal name in every SQL string; the
new code references `agents.jkt` / `dashboards.agent_jkt`. There is **no
forwards-compatibility window** between the two schemas. Sequence the
deploy so that the service is stopped during the migration:

1. Stop mcon.
2. Deploy new code revision.
3. Apply migration.
4. Start mcon.

Expect a brief unavailability (seconds, not minutes) for `mcon.alien.org`
during the switch. The static landing page and `/d/<id>` dashboard views
are dynamic too — they read from the same SQLite file — so plan a quiet
moment if any dashboards are actively being watched.

## Migrations in this directory

| Date | File | Summary |
|---|---|---|
| 2026-05-13 | `2026_05_13_dpop_schema_cutover.sql` | Rename `agents.fingerprint` → `jkt` and `dashboards.agent_fingerprint` → `agent_jkt`; drop `agents.public_key_pem`. Required after the DPoP cutover (PR #6 in sso-sdk-py). Apply with the service stopped between code-deploy and start. |
