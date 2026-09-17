import base64
import importlib.util
import json
import sqlite3
import struct
import time
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

SOURCE = Path(__file__).resolve().parents[1] / "tools/maozi-credential-refresh/refresh.py"
spec = importlib.util.spec_from_file_location("credential_refresh", SOURCE)
refresh = importlib.util.module_from_spec(spec)
spec.loader.exec_module(refresh)


def vint(n):
    result = bytearray()
    while n >= 128:
        result.append((n & 127) | 128)
        n >>= 7
    result.append(n)
    return bytes(result)


def token(uid="account-a", issued=10, expiry=None):
    encode = lambda value: base64.urlsafe_b64encode(json.dumps(value).encode()).rstrip(b"=").decode()
    return encode({"alg": "HS256"}) + "." + encode({
        "uid": uid, "iss": "fixture-maozi", "iat": issued,
        "exp": expiry if expiry is not None else time.time() + 3600,
    }) + ".fixture_signature"


def batch(seq, values):
    output = struct.pack("<QI", seq, len(values))
    for key, value in values:
        output += bytes([0 if value is None else 1]) + vint(len(key)) + key
        if value is not None:
            output += vint(len(value)) + value
    return output


def physical(value, kind=1):
    return b"\0" * 4 + struct.pack("<HB", len(value), kind) + value


def data_block(items):
    output = b""
    previous = b""
    for key, value in items:
        shared = 0
        while shared < min(len(previous), len(key)) and previous[shared] == key[shared]:
            shared += 1
        suffix = key[shared:]
        output += vint(shared) + vint(len(suffix)) + vint(len(value)) + suffix + value
        previous = key
    return output + struct.pack("<II", 0, 1)


def sst(seq, key, value):
    internal = key + struct.pack("<Q", (seq << 8) | 1)
    body = data_block([(internal, value)])
    index = data_block([(internal, vint(0) + vint(len(body)))])
    trailer = b"\0" * 5
    index_offset = len(body) + 5
    footer = vint(0) + vint(0) + vint(index_offset) + vint(len(index))
    return body + trailer + index + trailer + footer.ljust(40, b"\0") + bytes.fromhex("57fb808b247547db")


@pytest.mark.parametrize("encoded,expected", [
    (b"\x03\x08abc", b"abc"),
    (b"\x09\x00a\x11\x01", b"aaaaaaaaa"),  # copy-1, overlapping
    (b"\x06\x08abc\x0a\x03\x00", b"abcabc"),  # copy-2
    (b"\x06\x08abc\x0b\x03\x00\x00\x00", b"abcabc"),  # copy-4
    (vint(61) + b"\xf0\x3c" + b"x" * 61, b"x" * 61),
])
def test_snappy_all_encoding_types(encoded, expected):
    assert refresh.snappy(encoded) == expected


@pytest.mark.parametrize("value", [b"\x05\x01\x01", b"\x03\x08a", b"\x04\x08abc", b"\xff" * 11])
def test_snappy_rejects_corruption(value):
    with pytest.raises(ValueError):
        refresh.snappy(value)


def test_newest_record_and_logout_tombstone(tmp_path):
    old, new = token(issued=1), token(issued=2)
    (tmp_path / "000001.ldb").write_bytes(sst(12, refresh.ACCESS_KEY, b"\x01" + old.encode()))
    (tmp_path / "000002.log").write_bytes(physical(batch(20, [(refresh.ACCESS_KEY, b"\x01" + new.encode())])))
    assert refresh.profile_token(tmp_path) == new
    (tmp_path / "000003.log").write_bytes(physical(batch(30, [(refresh.ACCESS_KEY, None)])))
    assert refresh.profile_token(tmp_path) is None


def test_ignore_other_origins_and_decode_utf16(tmp_path):
    valid = token()
    value = b"\0" + json.dumps({"accessToken": valid}).encode("utf-16-le")
    (tmp_path / "000001.log").write_bytes(physical(batch(10, [
        (refresh.ACCESS_KEY, value),
        (b"_https://other.example\x00\x01token", b"\x01" + token("unrelated").encode()),
    ])))
    assert refresh.profile_token(tmp_path) == valid


def test_fragmented_log_reconstruction():
    key = b"large-unrelated-entry"
    value = b"x" * 40000
    logical = batch(1, [(key, value)])
    split = 32768 - 7
    data = physical(logical[:split], 2) + physical(logical[split:], 4)
    assert list(refresh.log_records(data)) == [(1, key, value)]
    with pytest.raises(ValueError):
        list(refresh.log_records(data[:-3]))


def test_expired_profile_not_a_candidate(tmp_path):
    folder = tmp_path / "Default/Local Storage/leveldb"
    folder.mkdir(parents=True)
    (folder / "000001.log").write_bytes(physical(batch(1, [
        (refresh.ACCESS_KEY, b"\x01" + token(expiry=1).encode()),
    ])))
    assert refresh.chrome_candidates(tmp_path) == []


