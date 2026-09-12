"""Single writer with durable leases, immutable intents and read-only uncertain-write recovery."""

import asyncio
import fcntl
import json
import secrets
import time

from .db import Database
from .modules import ModuleError, ModuleHost, candidate, image_url, number

TERMINAL = ("selling", "rejected", "attention")


class Worker:
    def __init__(self, db):
        self.db = db
        self.host = ModuleHost(db)

    def context(self, job):
        data = json.loads(job["data"])
        data["owner"] = job["owner"]
        data["idempotency_key"] = job["id"]
        if job["plan"]:
            data |= self.db.open(job["plan"])
        return data

    async def call(self, job, kind, operation):
        context = self.context(job)
        if kind != "publisher":
            context.pop("store", None)
        with self.db.connect() as db:
            wf = db.execute("SELECT secrets FROM workflows WHERE owner=?", (job["owner"],)).fetchone()
        token = self.db.open(wf["secrets"]).get(json.loads(job["modules"])[kind]["id"], "")
        return await asyncio.wait_for(
            self.host.invoke(json.loads(job["modules"])[kind], operation, context, token),
            280
            if json.loads(job["modules"])[kind]["driver"] == "comparebot"
            else 130
            if json.loads(job["modules"])[kind]["driver"] in ("flowb", "maozi", "ozon-direct")
            else 35,
        )

    def move(self, job, phase, note="", data=None, delay=0, plan=None, store=None):
        with self.db.connect() as db:
            db.execute(
                "UPDATE jobs SET phase=?,note=?,data=COALESCE(?,data),plan=COALESCE(?,plan),store_id=COALESCE(?,store_id),next_at=?,updated=? WHERE id=? AND lease=?",
                (
                    phase,
                    note,
                    json.dumps(data) if data else None,
                    plan,
                    store,
                    time.time() + delay,
                    time.time(),
                    job["id"],
                    job["lease"],
                ),
            )
            if phase != job["phase"]:
                self.db.event(db, job["owner"], phase, note or "任务进入下一步骤", job["id"])
        job["phase"] = phase
        if data:
            job["data"] = json.dumps(data)
        if plan:
            job["plan"] = plan
        if store:
            job["store_id"] = store

    def blocked(self, job):
        with self.db.connect() as db:
            return bool(
                db.execute(
                    "SELECT 1 FROM blocks WHERE owner=? AND source_key=?", (job["owner"], job["source_key"])
                ).fetchone()
            )

    async def select_store(self, job):
        c = self.context(job)
        with self.db.connect() as db:
            stores = [
                dict(r)
                for r in db.execute(
                    "SELECT * FROM stores WHERE owner=? AND enabled=1 AND verified=1 ORDER BY position,id",
                    (job["owner"],),
                )
            ]
            active = db.execute(
                "SELECT active_store FROM workflows WHERE owner=?", (job["owner"],)
            ).fetchone()[0]
            selected = db.execute("SELECT selected_store_ids FROM workflows WHERE owner=?", (job["owner"],)).fetchone()[0]
            if selected is not None:
                stores = [s for s in stores if s["id"] in json.loads(selected)]
        if active in [s["id"] for s in stores]:
            i = next(i for i, s in enumerate(stores) if s["id"] == active)
            stores = stores[i:] + stores[:i]
        module = json.loads(job["modules"])["publisher"]
        with self.db.connect() as db:
            secrets_map = self.db.open(
                db.execute("SELECT secrets FROM workflows WHERE owner=?", (job["owner"],)).fetchone()[0]
            )
        with self.db.connect() as db:
            assigned = db.execute(
                "SELECT COUNT(*) FROM jobs WHERE owner=? AND store_id IS NOT NULL", (job["owner"],)
            ).fetchone()[0]
        if c["rules"].get("max_publications") and assigned >= c["rules"]["max_publications"]:
            return None
        for row in stores:
            if c["rules"]["live"] == (row["kind"] == "demo"):
                continue
            if module["driver"] == "maozi" and row["kind"] != "maozi":
                continue
            if module["driver"] == "ozon-direct" and row["kind"] not in ("ozon", "maozi"):
                continue
            store = {
                "id": row["id"],
                "name": row["name"],
                "config": json.loads(row["config"]),
                "credentials": self.db.open(row["secret"]),
            }
            try:
                q = await self.host.invoke(
                    module, "quota", c | {"store": store}, secrets_map.get(module["id"], "")
                )
                remaining = int(number(q["remaining"], 0, 100000))
                reset = number(q["reset_at"], time.time() + 0.01, time.time() + 90000)
                if q["store_id"] != row["id"]:
                    raise ModuleError("quota store mismatch")
            except Exception:
                continue
            with self.db.connect() as db:
                pending = db.execute(
                    "SELECT COUNT(*) FROM jobs WHERE store_id=? AND phase NOT IN ('selling','rejected','attention')",
                    (row["id"],),
                ).fetchone()[0]
                db.execute(
                    "INSERT OR REPLACE INTO quotas VALUES(?,?,?,?)",
                    (row["id"], remaining, reset, time.time()),
                )
                if remaining > pending:
                    db.execute(
                        "UPDATE workflows SET active_store=?,notice=? WHERE owner=?",
                        (row["id"], "当前店铺：" + row["name"], job["owner"]),
                    )
                    return store
        return None

    async def advance(self, job):
        c = self.context(job)
        phase = job["phase"]
        data = json.loads(job["data"])
        if phase in ("ready", "prepared"):
            with self.db.connect() as db:
                selected = db.execute("SELECT selected_store_ids FROM workflows WHERE owner=?", (job["owner"],)).fetchone()[0]
                available = db.execute("SELECT 1 FROM stores WHERE id=? AND owner=? AND enabled=1 AND verified=1", (job["store_id"], job["owner"])).fetchone()
            if not available or (selected is not None and job["store_id"] not in json.loads(selected)):
                self.move(job, phase, "原绑定店铺未勾选或已停用；重新选择原店后继续", delay=30)
                return
        if self.blocked(job):
            self.move(
                job,
                "attention"
                if phase in ("publishing", "reconciling", "stock_pending", "checking")
                else "rejected",
                "命中禁止上架清单；已提交商品需在原店处理",
            )
            return
        if time.time() - job["created"] > 86400:
            self.move(job, "attention", "等待超过24小时，需要检查外部平台")
            return
        if (
            phase in ("prepared", "stock_ready")
            and json.loads(job["modules"])["candidates"]["driver"] == "flowb"
        ):
            from .compat import guard

            await guard(c)
        if phase == "queued":
            result = await self.call(job, "matcher", "match")
            is_comparebot = json.loads(job["modules"])["matcher"]["driver"] == "comparebot"
            if result.get("manual_review"):
                self.move(
                    job,
                    "attention",
                    "compareBot 需要人工审核：" + str(result.get("reason", ""))[:150],
                    data | {"match": result},
                )
                return
            if result.get("rejected"):
                self.move(job, "rejected", "同款识别或来源核验未通过", data | {"match": result})
                return
            if (
                is_comparebot
                and result.get("evidence", {})
                .get("source", {})
                .get("comparebot", {})
                .get("decision", {})
                .get("outcome")
                != "approved"
            ):
                raise ModuleError("compareBot approval evidence missing")
            raw_evidence = result.get("evidence")
            result = dict(
                supplier_id=str(result["supplier_id"])[:150],
                supplier_url=str(result["supplier_url"])[:2000],
                image=image_url(result["image"]),
                purchase=number(result["purchase"], 0.01),
                score=number(result["score"], 0, 1),
                dhash=None if is_comparebot else number(result["dhash"], 0, 1),
                observed_at=number(result["observed_at"], time.time() - 21600, time.time() + 60),
            )
            if raw_evidence:
                result["evidence"] = raw_evidence
                facts = raw_evidence["profit"]["input"]
                data["candidate"]["weight_g"] = number(facts["package_weight"], 1)
                data["candidate"]["dimensions_cm"] = [
                    number(facts[k], 0.01, 500) for k in ("package_length", "package_width", "package_height")
                ]
            if not result["supplier_url"].startswith("https://"):
                raise ModuleError("invalid supplier URL")
            if not is_comparebot and (
                result["score"] * 100 < c["rules"]["image_min"]
                or result["dhash"] * 100 < c["rules"]["dhash_min"]
            ):
                self.move(job, "rejected", "图片相似度未达到当前规则", data | {"match": result})
                return
            self.move(job, "matched", "同款证据已绑定", data | {"match": result})
        elif phase == "matched":
            number(c["candidate"]["weight_g"], 1)
            for x in c["candidate"]["dimensions_cm"]:
                number(x, 0.01, 500)
            r = await self.call(job, "profit", "profit")
            result = dict(
                cost_return=number(r["cost_return"], -100, 100000),
                total_cost=number(r["total_cost"], 0.01),
                route_available=r["route_available"] is True,
                logistics=str(r["logistics"]),
                observed_at=number(r["observed_at"], time.time() - 3600, time.time() + 60),
            )
            if (
                not result["route_available"]
                or result["logistics"] != c["rules"]["logistics"]
                or (result["cost_return"] < 25 if json.loads(job["modules"])["matcher"]["driver"] == "comparebot" else result["cost_return"] <= c["rules"]["profit_min"])
            ):
                self.move(job, "rejected", "利润门槛或物流条件未通过", data | {"profit": result})
                return
            if r.get("sell_price"):
                data["candidate"]["price"] = number(r["sell_price"], 0.01)
            self.move(job, "qualified", "利润规则通过", data | {"profit": result})
        elif phase == "qualified":
            store = await self.select_store(job)
            if not store:
                self.move(job, phase, "等待店铺额度或连接恢复", delay=30)
                return
            self.move(
                job, "ready", "已绑定店铺和仓库", plan=self.db.seal({"store": store}), store=store["id"]
            )
        elif phase == "ready":
            if time.time() - c["match"]["observed_at"] > 21600:
                self.move(job, "rejected", "同款证据已过期，请重新获取候选")
                return
            r = await self.call(job, "publisher", "prepare")
            if r.get("needs_input"):
                self.move(
                    job,
                    "attention",
                    "商品资料需要补齐",
                    data=data | {"dossier_missing": r.get("missing", [])},
                )
                return
            if not r.get("ready"):
                self.move(job, phase, "等待收藏或平台准备", delay=15)
                return
            self.move(job, "prepared", "准备提交", data=data | {"prepared": r})
        elif phase == "prepared":
            # Durable write marker must commit before any external publication call.
            self.move(job, "publishing", "等待提交结果", delay=10)
            r = await self.call(job, "publisher", "publish")
            if r.get("not_sent") is True:
                self.move(job, "prepared", "原店额度已耗尽，留在原店等待", delay=30)
            else:
                self.move(job, "reconciling", "正在回查原店商品", delay=5)
        elif phase in ("publishing", "reconciling"):
            r = await self.call(job, "publisher", "reconcile")
            if r.get("store_id") != c["store"]["id"]:
                raise ModuleError("store mismatch")
            if r.get("issue"):
                self.move(job, "attention", "平台提示商品异常")
                return
            if not r.get("found"):
                self.move(job, "reconciling", "等待平台生成商品", delay=15)
                return
            self.move(
                job, "stock_ready", "商品已创建，准备库存", data=data | {"product_id": str(r["product_id"])}
            )
        elif phase == "stock_ready":
            self.move(job, "stock_pending", "等待库存写入结果", delay=10)
            await self.call(job, "publisher", "stock")
            self.move(job, "checking", "正在核对可售和库存", delay=5)
        elif phase in ("stock_pending", "checking"):
            r = await self.call(job, "publisher", "check_stock")
            if r.get("store_id") != c["store"]["id"] or str(r.get("warehouse_id")) != str(
                c["store"]["config"]["warehouse_id"]
            ):
                raise ModuleError("stock identity mismatch")
            if r.get("selling") is True and r.get("stock") == c["rules"]["stock"]:
                self.move(
                    job, "selling", "模拟上架完成" if not c["rules"]["live"] else "已确认可售，库存核验通过"
                )
            else:
                self.move(job, "checking", "等待可售与目标库存确认", delay=15)

    def claim(self):
        now = time.time()
        lease = secrets.token_hex(16)
        with self.db.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            r = db.execute(
                "SELECT j.* FROM jobs j JOIN users u ON u.id=j.owner JOIN workflows w ON w.owner=j.owner WHERE u.active=1 AND (w.enabled=1 OR j.phase IN ('publishing','reconciling','stock_ready','stock_pending','checking')) AND j.phase NOT IN ('selling','rejected','attention') AND j.next_at<=? AND j.lease_until<=? ORDER BY CASE WHEN j.phase IN ('publishing','reconciling','stock_ready','stock_pending','checking') THEN 0 WHEN j.phase IN ('ready','prepared','qualified','matched') THEN 1 ELSE 2 END,j.next_at,j.created LIMIT 1",
                (now, now),
            ).fetchone()
            if not r:
                return None
            db.execute("UPDATE jobs SET lease=?,lease_until=? WHERE id=?", (lease, now + 300, r["id"]))
            return dict(r) | {"lease": lease}

    async def step(self):
        job = self.claim()
        if not job:
            return False
        try:
            await self.advance(job)
        except Exception as error:
            count = job["attempts"] + 1
            # advance() may already have committed a write marker before raising.
            with self.db.connect() as db:
                persisted = db.execute("SELECT phase FROM jobs WHERE id=?", (job["id"],)).fetchone()
            job = job | {"phase": persisted["phase"]}
            unknown = job["phase"] in ("publishing", "reconciling", "stock_pending", "checking")
            phase = job["phase"] if unknown or count < 8 else "attention"
            self.move(
                job,
                phase,
                "外部响应未确认，保留原任务等待回查" if unknown else "模块暂不可用，稍后自动重试",
                delay=min(300, 2 ** min(count, 8)),
            )
            with self.db.connect() as db:
                db.execute("UPDATE jobs SET attempts=? WHERE id=?", (count, job["id"]))
                self.db.event(
                    db, job["owner"], type(error).__name__, "模块调用未完成；敏感响应未记录", job["id"]
                )
        finally:
            with self.db.connect() as db:
                db.execute(
                    "UPDATE jobs SET lease=NULL,lease_until=0 WHERE id=? AND lease=?",
                    (job["id"], job["lease"]),
                )
        self.db.health("worker", 1)
        return True

    async def replenish(self):
        with self.db.connect() as db:
            workflows = [
                dict(r)
                for r in db.execute(
                    "SELECT w.* FROM workflows w JOIN users u ON u.id=w.owner WHERE w.enabled=1 AND u.active=1"
                )
            ]
        for w in workflows:
            rules = json.loads(w["rules"])
            with self.db.connect() as db:
                total = db.execute("SELECT COUNT(*) FROM jobs WHERE owner=?", (w["owner"],)).fetchone()[0]
            if rules.get("max_items") and total >= rules["max_items"]:
                continue
            if time.time() - w["last_fetch"] < rules["interval"]:
                continue
            with self.db.connect() as db:
                pending = db.execute(
                    "SELECT COUNT(*) FROM jobs WHERE owner=? AND (phase NOT IN ('selling','rejected','attention') OR (phase='attention' AND json_extract(data,'$.candidate.source_contract')='flowhub-source-candidates-v1'))",
                    (w["owner"],),
                ).fetchone()[0]
                if pending >= 30:
                    continue
                db.execute("UPDATE workflows SET last_fetch=? WHERE owner=?", (time.time(), w["owner"]))
                definitions = {
                    k: dict(db.execute("SELECT * FROM modules WHERE id=? AND enabled=1", (v,)).fetchone())
                    for k, v in json.loads(w["modules"]).items()
                }
            try:
                with self.db.connect() as db:
                    saved = db.execute(
                        "SELECT body FROM candidate_pages WHERE owner=?", (w["owner"],)
                    ).fetchone()
                if saved:
                    r = json.loads(saved[0])
                else:
                    r = await self.host.invoke(
                        definitions["candidates"],
                        "candidates",
                        {"cursor": w["cursor"], "rules": rules} | ({"owner": w["owner"]} if definitions["candidates"]["driver"] == "source-library" else {}),
                        self.db.open(w["secrets"]).get(definitions["candidates"]["id"], ""),
                    )
                if not isinstance(r["items"], list) or len(r["items"]) > 50:
                    raise ModuleError("candidate page invalid")
                library_source = definitions["candidates"]["driver"] == "source-library"
                if library_source:
                    from .source_library import refresh_delivery

                    items = refresh_delivery(self.db, w["owner"], r["items"])
                else:
                    items = [candidate(p) for p in r["items"]]
                    if any(len(p["dimensions_cm"]) != 3 for p in items):
                        raise ModuleError("dimensions missing")
                capacity = min(30 - pending, (rules["max_items"] - total) if rules.get("max_items") else 30)
                with self.db.connect() as db:
                    for p in items[:capacity]:
                        if not p["pure_fbs"] and not library_source:
                            continue
                        now = time.time()
                        db.execute(
                            "INSERT OR IGNORE INTO jobs(id,owner,source_key,phase,data,modules,next_at,created,updated) VALUES(?,?,?,?,?,?,?,?,?)",
                            (
                                "fh-" + secrets.token_hex(12),
                                w["owner"],
                                p["source_key"],
                                "attention" if library_source else "queued",
                                json.dumps({"candidate": p, "rules": rules} | ({"dossier_missing": p["dossier_missing"]} if library_source else {})),
                                json.dumps(definitions),
                                now,
                                now,
                                now,
                            ),
                        )
                    if len(items) > capacity:
                        db.execute(
                            "INSERT OR REPLACE INTO candidate_pages VALUES(?,?)",
                            (w["owner"], json.dumps({"items": items[capacity:], "cursor": r["cursor"]})),
                        )
                    else:
                        db.execute("DELETE FROM candidate_pages WHERE owner=?", (w["owner"],))
                        db.execute(
                            "UPDATE workflows SET cursor=?,notice=? WHERE owner=?",
                            (str(r["cursor"])[:1000], "候选已更新", w["owner"]),
                        )
            except Exception as error:
                with self.db.connect() as db:
                    db.execute(
                        "UPDATE workflows SET notice=? WHERE owner=?",
                        ("候选模块暂不可用，将自动重试", w["owner"]),
                    )
                    self.db.event(db, w["owner"], type(error).__name__, "候选模块调用未完成")

    async def run(self):
        lock = (self.db.directory / "worker.lock").open("w")
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)

        async def heartbeat():
            while True:
                self.db.health("worker")
                await asyncio.sleep(5)

        async def refill():
            while True:
                try:
                    await self.replenish()
                except Exception:
                    pass
                await asyncio.sleep(2)

        async def collect_sources():
            from .source_acquisition import SourceAcquirer
            from .source_library import SourceLibrary

            acquirer = SourceAcquirer(SourceLibrary(self.db))
            while True:
                with self.db.connect() as c:
                    settings = [dict(r) for r in c.execute(
                        "SELECT s.* FROM sourcing_settings s JOIN users u ON u.id=s.owner WHERE s.enabled=1 AND u.active=1"
                    )]
                for setting in settings:
                    try:
                        await acquirer.cycle(setting["owner"], self.db.open(setting["secret"])["erp_token"])
                    except Exception:
                        self.db.health("source-collector-error")
                await asyncio.sleep(5)

        tasks = [asyncio.create_task(heartbeat()), asyncio.create_task(refill()),
                 asyncio.create_task(collect_sources())]
        try:
            while True:
                if not await self.step():
                    await asyncio.sleep(0.5)
        finally:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            lock.close()


if __name__ == "__main__":
    asyncio.run(Worker(Database()).run())
