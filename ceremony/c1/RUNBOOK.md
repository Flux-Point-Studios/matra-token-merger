# C1: move the cMATRA merger pool to fresh admin keys

Ceremony runbook, prepared 2026-11-04. Nothing in `ceremony/c1/` has submitted a transaction, touched a real key, or started a service. Every step marked **deci** needs deci's direct go in his own session. This directory is one-off ceremony material; the reusable parts are `tools/pool_migration.py`, `services/redemption_ledger.py` and `services/deploy/`.

Target: run C1 before the reopen (about 11-10). On 11-03 deci decided to move the pool to fresh keys first, as a custody precaution. The host findings behind section 2 come from the private host audit of 11-04, which is not in this repository.

## 1. The transaction

**Path: ProcessSurrender, before the old deadline.** Before its deadline, the live validator e1a181b2 checks only that both admin keys are among the transaction's required signers and that the validity interval ends before 2026-11-29 00:00Z (`claim_validator.ak:62-75`). It does not look at outputs. So the two old admins can move the pool anywhere with redeemer `ProcessSurrender` (`d87980`), and must do so before the deadline. `AdminWithdraw` (`d87a80`) only validates after the deadline. The simulation evaluates both against the live bytes (section 9).

**Destination: the same validator, re-applied.** `onchain/claim_validator/apply_params.sh NEW_ADMIN_1 NEW_ADMIN_2 1798502400000 OUT.json` applies the new parameters to the committed blueprint under aiken v1.1.21:

- It re-applies the old parameters to e1a181b2, byte-identical to the 588-byte script on chain.
- CI proves this for every line of `onchain/claim_validator/deployed_pools.txt`.
- The new pool is an enterprise script address (header `0x71`), like the old one.

| | C1a: test slice | C1b: the rest | C1-direct: race form |
|---|---|---|---|
| Pool input | d3a1e632…#1 | C1a#1 (the old-pool continuation) | d3a1e632…#1 |
| Fee and collateral input | F (new admin_1 address) | C1a#2 (change of C1a) | F |
| Output 0 | new pool: 1 cMATRA, 1.090430 ADA (minimum), inline `D87980` | new pool: 301,731,978.205950 cMATRA, 1.5 ADA, `D87980` | new pool: 301,731,979.205950 cMATRA, 1.5 ADA, `D87980` |
| Output 1 | old pool: 301,731,978.205950 cMATRA, 1.5 ADA, `D87980` | change to the fee address | change to the fee address |
| Output 2 | change to the fee address | | |
| Required signers | old f1eee828 + old 77fdb621 + new admin_1 + new admin_2 | same | same |
| Redeemer | ProcessSurrender on the pool input, budget 2,000,000 mem / 900,000,000 steps | same | same |
| Validity | at most 6 h, ttl at or before slot 204,344,108 | same | same |
| Fee (simulated) | 0.416203 ADA | 0.412111 ADA | 0.412111 ADA |
| Signed size | 1,603 B | 1,510 B | 1,510 B |

Rules that hold for all three:

- **The tool derives the destination from the live script.** It takes the live e1a181b2 bytes and swaps the three parameter constants for the plan's new values: `581c`+admin_1 at byte 509, `581c`+admin_2 at 543, and `1b`+the 8-byte deadline at 577. Each must occur exactly once. The tool then refuses any plan whose `new_script_hash` is not the hash of the result.
  - The swap reproduces aiken v1.1.21 `apply_params.sh` byte for byte. The tests pin aiken's output for a fixture pair (21b85d9b), the same pair swapped (abbb243e), the same pair one day later (f26ee4c7) and a second pair (d521774f). The simulation applies its own pair with `apply_params.sh` at run time and compares whole scripts (`derived_destination_equals_aiken_apply`).
  - Order matters: `new_admins[0]` is applied as admin_1.
- **Both new keys are required signers.** The old validator needs only the old pair. Adding the new pair means the ledger refuses the move unless both new keys sign this exact transaction.
- **Together, the cMATRA can only land in a pool locked by the two keys that signed, with the plan's deadline.**
- **What no tool can check: whether those two keys are the ones on the key hosts.** Someone who edits the plan on the coordinator could name two other admins together with their matching hash, and `check` would pass it. Steps 4 and 6 close this gap: each signer compares the report with values that never passed through the coordinator, namely the PKHs from P3 and the derivations from P4.
- **Every pool output carries inline datum `D87980`.** The validator's `expect Some(_) = datum` locks any output without it forever; that is the 05-18 722.5M lesson. The simulation proves it again: `N_pool_output_without_datum_is_locked`.
- The fee input pays the fee and the collateral. Total collateral is ceil(1.5 × fee), about 0.62 ADA, and the tool refuses more than 5 ADA. The collateral return and the change go back to the fee address.
- No metadata, mint, certificates, withdrawals or reference inputs. The old script travels inline in the witness set, as every mainnet surrender does today.

**The new pool's compiled deadline is 2026-12-29 00:00Z (1,798,502,400,000 ms, slot 206,936,109).**

- **Why:** D7 approved a 30-day late window. With the late close compiled in, the late window needs no second ceremony (no AdminWithdraw into a separate late pool).
- **It costs no security.** Before its deadline the pool is already fully at the two admins' discretion. The deadline only decides which redeemer they must use.
- **The public window still closes on 11-29 00:00Z, enforced by the services.** Both signers keep `SURRENDER_DEADLINE_POSIX_MS=1795910400000` until then. The simulation shows the co-sign policy refuses a late claim under that setting and approves it once the setting is 1798502400000 (`cosign_late_claim_*`).
- **The public note must change.** It currently says the window "closes 2026-11-29 00:00 UTC as compiled into the contract". It should say: claims close 11-29 00:00 UTC; a late-claim window runs to 12-29 00:00 UTC, which is the deadline compiled into the pool contract.
- **AdminWithdraw of whatever remains** becomes possible from 12-29 00:00Z, by the two new keys only.

## 2. Where the new keys live

The separation that matters: compromising one host must never give both new keys. The private host audit (finding 8.2) found that, today, one compromised key host can reach the other. The conditions below close that before any new key exists.

**Each signer runs as its own user.** `services/deploy/install-signer.sh` lays a signer out from one commit and installs its unit from `services/deploy/systemd/`; it never enables or starts it.

| | Server A: new admin_1 | signer-b: new admin_2 |
|---|---|---|
| Unit | `surrender-api.service`, `User=cmatra-a1` | `cmatra-cosigner.service`, `User=cmatra-a2` |
| Code | `/opt/cmatra-a1/app` → `app.<commit>`: `git archive` of the commit and a `.venv` from the hash-pinned lock, root-owned | `/opt/cmatra-a2/app`, the same |
| Env | `/etc/cmatra-a1/env.surrender-api`, 0600 root in a 0700 root directory. systemd reads it; the service user cannot | `/etc/cmatra-a2/env.cosigner`, the same |
| Key | `/var/lib/cmatra-a1/keys/admin_1.skey`, 0400 cmatra-a1, in 0700 directories | `/var/lib/cmatra-a2/keys/admin_2.skey`, 0400 cmatra-a2 |
| Ledger | `/var/lib/cmatra-a1/ledger/surrender-ledger.sqlite3`, 0600 | `/var/lib/cmatra-a2/ledger/cosigned.sqlite3`, 0600 |
| Pager outbox | `/var/lib/cmatra-a1/ledger/pager-outbox.sqlite3`, 0600, made by the service at its first start | `/var/lib/cmatra-a2/ledger/pager-outbox.sqlite3`, the same |
| Other files the env names | `/var/lib/cmatra-a1/claim_validator.plutus.json` (the applied blueprint); a chain credential file, if the env uses one | `/var/lib/cmatra-a2/chain-api.key`, signer-b's own chain credential, unless `BLOCKFROST_PROJECT_ID` names its own project |