@pytest.fixture
def database(tmp_path):
    key = Fernet.generate_key()
    cipher = Fernet(key)
    (tmp_path / "master.key").write_bytes(key)
    db = sqlite3.connect(tmp_path / "flowhub.sqlite3")
    db.executescript("""
        CREATE TABLE stores(id TEXT PRIMARY KEY, owner TEXT, name TEXT, config TEXT, secret TEXT, enabled INT);
        CREATE TABLE workflows(owner TEXT PRIMARY KEY, secrets TEXT, updated REAL, enabled INT, notice TEXT);
        CREATE TABLE jobs(id TEXT, phase TEXT);
        INSERT INTO jobs VALUES ('existing-job','attention');
    """)
    encrypt = lambda value: cipher.encrypt(json.dumps(value).encode()).decode()
    old = token(issued=1)
    db.execute("INSERT INTO stores VALUES (?,?,?,?,?,?)", (
        "store-1", "owner-1", "店铺一", json.dumps({"shop_id": "shop-1"}),
        encrypt({"erp_token": old, "client_id": "client-1", "api_key": "existing-private-key"}), 1,
    ))
    db.execute("INSERT INTO workflows VALUES (?,?,?,?,?)", (
        "owner-1", encrypt({"flowb-candidates": old, "flowb-matcher": json.dumps({
            "erp_token": old, "dashscope_api_key": "keep-this-provider-key",
        }), "flowb-profit": old, "unrelated": "keep-this-value"}), 5, 1, "existing notice",
    ))
    db.commit()
    yield tmp_path, db, cipher, old
    db.close()


def shops(_):
    return [{"id": "shop-1", "api_client_id": "client-1"}]


def snapshot(db):
    return tuple(tuple(db.execute("SELECT * FROM " + table)) for table in ("stores", "workflows", "jobs"))


def test_check_never_writes(database):
    directory, db, _, _ = database
    before = snapshot(db)
    result = refresh.refresh(directory, [token(issued=2)], validator=shops)
    assert result["pending_stores"] == 1 and result["pending_modules"] == 3
    assert snapshot(db) == before
    assert not (directory / "credential-backups").exists()


def test_apply_preserves_controls_keys_and_repeated_run(database):
    directory, db, cipher, old = database
    new = token(issued=2)
    result = refresh.refresh(directory, [new], apply=True, validator=shops)
    assert result["updated_stores"] == 1 and result["updated_modules"] == 3
    store = json.loads(cipher.decrypt(db.execute("SELECT secret FROM stores").fetchone()[0].encode()))
    assert store == {"erp_token": new, "client_id": "client-1", "api_key": "existing-private-key"}
    keys = json.loads(cipher.decrypt(db.execute("SELECT secrets FROM workflows").fetchone()[0].encode()))
    assert json.loads(keys["flowb-matcher"]) == {"erp_token": new, "dashscope_api_key": "keep-this-provider-key"}
    assert keys["unrelated"] == "keep-this-value"
    assert db.execute("SELECT enabled,notice FROM workflows").fetchone() == (1, "existing notice")
    assert db.execute("SELECT phase FROM jobs").fetchone()[0] == "attention"
    backup = Path(result["backup"]).read_text()
    assert old not in backup and new not in backup and "existing-private-key" not in backup
    again = refresh.refresh(directory, [new], apply=True, validator=shops)
    assert again["updated_stores"] == 0 and again["updated_modules"] == 0
    assert len(list((directory / "credential-backups").iterdir())) == 1


@pytest.mark.parametrize("remote", [[], [{"id": "shop-1", "api_client_id": "wrong-client"}]])
def test_wrong_store_never_writes(database, remote):
    directory, db, _, _ = database
    before = snapshot(db)
    with pytest.raises(refresh.RefreshError, match="店铺身份"):
        refresh.refresh(directory, [token(issued=2)], apply=True, validator=lambda _: remote)
    assert snapshot(db) == before


def test_wrong_account_never_calls_remote(database):
    directory, db, _, _ = database
    before = snapshot(db)
    def forbidden(_):
        pytest.fail("must not verify an unrelated account")
    with pytest.raises(refresh.RefreshError, match="匹配"):
        refresh.refresh(directory, [token(uid="someone-else")], apply=True, validator=forbidden)
    assert snapshot(db) == before


def test_concurrent_update_rolls_back(database):
    directory, db, cipher, _ = database
    other = cipher.encrypt(b'{"new":"other-writer"}').decode()
    def racing(_):
        db.execute("UPDATE workflows SET secrets=?", (other,))
        db.commit()
        return shops(None)
    old_store = db.execute("SELECT secret FROM stores").fetchone()[0]
    with pytest.raises(refresh.RefreshError, match="并发"):
        refresh.refresh(directory, [token(issued=2)], apply=True, validator=racing)
    assert db.execute("SELECT secret FROM stores").fetchone()[0] == old_store
    assert db.execute("SELECT secrets FROM workflows").fetchone()[0] == other


def test_missing_database_never_initializes(tmp_path):
    with pytest.raises(refresh.RefreshError, match="不会创建"):
        refresh.refresh(tmp_path, [], apply=True)
    assert list(tmp_path.iterdir()) == []
