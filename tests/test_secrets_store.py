"""The dashboard's .env writer: allow-list, validation, in-place edits, masking."""

from __future__ import annotations

import os

import pytest

from tos_bot import secrets_store as ss

_TOUCHED = ("SCHWAB_API_KEY", "SCHWAB_APP_SECRET", "SCHWAB_ACCOUNT_ID", "SCHWAB_CALLBACK_URL",
            "IBKR_CLIENT_ID", "IBKR_PAPER_PORT", "IBKR_READONLY", "IBKR_MARKET_DATA")


@pytest.fixture
def env(tmp_path, monkeypatch):
    # write() mirrors values into os.environ; delenv first so monkeypatch cleans them up
    for k in _TOUCHED:
        monkeypatch.delenv(k, raising=False)
    p = tmp_path / ".env"
    p.write_text(
        "# header comment\n"
        "BROKER=paper\n"
        "SCHWAB_API_KEY=oldkey1234567\n"
        "SCHWAB_ACCOUNT_ID=                 # your 8-digit account\n"
        "IBKR_CLIENT_ID=11\n"
        "IBKR_CLIENT_ID=12\n"
        "DB_PASSWORD=untouched\n",
        encoding="utf-8",
    )
    return p


def test_updates_in_place_keeping_comments_and_other_lines(env):
    changed = ss.write({"SCHWAB_API_KEY": "newkey987654321", "SCHWAB_ACCOUNT_ID": "12345678"}, path=env)
    text = env.read_text(encoding="utf-8")
    assert changed == ["SCHWAB_ACCOUNT_ID", "SCHWAB_API_KEY"]
    assert "# header comment" in text and "BROKER=paper" in text and "DB_PASSWORD=untouched" in text
    assert "SCHWAB_API_KEY=newkey987654321" in text and "oldkey" not in text
    assert "SCHWAB_ACCOUNT_ID=12345678                 # your 8-digit account" in text
    assert os.environ["SCHWAB_API_KEY"] == "newkey987654321"


def test_duplicates_collapse_and_new_keys_go_under_one_marker(env):
    ss.write({"IBKR_CLIENT_ID": 21, "IBKR_PAPER_PORT": "7497"}, path=env)
    ss.write({"SCHWAB_APP_SECRET": "s3cr3tvalue999"}, path=env)
    text = env.read_text(encoding="utf-8")
    assert text.count("IBKR_CLIENT_ID=") == 1 and "IBKR_CLIENT_ID=21" in text
    assert text.count("saved from the AutoTradeBot dashboard") == 1
    assert "IBKR_PAPER_PORT=7497" in text and "SCHWAB_APP_SECRET=s3cr3tvalue999" in text


def test_unchanged_value_reports_nothing(env):
    assert ss.write({"SCHWAB_API_KEY": "oldkey1234567"}, path=env) == []
    assert ss.write({"SCHWAB_API_KEY": None}, path=env) == []        # None = leave alone


@pytest.mark.parametrize("updates,msg", [
    ({"DB_PASSWORD": "x"}, "can't be changed"),
    ({"SCHWAB_API_KEY": "abc\nBROKER=live"}, "line breaks"),
    ({"IBKR_PAPER_PORT": "abc"}, "whole number"),
    ({"IBKR_PAPER_PORT": "70000"}, "between"),
    ({"IBKR_MARKET_DATA": "fast"}, "one of"),
    ({"IBKR_READONLY": "maybe"}, "on or off"),
    ({"SCHWAB_CALLBACK_URL": "https://localhost:8182"}, "127"),
    ({"SCHWAB_CALLBACK_URL": "https://127.0.0.1"}, "127"),
    ({"SCHWAB_CALLBACK_URL": "http://127.0.0.1:8182"}, "127"),
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
    ss.write({"SCHWAB_APP_SECRET": tricky}, path=env)
    assert ss.read_env(env)["SCHWAB_APP_SECRET"] == tricky


def test_clearing_and_creating(env, tmp_path):
    ss.write({"SCHWAB_API_KEY": ""}, path=env)
    assert ss.read_env(env)["SCHWAB_API_KEY"] == ""
    fresh = tmp_path / "new.env"
    ss.write({"IBKR_HOST": "127.0.0.1"}, path=fresh)
    assert ss.read_env(fresh)["IBKR_HOST"] == "127.0.0.1"


def test_describe_never_returns_secret_values(env):
    rows = {r["key"]: r for r in ss.describe(env)}
    key = rows["SCHWAB_API_KEY"]
    assert key["set"] and key["hint"] == "…4567" and "value" not in key
    assert "oldkey" not in repr(rows)
    assert rows["IBKR_CLIENT_ID"]["value"] in ("11", "12")      # non-secrets are shown
    assert rows["IBKR_PAPER_PORT"]["default"] == "4002"
    assert ss.mask("DU1234567") == "…4567" and ss.mask("") == ""
