"""Credential redaction rules and pipeline integration."""

from __future__ import annotations

import json

import redact


def test_authorization_headers_mask_only_values():
    text = "Authorization: Bearer dummy-pass-123\nAuthorization: Basic ZHVtbXktcGFzcy0xMjM="

    masked, count = redact.redact(text)

    assert masked == "Authorization: Bearer [伏字]\nAuthorization: Basic [伏字]"
    assert count == 2


def test_curl_secret_headers_mask_only_values():
    text = 'curl -H "X-Something-Token: dummy-pass-123" -H \'X-Client-Secret: dummy-secret-456\''

    masked, count = redact.redact(text)

    assert masked == 'curl -H "X-Something-Token: [伏字]" -H \'X-Client-Secret: [伏字]\''
    assert count == 2


def test_non_secret_header_is_unchanged():
    text = 'curl -H "X-Request-Id: dummy-request-123"'

    assert redact.redact(text) == (text, 0)


def test_secret_name_masks_long_hex_and_base64_like_values():
    hex_value = "0123abcd" * 8
    base64_value = "Abc123_-" * 6
    text = f"# ADMIN_TOKEN {hex_value}\nCLIENT_SECRET={base64_value}"

    masked, count = redact.redact(text)

    assert masked == "# ADMIN_TOKEN [伏字]\nCLIENT_SECRET=[伏字]"
    assert count == 2


def test_notion_token_prefix_does_not_mask_secret_key_names():
    token = "secret_" + "a1" * 22
    text = f"secret_access_key: dummy-value-1\n{token}"

    masked, count = redact.redact(text)

    assert masked == f"secret_access_key: [伏字]\n[伏字]"
    assert count == 2


def test_secret_value_in_a_table_is_masked():
    value = "0123abcd" * 8
    text = f"| DB_PASSWORD | {value} |"

    assert redact.redact(text) == ("| DB_PASSWORD | [伏字] |", 1)


def test_long_random_value_without_secret_name_is_unchanged():
    value = "0123abcd" * 8
    text = f"通常の識別子: {value}"

    assert redact.redact(text) == (text, 0)


def test_notion_urls_and_page_ids_are_unchanged():
    urls = (
        "https://www.notion.so/Title-3c641f19206380b38c93e0d81c3b014e",
        "https://app.notion.com/p/3c641f19206380b38c93e0d81c3b014e",
    )
    text = "NOTION_TOKEN=" + urls[0] + "\nSECRET_KEY=" + urls[1]

    assert redact.redact(text) == (text, 0)


def test_quoted_secret_keys_mask_values():
    text = '{"password":"dummy-pass-123", "token": "dummy-token-456", "api_key": "dummy-key-789"}'

    masked, count = redact.redact(text)

    assert masked == '{"password":"[伏字]", "token": "[伏字]", "api_key": "[伏字]"}'
    assert count == 3


def test_quoted_secret_values_keep_spaces_inside_quotes():
    text = 'password = "hello world"\nDB_PASSWORD="my pass word"\n\'api_key\': \'a b c\''

    masked, count = redact.redact(text)

    assert masked == 'password = "[伏字]"\nDB_PASSWORD="[伏字]"\n\'api_key\': \'[伏字]\''
    assert count == 3


def test_placeholders_are_not_masked():
    values = ("<生成した値>", "<既存値>", "${VAR}", "$VAR", "xxx", "...")
    text = "\n".join(f'{{"password": "{value}"}}' for value in values)

    assert redact.redact(text) == (text, 0)
    url = "https://user:xxx@example.com"
    assert redact.redact(url) == (url, 0)


def test_password_labels_mask_values_but_path_prose_does_not():
    positive = "pass: xxxxx\nパスワード：xxxx\nPW dummy-pass-123"
    masked, count = redact.redact(positive)
    assert masked == "pass: [伏字]\nパスワード：[伏字]\nPW [伏字]"
    assert count == 3

    negative = "\n".join(
        (
            "パスは /opt/app",
            "compass value",
            "パスワードを設定したら、いったん抜ける",
            "パスワードリセット",
            "メールアドレスとパスワード入力すると",
            "please pass this",
        )
    )
    assert redact.redact(negative) == (negative, 0)


def test_glued_secret_environment_names_mask_values_but_ordinary_words_do_not():
    positive = (
        "PGPASSWORD='dummyPw1' psql -h db.example.com -U app -d appdb -f dump.sql\n"
        "MYSQL_PWD=dummyMysql1\n"
        "DBPASSWORD=dummyDb1\n"
        "APITOKEN=dummyApi1\n"
        "CLIENTSECRET=dummyClient1"
    )
    masked, count = redact.redact(positive)
    assert masked == (
        "PGPASSWORD='[伏字]' psql -h db.example.com -U app -d appdb -f dump.sql\n"
        "MYSQL_PWD=[伏字]\n"
        "DBPASSWORD=[伏字]\n"
        "APITOKEN=[伏字]\n"
        "CLIENTSECRET=[伏字]"
    )
    assert count == 5

    negative = "PGPASS=dummyPass1\nbypass dummyPass2\ncompass dummyPass3\ntokenization dummyPass4"
    assert redact.redact(negative) == (negative, 0)