- Both units run with `ProtectHome=yes` and `ProtectSystem=strict`. `ReadWritePaths` names only the ledger directory, which also holds the pager's outbox, so the service cannot change its code, its key, its blueprint or anything else. Under the unit, an outbox anywhere else cannot be created, and the signer would not start. `systemd-analyze security` rates each unit 1.5 (OK), against 8.4 (EXPOSED) for the current surrender-api unit.
- `sudo services/deploy/install-signer.sh ROLE --check` exits non-zero unless all of these hold:
  - every path, mode and owner above;
  - the env points its key and ledger into the layout, puts the pager's outbox in the ledger directory as a file of its own, names the pager's webhook, and points nothing under a home directory;
  - the service user cannot read its own env file;
  - the installed unit and release are the checkout's.
- CI runs the install as root in a throwaway container, then runs blocks K2 and W1 below as the service users and starts both services dark under their users (`tests/signer_layout_root.sh`).

**New admin_1: Server A (Gemtek)**, where surrender-api signs, under `cmatra-a1`: no login shell, no sudo.

Conditions before keygen:

- **A1.** `sudo services/deploy/install-signer.sh a1 --check` passes on Server A after M1, and the P9 dark start succeeded.
- **A2.** auditd watches `/var/lib/cmatra-a1/keys` (`-w … -p rwa`) and ships its logs off-host. A read by anything other than the surrender-api process pages.
- **A3.** Every cross-host SSH key that Server A accepts is restricted with `from=`, `restrict` and `command=` (audit item H2).
- **A4.** No CI agent runs on Server A (checked 11-04: none).
- **A5.** `PasswordAuthentication no` (a Phase 0 item).
- **A6.** Agent sessions on Server A cannot read `/var/lib/cmatra-a1` or `/etc/cmatra-a1`. Either the operator account's sudo is narrowed, or at least the agent harness denies both paths. A dedicated user keeps the key from the service's neighbours, not from an account with passwordless root.

Residual risk: any root on Server A still reads admin_1. That is acceptable only because admin_2 sits on a host that Server A has no shell on.

**New admin_2: the recommended host is a freshly installed, single-purpose signer ("signer-b").** It is the audit's zero-residual option.

- It runs only the co-signer, as `cmatra-a2` (`install-signer.sh a2`).
- It accepts SSH only from deci's clean device, never from Server A.
- The firewall allows 8421/tcp from Server A's address only.
- No docker, no CI agent, no validator.
- auditd watches `/var/lib/cmatra-a2/keys`, with logs shipped off-host.
- **It has its own view of the chain.** `env.cosigner` names signer-b's own credential:
  - either `BLOCKFROST_PROJECT_ID` of its own Blockfrost project;
  - or `CHAIN_API_KEY_FILE=/var/lib/cmatra-a2/chain-api.key` (0400 cmatra-a2), holding an Insight key no other host uses.

  It never uses Server A's local proxy. The co-signer's unspent, supply and cooling-off checks are only independent if its chain view is. Egress from signer-b is limited to that provider's API host, the pager's webhook host `discord.com` on 443, DNS and NTP. The pager fails closed: a co-signer whose pages cannot reach Discord stops signing `PAGER_GRACE_S` after its first claim.

This makes the dual-signer design real: Server A can reach signer-b only through the co-sign API, which signs only what `cosign_policy` approves.

**Fallback: Node-3**, which the private audit rates usable only once its items H1-H7 are done. Use it only if signer-b cannot be ready by the Phase 2 dark deploy.

- Every H item is then a hard gate, each with the check command the audit gives.
- They cover cross-host SSH trust, password logins and forwarding, the CI agent, console sessions, read-auditing, and a dedicated non-login key user with narrowed operator sudo.
- That last item conflicts with Node-3's validator, db-sync and CI duties, which is why Node-3 is only the fallback.
- On Node-3 the layout is the same: `install-signer.sh a2`, with M2 run there.

**Key generation**, the same on each host. deci does it at the console, never from an agent session.

1. Install the pinned `cardano-cli` 11.0.0.0 where the key user can run it:

   ```bash
   sudo services/deploy/install-cardano-cli.sh /opt/cardano-cli-11.0.0.0
   ```

   - It refuses a tarball other than sha256 `c37b26be04106ecd2ba88841e64d3b7ecdc80d838055102323691b087f7255b1`, and a binary other than `b55687eb503a26a96dd436cdb492cbee15a945588c346b81dc4ca86f3c8a1066`.
   - That binary is the one the old key hosts run.
2. Make the key as its service user with block K2, after C0 (section 4) has set `CLI`.
   - On Server A, first set `U=cmatra-a1 K=admin_1`; on signer-b, `U=cmatra-a2 K=admin_2`.
   - K2 refuses to run if the key file already exists, because `cardano-cli latest address key-gen` silently overwrites a key its user can write.
   - The refusal exits by itself: a shell that tests a subshell's status, such as `if ( … )` or `( … ) || …`, turns `set -e` off inside it.
   - The PKH it prints is the only output, and with the vkey the only thing that leaves the host.

   ```bash
   # K2: the new key, as its service user (set U and K first)
   ( set -euo pipefail
     cd /
     sudo -u "$U" test ! -e "/var/lib/$U/keys/$K.skey" || { echo "K2: $K.skey exists; a key is made once" >&2; exit 1; }
     sudo -u "$U" "$CLI" latest address key-gen --signing-key-file "/var/lib/$U/keys/$K.skey" --verification-key-file "/var/lib/$U/keys/$K.vkey"
     sudo chmod 0400 "/var/lib/$U/keys/$K.skey"
     echo "K2: $K PKH $(sudo -u "$U" "$CLI" latest address key-hash --payment-verification-key-file "/var/lib/$U/keys/$K.vkey")" )
   ```

3. **Backup: each key goes to its own new USB stick with its own passphrase.**
   - The 05-18 backups shared one passphrase for both keys; that is not repeated.
   - Stream the key out as root (`sudo cat …`) into `gpg --symmetric --cipher-algo AES256 --s2k-mode 3 --s2k-count 65011712 --passphrase-fd 0`.
   - Round-trip-verify the decrypt by comparing sha256 values as booleans, never printing them.
   - The passphrase goes on paper twice, stored in separate places.
4. Record the vkey sha256 and the skey sha256 fingerprints for later integrity checks (compared as booleans only).

**The old keys stay put** until all three hold:

- (a) C1b has 30 or more confirmations;
- (b) Phase 0 has moved the admin_1 reserve (233,260,862.570231 cMATRA, which old admin_1 alone controls) to the D4 multisig;
- (c) the token-registry question in section 7 is settled.

