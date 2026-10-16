# cMATRA Insight cutover

Status: prepared for materios review; no deployment authorized by Handoff D.
Mainnet requires G2 dedicated credentials and deci's explicit restart go.
Do not run mainnet steps from a Codex session. Do not revoke Blockfrost keys.

## Provider contract

`tools.config.chain_api_base_url` is the shared resolver. Mainnet and preprod
use `https://<network>.insight.iagon.com/api/v0` by default.
`CHAIN_API_BASE_URL` overrides `BLOCKFROST_BASE_URL`; both require `/api/v0`.
Preview requires an explicit base because Insight preview is not verified.
REST clients use `/api/v0`; pycardano receives `/api` and appends `/v0` itself.

Set `CHAIN_API_KEY_FILE` to an owner-readable file containing one key. It wins
against both legacy `BLOCKFROST_PROJECT_ID_<NETWORK>` and
`BLOCKFROST_PROJECT_ID`. Missing/empty/multiline files fail closed. The key is
read in-process and is never placed in argv or a unit's Environment value.
Legacy env keys remain available for rollback. The file is not read at module
import, so scripts can select their network before constructing a client.

Holder and policy lists are sorted by address and asset respectively, after
complete pagination. There is no reliance on the provider's list order.
History paging fails closed on provider errors; it does not silently truncate
at Insight's 100,000-row limit. Recheck history size as the deployment grows.
`services/chain_check.py` does not read `txs/{hash}.output_amount`, so Insight's
per-unit aggregation there cannot change the signer's verdict.

## Evidence and limits (2026-10-16)

A real unsigned preprod transaction consuming the existing rehearsal pool
was evaluated on both APIs, then through pycardano's `evaluate_tx_cbor`.
Both returned HTTP 200 and identical `result.EvaluationResult`:

```json
{"spend:0":{"memory":50236,"steps":16213805}}
```

Top-level field names: `type`, `version`, `servicename`, `methodname`, `result`,
`reflection`. Insight charged 7 credits for the raw evaluate request.
The seeded budget was 2,000,000 memory / 900,000,000 steps, exceeding measured
needs. Unsigned body hash:
`1b914ad9e5b8afdd1ab8134dfdd79935515407fd37d12ed8211619ba468dbe2b`.
No signature or broadcast was performed.

**G1 is only partially satisfied.** The existing fixture's deadline is
1780095117182 ms (May 2026), so this probe used `AdminWithdraw`. This proves
the success-response shape, SDK compatibility and exact evaluator parity;
it does not prove a new ProcessSurrender, co-sign, submission, confirmation,
or credit cost per complete surrender. Those remain mandatory before cutover.
Live holder/supply snapshots matched as sets: preprod 4 holders,
1,050,000,000,000,000 base units; mainnet 210 holders, 1,000,000,000,000,000
base units. Pool history counts matched at 53 / 279 respectively (both below
100,000 rows). `railway whoami` returned Unauthorized; no Railway inventory
or changes were possible.

The scratch evidence is `/tmp/cmatra-g1/{evaluate.json,unsigned.cbor,evaluate.py}`.

