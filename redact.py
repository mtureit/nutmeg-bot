"""Mask credentials in Notion text before it is chunked, embedded or stored.

Notion pages hold server logins, .env snippets and API tokens. Anything that
reaches data/clean, OpenAI or Vectorize can be read back through the bot by
every member of a channel it sits in, so values are replaced with MASK here,
at the single point text enters the pipeline (prepare.py), and again when
index_build.py loads records (for jsonl written before this filter existed).

Pattern matching cannot catch a password written in free prose ("rootのやつは
いつもの"), so this is a safety net, not a substitute for keeping secrets out
of Notion. `python redact.py --check` lists what still looks suspicious.

Run directly to rewrite existing data/clean/*.jsonl in place:
    uv run python redact.py            # rewrite and report counts
    uv run python redact.py --check    # report only, values masked
"""

from __future__ import annotations

import argparse
import json
import re
import sys

MASK = "[伏字]"

# A value that is only a placeholder ("<生成した値>", "${SECRET}", "xxx") is
# documentation, not a secret, and keeping it keeps setup guides readable.
_PLACEHOLDER = re.compile(
    r"^(<[^>]*>|\$\{?[A-Za-z_][A-Za-z0-9_]*\}?|xxx|\*{3,}|\.{3,}|…|changeme|your[_-].*)$",
    re.I,
)

_PEM = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(-----END [A-Z ]*PRIVATE KEY-----|\Z)", re.S
)

# scheme://user:password@host
_URL_CRED = re.compile(r"\b([a-z][a-z0-9+.\-]*://[^/\s:@]+):([^/\s@]+)@", re.I)

# Tokens whose prefix gives them away: GitHub, Slack, OpenAI, Stripe, AWS, Google, Notion.
_PREFIXED_TOKEN = re.compile(
    r"\b(?:ghp_|gho_|ghu_|ghs_|github_pat_|xox[abposr]-|xapp-|sk-(?:proj-)?|sk_live_|rk_live_"
    r"|AKIA|ASIA|AIza)[A-Za-z0-9_\-]{8,}"
    r"|(?<![A-Za-z0-9_])(?:ntn_|secret_)[A-Za-z0-9]{40,}(?![A-Za-z0-9_])"
)

# KEY=VALUE / KEY: VALUE where the key names a secret (.env, YAML, shell export).
# Keep the identifier boundaries conservative: "tokenization" is ordinary prose,
# while "ADMIN_TOKEN" and "SECRET_KEY" are secret names.  A few tools use a
# secret word without an underscore (for example, PGPASSWORD); PASS is excluded
# from that form because it is too common in ordinary words.
_GLUED_SECRET_KEY = r"[A-Za-z0-9]+(?:PASSWORD|PASSWD|SECRET|TOKEN)(?:_[A-Za-z0-9]+)*"
_SUFFIXED_PWD_KEY = r"[A-Za-z0-9]+_PWD"
_SECRET_KEY = (
    r"(?:"
    r"(?:[A-Za-z0-9]+_)*(?:PASSWORD|PASSWD|PASSPHRASE|PASS|SECRET|TOKEN|"
    r"API_?KEY|PRIVATE_?KEY|ACCESS_?KEY|CREDENTIALS?)(?:_[A-Za-z0-9]+)*"
    rf"|{_GLUED_SECRET_KEY}"
    rf"|{_SUFFIXED_PWD_KEY}"
    r")"
)
_SECRET_NAME = re.compile(rf"(?i)(?<![A-Za-z0-9_]){_SECRET_KEY}(?![A-Za-z0-9_])")
_KV = re.compile(rf"(?i)\b({_SECRET_KEY})(\s*[:=]\s*)([\"']?)([^\s\"'`]+)")

# JSON and Python-style dictionaries. Values may contain spaces; the simple
# escaped-character handling is enough for the credential snippets we ingest.
_QUOTED_KV = re.compile(
    rf"""(?ix)
    (?P<key_quote>[\"'])(?P<key>{_SECRET_KEY})(?P=key_quote)\s*:\s*
    (?P<value_quote>[\"'])
    (?P<value>(?:\\.|(?! (?P=value_quote) ).)*)
    (?P=value_quote)
    """
)
_QUOTED_ASSIGNMENT_KV = re.compile(
    rf"""(?ix)
    (?<![A-Za-z0-9_])(?P<key>{_SECRET_KEY})\s*[:=]\s*
    (?P<value_quote>[\"'])
    (?P<value>(?:\\.|(?! (?P=value_quote) ).)*)
    (?P=value_quote)
    """
)

# Authorization headers and curl's -H form. The value capture is deliberately
# separate so the header name and scheme stay readable.
_AUTH_HEADER = re.compile(
    r"(?i)(\bAuthorization\s*:\s*(?:Bearer|Basic)\s+)([^\s\"']+)"
)
_CURL_HEADER = re.compile(
    r"""(?ix)
    -\s*H\s+
    (?P<quote>[\"'])
    (?P<name>[A-Za-z0-9][A-Za-z0-9-]*(?:token|key|secret|auth)[A-Za-z0-9-]*)
    \s*:\s*
    (?P<value>[^\s\"']+)
    (?P<trailing>\s*)
    (?P=quote)
    """
)

