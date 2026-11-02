"""监控账号管理测试：SQLite accounts 表（config 同步 / 搜索添加 / 删除权限）。"""
from __future__ import annotations

from app.config import DouyinAccount
from app.db import Store

SEC_UID = "MS4wLjABAAAAabcdefghij0123456789_-abc"
SEC_UID2 = "MS4wLjABAAAAzzzzzzzzzz0123456789_-xyz"


def make_store(tmp_path) -> Store:
    store = Store(tmp_path / "t.db")
    store.sync_config_accounts([DouyinAccount(name="配置博主", sec_uid=SEC_UID)])
    return store


def test_sync_config_accounts_idempotent(tmp_path):
    store = make_store(tmp_path)
    store.sync_config_accounts([DouyinAccount(name="改名也不覆盖", sec_uid=SEC_UID)])
    accts = store.list_accounts()
    assert len(accts) == 1
    assert accts[0]["name"] == "配置博主" and accts[0]["source"] == "config"


def test_upsert_and_fetch_all_flag(tmp_path):
    store = make_store(tmp_path)
    store.upsert_account(SEC_UID2, "搜索到的博主")
    acct = store.get_account(SEC_UID2)
    assert acct["source"] == "search" and acct["fetched_all"] == 0
    store.set_fetched_all(SEC_UID2)
    assert store.get_account(SEC_UID2)["fetched_all"] == 1
    # 再同步同名 config 账号：不覆盖搜索来源信息
    store.sync_config_accounts([DouyinAccount(name="配置同名", sec_uid=SEC_UID2)])
    assert store.get_account(SEC_UID2)["source"] == "search"


def test_delete_only_search_source(tmp_path):
    store = make_store(tmp_path)
    store.upsert_account(SEC_UID2, "搜索到的博主")
    assert store.delete_account(SEC_UID2) is True
    assert store.get_account(SEC_UID2) is None
    # config 来源不可通过 API 删除
    assert store.delete_account(SEC_UID) is False
    assert store.get_account(SEC_UID) is not None
    store.close()