The registry sweep found the existing `surrender-api` primitive and its
BlockfrostClient/pycardano adapter; no new provider SDK is needed. Research:
`site.insight.iagon.com evaluate Blockfrost API` and
`site.github.com Python-Cardano pycardano blockfrost evaluate_tx_cbor`.
[Insight documentation](https://docs.iagon.com/products/insight) describes the
compatible surface. The installed pycardano 0.18 implementation consumes
`result.EvaluationResult`, as confirmed by the live probe, rather than the
Ogmios v6 error shape. Keep its existing fail-closed evaluate behavior.

## Preprod deployment after review and merge

1. Record the reviewed merge SHA. Create a dedicated release under
   `~/.local/share/cmatra-preprod/releases/<SHA>` using `git archive <SHA>`.
   Create its venv, install `services/deploy/requirements-cosigner.txt` with
   `pip install --require-hashes -r`, then `nice -n 10 .venv/bin/pip install -e '.[dev]'`.
   Keep the currently running checkout untouched.
2. Before stopping anything, capture the exact current `/proc/<pid>/environ`,
   `/proc/<pid>/cmdline`, cwd and interpreter paths for the processes listening
   on 8430 and 8431 into a **0700** rollback directory, files **0600**. Never
   print their values. At inspection the PIDs were 425479 and 373145, cwd
   `/home/deci/work/matra-token-merger`, venv `.venv-preprod`. Re-resolve PIDs
   from sockets and verify the uvicorn module and ports; never signal a stale
   recorded PID. Preserve the old env.local and fixture directory privately.
3. A fresh fixture is required: the May fixture is expired. From the isolated
   release, run `NETWORK=preprod CHAIN_API_KEY_FILE=$HOME/.secrets/iagon/insights-api-key
   .venv/bin/python -m scripts.preprod_setup --funder-skey
   /home/deci/work/matra-token-merger/audit_pack/preprod_chain_rehearsal/keys/admin_1.skey
   --work-dir <new-private-fixture> --deadline-hours 168 --nft-count 40` as one
   shell command. This step broadcasts **preprod** setup transactions, so it
   belongs to the post-review deployment. Never reuse the expired setup state.
4. The setup script predates the hardened co-signer. Its generated env alone
   is insufficient: create a preprod redemption pin with `scripts.pin_redemption`
   for the fresh NFTs and initialize **separate** primary/co-signer ledgers
   with `services.redemption_ledger.create_ledger`. Set both services' identical
   `REDEMPTION_PIN_PATH`, `RATE_TABLE_PATH`, `SURRENDER_SCRIPT_ADDRESS`,
   `QUARANTINE_ADDRESS`, `CMATRA_POLICY_HEX`, `CMATRA_ASSET_HEX`,
   `SURRENDER_DEADLINE_POSIX_MS`, `MAX_CMATRA_PER_TX`, `MAX_CMATRA_PER_DAY`.
   Set `COSIGNER_PRIMARY_ADMIN_PKH` to the fresh admin_1 hash and
   `COSIGNER_LEDGER_PATH` to the co-signer ledger; `SURRENDER_LEDGER_PATH`
   names the primary ledger. Preserve the ledger files across service restarts.
   Check the pinned NFT policy agrees with the generated `FLUX_PASS_POLICY`.
5. Write private configuration to `~/.config/cmatra-preprod/{surrender,cosigner}.env`
   (directory 0700, files 0600), using the fresh fixture paths. Set
   `COSIGNER_URL=http://127.0.0.1:8431`; retain shared secrets in those private
   files. Remove `CHAIN_API_*` and `BLOCKFROST_*` entries from these env files
   so they cannot override the units' provider settings. The supplied user
   units read `~/.secrets/iagon/insights-api-key` directly in Python.
6. Symlink `~/.local/share/cmatra-preprod/current` to the reviewed release.
   Copy `services/deploy/preprod/*.service` to `~/.config/systemd/user/`.
   Run `systemd-analyze --user verify` on the units, then
   `systemctl --user daemon-reload`. Stop only the re-verified old preprod
   processes. Run `systemctl --user enable --now cmatra-cosigner-preprod
   cmatra-surrender-preprod`. Verify `/health` on 8431 and 8430. User units
   require an active user manager; arrange linger with the operator if reboot
   persistence is required (`loginctl show-user deci -p Linger`).
7. Run `scripts.preprod_chain_rehearsal` against 8430 with the fresh wallet,
   taking the API secret from the private config **in-process**, never argv.
   Call its `main(argv)` from a private Python launcher. Include both `abce`
   and `depth-cap` modes; use restart command
   `systemctl --user restart cmatra-surrender-preprod` for the restart check.
   Compare the exact unsigned ProcessSurrender CBOR on both evaluate APIs
   before signing. Capture each request's `X-Credits-Charged` header and sum
   all reads/evaluate/submit attributable to that surrender.
8. Require an actual co-signed, confirmed transaction, the resulting
   `chain_check.confirm_on_chain` verdict, pending-tip eviction evidence, and
   zero ledger mismatches. Record tx hash and block. Only then mark G1 complete.

Preprod rollback: stop/disable only the two new user units; restore the old
private env/config and launch each saved argv/cwd/environment through a private
Python launcher using `subprocess.Popen`, never by printing or shell-evaluating
secrets. Restore the old checkout and state unchanged. The old fixture is
expired; restoring process availability does not restore surrender capability.

## Mainnet cutover runbook — operator only, G2-gated

Read-only inspection confirmed:

| Host | Unit | Working directory | Env file | Port |
|---|---|---|---|---|
| Gemtek | surrender-api.service | /home/deci/surrender-api | env.surrender-api | 127.0.0.1:8420 |
| Node-3 | cmatra-cosigner.service | /home/deci/cmatra-cosigner | env.cosigner | 192.168.0.133:8421 |

1. Require the reviewed merge, complete preprod G1, G2 cMATRA account/key,
   and deci's explicit authorization. Insight serializes evaluations per
   account, so another key under the same shared account does not by itself
   establish isolation. Confirm the account is isolated or add/test bounded
   Retry-After handling before enabling shared-account traffic.
2. Export the reviewed source into an isolated staging directory. Snapshot
   both deployed directories and unit files into task-unique 0700 backup
   directories; use SQLite's backup API for each ledger while services run.
   Record file hashes and current unit MainPID/WorkingDirectory. Keep signing
   keys and backups private. Reconcile deployed source differences before
   replacing files; do not overwrite env, keys, audit packs or ledgers.
3. Deliver the dedicated cMATRA key as
   `/home/deci/.secrets/iagon/cmatra-insight.key` on each host, directory 0700,
   file 0600 owned by deci. Copy to Node-3 with SSH stdin, never command-line
   contents. Compare only sha256 prefixes in-process. Do not use the shared
   dev key for this production cutover.
4. Replay `scripts/fetch_cosign_golden.py` against Insight (read-only), then the
   existing golden verdict tests: identical approvals/rejections are required.
   Confirm the Railway app named `cosigner-production` belongs to cMATRA
   before changing it. Its public SaturnSwap-looking hostname is not evidence
   of cMATRA ownership. If dashboard inventory is unavailable, this item stays
   unverified and the owning session must resolve it.
5. On Node-3 first, stage reviewed `tools/` and `services/` modules into
   `/home/deci/cmatra-cosigner`, preserving all non-source files. In
   `env.cosigner` set `CHAIN_API_BASE_URL=https://mainnet.insight.iagon.com/api/v0`
   and `CHAIN_API_KEY_FILE=/home/deci/.secrets/iagon/cmatra-insight.key` by
   parsing/writing the file in-process. Remove stale `CHAIN_API_*` duplicates;
   preserve policy, caps, pin, keys, ledger and API authentication settings.
   After the operator's go: `sudo systemctl restart cmatra-cosigner.service`.
   Check `/health` at 192.168.0.133:8421 and inspect sanitized logs.
6. On Gemtek, repeat source staging and the two provider settings for
   `/home/deci/surrender-api/env.surrender-api`. After the operator's go:
   `sudo systemctl restart surrender-api.service`. Check localhost:8420/health.
   Compare the same unsigned mainnet surrender on both evaluators before the
   claimant signs. Mainnet signing/broadcast belongs to deci at his terminal.
7. Record the next real surrender's co-sign, chain confirmation, pool balance,
   pending-tip eviction and total charged credits. Monitor 24 hours with zero
   provider 4xx/5xx and no changed signer verdicts. Keep Blockfrost projects
   alive for seven request-free days; deci handles revocation in the dashboard.

Mainnet rollback: operator restores the backed-up source and env on the affected
host, keeps the **current** ledgers (never roll back approvals), and restarts
only that cMATRA unit. Confirm health and a read-only pool query. Do not restore
a stale ledger backup over approvals recorded since cutover.

No project is freed by this PR. Pending consumers are preprod key fingerprint
3b45f357, Node-3 828b5087, and Gemtek mainnet 86494380. Other users of those
projects must migrate before revocation.