# Japanese / English labels in prose and tables: "パスワード：xxx", "pass: xxx", "PW xxx".
_LABEL = re.compile(
    r"(?i)(?<![A-Za-z])(パスワード|暗証番号|ネットワークキー|シークレット|トークン|password|passwd|passphrase|pass|pwd)"
    r"(\s*(?:[:：=→])\s*)(`?)([^\s`、。，,）)」】]+)"
)

# Short English labels are common in setup notes. Bare "pass" is intentionally
# excluded here because it appears in normal prose (for example, "pass this").
_SHORT_LABEL = re.compile(
    r"(?i)(?<![A-Za-z])(pw|pwd)(\s+)(`?)([^\s`、。，,）)」】]+)"
)

# Japanese labels are often written with a particle or as an instruction rather
# than with a colon: "パスワードは...", "パスワードの欄に...".  Keep the
# search local to one sentence so an unrelated ASCII word is not attributed to
# the label.  English short labels stay in _LABEL/_SHORT_LABEL because a bare
# "pass" is common prose ("please pass this").
_ASCII_RUN = r"[A-Za-z0-9!#$%&()*+,\-./:;<=>?@\[\\\]^_`{|}~]{4,}"
_LABEL_PROSE = re.compile(
    rf"(?ix)"
    rf"(?<![A-Za-z])(?P<label>"
    rf"パスワード|暗証番号|ネットワークキー|"
    rf"passphrase(?![A-Za-z])|password(?![A-Za-z])|passwd(?![A-Za-z])"
    rf")"
    rf"(?P<gap>[^A-Za-z0-9\r\n。\[\]\"'`$<>]{{0,20}}?)"
    rf"(?<![A-Za-z0-9])(?P<value>{_ASCII_RUN})(?![A-Za-z0-9])"
)

# Query parameters that carry a token (?token=..., &redir_token=...).
_QUERY_TOKEN = re.compile(r"(?i)([?&](?:[a-z_]*token|key|secret|sig|signature|password)=)([^&\s)\]]+)")

_NOTION_URL = re.compile(
    r"(?i)https?://(?:www\.)?notion\.so/\S+|https?://app\.notion\.com/p/\S+"
)
_RANDOM_RUN = re.compile(r"(?<![A-Za-z0-9])[A-Za-z0-9_+./=-]{32,}(?![A-Za-z0-9])")
_COMMON_LABEL_VALUES = {
    "ok",
    "on",
    "off",
    "url",
    "id",
    "enter",
    "login",
    "reset",
    "hash",
    "sha256",
    "bcrypt",
    "argon2",
    "null",
    "none",
    "true",
    "false",
    "required",
}
_NO_VALUE_CONTEXT = re.compile(r"^\s*(?:再設定|リセット|変更|の(?:再設定|リセット|変更)|を設定)")


def _keep(value: str) -> bool:
    cleaned = value.strip("`\"'")
    return cleaned == MASK or bool(_PLACEHOLDER.match(cleaned))


def _is_notion_url(value: str) -> bool:
    cleaned = value.strip("`\"'").rstrip(".,;:!?)]}")
    return _NOTION_URL.fullmatch(cleaned) is not None


def _sub_value(match: re.Match, group: int | str) -> str:
    value = match.group(group)
    if _keep(value) or _is_notion_url(value):
        return match.group(0)
    start, end = match.span(group)
    base = match.start(0)
    whole = match.group(0)
    return whole[: start - base] + MASK + whole[end - base :]


_UNAMBIGUOUS_LABELS = {
    "パスワード",
    "暗証番号",
    "ネットワークキー",
    "password",
    "passwd",
    "pw",
    "pwd",
}


def _is_unambiguous_label(label: str) -> bool:
    normalized = label.lower()
    return normalized in _UNAMBIGUOUS_LABELS or normalized.rsplit("_", 1)[-1] in _UNAMBIGUOUS_LABELS


def _mask_secret_value(match: re.Match, key_group: int | str, value_group: int | str) -> str:
    label = match.group(key_group)
    value = match.group(value_group)
    cleaned = value.strip("`\"'")
    if (cleaned.startswith("/") or cleaned.startswith("~/")) and not _is_unambiguous_label(label):
        return match.group(0)
    return _sub_value(match, value_group)


def _mask_label_value(match: re.Match) -> str:
    return _mask_secret_value(match, 1, 4)


def _mask_prose_label_value(match: re.Match) -> str:
    label = match.group("label")
    value = match.group("value")
    if _NO_VALUE_CONTEXT.match(match.group("gap")):
        return match.group(0)
    if value.casefold() in _COMMON_LABEL_VALUES:
        return match.group(0)
    cleaned = value.strip("`\"'")
    if _keep(value) or _is_notion_url(value):
        return match.group(0)
    if (cleaned.startswith("/") or cleaned.startswith("~/")) and not _is_unambiguous_label(label):
        return match.group(0)
    return _sub_value(match, "value")


