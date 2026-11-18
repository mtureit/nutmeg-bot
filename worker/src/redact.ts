// Credential redaction ported from redact.py. Notion text is untrusted input,
// so this is applied again immediately before live content enters a prompt.

export const MASK = "[伏字]";

const PLACEHOLDER = /^(<[^>]*>|\$\{?[A-Za-z_][A-Za-z0-9_]*\}?|xxx|\*{3,}|\.{3,}|…|changeme|your[_-].*)$/i;
const PEM = /-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(-----END [A-Z ]*PRIVATE KEY-----|$)/gis;
const URL_CRED = /\b([a-z][a-z0-9+.\-]*:\/\/[^/\s:@]+):([^/\s@]+)@/gi;
const PREFIXED_TOKEN = /\b(?:ghp_|gho_|ghu_|ghs_|github_pat_|xox[abposr]-|xapp-|sk-(?:proj-)?|sk_live_|rk_live_|AKIA|ASIA|AIza)[A-Za-z0-9_\-]{8,}|(?<![A-Za-z0-9_])(?:ntn_|secret_)[A-Za-z0-9]{40,}(?![A-Za-z0-9_])/g;

const GLUED_SECRET_KEY = "[A-Za-z0-9]+(?:PASSWORD|PASSWD|SECRET|TOKEN)(?:_[A-Za-z0-9]+)*";
const SUFFIXED_PWD_KEY = "[A-Za-z0-9]+_PWD";
const SECRET_KEY = `(?:` +
  `(?:[A-Za-z0-9]+_)*(?:PASSWORD|PASSWD|PASSPHRASE|PASS|SECRET|TOKEN|API_?KEY|PRIVATE_?KEY|ACCESS_?KEY|CREDENTIALS?)(?:_[A-Za-z0-9]+)*` +
  `|${GLUED_SECRET_KEY}|${SUFFIXED_PWD_KEY})`;
const SECRET_NAME = new RegExp(`(?<![A-Za-z0-9_])${SECRET_KEY}(?![A-Za-z0-9_])`, "gi");
const KV = new RegExp("\\b(" + SECRET_KEY + ")(\\s*[:=]\\s*)([\"']?)([^\\s\"'`]+)", "gi");
const QUOTED_KV_DOUBLE = new RegExp(`([\"'])(${SECRET_KEY})\\1\\s*:\\s*(\"((?:\\\\.|[^\"])*)\")`, "gi");
const QUOTED_KV_SINGLE = new RegExp(`([\"'])(${SECRET_KEY})\\1\\s*:\\s*('((?:\\\\.|[^'])*)')`, "gi");
const QUOTED_ASSIGN_DOUBLE = new RegExp(`(?<![A-Za-z0-9_])(${SECRET_KEY})\\s*[:=]\\s*(\"((?:\\\\.|[^\"])*)\")`, "gi");
const QUOTED_ASSIGN_SINGLE = new RegExp(`(?<![A-Za-z0-9_])(${SECRET_KEY})\\s*[:=]\\s*('((?:\\\\.|[^'])*)')`, "gi");
const AUTH_HEADER = /(\bAuthorization\s*:\s*(?:Bearer|Basic)\s+)([^\s"']+)/gi;
const CURL_HEADER = /-\s*H\s+("|')(?<name>[A-Za-z0-9][A-Za-z0-9-]*(?:token|key|secret|auth)[A-Za-z0-9-]*)\s*:\s*(?<value>[^\s"']+)(?<trailing>\s*)\1/gi;
const LABEL = /(?<![A-Za-z])(パスワード|暗証番号|ネットワークキー|シークレット|トークン|password|passwd|passphrase|pass|pwd)(\s*[:：=→]\s*)(`?)([^\s`、。，,）)」】]+)/gi;
const SHORT_LABEL = /(?<![A-Za-z])(pw|pwd)(\s+)(`?)([^\s`、。，,）)」】]+)/gi;
const ASCII_RUN = "[A-Za-z0-9!#$%&()*+,\\-./:;<=>?@\\[\\\\\\]^_`{|}~]{4,}";
const LABEL_PROSE = new RegExp(
  `(?<![A-Za-z])(?<label>パスワード|暗証番号|ネットワークキー|passphrase(?![A-Za-z])|password(?![A-Za-z])|passwd(?![A-Za-z]))` +
    "(?<gap>[^A-Za-z0-9\\r\\n。\\[\\]\\\"'`$<>]{0,20}?)" +
    `(?<![A-Za-z0-9])(?<value>${ASCII_RUN})(?![A-Za-z0-9])`,
  "gi",
);
const QUERY_TOKEN = /([?&](?:[a-z_]*token|key|secret|sig|signature|password)=)([^&\s)\]]+)/gi;
const NOTION_URL = /https?:\/\/(?:www\.)?notion\.so\/\S+|https?:\/\/app\.notion\.com\/p\/\S+/gi;
const RANDOM_RUN = /(?<![A-Za-z0-9])[A-Za-z0-9_+./=-]{32,}(?![A-Za-z0-9])/g;
const COMMON_LABEL_VALUES = new Set([
  "ok", "on", "off", "url", "id", "enter", "login", "reset", "hash", "sha256", "bcrypt", "argon2",
  "null", "none", "true", "false", "required",
]);
const NO_VALUE_CONTEXT = /^\s*(?:再設定|リセット|変更|の(?:再設定|リセット|変更)|を設定)/;
const UNAMBIGUOUS_LABELS = new Set(["パスワード", "暗証番号", "ネットワークキー", "password", "passwd", "pw", "pwd"]);

