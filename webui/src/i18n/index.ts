import { createContext, useContext } from "react";

import { en, type MessageKey } from "./en";
import { ja } from "./ja";
import { zhHans } from "./zh-Hans";
import { zhHant } from "./zh-Hant";

export type { MessageKey };

/**
 * The language menu is built from this list, so adding a language is: write one
 * more catalogue, add one row here. Nothing in the layout has to change.
 */
export interface LanguageEntry {
  /** BCP-47 tag, also written to `<html lang>`. */
  readonly code: string;
  /** Endonym — a language menu that names languages in English helps nobody. */
  readonly name: string;
  /** Two characters at most, for the collapsed control in the rail. */
  readonly short: string;
}

export const LANGUAGES: readonly LanguageEntry[] = [
  { code: "en", name: "English", short: "EN" },
  { code: "zh-Hant", name: "繁體中文", short: "繁" },
  { code: "zh-Hans", name: "简体中文", short: "简" },
  { code: "ja", name: "日本語", short: "日" },
];

const CATALOGUES: Record<string, Record<MessageKey, string>> = {
  en,
  "zh-Hant": zhHant,
  "zh-Hans": zhHans,
  ja,
};

/** Browser language, mapped onto what we actually have. English is the fallback. */
export function detectLanguage(): string {
  const l = (navigator.language || "en").toLowerCase();
  if (l.startsWith("ja")) return "ja";
  if (l.startsWith("zh")) {
    return l.includes("cn") || l.includes("hans") || l.includes("sg") ? "zh-Hans" : "zh-Hant";
  }
  return "en";
}

/**
 * Look up `key` and substitute `{0}`, `{1}`, … positionally.
 *
 * Falls back to English rather than to the raw key: a reader seeing an English
 * sentence has lost a translation, whereas a reader seeing `st_e_null_note` has
 * lost the meaning.
 */
export function translate(lang: string, key: MessageKey, ...args: (string | number)[]): string {
  const dict = CATALOGUES[lang] ?? en;
  let s: string = dict[key] ?? en[key] ?? key;
  args.forEach((v, i) => {
    s = s.split(`{${i}}`).join(String(v));
  });
  return s;
}

export interface I18n {
  lang: string;
  setLang: (lang: string) => void;
  t: (key: MessageKey, ...args: (string | number)[]) => string;
}

export const I18nContext = createContext<I18n>({
  lang: "en",
  setLang: () => {},
  t: (key, ...args) => translate("en", key, ...args),
});

export function useI18n(): I18n {
  return useContext(I18nContext);
}