def _looks_random(value: str) -> bool:
    """Return whether a long run resembles a generated secret."""
    if _keep(value):
        return False
    if re.fullmatch(r"[0-9a-fA-F]{32,}", value):
        return True
    return (
        len(value) >= 32
        and bool(re.search(r"[A-Za-z]", value))
        and bool(re.search(r"[0-9]", value))
    )


def _redact_long_random_values(text: str) -> tuple[str, int]:
    """Mask generated-looking values on lines that contain a secret name."""
    count = 0
    output: list[str] = []

    for raw_line in text.splitlines(keepends=True):
        line = raw_line.rstrip("\r\n")
        ending = raw_line[len(line) :]
        if not _SECRET_NAME.search(line):
            output.append(raw_line)
            continue

        protected = [match.span() for match in _NOTION_URL.finditer(line)]

        def replace(match: re.Match) -> str:
            nonlocal count
            start, end = match.span()
            if any(start >= protected_start and end <= protected_end for protected_start, protected_end in protected):
                return match.group(0)
            if not _looks_random(match.group(0)):
                return match.group(0)
            count += 1
            return MASK

        output.append(_RANDOM_RUN.sub(replace, line) + ending)

    return "".join(output), count


def _mask_url_credential(match: re.Match) -> str:
    value = match.group(2)
    if _keep(value):
        return match.group(0)
    return f"{match.group(1)}:{MASK}@"


def redact(text: str) -> tuple[str, int]:
    """Return text with credential values replaced by MASK, and how many were replaced."""
    count = 0

    def counted(fn):
        def inner(match: re.Match) -> str:
            nonlocal count
            out = fn(match)
            if out != match.group(0):
                count += 1
            return out

        return inner

    text = _PEM.sub(counted(lambda m: MASK), text)
    text = _URL_CRED.sub(counted(_mask_url_credential), text)
    text = _PREFIXED_TOKEN.sub(counted(lambda m: MASK), text)
    text = _AUTH_HEADER.sub(counted(lambda m: _sub_value(m, 2)), text)
    text = _CURL_HEADER.sub(counted(lambda m: _sub_value(m, "value")), text)
    text = _QUERY_TOKEN.sub(counted(lambda m: _sub_value(m, 2)), text)
    text = _QUOTED_KV.sub(counted(lambda m: _mask_secret_value(m, "key", "value")), text)
    text = _QUOTED_ASSIGNMENT_KV.sub(
        counted(lambda m: _mask_secret_value(m, "key", "value")), text
    )
    text = _KV.sub(counted(lambda m: _mask_secret_value(m, 1, 4)), text)
    text = _LABEL.sub(counted(_mask_label_value), text)
    text = _SHORT_LABEL.sub(counted(_mask_label_value), text)
    text = _LABEL_PROSE.sub(counted(_mask_prose_label_value), text)
    text, random_count = _redact_long_random_values(text)
    count += random_count
    return text, count


def redact_record(record: dict) -> int:
    """Mask record text and title in place; return the number of values masked."""
    record["text"], count = redact(record.get("text", ""))
    if "title" in record:
        record["title"], title_count = redact(record.get("title", ""))
        count += title_count
    return count


# Lines still worth a human look after masking: a secret-ish word with something after it.
_SUSPECT = re.compile(r"(?i)(パスワード|password|passwd|secret|token|認証情報|ログイン情報|root\s*/)")


def _shape(line: str) -> str:
    """Show a line's structure without its values: ASCII runs become their length."""
    return re.sub(r"[A-Za-z0-9_\-./+=@:!#$%^&*]{3,}", lambda m: f"<{len(m.group(0))}>", line)


def main() -> int:
    import config

    parser = argparse.ArgumentParser(description="data/clean の認証情報を伏字にする")
    parser.add_argument("--check", action="store_true", help="書き換えずに件数と要確認行（値は伏せる）を表示")
    args = parser.parse_args()

    total = 0
    files = 0
    suspects: list[str] = []
    for path in sorted(config.CLEAN_DIR.glob("*.jsonl")):
        records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
        masked = sum(redact_record(record) for record in records)
        for record in records:
            for line in record["text"].splitlines():
                if _SUSPECT.search(line) and MASK not in line:
                    suspects.append(f"{record['title']}: {_shape(line.strip())[:100]}")
        if masked:
            files += 1
            total += masked
            if not args.check:
                with path.open("w", encoding="utf-8") as handle:
                    for record in records:
                        handle.write(json.dumps(record, ensure_ascii=False) + "\n")

    verb = "伏字の対象" if args.check else "伏字にしました"
    print(f"{verb}: {total} 箇所（{files} ファイル）")
    if suspects:
        print(f"要確認（伏字にならなかったが怪しい行。値は <長さ> で表示）: {len(suspects)} 行")
        for line in suspects:
            print("  " + line)
    if not args.check and total:
        print("次は: uv run python index_build.py --target vectorize（Vectorize 側を上書き）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