type MatchReplacer = (match: string, ...args: any[]) => string;

const replaceCount = (text: string, regex: RegExp, replacer: MatchReplacer): [string, number] => {
  let count = 0;
  const replaced = text.replace(regex, (...args: any[]) => {
    const match = String(args[0]);
    const output = replacer.apply(null, args as [string, ...any[]]);
    if (output !== match) count++;
    return output;
  });
  return [replaced, count];
};

const keep = (value: string): boolean => {
  const cleaned = value.trim().replace(/^['"`]|['"`]$/g, "");
  return cleaned === MASK || PLACEHOLDER.test(cleaned);
};

const isNotionUrl = (value: string): boolean => {
  const cleaned = value.trim().replace(/^['"`]|['"`]$/g, "").replace(/[.,;:!?)]}]+$/, "");
  NOTION_URL.lastIndex = 0;
  const match = NOTION_URL.exec(cleaned);
  NOTION_URL.lastIndex = 0;
  return match?.[0] === cleaned;
};

const replaceCaptured = (whole: string, value: string): string => {
  const index = whole.indexOf(value);
  if (index < 0) return whole;
  return whole.slice(0, index) + MASK + whole.slice(index + value.length);
};

const isUnambiguousLabel = (label: string): boolean => {
  const normalized = label.toLowerCase();
  return UNAMBIGUOUS_LABELS.has(normalized) || UNAMBIGUOUS_LABELS.has(normalized.split("_").pop() ?? "");
};

const maskSecretValue = (whole: string, label: string, value: string): string => {
  const cleaned = value.trim().replace(/^['"`]|['"`]$/g, "");
  if ((cleaned.startsWith("/") || cleaned.startsWith("~/")) && !isUnambiguousLabel(label)) return whole;
  if (keep(value) || isNotionUrl(value)) return whole;
  return replaceCaptured(whole, value);
};

const looksRandom = (value: string): boolean => {
  if (keep(value)) return false;
  if (/^[0-9a-f]{32,}$/i.test(value)) return true;
  return value.length >= 32 && /[A-Za-z]/.test(value) && /[0-9]/.test(value);
};

const redactLongRandomValues = (text: string): [string, number] => {
  let count = 0;
  const lines = text.split(/(?<=\n)/);
  const output = lines.map((rawLine) => {
    const line = rawLine.replace(/[\r\n]+$/, "");
    const ending = rawLine.slice(line.length);
    SECRET_NAME.lastIndex = 0;
    if (!SECRET_NAME.test(line)) return rawLine;
    const protectedSpans: Array<[number, number]> = [];
    NOTION_URL.lastIndex = 0;
    for (const match of line.matchAll(NOTION_URL)) protectedSpans.push([match.index ?? 0, (match.index ?? 0) + match[0].length]);
    RANDOM_RUN.lastIndex = 0;
    const replaced = line.replace(RANDOM_RUN, (value: string, offset: number) => {
      if (protectedSpans.some(([start, end]) => offset >= start && offset + value.length <= end)) return value;
      if (!looksRandom(value)) return value;
      count++;
      return MASK;
    });
    return replaced + ending;
  });
  return [output.join(""), count];
};

export const redact = (input: string): { text: string; count: number } => {
  let text = input;
  let count = 0;
  const apply = (regex: RegExp, replacer: MatchReplacer): void => {
    const result = replaceCount(text, regex, replacer);
    text = result[0];
    count += result[1];
  };

  apply(PEM, () => MASK);
  apply(URL_CRED, (whole, prefix, value) => keep(value) ? whole : `${prefix}:${MASK}@`);
  apply(PREFIXED_TOKEN, () => MASK);
  apply(AUTH_HEADER, (whole, prefix, value) => keep(value) || isNotionUrl(value) ? whole : `${prefix}${MASK}`);
  apply(CURL_HEADER, (whole, ...args) => {
    const groups = args[args.length - 1] as Record<string, string>;
    const value = groups.value;
    return keep(value) || isNotionUrl(value) ? whole : replaceCaptured(whole, value);
  });
  apply(QUERY_TOKEN, (whole, prefix, value) => keep(value) || isNotionUrl(value) ? whole : `${prefix}${MASK}`);
  const quotedReplacer = (whole: string, label: string, _quoted: string, value: string) => maskSecretValue(whole, label, value);
  apply(QUOTED_KV_DOUBLE, (whole, _quote, label, _outer, value) => quotedReplacer(whole, label, _outer, value));
  apply(QUOTED_KV_SINGLE, (whole, _quote, label, _outer, value) => quotedReplacer(whole, label, _outer, value));
  apply(QUOTED_ASSIGN_DOUBLE, (whole, label, _outer, value) => quotedReplacer(whole, label, _outer, value));
  apply(QUOTED_ASSIGN_SINGLE, (whole, label, _outer, value) => quotedReplacer(whole, label, _outer, value));
  apply(KV, (whole, label, _separator, _quote, value) => maskSecretValue(whole, label, value));
  apply(LABEL, (whole, label, _separator, _quote, value) => maskSecretValue(whole, label, value));
  apply(SHORT_LABEL, (whole, label, _space, _quote, value) => maskSecretValue(whole, label, value));
  apply(LABEL_PROSE, (whole, ...args) => {
    const groups = args[args.length - 1] as Record<string, string>;
    const label = groups.label;
    const gap = groups.gap;
    const value = groups.value;
    if (NO_VALUE_CONTEXT.test(gap) || COMMON_LABEL_VALUES.has(value.toLowerCase())) return whole;
    const cleaned = value.trim().replace(/^['"`]|['"`]$/g, "");
    if (keep(value) || isNotionUrl(value)) return whole;
    if ((cleaned.startsWith("/") || cleaned.startsWith("~/")) && !isUnambiguousLabel(label)) return whole;
    return replaceCaptured(whole, value);
  });
  const random = redactLongRandomValues(text);
  text = random[0];
  count += random[1];
  return { text, count };
};

export const redactRecord = (record: { text?: string; title?: string }): number => {
  const body = redact(record.text ?? "");
  record.text = body.text;
  let count = body.count;
  if ("title" in record) {
    const title = redact(record.title ?? "");
    record.title = title.text;
    count += title.count;
  }
  return count;
};
