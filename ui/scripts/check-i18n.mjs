// i18n consistency check. TypeScript already enforces de/en key parity via
// `en: Record<MessageKey, string>`; this script makes that explicit, reports
// coverage, and warns about en values that still look German (untranslated).
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const here = dirname(fileURLToPath(import.meta.url));
const file = join(here, "..", "src", "i18n", "messages.ts");
const src = readFileSync(file, "utf8");

function block(anchor) {
  const start = src.indexOf(anchor);
  if (start === -1) throw new Error(`anchor not found: ${anchor}`);
  const open = src.indexOf("{", start);
  let depth = 0;
  let i = open;
  for (; i < src.length; i += 1) {
    const c = src[i];
    if (c === "{") depth += 1;
    else if (c === "}") {
      depth -= 1;
      if (depth === 0) break;
    }
  }
  return src.slice(open, i + 1);
}

const KEY_RE = /"([^"]+)":\s*"((?:[^"\\]|\\.)*)"/g;
function parse(blockText) {
  const out = {};
  for (const match of blockText.matchAll(KEY_RE)) out[match[1]] = match[2];
  return out;
}

const de = parse(block("const de = {"));
const en = parse(block("const en: Record<MessageKey, string> = {"));

const deKeys = Object.keys(de);
const enKeys = Object.keys(en);
const missingInEn = deKeys.filter((k) => !(k in en));
const missingInDe = enKeys.filter((k) => !(k in de));

let failures = 0;
if (missingInEn.length) {
  console.error(`missing in en: ${missingInEn.join(", ")}`);
  failures += 1;
}
if (missingInDe.length) {
  console.error(`missing in de: ${missingInDe.join(", ")}`);
  failures += 1;
}

// Values that are legitimately identical across languages (brand, endonyms,
// protocol literals) are exempt from the "looks German" warning.
const EXEMPT = new Set(["app.brand", "lang.de", "lang.en", "api.httpError"]);
const GERMAN = /[äöüß]/;
const suspicious = enKeys.filter((k) => !EXEMPT.has(k) && GERMAN.test(en[k]));
if (suspicious.length) {
  console.warn(`warning: en values that look German (verify): ${suspicious.join(", ")}`);
}

if (failures) {
  console.error(`i18n check failed: ${deKeys.length} de keys, ${enKeys.length} en keys`);
  process.exit(1);
}
console.log(`i18n ok: ${deKeys.length} keys, de/en parity holds`);
