"""Refresh existing local FlowHub credentials from Chrome. Never exports a token.

Requires FlowHub's existing Python dependencies: cryptography and httpx.
Only the Maozi origin's current access record is inspected. No Chrome automation.
"""
import argparse
import base64
import json
import os
import re
import sqlite3
import struct
import sys
import time
from datetime import datetime
from pathlib import Path

MAX_BLOCK = 64 * 1024 * 1024
ACCESS_KEY = b"_https://ozon.maozierp.com\x00\x01maozierp-core-access"
JWT = re.compile(r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+")


class RefreshError(Exception):
    pass


def varint(data, pos):
    result = 0
    for shift in range(0, 70, 7):
        if pos >= len(data):
            raise ValueError("short varint")
        byte = data[pos]
        pos += 1
        result |= (byte & 127) << shift
        if byte < 128:
            return result, pos
    raise ValueError("long varint")


def snappy(data):
    """Raw Snappy blocks, including overlapping copy operations; no extra wheel."""
    expected, pos = varint(data, 0)
    if expected > MAX_BLOCK:
        raise ValueError("oversized block")
    out = bytearray()
    while pos < len(data):
        tag = data[pos]
        pos += 1
        kind = tag & 3
        if kind == 0:
            length = tag >> 2
            if length < 60:
                length += 1
            else:
                size = length - 59
                if pos + size > len(data):
                    raise ValueError("short literal size")
                length = int.from_bytes(data[pos:pos + size], "little") + 1
                pos += size
            if pos + length > len(data) or len(out) + length > expected:
                raise ValueError("short literal")
            out.extend(data[pos:pos + length])
            pos += length
        else:
            size = {1: 1, 2: 2, 3: 4}[kind]
            if pos + size > len(data):
                raise ValueError("short copy")
            offset = int.from_bytes(data[pos:pos + size], "little")
            pos += size
            length = (4 + ((tag >> 2) & 7)) if kind == 1 else (1 + (tag >> 2))
            if kind == 1:
                offset |= (tag & 224) << 3
            if not 0 < offset <= len(out) or len(out) + length > expected:
                raise ValueError("invalid copy")
            pattern = bytes(out[-offset:])
            out.extend((pattern * ((length + offset - 1) // offset))[:length])
    if len(out) != expected:
        raise ValueError("wrong decoded length")
    return bytes(out)


def block(data, offset, size):
    if size > MAX_BLOCK or offset + size + 5 > len(data):
        raise ValueError("invalid block handle")
    value = data[offset:offset + size]
    compression = data[offset + size]
    if compression == 1:
        return snappy(value)
    if compression == 0:
        return value
    raise ValueError("unknown compression")


def entries(data):
    if len(data) < 4:
        raise ValueError("short block")
    end = len(data) - 4 - int.from_bytes(data[-4:], "little") * 4
    if end < 0:
        raise ValueError("invalid restart array")
    pos, key = 0, b""
    while pos < end:
        shared, pos = varint(data, pos)
        added, pos = varint(data, pos)
        length, pos = varint(data, pos)
        if shared > len(key) or pos + added + length > end:
            raise ValueError("invalid entry")
        key = key[:shared] + data[pos:pos + added]
        pos += added
        value = data[pos:pos + length]
        pos += length
        yield key, value


def sst_records(data):
    if len(data) < 48 or data[-8:] != bytes.fromhex("57fb808b247547db"):
        raise ValueError("not LevelDB table")
    _, pos = varint(data, len(data) - 48)
    _, pos = varint(data, pos)
    offset, pos = varint(data, pos)
    size, pos = varint(data, pos)
    for _, handle in entries(block(data, offset, size)):
        offset, pos = varint(handle, 0)
        size, pos = varint(handle, pos)
        for key, value in entries(block(data, offset, size)):
            if len(key) < 8:
                raise ValueError("short internal key")
            number = int.from_bytes(key[-8:], "little")
            yield number >> 8, key[:-8], value if number & 255 == 1 else None


def log_records(data):
    pending = bytearray()
    for start in range(0, len(data), 32768):
        chunk = data[start:start + 32768]
        pos = 0
        while pos + 7 <= len(chunk):
            length = int.from_bytes(chunk[pos + 4:pos + 6], "little")
            kind = chunk[pos + 6]
            pos += 7
            if kind == 0 and length == 0:
                break
            if pos + length > len(chunk):
                raise ValueError("incomplete live log; retry")
            fragment = chunk[pos:pos + length]
            pos += length
            if kind == 1:
                record = fragment
                pending.clear()
            elif kind == 2:
                pending = bytearray(fragment)
                continue
            elif kind in (3, 4) and pending:
                pending.extend(fragment)
                if kind == 3:
                    continue
                record = bytes(pending)
                pending.clear()
            else:
                raise ValueError("invalid log fragment")
            if len(record) < 12:
                raise ValueError("short write batch")
            seq, count = struct.unpack_from("<QI", record)
            cursor = 12
            for index in range(count):
                kind = record[cursor]
                cursor += 1
                size, cursor = varint(record, cursor)
                key = record[cursor:cursor + size]
                cursor += size
                value = None
                if kind == 1:
                    size, cursor = varint(record, cursor)
                    value = record[cursor:cursor + size]
                    cursor += size
                elif kind != 0:
                    raise ValueError("unknown write type")
                if cursor > len(record):
                    raise ValueError("short write value")
                yield seq + index, key, value
    if pending:
        raise ValueError("unfinished live log; retry")


def claims(token):
    try:
        value = json.loads(base64.urlsafe_b64decode(token.split(".")[1] + "==="))
        if not value.get("uid") or not value.get("iss"):
            raise ValueError()
        return value
    except (ValueError, IndexError, TypeError):
        raise RefreshError("凭证格式不受支持，请重新登录毛子 ERP。") from None


def account(token):
    value = claims(token)
    return str(value["uid"]), str(value["iss"])


def profile_token(directory):
    """Resolve sequence numbers and tombstones, never resurrect a logged-out token."""
    latest = (-1, None)
    files = sorted(p for p in directory.iterdir() if p.suffix in (".ldb", ".log"))
    for path in files:
        data = path.read_bytes()
        parser = sst_records if path.suffix == ".ldb" else log_records
        for seq, key, value in parser(data):
            if key == ACCESS_KEY and seq > latest[0]:
                latest = seq, value
    value = latest[1]
    if not value:
        return None
    if value[0] == 1:
        text = value[1:].decode("utf-8")
    elif value[0] == 0:
        text = value[1:].decode("utf-16-le")
    else:
        raise ValueError("unsupported Chromium string")
    tokens = set(JWT.findall(text))
    if len(tokens) != 1:
        raise ValueError("ambiguous access record")
    return tokens.pop()


def chrome_candidates(root, profile=""):
    profiles = [root / profile] if profile else sorted(
        p for p in root.iterdir() if p.name == "Default" or p.name.startswith("Profile ")
    )
    result, unreadable = [], []
    for path in profiles:
        directory = path / "Local Storage" / "leveldb"
        if not directory.is_dir():
            continue
        try:
            token = profile_token(directory)
            if token and float(claims(token).get("exp", 0)) > time.time() + 60:
                result.append(token)
        except (OSError, ValueError, IndexError, RefreshError):
            unreadable.append(path.name)
    if unreadable:
        raise RefreshError("Chrome 登录文件正在变化或格式不受支持：" + ", ".join(unreadable)
                           + "。请正常退出 Chrome 后重试，或在配置中指定已登录的 profile。")
    return list(set(result))


def validate_remote(token):
    import httpx
    rows = []
    try:
        with httpx.Client(base_url="https://api.maozierp.com", trust_env=False, timeout=25,
                          headers={"Authorization": "Bearer " + token, "Client": "pc"}) as client:
            for page in range(1, 101):
                response = client.get("/api.shop/lists", params={"scene": "erp", "page": page, "page_size": 100})
                if response.status_code != 200:
                    raise RefreshError(f"毛子 ERP 验证失败（HTTP {response.status_code}），未更新凭证。")
                body = response.json()
                if body.get("code") != 1:
                    raise RefreshError("毛子 ERP 未接受新凭证，请重新登录后重试。")
                data = body["data"]
                page_rows = data if isinstance(data, list) else data["data"]
                if not isinstance(page_rows, list):
                    raise ValueError()
                rows.extend(page_rows)
                if (isinstance(data, dict) and page >= int(data.get("last_page", 101))) or len(page_rows) < 100:
                    return rows
        raise RefreshError("店铺列表未能完整读取，未更新凭证。")
    except httpx.HTTPError:
        raise RefreshError("无法直连毛子 ERP，请检查网络后重试；未更新凭证。") from None
    except (ValueError, KeyError, TypeError):
        raise RefreshError("毛子 ERP 返回格式发生变化，未更新凭证。") from None


def refresh(data_dir, tokens, apply=False, validator=validate_remote):
    from cryptography.fernet import Fernet
    if not (data_dir / "flowhub.sqlite3").is_file() or not (data_dir / "master.key").is_file():
        raise RefreshError("指定目录不是已配置的 FlowHub 数据目录；不会创建或初始化数据库。")
    cipher = Fernet((data_dir / "master.key").read_bytes())
    decrypt = lambda text: json.loads(cipher.decrypt(text.encode()))
    encrypt = lambda value: cipher.encrypt(json.dumps(value).encode()).decode()
    db = sqlite3.connect(str(data_dir / "flowhub.sqlite3"), timeout=15)
    db.row_factory = sqlite3.Row
    try:
        stores = list(db.execute("SELECT id,owner,name,config,secret FROM stores"))
        workflows = list(db.execute("SELECT owner,secrets FROM workflows"))
        targets = {}
        for row in stores:
            keys = decrypt(row["secret"])
            if keys.get("erp_token"):
                targets.setdefault(account(keys["erp_token"]), []).append((row, keys))
        matched = [t for t in tokens if account(t) in targets]
        identities = {account(t) for t in matched}
        if len(identities) != 1:
            raise RefreshError("未找到与现有 FlowHub 店铺匹配的单一账号。请先在 Chrome 登录原毛子账号；"
                               "如登录了多个账号，请配置 profile。首次店铺接入仍需先在 FlowHub 配置。")
        token = max(matched, key=lambda t: float(claims(t).get("iat", 0)))
        identity = account(token)
        remote = validator(token)
        remote_by_id = {str(r["id"]): r for r in remote}
        changes, workflow_changes, owners = [], [], set()
        for row, keys in targets[identity]:
            config = json.loads(row["config"])
            shop = remote_by_id.get(str(config.get("shop_id")))
            if not shop or str(shop.get("api_client_id")) != str(keys.get("client_id")):
                raise RefreshError("店铺身份核验未通过：" + row["name"] + "。没有写入任何凭证。")
            owners.add(row["owner"])
            if keys["erp_token"] != token:
                keys["erp_token"] = token
                changes.append((row["id"], row["secret"], encrypt(keys)))
        count = 0
        for row in workflows:
            if row["owner"] not in owners:
                continue
            keys = decrypt(row["secrets"])
            changed = False
            for name, value in list(keys.items()):
                # Only existing ERP module credentials; preserve nested provider keys.
                if name not in ("flowb-candidates", "flowb-matcher", "flowb-profit") or not isinstance(value, str):
                    continue
                try:
                    nested = json.loads(value) if value.lstrip().startswith("{") else None
                    previous = nested.get("erp_token", "") if isinstance(nested, dict) else value
                    if previous and account(previous) == identity and previous != token:
                        if nested is not None:
                            nested["erp_token"] = token
                            keys[name] = json.dumps(nested)
                        else:
                            keys[name] = token
                        count += 1
                        changed = True
                except (RefreshError, ValueError):
                    continue
            if changed:
                workflow_changes.append((row["owner"], row["secrets"], encrypt(keys)))
        result = dict(mode="apply" if apply else "check", verified_stores=len(targets[identity]),
                      updated_stores=len(changes) if apply else 0, updated_modules=count if apply else 0,
                      pending_stores=len(changes), pending_modules=count,
                      expires_at=datetime.fromtimestamp(float(claims(token)["exp"])).astimezone().isoformat())
        if apply and (changes or workflow_changes):
            backup_dir = data_dir / "credential-backups"
            backup_dir.mkdir(mode=0o700, exist_ok=True)
            backup = backup_dir / (str(time.time_ns()) + ".json")
            # The backup contains only existing Fernet ciphertext, never a raw token.
            fd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as stream:
                json.dump({"stores": [(a, b) for a, b, _ in changes],
                           "workflows": [(a, b) for a, b, _ in workflow_changes]}, stream)
                stream.flush()
                os.fsync(stream.fileno())
            with db:
                for sid, before, after in changes:
                    if db.execute("UPDATE stores SET secret=? WHERE id=? AND secret=?", (after, sid, before)).rowcount != 1:
                        raise RefreshError("更新时检测到并发修改，已回滚，请重试。")
                for owner, before, after in workflow_changes:
                    if db.execute("UPDATE workflows SET secrets=?,updated=? WHERE owner=? AND secrets=?",
                                  (after, time.time(), owner, before)).rowcount != 1:
                        raise RefreshError("更新时检测到并发修改，已回滚，请重试。")
                for sid, _, after in changes:
                    if db.execute("SELECT secret FROM stores WHERE id=?", (sid,)).fetchone()[0] != after:
                        raise RefreshError("凭证回查失败，已回滚。")
                for owner, _, after in workflow_changes:
                    if db.execute("SELECT secrets FROM workflows WHERE owner=?", (owner,)).fetchone()[0] != after:
                        raise RefreshError("模块凭证回查失败，已回滚。")
            result["backup"] = str(backup)
        result["verified"] = True
        return result
    finally:
        db.close()


def main():
    parser = argparse.ArgumentParser(description="本地 Chrome → 毛子 ERP 验证 → FlowHub 凭证更新")
    parser.add_argument("--config", type=Path, default=Path(__file__).with_name("config.json"))
    parser.add_argument("--data", type=Path)
    parser.add_argument("--chrome-root", type=Path)
    parser.add_argument("--profile")
    parser.add_argument("--apply", action="store_true", help="写入已验证的凭证；默认只检查")
    args = parser.parse_args()
    config = json.loads(args.config.read_text(encoding="utf-8-sig")) if args.config.exists() else {}
    data = args.data or (Path(config["data_dir"]).expanduser() if config.get("data_dir") else None)
    if data is None and sys.platform == "darwin":
        candidates = list((Path.home() / "Library/Application Support").glob("FlowHub*/backend-data/flowhub.sqlite3"))
        if len(candidates) == 1:
            data = candidates[0].parent
    if data is None:
        raise RefreshError("请在 config.json 的 data_dir 中指定本机 FlowHub 数据目录。")
    chrome = args.chrome_root or (Path(config["chrome_root"]).expanduser() if config.get("chrome_root") else None)
    if chrome is None:
        chrome = (Path.home() / "Library/Application Support/Google/Chrome" if sys.platform == "darwin"
                  else Path(os.environ.get("LOCALAPPDATA", "")) / "Google/Chrome/User Data")
    if not chrome.is_dir():
        raise RefreshError("找不到 Chrome 数据目录，请在 config.json 设置 chrome_root。")
    profile = args.profile if args.profile is not None else config.get("profile", "")
    if profile and (Path(profile).name != profile or profile in (".", "..")):
        raise RefreshError("profile 只能填写 Default 或 Profile 1 这样的目录名称。")
    print("读取本机 Chrome 登录记录，验证毛子 ERP 和已有店铺……", flush=True)
    tokens = chrome_candidates(chrome, profile)
    if not tokens:
        raise RefreshError("未找到有效登录凭证。请在 Chrome 重新登录 https://ozon.maozierp.com/ 后重试。")
    result = refresh(data, tokens, apply=args.apply)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    print("完成：连接已验证，凭证已更新或已是最新。" if args.apply else "检查通过；未修改凭证。")


if __name__ == "__main__":
    try:
        main()
    except RefreshError as error:
        print("未完成：" + str(error), file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print("已取消。", file=sys.stderr)
        sys.exit(130)
    except Exception:
        # Never leak HTTP headers, DB secrets, input values, or raw tracebacks.
        print("未完成：本地环境或数据读取失败，请检查路径、运行环境和文件权限。", file=sys.stderr)
        sys.exit(1)
