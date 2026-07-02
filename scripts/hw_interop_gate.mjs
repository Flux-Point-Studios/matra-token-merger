#!/usr/bin/env node
// HW-wallet acceptance gate (task #485): a built surrender tx must pass the
// exact CIP-21 canonicalization pipeline every hardware-wallet integration
// runs (cardano-hw-interop-lib — the Ledger/Trezor layer inside Eternl, Lace,
// Typhon, NuFi) with zero errors and zero byte drift. If the canonicalizer is
// a no-op on our bytes, the device signs exactly the blake2b-256 body hash
// admin_1 and the cosigner already signed.
//
// Usage: node scripts/hw_interop_gate.mjs <tx-cbor-hex-file>
// Exit:  0 = all checks pass, 1 = any check fails, 2 = usage error.
//
// Dependencies are vendored in scripts/hw-gate/ (own package.json + lockfile);
// run `npm ci --prefix scripts/hw-gate` once before invoking.
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';

const require = createRequire(new URL('./hw-gate/', import.meta.url));
const {
  validateTx,
  decodeTx,
  encodeTx,
  decodeTxBody,
  encodeTxBody,
  transformTxBody,
} = require('cardano-hw-interop-lib');

const file = process.argv[2];
if (!file) {
  console.error('usage: hw_interop_gate.mjs <tx-cbor-hex-file>');
  process.exit(2);
}
const tx = Buffer.from(readFileSync(file, 'utf8').trim(), 'hex');

let failed = false;
const check = (name, ok, detail = '') => {
  console.log(`${ok ? 'PASS' : 'FAIL'} ${name}${detail ? ` — ${detail}` : ''}`);
  failed ||= !ok;
};

// 1. Zero validation errors. Not merely "no set-tag error": any FIXABLE error
//    means some wallet transforms the tx before signing and the device hash
//    diverges from the co-signed one.
const errors = validateTx(tx);
check(
  'validateTx has zero errors',
  errors.length === 0,
  errors.map((e) => `${e.fixable ? 'FIXABLE' : 'UNFIXABLE'}:${e.reason}@${e.position}`).join('; '),
);

// 2. Byte round-trip through the library's own decode/encode.
check('encodeTx(decodeTx(tx)) byte-equal', Buffer.compare(encodeTx(decodeTx(tx)), tx) === 0);

// 3. The CIP-21 body canonicalizer is an identity on our body bytes.
//    transformTxBody mutates its argument in place (hasTag flips), so both
//    sides MUST come from fresh decodes.
const bodyBytes = encodeTxBody(decodeTx(tx).body);
const transformed = encodeTxBody(transformTxBody(decodeTxBody(bodyBytes), null));
check(
  'transformTxBody is identity on the body bytes',
  Buffer.compare(transformed, bodyBytes) === 0,
  `${bodyBytes.length} -> ${transformed.length} bytes`,
);

process.exit(failed ? 1 : 0);
