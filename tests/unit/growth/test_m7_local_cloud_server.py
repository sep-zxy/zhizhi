"""An isolated acceptance cloud can resume the same account after restart."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest
from acceptance.scenarios.m7_local_cloud_server import load_identity

if TYPE_CHECKING:
    from pathlib import Path


def test_local_cloud_identity_survives_restart(tmp_path: Path) -> None:
    credentials = tmp_path / "credentials.json"
    dsn = "host=127.0.0.1 port=55433 dbname=acceptance"
    first_auth = load_identity(credentials, dsn, 8777, [])
    original = json.loads(credentials.read_text(encoding="utf-8"))
    original_account = first_auth.verify(original["access_token"])

    renewed_auth = load_identity(credentials, dsn, 8781, [])
    renewed = json.loads(credentials.read_text(encoding="utf-8"))
    assert renewed_auth.verify(original["access_token"]) == original_account
    assert renewed_auth.verify(renewed["access_token"]) == original_account
    assert renewed["account_id"] == original["account_id"]
    assert renewed["port"] == 8781

    with pytest.raises(ValueError, match="数据库与原账号不一致"):
        load_identity(credentials, dsn + "_other", 8781, [])

    (tmp_path / "issuer-private-key.pem").unlink()
    with pytest.raises(ValueError, match="身份文件不完整"):
        load_identity(credentials, dsn, 8781, [])
