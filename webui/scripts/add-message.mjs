/**
 * Add one message key to all four catalogues at once.
 *
 *   node scripts/add-message.mjs t_open "Open details" "開啟詳情" "打开详情" "詳細を開く"
 *
 * TypeScript already refuses a build when a translation is missing a key; this
 * exists so adding one is a single step rather than four edits that are easy to
 * leave half-done. Argument order follows LANGUAGES in src/i18n/index.ts.
 */
import fs from "node:fs";

const FILES = ["en.ts", "zh-Hant.ts", "zh-Hans.ts", "ja.ts"];
const [key, ...values] = process.argv.slice(2);

if (!key || values.length !== FILES.length) {
  console.error("usage: node scripts/add-message.mjs <key> <en> <zh-Hant> <zh-Hans> <ja>");
  process.exit(2);
}
if (!/^[a-z][\w]*$/.test(key)) {
  console.error(`invalid key: ${key}`);
  process.exit(2);
}

FILES.forEach((file, i) => {
  const path = `src/i18n/${file}`;
  const src = fs.readFileSync(path, "utf8");
  if (new RegExp(`^  ${key}:`, "m").test(src)) {
    console.error(`${file}: ${key} is already present`);
    process.exit(1);
  }
  const close = file === "en.ts" ? "\n} as const;" : "\n};";
  const at = src.lastIndexOf(close);
  fs.writeFileSync(path, `${src.slice(0, at)}\n  ${key}: ${JSON.stringify(values[i])},${src.slice(at)}`);
});

console.log(`added ${key} to ${FILES.length} catalogues`);
