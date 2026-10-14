"""Retain-side secret scrub (local patch t_38ec6109).

24.09.2026 a live Telegram bot token reached hermes-default verbatim: the scrub lived in the
retired ACP-bridge overlay and the catalog reinstall dropped it, while every profile config
still said ``retain_redact_secrets: true``. These tests pin the scrub to the real payload path.
"""

from __future__ import annotations

import json


TG = "1234567890:AA" + "b" * 33  # synthetic, shape-only


def _content(p, text, **kw):
    return p._build_retain_kwargs(text, **kw)["content"]


def test_tg_token_is_masked_on_the_payload(provider):
    p, _ = provider()
    out = _content(p, f"Привяжи к @some_bot, код {TG}\n\nдальше текст")
    assert TG not in out and "1234567890:AA" not in out
    assert "<redacted-secret>" in out and "дальше текст" in out


def test_tg_token_inside_json_turn_is_masked(provider):
    p, _ = provider()
    turn = json.dumps([{"role": "user", "content": f"код {TG}"}], ensure_ascii=False)
    assert TG not in _content(p, turn)


def test_patterned_secrets_and_context_are_masked(provider):
    p, _ = provider()
    gl = "glpat-" + "x" * 20
    item = p._build_retain_kwargs(f"token {gl}", context=f"ctx {TG}")
    assert gl not in item["content"] and TG not in item["context"]


def test_file_literal_is_masked(tmp_path, provider):
    f = tmp_path / "pass"
    f.write_text("Tr0ub4dor&3xK\n")
    p, _ = provider({"retain_secret_files": [str(f)]})
    out = _content(p, "sudo password is Tr0ub4dor&3xK, remember")
    assert "Tr0ub4dor&3xK" not in out and "sudo password" in out


def test_innocent_text_untouched(provider):
    p, _ = provider()
    s = "Клешня: 12 таблиц, sk-short, id 1234567890 без токена."
    assert _content(p, s) == s


def test_missing_file_and_short_literal_do_not_break(tmp_path, provider):
    short = tmp_path / "s"
    short.write_text("abc")
    p, _ = provider({"retain_secret_files": [str(tmp_path / "nope"), str(short)]})
    assert _content(p, "abc abcdef") == "abc abcdef"


def test_default_on_and_switch_off(provider):
    p, _ = provider()
    assert p._retain_redact_secrets is True
    q, _ = provider({"retain_redact_secrets": False})
    assert _content(q, f"код {TG}") == f"код {TG}"


def test_sync_turn_ships_masked_payload(provider):
    """End to end through sync_turn -> writer -> client: the real wire payload."""
    p, fake = provider()
    p.sync_turn(f"Привяжи бота, код {TG}", "ок")
    p.shutdown()
    blob = json.dumps(fake.retains, ensure_ascii=False, default=str)
    assert fake.retains, "retain never reached the client"
    assert "<redacted-secret>" in blob
    assert TG not in blob