After C1 the old keys have no power over the pool: `N_old_admins_cannot_spend_new_pool` and `N_admin_withdraw_old_admins_after_12_29` both FAIL as expected.

### One-time move of each signer to its own user (Phase 2, before P9)

Both services are stopped; the merger has been paused since 11-02. deci runs these at each host's console from a checkout of the merge commit (P1).

```bash
# M1: surrender-api to cmatra-a1 (Server A, once, surrender-api stopped)
sudo services/deploy/install-signer.sh a1 --ledger-from /home/deci/surrender-api/data/surrender-ledger.sqlite3
sudo chattr +i /home/deci/surrender-api/data/surrender-ledger.sqlite3
```

M1 replaces the old `surrender-api.service`, which ran as deci from `/home/deci/surrender-api`, with the new unit of the same name. The old layout therefore cannot be started by mistake. The ledger moves with `python -m services.redemption_ledger copy`:

- It refuses to run while a journal sits beside the ledger, which means a write is in flight or was interrupted.
- It copies the file byte for byte under a shared lock, so no writer can change it meanwhile.
- It proves the copy before touching the source. The two sha256 values must match, SQLite's `integrity_check` must say `ok`, and every approvals row must be the same. A copy that fails is removed.
- Only then does it make the old file 0400; `chattr +i` makes it immutable even to root until the ceremony completes.
- It prints the proof. deci writes the sha256, the approvals count and the `approvals_digest` in the ceremony log.

Then, on Server A:

1. `sudo install -m 0600 -o root -g root /home/deci/surrender-api/env.surrender-api /etc/cmatra-a1/env.surrender-api`, then `sudoedit` it to the section 6 values. The edit points `ADMIN_SKEY_PATH`, `SURRENDER_LEDGER_PATH`, `PAGER_OUTBOX_PATH`, `BLUEPRINT_PATH` and any credential file into `/var/lib/cmatra-a1`. Nothing may stay under `/home`: `ProtectHome=yes` hides it, and `--check` refuses it. The old pager outbox is not moved, so the old service's `/health` must have shown `"pages_owed": 0` before it stopped; otherwise its owed pages never reach Discord.
2. Place the applied blueprint. Run `onchain/claim_validator/apply_params.sh <P3 admin_1> <P3 admin_2> 1798502400000 new-pool.json`, check that its `script_hash` equals the P4 paper, then `sudo install -m 0400 -o cmatra-a1 -g cmatra-a1 new-pool.json /var/lib/cmatra-a1/claim_validator.plutus.json`.
3. After K2 has made the key: `sudo services/deploy/install-signer.sh a1 --check`. Attach its output to the ceremony log; this is A1.

```bash
# M2: the co-signer to cmatra-a2 (signer-b, once; Node-3 if it is the fallback)
sudo services/deploy/install-signer.sh a2 --ledger-from ~/cosigned.from-old-host.sqlite3
```

**The co-signer's ledger travels from the old admin_2 host.**

1. On that host, stop its co-signer once its `/health` shows `"pages_owed": 0` (its pager outbox is not moved either). `chmod 0400` its ledger (the old `COSIGNER_LEDGER_PATH`), and print `sha256sum` and `python -m services.redemption_ledger digest` of it.
2. Move the file to signer-b by USB or scp from deci's clean device. It holds approvals, no key.
3. On signer-b, M2's copy prints its own sha256 and digest. deci compares them on paper:
   - the sha256 must equal the old host's;
   - the `approvals_digest` must equal the old host's and M1's.

   The digest covers every approval column except `signed_at`, so two signers that approved the same surrenders agree. A different digest stops the ceremony until the two ledgers are reconciled, as Phase 2 requires.
4. Never run `redemption_ledger init` for a signer that has signed. A fresh ledger resets every limit.
5. `sudo chattr +i` the old file on the old host.