def test_japanese_label_particles_and_instructions_mask_ascii_values():
    text = "\n".join(
        (
            "無線の場合，基本的にネットワーク名はexample-ssid，パスワードはdummy123",
            "パスワードが dummy123 です",
            "パスワード dummy123",
            "パスワードはdummy123で",
            "パスワードの欄に dummy123 と入力してログインを押下",
            "ログインは学籍番号s000000・パスワードdummy1でOK",
            "暗証番号はdummyPin1",
            "ネットワークキーがdummyKey1",
            "PW dummyPw2",
            "pass: dummyPass2",
        )
    )

    masked, count = redact.redact(text)

    assert masked == "\n".join(
        (
            "無線の場合，基本的にネットワーク名はexample-ssid，パスワードは[伏字]",
            "パスワードが [伏字] です",
            "パスワード [伏字]",
            "パスワードは[伏字]で",
            "パスワードの欄に [伏字] と入力してログインを押下",
            "ログインは学籍番号s000000・パスワード[伏字]でOK",
            "暗証番号は[伏字]",
            "ネットワークキーが[伏字]",
            "PW [伏字]",
            "pass: [伏字]",
        )
    )
    assert count == 10


def test_prose_label_values_keep_terms_placeholders_urls_and_no_value_phrases():
    text = "\n".join(
        (
            "パスワードはLogin",
            "パスワードはrequired",
            "パスワードはsha256",
            "パスワードは${PASSWORD}",
            "パスワードは[伏字]",
            "パスワードはhttps://www.notion.so/example-page",
            "パスワード再設定",
            "パスワードリセット",
            "パスワード変更",
            "パスワードを設定",
            "ユーザ名:パスワード@ホスト",
        )
    )

    assert redact.redact(text) == (text, 0)


def test_labels_do_not_mask_absolute_paths_unless_unambiguous():
    text = "\n".join(
        (
            "インストールパス：/opt/app/bin",
            "パス: /usr/local/bin",
            "pass: /srv/app",
            "token: ~/token-file",
            "password: /srv/password-file",
            "パスワード：/srv/password-file",
            "pw ~/password-file",
        )
    )

    masked, count = redact.redact(text)

    assert masked == "\n".join(
        (
            "インストールパス：/opt/app/bin",
            "パス: /usr/local/bin",
            "pass: /srv/app",
            "token: ~/token-file",
            "password: [伏字]",
            "パスワード：[伏字]",
            "pw [伏字]",
        )
    )
    assert count == 3


def test_redact_count_and_record_mutation():
    record = {"text": "token: dummy-token-123", "title": "DB password: dummy123"}

    count = redact.redact_record(record)

    assert count == 2
    assert record == {"text": "token: [伏字]", "title": "DB password: [伏字]"}


def test_prepare_redacts_before_chunking(monkeypatch):
    import prepare

    seen: list[str] = []
    seen_titles: list[str] = []
    monkeypatch.setattr(
        prepare.notion_md,
        "blocks_to_markdown",
        lambda *args, **kwargs: seen_titles.append(kwargs["title"]) or "password: dummy-pass-123",
    )
    monkeypatch.setattr(
        prepare.chunking,
        "chunk_markdown",
        lambda markdown: seen.append(markdown) or [{"text": markdown, "heading_path": []}],
    )

    records = prepare.process_page(
        {
            "id": "page-1",
            "title": "DB password: title-secret",
            "url": "https://www.notion.so/page-1",
            "last_edited": "2026-11-09T00:00:00.000Z",
            "blocks": [],
        },
        skip_images=True,
    )

    assert seen == ["password: [伏字]"]
    assert seen_titles == ["DB password: [伏字]"]
    assert records[0]["text"] == "password: [伏字]"
    assert records[0]["title"] == "DB password: [伏字]"


def test_load_records_redacts_legacy_jsonl(monkeypatch, tmp_path):
    import index_build

    path = tmp_path / "page.jsonl"
    path.write_text(
        json.dumps({"text": "token: dummy-token-123", "id": "page-1"}) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(index_build.config, "CLEAN_DIR", tmp_path)
    monkeypatch.setattr(index_build.config, "DATA_DIR", tmp_path)

    records = index_build._load_records()

    assert records[0]["text"] == "token: [伏字]"
