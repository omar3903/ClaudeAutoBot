"""The dashboard's .env writer: allow-list, validation, in-place edits, masking."""

from __future__ import annotations

import os

import pytest

from tos_bot import secrets_store as ss

_TOUCHED = ("IBKR_HOST", "IBKR_ACCOUNT_ID", "IBKR_CLIENT_ID", "IBKR_PAPER_PORT", "IBKR_READONLY", "IBKR_MARKET_DATA")


@pytest.fixture
def env(tmp_path, monkeypatch):
    # write() mirrors values into os.environ; delenv first so monkeypatch cleans them up
    for k in _TOUCHED:
        monkeypatch.delenv(k, raising=False)
    p = tmp_path / ".env"
    p.write_text(
        "# header comment\n"
        "PAPER_PLATFORM=ibkr\n"
        "IBKR_ACCOUNT_ID=DU7654321\n"
        "IBKR_HOST=                 # the Gateway machine\n"
        "IBKR_CLIENT_ID=11\n"
        "IBKR_CLIENT_ID=12\n"
        "DB_PASSWORD=untouched\n",
        encoding="utf-8",
    )
    return p


def test_updates_in_place_keeping_comments_and_other_lines(env):
    changed = ss.write({"IBKR_ACCOUNT_ID": "DU1234567", "IBKR_HOST": "192.168.1.20"}, path=env)
    text = env.read_text(encoding="utf-8")
    assert changed == ["IBKR_ACCOUNT_ID", "IBKR_HOST"]
    assert "# header comment" in text and "PAPER_PLATFORM=ibkr" in text and "DB_PASSWORD=untouched" in text
    assert "IBKR_ACCOUNT_ID=DU1234567" in text and "DU7654321" not in text
    assert "IBKR_HOST=192.168.1.20                 # the Gateway machine" in text
    assert os.environ["IBKR_ACCOUNT_ID"] == "DU1234567"


def test_duplicates_collapse_and_new_keys_go_under_one_marker(env):
    ss.write({"IBKR_CLIENT_ID": 21, "IBKR_PAPER_PORT": "7497"}, path=env)
    ss.write({"IBKR_MARKET_DATA": "delayed"}, path=env)
    text = env.read_text(encoding="utf-8")
    assert text.count("IBKR_CLIENT_ID=") == 1 and "IBKR_CLIENT_ID=21" in text
    assert text.count("saved from the AutoTradeBot dashboard") == 1
    assert "IBKR_PAPER_PORT=7497" in text and "IBKR_MARKET_DATA=delayed" in text


def test_unchanged_value_reports_nothing(env):
    assert ss.write({"IBKR_ACCOUNT_ID": "DU7654321"}, path=env) == []
    assert ss.write({"IBKR_ACCOUNT_ID": None}, path=env) == []         # None = leave alone


@pytest.mark.parametrize("updates,msg", [
    ({"DB_PASSWORD": "x"}, "can't be changed"),
    ({"SCHWAB_API_KEY": "x"}, "can't be changed"),
    ({"IBKR_HOST": "abc\nPAPER_PLATFORM=live"}, "line breaks"),
    ({"IBKR_PAPER_PORT": "abc"}, "whole number"),
    ({"IBKR_PAPER_PORT": "70000"}, "between"),
    ({"IBKR_MARKET_DATA": "fast"}, "one of"),
    ({"IBKR_READONLY": "maybe"}, "on or off"),
])
def test_validation_rejects_and_writes_nothing(env, updates, msg):
    before = env.read_text(encoding="utf-8")
    with pytest.raises(ValueError, match=msg):
        ss.write(updates, path=env)
    assert env.read_text(encoding="utf-8") == before


def test_bool_and_int_are_normalised(env):
    ss.write({"IBKR_READONLY": True, "IBKR_CLIENT_ID": " 007 "}, path=env)
    vals = ss.read_env(env)
    assert vals["IBKR_READONLY"] == "1" and vals["IBKR_CLIENT_ID"] == "7"


def test_values_that_need_quotes_round_trip(env):
    tricky = 'a b#c"d\\e'
    ss.write({"IBKR_ACCOUNT_ID": tricky}, path=env)
    assert ss.read_env(env)["IBKR_ACCOUNT_ID"] == tricky


def test_clearing_and_creating(env, tmp_path):
    ss.write({"IBKR_ACCOUNT_ID": ""}, path=env)
    assert ss.read_env(env)["IBKR_ACCOUNT_ID"] == ""
    fresh = tmp_path / "new.env"
    ss.write({"IBKR_HOST": "127.0.0.1"}, path=fresh)
    assert ss.read_env(fresh)["IBKR_HOST"] == "127.0.0.1"


def test_describe_never_returns_secret_values(env):
    rows = {r["key"]: r for r in ss.describe(env)}
    account = rows["IBKR_ACCOUNT_ID"]
    assert account["set"] and account["hint"] == "…4321" and "value" not in account
    assert "DU7654321" not in repr(rows)
    assert rows["IBKR_CLIENT_ID"]["value"] in ("11", "12")             # non-secrets are shown
    assert rows["IBKR_PAPER_PORT"]["default"] == "4002"
    assert all(key.startswith("IBKR_") for key in rows)
    assert ss.mask("DU1234567") == "…4567" and ss.mask("") == ""


def test_the_news_key_has_its_own_group_and_stays_secret(env, monkeypatch):
    monkeypatch.delenv("FINNHUB_API_KEY", raising=False)
    assert "FINNHUB_API_KEY" not in {r["key"] for r in ss.describe(env)}
    assert ss.write({"FINNHUB_API_KEY": "abcd1234efgh5678"}, env) == ["FINNHUB_API_KEY"]
    [row] = ss.describe(env, fields=ss.SIGNAL_FIELDS)
    assert row["secret"] and row["set"] and row["hint"] == "…5678" and "abcd1234" not in repr(row)