Then write `/etc/cmatra-a2/env.cosigner` (0600 root; section 6, with signer-b's own chain credential), make the key with K2, and run `sudo services/deploy/install-signer.sh a2 --check`.

**After the ceremony completes** (C1b at 30 confirmations and the test claim done):

- Run `chattr -i` on the old ledgers, record their sha256 in the ceremony log, archive them, then remove the old directories.
- Shred the old env files, which hold the retired secrets.

## 3. Before the ceremony

| # | Gate | Check |
|---|---|---|
| P1 | PR #45 (Phase 1: deny-list, cooling-off, caps, T2 limit, pager) merged, then PR #44, which is built on it; CI green on main at PR #44's merge commit; both signers installed at that commit | `cat /opt/cmatra-a1/app/RELEASE` on Server A and `/opt/cmatra-a2/app/RELEASE` on signer-b both equal PR #44's merge commit, the one whose CI ran the C1 simulation and the policy together; `--check` passes on each |
| P2 | Host conditions done (A1-A6; H1-H7 if Node-3) | each item's command, with output attached to the ceremony log; for A1 and signer-b, `install-signer.sh … --check` |
| P3 | Both new keys generated (K2) and backed up | deci reads each PKH off its own key host's console (K2's last line) and writes it on paper as new admin_1 and new admin_2. These two lines are the reference for steps 4 and 6 |
| P4 | New script hash agreed | Two derivations, on **two different hosts, neither of them the coordinator** (for example deci's clean device and signer-b). Each host types in the P3 paper PKHs itself, not from any file that came from the coordinator. Each runs `apply_params.sh NEW_ADMIN_1 NEW_ADMIN_2 1798502400000 out.json` under aiken v1.1.21 (binary sha256 `07f3f927f1a8c71fc61273fdff850d9b98edb28dde207c8a32585991f2c88e79`) at the merge commit. deci writes both hosts' `script_hash` and `address` on paper; they must agree |
| P5 | `deployed_pools.txt` line for the new pool merged | the line equals the P3 PKHs, 1798502400000 and the P4 hash; its CI log shows `PASS: <new hash> = apply(…)` |
| P6 | Fee input F funded: 20 ADA from the fresh FPS wallet (f735882b) to new admin_1's enterprise address, one output | Insight `/api/v0/txs/<F>/utxos`: pure ADA, unspent |
| P7 | Pool still where it was | Insight `/api/v0/txs/d3a1e632…/utxos`: output 1 has `consumed_by_tx` null, 301731979205950 cMATRA, `d87980`. Cross-check with a second provider (Kupo or Koios) |
| P8 | custody-watch pre-announced on shared ops | "C1a/C1b will page CRITICAL as non-surrender pool spends; expected" |
| P9 | **Dark start and stop of both services on the new config, before C1a** | below |
| P10 | Witness logistics agreed: who is at which console, how files move (scp or USB; witness files hold no secret) | |
| P11 | Test claimant T made and funded, at least 72 h before step 13 | deci runs C0 and the P11 block below on Server A. **deci** then submits one transaction from the fresh wallet to `T.addr` with two outputs: 2 AGENT with 5 ADA, and 5 ADA alone (surrender-api takes an ADA-only UTxO of at least 5 ADA as collateral). The fresh wallet signs it with its offline command-line signer, never in a browser wallet. Insight `/api/v0/txs/<T-fund>/utxos`: both outputs at T, unspent. `/api/v0/txs/<T-fund>`: its slot is at least 259,200 before step 13, or the cooling-off refuses the claim |
| P12 | Only if the flux1 page replaces the API at step 13: D13 done | Vercel tokens and every GitHub token that can push to flux1 revoked; Vercel deployments and env audited since 10-28 |
| P13 | Only if the flux1 page is used: a fresh bundle proof | the Phase 2 proof of the `/matra-merger` production bundle (proxy URL correct, no foreign addresses), taken within the hour before step 13. The browser wallet holds only test assets, never the fresh wallet |

**P9: dark start.** Both services start on the section 6 config: the new pool address, the applied blueprint, the new keys, and `ALLOWED_USER_ADDRESSES` set to T alone. surrender-api seeds the pool lazily, on the first build. So with no UTxO at the new address yet, a start proves five things before C1a moves anything:

- the blueprint matches the address;
- each key loads under its user;
- both ledgers, and the pager outboxes beside them, open read-write;
- each signer loaded the release's deny-list and pages nothing yet;
- the signers reach each other.

Nothing can be built meanwhile: the new address holds nothing, and only T is admitted.

1. signer-b: `sudo systemctl start cmatra-cosigner`.
   - `journalctl -u cmatra-cosigner -b` shows `Co-signer ready: PKH=` followed by the P3 paper's new admin_2.
   - It shows `deny-list ` followed by the `sha256sum` of `/opt/cmatra-a2/app/audit_pack/2026-11-01/denylist.json`.
   - `curl -s http://127.0.0.1:8421/health` answers `{"status":"ok","pages_owed":0,"oldest_page_owed_s":null,"signing_paused":false,"webhook_refused":null,"key_loaded":true,"pkh":…}`, with the first 16 hex digits of that PKH.
2. Server A: `curl -s http://<signer-b>:8421/health` answers, so the firewall admits Server A.
3. Server A: `sudo systemctl start surrender-api`.
   - `journalctl -u surrender-api -b` shows `Loaded script from blueprint` and no `does not hash` error.
   - It shows `Admin key loaded: PKH=` followed by the P3 paper's new admin_1, and `Co-signer PKH:` followed by the first 16 hex digits of new admin_2.
   - It shows the same `deny-list ` line as the co-signer, from `/opt/cmatra-a1/app/audit_pack/2026-11-01/denylist.json`.
   - `curl -s http://127.0.0.1:8420/health` answers `{"status":"ok","pages_owed":0,"oldest_page_owed_s":null,"signing_paused":false,"webhook_refused":null,"rate_table_loaded":true,"script_loaded":true,"admin_key_loaded":true,"configured":true}`.
   - `sudo services/deploy/install-signer.sh a1 --check` now also lists the outbox the service made, 0600 cmatra-a1; the same on signer-b for a2.
4. `sudo systemctl stop surrender-api` on Server A and `sudo systemctl stop cmatra-cosigner` on signer-b.

Any other answer is fixed now, while the whole pool still sits at the old address under the old pair, and not between C1a and C1b.

Why the fee input sits at new admin_1's address: the old admin_1 address holds only the reserve UTxO d2ba7412#1, and the old admin_2 address is empty (Insight, 11-04). The new admin_1 witness is required anyway, so the fee needs no extra key, and the move stays independent of Phase 0.

**Why a dedicated test claimant.** The fresh wallet holds far more than the test needs. Claiming from it through the page would mean importing its keys into a browser wallet, and signing a transaction served by a frontend whose deploy credentials are treated as exposed (D13). T holds 10 ADA and 2 AGENT, so a bad transaction or a leaked T key costs at most that. T is a cardano-cli key that signs only after step 13b has checked the transaction. The fresh wallet signs only the plain transfer that funds T. T needs no backup.

```bash
# P11: make T (Server A console, in ~/c1-ceremony/claimant/, once)
( set -euo pipefail
  test "$(sha256sum < "$CLI" | cut -c1-64)" = b55687eb503a26a96dd436cdb492cbee15a945588c346b81dc4ca86f3c8a1066
  "$CLI" latest address key-gen --signing-key-file T.skey --verification-key-file T.vkey
  chmod 0400 T.skey
  "$CLI" latest address build --payment-verification-key-file T.vkey --mainnet --out-file T.addr
  echo "P11: T is $(cat T.addr)" )
```

The cooling-off (L6) counts from the slot at which the AGENT reached T's payment key, so for an 11-10 ceremony T is funded by 11-06.

## 4. Ceremony: C1a, then a test claim, then C1b

The coordinator is Server A, working from the installed release (P1) and its venv. Every file name below lives in a fresh `~/c1-ceremony/` directory. Every shell on Server A starts with C0:

```bash
# C0: the ceremony shell; run it first in every new shell on Server A
CLI=/opt/cardano-cli-11.0.0.0/cardano-cli
APP=/opt/cmatra-a1/app
PY=/opt/cmatra-a1/app/.venv/bin/python
API=http://127.0.0.1:8420
API_ENV=/etc/cmatra-a1/env.surrender-api
export PYTHONPATH="$APP" CHAIN_API_BASE_URL=http://127.0.0.1:18777/api/v0
test "$(sha256sum < "$CLI" | cut -c1-64)" = b55687eb503a26a96dd436cdb492cbee15a945588c346b81dc4ca86f3c8a1066 \
  && echo "C0: cardano-cli verified" || echo "C0: $CLI is not the pinned cardano-cli; stop"
```

Chain reads go through the local Insight proxy, which injects the key.

**C1a**

1. **Snapshot.** `"$PY" -m tools.pool_migration snapshot --pool-ref d3a1e632424794bed36d7062b4c09c5fe547fa7a1addbd2d45f9b84df773c733#1 --fee-ref <F>#0 --out c1a.snapshot.json`.
   - It prints the file's sha256.
   - It refuses an input that is already spent or does not hash to its id.
2. **Plan.** `cp "$APP/ceremony/c1/plan-C1a.template.json" c1a.plan.json` (from `ceremony/c1/plan-C1a.template.json`). Fill in:
   - the fee ref;
   - the two new PKHs, in P3/P4 order (`new_admins[0]` = new admin_1);
   - the P4 script hash;
   - `validity_start` (tip slot minus 120) and `ttl` (start plus 10,800).

   The template's placeholders cannot load, so a half-filled plan fails. `build` refuses a hash that is not the old script re-applied to these PKHs and deadline.
3. **Build.** `"$PY" -m tools.pool_migration build --snapshot c1a.snapshot.json --plan c1a.plan.json --out c1a.unsigned.tx` writes the unsigned tx and prints the check report. The report gives:
   - the txid, the moved and the left cMATRA, the new pool address and script hash;
   - `old_admins`, `new_admins`, `new_deadline_ms` and `new_deadline_utc`;
   - the fee, the minimum fee, the signers and the ttl in UTC.
4. **Independent check, on a second machine that is not the coordinator** (deci's clean device or signer-b).
   - Use the same commit and compare the three files by sha256.
   - Run `python -m tools.pool_migration check …` and `cardano-cli latest transaction txid --tx-file c1a.unsigned.tx`. Both must give the same txid and the same report as step 3. `cardano-cli debug transaction view --tx-file c1a.unsigned.tx` gives a human-readable view.
   - **Then deci compares the report with the paper, never with the coordinator's files:**

     | Report field | Must equal |
     |---|---|
     | `new_admins` | the two P3 PKHs, in order |
     | `new_pool_address`, `new_script_hash` | this machine's own P4 output, and the `deployed_pools.txt` line merged at P5 |
     | `new_deadline_utc` | `2026-12-29T00:00:00+00:00` |
     | `old_admins` | f1eee828…, 77fdb621… |
     | `pool_input` | d3a1e632…#1 |

   - deci writes the txid on the paper next to these values. Any difference: stop.
5. **Offline evaluation.** `"$PY" "$APP/ceremony/c1/evaluate.py" c1a.snapshot.json c1a.unsigned.tx` (`ceremony/c1/evaluate.py`) must print PASS. It runs aiken v1.1.21, from PATH or `$AIKEN`, against the inputs as the snapshot resolved them.
6. **Witnesses.** deci is at each host. Before every signature:
   - Compare `"$CLI" latest transaction txid --tx-file c1a.unsigned.tx` with the txid on the paper from step 4. The txid commits to the outputs and the required signers, so a match means the report checked in step 4 describes these bytes.
   - Tick off the step 4 report against the paper once more: `new_admins` against the P3 PKHs, and `new_pool_address` against the P4 output and the `deployed_pools.txt` line.
   - On signer-b, which has the repo, run `check` there as well and compare its report.

   Then sign:
   - Server A, old admin_1: `"$CLI" latest transaction witness --tx-body-file c1a.unsigned.tx --signing-key-file /home/deci/cmatra-merger-keys/admin_1.skey --mainnet --out-file c1a.old1.witness`
   - Node-3, old admin_2: the same with `/home/deci/cmatra-merger-keys/admin_2.skey` and `/home/deci/bin/cardano-cli` (the `/usr/local/bin` one is a docker wrapper).
   - Server A, new admin_1, as cmatra-a1, with W1 and `FILE=c1a`.
   - signer-b (or Node-3), new admin_2, as cmatra-a2: W1 with `cmatra-a2`, `admin_2` and `new2` in place of `cmatra-a1`, `admin_1` and `new1`.

   ```bash
   # W1: new admin_1 witnesses $FILE.unsigned.tx as cmatra-a1 (Server A; FILE=c1a, then c1b)
   sudo -u cmatra-a1 sh -c 'cat | "$1" latest transaction witness --tx-body-file /dev/stdin \
     --signing-key-file /var/lib/cmatra-a1/keys/admin_1.skey --mainnet --out-file /dev/stdout | cat' sh "$CLI" \
     < "$FILE.unsigned.tx" > "$FILE.new1.witness"
   ```

   cmatra-a1 cannot open files in deci's home, and cardano-cli opens `/dev/stdin` and `/dev/stdout` by path. That reopens whatever they are with cmatra-a1's permissions. A file in deci's home is refused, and so is a pipe deci's shell or sudo made, since a pipe belongs to its maker with mode 0600. So W1's two `cat`s, running as cmatra-a1, carry the bytes through pipes cmatra-a1 made itself; they read and write the descriptors deci's shell opened, which needs no reopen. The witness is the same as one written to a file, and CI runs W1 as cmatra-a1 from a directory that user cannot enter.
7. **Assemble.** `"$PY" -m tools.pool_migration assemble --snapshot c1a.snapshot.json --plan c1a.plan.json --tx c1a.unsigned.tx --witness c1a.old1.witness --witness c1a.old2.witness --witness c1a.new1.witness --witness c1a.new2.witness --out c1a.signed.tx`.
   - It refuses a missing, foreign, repeated or bad signature, and it re-checks the result.
   - It keeps the body, redeemer and script bytes verbatim, so the txid stays the same. Confirm with `"$CLI" latest transaction txid --tx-file c1a.signed.tx`.
   - `cardano-cli latest transaction assemble` also passes the check in the rehearsal; it is the fallback assembler.
8. **Evaluate the signed tx.** `"$PY" "$APP/ceremony/c1/evaluate.py" c1a.snapshot.json c1a.signed.tx` must print PASS. Optionally run a read-only `evaluateTransaction` on our Ogmios; that evaluates, it does not submit.
9. **Unspent check** right before submit, as in P7, for both inputs.
10. **deci: submit** `c1a.signed.tx`. Record the txid in the ceremony log.
11. **Confirm.** Insight `/api/v0/txs/<C1a>/utxos` shows:
    - output 0 at the new pool address, with 1,000,000 cMATRA and `d87980`;
    - output 1 at the old address, with 301,731,978,205,950 and `d87980`;
    - output 2 as change.

    Wait for 3 confirmations.

**Test claim through the full new-pool claim path.** This is the build, broadcast and spend-back test the 05-18 incident lacked.

12. **deci: start both services again** on the P9 config: `sudo systemctl start cmatra-cosigner` on signer-b, then `sudo systemctl start surrender-api` on Server A.
    - `ALLOWED_USER_ADDRESSES` is T's address (`T.addr`) only, with the deny-list and caps from Phase 1.
    - surrender-api refuses to start unless its blueprint's script hashes to `SURRENDER_SCRIPT_ADDRESS`, so a half-applied config fails at startup; P9 already proved this config.
13. **deci: claim 2 AGENT from T through the API**, on Server A in `~/c1-ceremony/claimant/`, all in one shell started with C0. The flux1 page is not used; P12 and P13 apply only if it ever is. This is a deliberate 2-unit exception to D5, made for the test only.
    - First, deci types the new pool address from the P4 paper into `new-pool.addr`.
    - Expected payout: floor(2 × 449168051406055 / 970355344) = **925,780 base units** (0.925780 cMATRA), taken from the 1 cMATRA slice. The services spend the largest pool UTxO at the configured address, which at this point is the slice.
    - Expected on chain: quarantine receives 2 AGENT and T receives 925,780. The new pool keeps 74,220 base units (with `d87980`) and at least 1.5 ADA: surrender-api floors the continuation's ADA at 1.5, so T pays about 0.41 ADA into the pool. `cosign_policy` caps only lovelace leaving the pool, so it approves. This is the first run of the real `/build-surrender` against a 1 cMATRA slice; the simulation's stand-in serves a transaction the simulation built itself.

```bash
# 13a: build the claim
rm -f claim.*
api() {
  sudo "$PY" -c 'import sys; from dotenv import dotenv_values as v; print("X-API-Secret: " + v(sys.argv[1])["SURRENDER_API_SECRET"])' "$API_ENV" |
    curl -sS --fail-with-body -H @- -H 'Content-Type: application/json' --data-binary @"$2" -o "$3" "$API/$1"
}
jq -n --arg a "$(cat T.addr)" '{user_address: $a, assets: [{asset_key: "AGENT", quantity_base: 2}]}' > claim.request.json
api build-surrender claim.request.json claim.build.json
```

The env file is root's, so the secret is read as root. It goes from the env file into curl's standard input, so it never reaches the screen, the shell history or the process list.

```bash
# 13b: check the claim, then T signs it
( set -euo pipefail
  test "$(sha256sum < "$CLI" | cut -c1-64)" = b55687eb503a26a96dd436cdb492cbee15a945588c346b81dc4ca86f3c8a1066
  jq '{type: "Tx ConwayEra", description: "", cborHex: .tx_cbor_hex}' claim.build.json > claim.unsigned.tx
  test "$("$CLI" latest transaction txid --tx-file claim.unsigned.tx | jq -r .txhash)" = "$(jq -r .tx_hash claim.build.json)"
  "$CLI" debug transaction view --tx-file claim.unsigned.tx > claim.view.json
  jq -e --arg pool "$(cat new-pool.addr)" --arg t "$(cat T.addr)" \
    --arg q addr1wy5gl6nh5rm8f3sgp2ka3mfu5skdt2fqhu0spsxnucesdeqatlhxl \
    --arg cm "policy 7ff33a5565393dc47b48ac47becc12d92c9952e724e8446dfb6adc66" '
    ([.outputs[].address] - [$pool, $q, $t]) == []
    and [.outputs[] | select(.address == $pool) | .datum] == [{constructor: 0, fields: []}]
    and ([.outputs[] | select(.address == $t) | .amount[$cm] // {} | .[]] | add) == 925780
    and .mint == null and .withdrawals == null and .certificates == null
    and (.["return collateral"].address // $t) == $t' claim.view.json
  "$CLI" latest transaction sign --tx-file claim.unsigned.tx --signing-key-file T.skey --mainnet --out-file claim.T.signed.tx
  jq -n --slurpfile b claim.build.json --slurpfile s claim.T.signed.tx \
    '{tx_cbor_hex: $s[0].cborHex, tx_hash: $b[0].tx_hash}' > claim.submit.json
  echo "13b: T signed $(jq -r .tx_hash claim.build.json)" )
```

13b signs only if every check holds:

- the txid cardano-cli computes is the one the service returned;
- every output goes to the new pool, quarantine or T;
- the one new-pool output carries `d87980`;
- T receives exactly 925,780;
- there is no mint, withdrawal or certificate;
- the collateral returns to T.

Otherwise it stops before signing. Its last line is then not `13b: T signed …`, and no `claim.submit.json` exists, so 13c fails with nothing to send.

```bash
# 13c: submit; the service adds both admins' witnesses after its own checks
api submit-surrender claim.submit.json claim.submitted.json
jq . claim.submitted.json
```

/submit-surrender takes the whole T-signed transaction and keeps only T's witness. It drops any witness under a required signer's key and adds the admins' own after the co-sign checks. Do not post the output of `transaction witness` instead: that file holds a bare `[vkey, sig]` pair, which /submit-surrender refuses with 400 (sim `N_bare_cli_witness_refused_by_submit`).

The simulation runs C0, P11 and 13a-13c exactly as written here (section 9). Only C0's settings change, and `sudo` becomes a plain call. The URL points at a stand-in for the two routes, which checks the secret header and uses the service's own request models and witness merge. The merged claim, with the admins' witnesses added, passes the signers' `cosign_policy` (APPROVED 925780), the claimant-signature check and aiken. Served the real d3a1e632 surrender instead, 13b stops before T signs.

The claim at 13c passes inside the pager's grace even when a pager cannot deliver, so only a delivered page shows that both signers can keep signing. The block waits up to the 600 s grace for each signer's `/health`; its last line for each must read `delivered every page`, and otherwise the egress or webhook of that signer is wrong: stop and fix it before C1b.

```bash
# After 13c: both pagers delivered the claim's pages
( set -euo pipefail
  COSIGNER=$(sudo "$PY" -c 'import sys; from dotenv import dotenv_values as v; print(v(sys.argv[1])["COSIGNER_URL"].rstrip("/"))' "$API_ENV")
  for health in "$API/health" "$COSIGNER/health"; do
    for _ in $(seq 40); do
      curl -sS "$health" | jq -e '.pages_owed == 0 and .webhook_refused == null' >/dev/null && break
      sleep 15
    done
    curl -sS "$health" | jq -e '.pages_owed == 0 and .webhook_refused == null' >/dev/null
    echo "13d: $health delivered every page"
  done )
```

14. Stop the services, or keep them dark, until C1b has confirmed.
    - `ALLOWED_USER_ADDRESSES` stays T-only until the Phase 3 reopen empties it.
    - T is never used again. Its leftovers (about 8 ADA and 0.925780 cMATRA) can be swept back with the same CLI signing at any time.

**C1b**

15. Snapshot with `--pool-ref <C1a>#1 --fee-ref <C1a>#2`. The plan comes from `plan-C1b.template.json` (`move: "all"`). Then repeat steps 3-11 with `c1b.*` files, and `FILE=c1b` for W1. Expected output 0: 301,731,978,205,950 cMATRA, 1.5 ADA, `d87980` at the new pool address.

**Abort rules**

| Event | Action |
|---|---|
| `check` refuses, or txids differ between hosts | Stop. Sign nothing. |
| `new_admins`, `new_pool_address` or `new_script_hash` in a report differs from the paper (P3, P4, P5) | Stop. Sign nothing. Treat the coordinator as compromised and rebuild the plan from the paper values on a host that is not the coordinator. |
| P9 fails | Fix the config while the pool is untouched. Never loosen a key's mode or owner to make a start work: `--check` names what is wrong. |
| The ttl passes before submit | Rebuild with a new validity window (the txid changes). All four sign again. |
| Phase-1 rejection at submit | Nothing is spent. Take a fresh snapshot and rebuild. |
| Phase-2 failure | Cannot happen if step 8 passed. If it does, only the collateral (about 0.62 ADA) is lost and the pool is untouched. |
| 13b stops before T signs | Nothing is signed or spent. Stop the services and find out why surrender-api built that transaction before running 13a again. |
| The test claim fails | The bulk is still at the old address under the old pair. Fix the services config. At most the 1 cMATRA slice is at stake, and both new keys can move it back with this same tool. |
| After C1b | There is no undo. Only the two new keys can move the pool, with the same tool and a new plan. |

**Race form.** Something may signal that the old pool is being, or is about to be, spent without us: a custody-watch CRITICAL, a mempool sighting, or a confirmed key read. Then skip the slice and the test claim and run C1-direct from `plan-direct.template.json` through steps 1-11. Generating the new keys and funding F early (P3-P6) keeps the race to minutes. Whichever transaction spends d3a1e632…#1 first wins.

## 5. Post-flight via Insight (targeted reads)

| Read | Expected |
|---|---|
| `/api/v0/txs/<C1b>/utxos` | out 0: new pool address, 1,500,000 lovelace, 301,731,978,205,950 cMATRA, `inline_datum` `d87980`; out 1: change to the fee address |
| `/api/v0/addresses/<new pool address>/utxos` | two UTxOs: the claim continuation (74,220 base units, at least 1.5 ADA) and C1b#0. Total 301,731,978,280,170 = 301,731,979,205,950 − 925,780 |
| `/api/v0/addresses/addr1w8s6rqdjlzm5he27v9s202p8vjumza8qfsmufm2f6dy68hg9mn27a/utxos` | only 9a68849f…#0 (the v1 zombie, policy 98c61f40, no datum); no 7ff33a55 units |
| `/api/v0/assets/7ff33a5565393dc47b48ac47becc12d92c9952e724e8446dfb6adc66634d41545241` | quantity 1,000,000,000,000,000, unchanged |
| `/api/v0/txs/<test claim>/utxos` | quarantine +2 AGENT; T +925,780; no output anywhere else but the new pool |
| custody-watch | two CRITICAL pages (C1a, C1b) at the expected times; after the config flip, coverage reads the new pool |

Then add the new pool's line to `deployed_pools.txt` if P5 has not already done so, and update `scripts/sweep_params.env` (section 6).

## 6. Everything keyed to the old script, address, admins or deadline

| Component | Where | Change |
|---|---|---|
| surrender-api env | `/etc/cmatra-a1/env.surrender-api` on Server A (M1; never print it) | `SURRENDER_SCRIPT_ADDRESS` → new address; `BLUEPRINT_PATH` → `/var/lib/cmatra-a1/claim_validator.plutus.json` (the startup check enforces the match); `ADMIN_SKEY_PATH` → `/var/lib/cmatra-a1/keys/admin_1.skey`; `SURRENDER_LEDGER_PATH` → `/var/lib/cmatra-a1/ledger/surrender-ledger.sqlite3`, the M1 copy (the ledger carries over and is never re-initialised); `PAGER_OUTBOX_PATH` → `/var/lib/cmatra-a1/ledger/pager-outbox.sqlite3`; `PAGER_DISCORD_WEBHOOK_URL` stays the on-call channel's webhook (never print it); `PAGER_GRACE_S` unset (600 s); `COSIGNER_PKH` → new admin_2; `COSIGNER_URL` → signer-b; `ALLOWED_USER_ADDRESSES` → T only until the reopen; `SURRENDER_DEADLINE_POSIX_MS` stays 1795910400000 until 11-29, then 1798502400000 for the late window. No setting may point under `/home` |
| Co-signer env | `/etc/cmatra-a2/env.cosigner` on signer-b (M2) | `SURRENDER_SCRIPT_ADDRESS`; `COSIGNER_SKEY_PATH` → `/var/lib/cmatra-a2/keys/admin_2.skey`; `COSIGNER_LEDGER_PATH` → `/var/lib/cmatra-a2/ledger/cosigned.sqlite3`; `PAGER_OUTBOX_PATH` → `/var/lib/cmatra-a2/ledger/pager-outbox.sqlite3`; `PAGER_DISCORD_WEBHOOK_URL` and `PAGER_GRACE_S` as on Server A; `COSIGNER_PRIMARY_ADMIN_PKH` → new admin_1; `SURRENDER_DEADLINE_POSIX_MS` as above; signer-b's own chain credential (section 2) |
| Co-signer ledger | `COSIGNER_LEDGER_PATH` | Moved by M2 with its sha256 and `approvals_digest` compared on paper against the old host and Server A. Never `redemption_ledger init`: a fresh ledger resets every limit |
| systemd and firewall | Server A and signer-b | the units in `services/deploy/systemd/`, installed by `install-signer.sh`; 8421/tcp from Server A only |
| `scripts/sweep_params.env` | repo | Phase 1 (PR #45) already pins policy 7ff33a55, the 11-29 deadline slot 204344109 and an empty `SWEEP_DEST_ADDR`. After C1 the whole block points at the new pool: new script hash and address, `ADMIN_PKH_1/2`, `ADMIN_2_HOST`, deadline 1798502400000 / slot 206936109, destination the D4 multisig |
| `tools/admin_reclaim.py` | repo | Needed after 12-29 for the new pool. PR #44 fixes its redeemer and PR #45 its slot clock, but **it still writes a body-only `TxBodyConway` envelope, which cardano-cli 11.0.0.0 refuses** ("Expected one of: … Tx ConwayEra"). Its assembled tx would also carry no script or redeemer. Fix it, or move the sweep onto `pool_migration`, before 12-29 |
| `deployed_pools.txt` | repo | Add `NEW1 NEW2 1798502400000 <new hash>` |
| custody-watch | its config on the ops host | `surrender_pool.address` → new; `coverage.deadline_utc` stays `2026-11-29T00:00:00Z` until 11-29, then `2026-12-29T00:00:00Z`; add the old pool address as a `contract` address (cMATRA arriving there is a mistake); add the new admin addresses as `custody`. Flip right after C1b confirms; until then coverage pages that the old pool cannot pay, which is expected |
| Deny-list v1 (Phase 1) | repo | Add the old admin PKHs f1eee828 and 77fdb621 and their enterprise addresses. `cosign_policy` refuses a claimant that is a *current* admin only |
| Token registry | cardano-token-registry `mappings/7ff33a55…634d41545241.json` | See section 7 |
| flux1 `/matra-merger` | `pages/matra-merger.tsx` | No policy, address or script is baked in. Only `WINDOW_CLOSE_DISPLAY = 'November 28, 2026'`; after 11-29 it should show the late-window close |
| Public note and docs | announcement, docs.fluxpointstudios.com merger page | The deadline sentence (section 1). Any published pool address or script hash changes. The local docs repo has no match; check the published site |
| Daily pool-coverage digest (Phase 3) | | Point it at the new address |
| Brief's Phase 5 | | No 11-29 AdminWithdraw of the old pool (it is empty). The late window runs on the new pool. The new pool's AdminWithdraw after 12-29 goes to D4 custody |
| Custody records | internal | New PKHs, address, paths and fingerprints after the ceremony |

Nothing else pins the old pool: the redemption pin and the ledgers key on units and pool outputs, not on the address. The operator-kit hits are test fixtures.

## 7. Token registry: the old keys keep one power

The live cMATRA registry entry (read 11-04, read-only) has every property attested by f1eee828 and 77fdb621, at sequence 0, with no `policy` field. Whoever holds both old keys could therefore sign updated metadata, such as a new `url`, at a higher sequence number.

- Do not shred the old keys until a replacement attester is in place.
- Ask the registry maintainers how a Plutus-policy subject without a policy field changes attesters. That is UNVERIFIED, as the brief already noted.
- Meanwhile, put a tripwire on any registry PR touching that subject (GitHub watch or the CI digest).

## 8. Decisions for deci

| ID | Decision | CTO recommendation |
|---|---|---|
| C1-D1 | Host for new admin_2 | A fresh single-purpose signer-b. Node-3 only with H1-H7 all done and verified |
| C1-D2 | New pool's compiled deadline | 2026-12-29 00:00Z, matching D7; the public window still closes 11-29 through the services |
| C1-D3 | Test claim of 2 AGENT (a D5 exception) by a dedicated claimant T, through the API only | Yes: the fresh wallet funds T with 2 AGENT and 10 ADA at least 72 h ahead (P11), and T claims 0.925780 cMATRA, FPS to FPS. Only T's 10 ADA and 2 AGENT ever depend on the claim's signature |
| C1-D4 | Fee input: 20 ADA from the fresh wallet to new admin_1's address | Yes, funded early so the race form stays minutes away |
| C1-D5 | Ceremony date and console presence at the four signing points | About 11-10, after the Phase 2 dark deploy |
| C1-D6 | When to retire the old keys | After C1b + 30 confirmations, Phase 0 done, and the registry question settled |

## 9. Simulation evidence

`ceremony/c1/sim.py` runs offline against the chain reads recorded in `ceremony/c1/chain/` on 11-04 (tip slot 202,188,020). `--live` reads the pool again through the configured provider. Its keys are sim-only pairs derived from public seeds; `apply_params.sh` applies their pools at run time, under aiken v1.1.21. CI runs it on every push (the `c1-ceremony` step, `ceremony/c1/test_ceremony.py`): **68/68 as expected** (LIVE 20, CLAIM 16, API 22, REHEARSAL 10).

**LIVE (validator e1a181b2, real old PKHs)**
- `apply_params.sh` applied to the old admins and 11-29 hashes to the live pool's script credential.
- PASS: C1-direct, C1a, C1b chained from C1a's outputs.
- Measured cost: 56,658 mem / 18,072,377 steps against a budget of 2,000,000 / 900,000,000. The cost depends on where the old admins sort among the four required signers; an earlier sim pair gave 59,788 / 18,976,025.
- FAIL as expected: without old admin_1; without old admin_2; new admins only; ttl past the old deadline; AdminWithdraw before the deadline; a datum-less pool output.
- The chain accepts a ttl equal to the deadline slot; the tool stops one slot earlier.
- cMATRA is conserved across C1a + C1b.
- `cardano-cli latest transaction txid` equals the tool's txid.
- Two snapshots read independently build byte-identical transactions.
- The tool's derived destination is byte-identical to aiken's `apply_params.sh` output for the sim's new pair.
- REFUSED `new_script`: a plan with the real new admins as signers whose destination is another script; the same with the validator applied to other admins. In review round 1 both were ACCEPTED.

**CLAIM (the re-applied validator)**
- A 2-AGENT claim passes in aiken and is APPROVED 925780 by `cosign_policy` configured for the new pool.
- The claimant's and both admins' cardano-cli signatures verify.
- FAIL as expected: old admins spending the new pool; one new admin alone; a claim past 12-29; AdminWithdraw before 12-29; AdminWithdraw by the old admins.
- PASS as expected: a late-window claim on chain (refused by the services at the 11-29 setting, approved at the 12-29 setting); AdminWithdraw by the new pair after 12-29; a claim against the C1b bulk UTxO.

**API (blocks C0, P11 and 13a-13c of this file, run as written)**
- The sim reads the blocks out of this file and runs them in a bash without errexit, as an operator's shell would. Only C0's settings change, to the simulation's own, and `sudo` becomes a plain call. The sim refuses blocks after C0 that name a host location instead of a C0 setting.
- The stand-in checks `X-API-Secret` and answers with surrender-api's own request models and witness merge.
- P11 makes an enterprise T with `T.skey` at mode 0400. The claim is funded as P11 funds it: 2 AGENT with 5 ADA, and a separate 5 ADA as collateral.
- 13a-13c:
  - the build request fits `BuildSurrenderRequest`, and the secret arrives as a header;
  - 13b signs, and the submit fits `SubmitRequest`;
  - the merge keeps the body bytes and takes T's witness only;
  - with both admins' witnesses added, `cosign_policy` approves 925780, the claimant-signature check passes, and aiken passes (50,398 mem / 16,265,081 steps).
- The output of `transaction witness` posted as the submit is refused by the merge, which is why 13b uses `transaction sign`.
- 13b stops before T signs on each of these served transactions:
  - a txid the body does not hash to;
  - an output to another address;
  - a new-pool output without datum;
  - a payout of 925,781;
  - a mint, a withdrawal or a certificate;
  - a collateral return elsewhere;
  - the real d3a1e632 surrender.

  An earlier attempt's signed files never survive into a new attempt.
- cardano-cli computes d3a1e632's txid from the transaction as surrender-api served it, and its `transaction sign` witness verifies over those body bytes, with the service's tag-258 framing kept.
- `ceremony/c1/runbook_mutation.py` drops or weakens each command and check of those blocks in turn: **15/15 killed**, and nothing is scored unless the unmutated runbook passes first. The cardano-cli sha256 line is the one line not covered, since only a different binary would fail it.

**REHEARSAL (a throwaway old pair, same source and deadline)**
- The CLI builds twice with identical bytes.
- Four cardano-cli 11.0.0.0 witnesses → `assemble` → the signed tx's txid is unchanged and its fee covers the signed size.
- `check` passes on the signed tx, and aiken PASS.
- Three of four witnesses are refused.
- A tx assembled by cardano-cli itself also passes the check.

**The migration tool's own tests.** `tests/test_pool_migration_mutation.py` runs on every push: **75/75 mutants killed**.

- 61 disarm one refusal each; 14 are targeted at the derivation, the deadline rule, the report fields, the 5 ADA collateral cap and the fee payer's witness.
- The last two survived review round 3 and are now covered by `test_collateral_above_its_cap` and `test_a_fee_payer_who_is_no_admin_signs_too`.

**The signer layout.** `tests/signer_layout_root.sh` is CI's `signer-layout` step, run as root in a throwaway container:

- It installs both signers with `install-signer.sh`, moving a ledger in as M1 and M2 do, and proves the copy.
- It runs K2 twice per signer (the second run must refuse) and W1 as cmatra-a1 from a directory that user cannot enter.
- It requires `--check` to pass, and to fail once a mode is loosened, the pager outbox leaves the ledger directory (or is the ledger itself, or the directory) or the webhook is missing.
- It starts each service dark under its own user with its unit's command and environment, and reads `/health` with an idle pager, the committed deny-list's line in each log, and each outbox the service made, 0600 under its own user.

**Findings the simulation, its tests and the reviews turned up**, all fixed in PR #44 or the PR #45 it is built on, except `admin_reclaim`'s envelope type:
- cardano-cli 11 writes a witness as the bare `[vkey, sig]` pair.
- The collateral return also needs its minimum ADA.
- The mainnet slot clock was 5 days early (fixed once, in PR #45: `tools/cardano_utils.py` holds the one offset table every slot conversion reads).
- `admin_reclaim` used the wrong redeemer.
- `admin_reclaim`'s envelope type (open, section 6).
- Review round 1: the destination came from the plan verbatim. Required signers proved only that the new keys can sign, not that the pool would be locked by them. The destination is now derived from the old script, and the plan's old deadline is bound to it too. The runbook also gained the P3/P4 paper comparison at steps 4 and 6.
- Review round 2: step 13 let the fresh wallet sign the test claim, possibly through the flux1 page, and never said how the claimant witness is made on the API path. The claim now comes from a dedicated claimant T through the API only (P11, 13a-13c), and the page would need P12 and P13.
- Review round 3:
  - The ceremony files lived only in a session scratch directory; they are now this directory, tested in CI.
  - A1 was unreachable: the service's code, venv, env and ledger sat in a home directory its new user could not enter, and the first start under that user would have come between C1a and C1b. Each signer now has its own user and layout (section 2), M1 and M2 move the ledgers with a proof, and P9 starts both dark before C1a.
  - `cardano-cli latest address key-gen` overwrites an existing key without a word; K2 refuses to run over one.
  - PR #44 and PR #45 conflicted in 7 files, carried the slot-clock fix twice, and failed 6 tests together; nothing required the policy run at step 13 to be the one tested. PR #44 is now built on PR #45 with one slot clock, its tests and CI run both together, the signer layout carries the pager, and P1 names PR #44's merge commit.

**Files**
- `RUNBOOK.md`: this file.
- `evaluate.py` and the plan templates for C1a, C1b and C1-direct.
- `sim.py`: the simulation. `chain/`: the recorded Insight reads it runs on. Its outputs go to `out/` (not tracked), or to `--out`.
- `runbook_mutation.py`: the mutation run over the blocks of this file.
- `test_ceremony.py`: CI's `c1-ceremony` step.
