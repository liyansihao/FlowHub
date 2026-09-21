import hashlib
import json
import os
import re
import secrets
import time

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from .db import ROOT, Database, password_hash, password_ok
from .local_profit import ProfitInput, calculate, catalog
from .modules import ModuleHost, public_endpoint


def create_app(database=None):
    db = database or Database()
    from .runtime_identity import capture, save
    runtime = capture('web', ROOT)
    save(db.directory, runtime)
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.state.db = db

    @app.middleware("http")
    async def headers(request, call_next):
        try:
            r = await call_next(request)
        except Exception:
            r = JSONResponse({"detail": "请求未完成，请稍后重试"}, status_code=500)
        r.headers.update(
            {
                "X-Content-Type-Options": "nosniff",
                "X-Frame-Options": "DENY",
                "Referrer-Policy": "no-referrer",
                "Cache-Control": "no-store",
                "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' https:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
            }
        )
        return r

    def auth(request: Request):
        token = request.cookies.get("fh_session", "")
        with db.connect() as c:
            row = c.execute(
                "SELECT u.*,s.csrf FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token=? AND s.expires>? AND u.active=1",
                (hashlib.sha256(token.encode()).hexdigest(), time.time()),
            ).fetchone()
        if not row:
            raise HTTPException(401, "请登录")
        if request.method not in ("GET", "HEAD"):
            if request.headers.get("X-CSRF-Token") != row["csrf"]:
                raise HTTPException(403, "会话校验失败，请刷新页面")
            origin = request.headers.get("origin")
            if origin and origin != str(request.base_url).rstrip("/"):
                raise HTTPException(403, "来源不匹配")
        if row["must_change"] and request.url.path not in ("/api/me", "/api/password", "/api/logout"):
            raise HTTPException(403, "首次登录请修改密码")
        return dict(row)

    def scope(request: Request, user=Depends(auth)):
        owner = request.query_params.get("owner") or user["id"]
        if owner != user["id"] and user["role"] != "admin":
            raise HTTPException(403, "无权访问此工作区")
        with db.connect() as c:
            if not c.execute("SELECT 1 FROM users WHERE id=?", (owner,)).fetchone():
                raise HTTPException(404, "工作区不存在")
        return owner

    def admin(user=Depends(auth)):
        if user["role"] != "admin":
            raise HTTPException(403, "需要管理员权限")
        return user

    from .source_api import register_source_api

    register_source_api(app, db, scope, admin)
    from .manual_reviews import register as register_manual_reviews
    register_manual_reviews(app, db, scope, auth)

    class Login(BaseModel):
        username: str = Field(min_length=1, max_length=60)
        password: str = Field(min_length=1, max_length=200)

    @app.post("/api/login")
    def login(payload: Login, request: Request, response: Response):
        origin = request.headers.get("origin")
        if origin and origin != str(request.base_url).rstrip("/"):
            raise HTTPException(403, "来源不匹配")
        bucket = (request.client.host if request.client else "") + "|" + payload.username.lower()
        with db.connect() as c:
            c.execute("DELETE FROM login_attempts WHERE at<?", (time.time() - 900,))
            if c.execute("SELECT COUNT(*) FROM login_attempts WHERE bucket=?", (bucket,)).fetchone()[0] >= 8:
                raise HTTPException(429, "尝试过多，请15分钟后重试")
            c.execute("INSERT INTO login_attempts VALUES(?,?)", (bucket, time.time()))
        with db.connect() as c:
            user = c.execute(
                "SELECT * FROM users WHERE username=? AND active=1", (payload.username,)
            ).fetchone()
            if not user or not password_ok(payload.password, user["password"]):
                raise HTTPException(401, "账号或密码不正确")
            c.execute("DELETE FROM login_attempts WHERE bucket=?", (bucket,))
            token = secrets.token_urlsafe(32)
            csrf = secrets.token_urlsafe(24)
            c.execute(
                "INSERT INTO sessions VALUES(?,?,?,?)",
                (hashlib.sha256(token.encode()).hexdigest(), user["id"], csrf, time.time() + 43200),
            )
        response.set_cookie(
            "fh_session",
            token,
            httponly=True,
            samesite="strict",
            secure=os.environ.get("FLOWHUB_HTTPS") == "1",
            max_age=43200,
        )
        return {"csrf": csrf, "must_change": bool(user["must_change"])}

    @app.get("/api/me")
    def me(user=Depends(auth)):
        return {k: user[k] for k in ("id", "username", "role", "must_change", "csrf")}

    class Password(BaseModel):
        current: str
        new: str = Field(min_length=12, max_length=200)

    @app.post("/api/password")
    def change_password(p: Password, response: Response, user=Depends(auth)):
        if not password_ok(p.current, user["password"]):
            raise HTTPException(400, "当前密码错误")
        if p.new == p.current:
            raise HTTPException(400, "请使用不同的新密码")
        with db.connect() as c:
            c.execute(
                "UPDATE users SET password=?,must_change=0 WHERE id=?", (password_hash(p.new), user["id"])
            )
            c.execute("DELETE FROM sessions WHERE user_id=?", (user["id"],))
        response.delete_cookie("fh_session")
        return {"ok": True}

    @app.post("/api/logout")
    def logout(request: Request, response: Response, user=Depends(auth)):
        with db.connect() as c:
            c.execute(
                "DELETE FROM sessions WHERE token=?",
                (hashlib.sha256(request.cookies["fh_session"].encode()).hexdigest(),),
            )
        response.delete_cookie("fh_session")
        return {"ok": True}

    @app.get("/api/users")
    def users(user=Depends(admin)):
        with db.connect() as c:
            return [
                dict(r)
                for r in c.execute("SELECT id,username,role,active,created FROM users ORDER BY created")
            ]

    class NewUser(Login):
        pass

    @app.post("/api/users")
    def new_user(p: NewUser, user=Depends(admin)):
        if len(p.password) < 12 or not re.fullmatch(r"[a-zA-Z0-9_.-]{3,40}", p.username):
            raise HTTPException(400, "账号需3–40位字母数字，密码至少12位")
        with db.connect() as c:
            if c.execute("SELECT 1 FROM users WHERE username=?", (p.username,)).fetchone():
                raise HTTPException(409, "账号已存在")
            return {"id": db.create_user(c, p.username, p.password)}

    @app.post("/api/users/{uid}/disable")
    def disable(uid: str, user=Depends(admin)):
        if uid == user["id"]:
            raise HTTPException(400, "不能停用自己")
        with db.connect() as c:
            c.execute("UPDATE users SET active=0 WHERE id=?", (uid,))
            c.execute("DELETE FROM sessions WHERE user_id=?", (uid,))
            c.execute("UPDATE workflows SET enabled=0 WHERE owner=?", (uid,))
        return {"ok": True}

    class Store(BaseModel):
        model_config = ConfigDict(extra="forbid")
        name: str = Field(min_length=1, max_length=80)
        kind: str = "demo"
        shop_id: str = "demo"
        warehouse_id: str = "demo-warehouse"
        watermark_id: str = "0"
        client_id: str = ""
        api_key: str = ""
        erp_token: str = ""
        enabled: bool = True
        position: int = Field(default=0, ge=0, le=100)

    def store_public(row):
        r = dict(row)
        r.pop("secret")
        r["config"] = json.loads(r["config"])
        return r

    class DossierCheck(BaseModel):
        model_config = ConfigDict(extra="forbid")
        source_key: str = Field(min_length=1, max_length=150)
        price: float = Field(gt=0, allow_inf_nan=False)
        dossier: dict

    @app.post("/api/stores/{sid}/dossier-check")
    async def dossier_check(sid: str, p: DossierCheck, owner=Depends(scope)):
        from .ozon_direct import OzonDirectPublisher

        with db.connect() as connection:
            row = connection.execute("SELECT * FROM stores WHERE id=? AND owner=?", (sid, owner)).fetchone()
        if not row:
            raise HTTPException(404, "店铺不存在")
        if row["kind"] not in ("maozi", "ozon"):
            raise HTTPException(400, "需要 Ozon 店铺连接")
        context = {
            "owner": owner,
            "idempotency_key": "preflight-" + secrets.token_hex(12),
            "candidate": {
                "source_key": p.source_key,
                "price": p.price,
                "origin": {"ozon_dossier": p.dossier},
            },
            "store": {"id": sid, "config": json.loads(row["config"]), "credentials": db.open(row["secret"])},
        }
        try:
            result = await OzonDirectPublisher(context, db).invoke("prepare")
        except Exception:
            raise HTTPException(400, "官方资料校验未完成，请检查店铺连接和商品字段") from None
        # This route cannot create jobs, submit products or adjust inventory.
        return {"ready": result["ready"], "missing": result.get("missing", []), "submitted": False}

    @app.get("/api/stores")
    def stores(owner=Depends(scope)):
        with db.connect() as c:
            return [
                store_public(r)
                for r in c.execute("SELECT * FROM stores WHERE owner=? ORDER BY position,id", (owner,))
            ]

    @app.post("/api/stores")
    def store_create(p: Store, owner=Depends(scope)):
        if p.kind not in ("demo", "maozi", "ozon", "http"):
            raise HTTPException(400, "店铺类型无效")
        if p.kind == "maozi" and (
            not all(
                x.isdigit() and int(x) > 0 for x in (p.shop_id, p.warehouse_id, p.watermark_id, p.client_id)
            )
            or not p.erp_token
            or not p.api_key
        ):
            raise HTTPException(400, "请填写完整店铺、仓库和连接凭据")
        if p.kind == "ozon":
            if not p.client_id.isdigit() or not p.warehouse_id.isdigit() or not p.api_key:
                raise HTTPException(400, "请填写 Ozon Client ID、API Key 和目标仓库 ID")
            p.shop_id = p.client_id
            p.watermark_id = "0"
            p.erp_token = ""
        sid = secrets.token_hex(12)
        with db.connect() as c:
            c.execute(
                "INSERT INTO stores VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    sid,
                    owner,
                    p.name,
                    p.kind,
                    json.dumps(
                        {"shop_id": p.shop_id, "warehouse_id": p.warehouse_id, "watermark_id": p.watermark_id}
                    ),
                    db.seal({"client_id": p.client_id, "api_key": p.api_key, "erp_token": p.erp_token}),
                    p.enabled,
                    p.kind == "demo",
                    p.position,
                ),
            )
        return {"id": sid}

    @app.patch("/api/stores/{sid}")
    def store_update(sid: str, p: dict, owner=Depends(scope)):
        if set(p) - {"enabled", "position", "name"}:
            raise HTTPException(400, "仅可修改名称、顺序和启用状态；密钥变更请重新连接店铺")
        with db.connect() as c:
            r = c.execute("SELECT * FROM stores WHERE id=? AND owner=?", (sid, owner)).fetchone()
            if not r:
                raise HTTPException(404, "店铺不存在")
            c.execute(
                "UPDATE stores SET name=?,enabled=?,position=? WHERE id=?",
                (
                    str(p.get("name", r["name"]))[:80],
                    bool(p.get("enabled", r["enabled"])),
                    max(0, min(100, int(p.get("position", r["position"])))),
                    sid,
                ),
            )
        return {"ok": True}

    @app.post("/api/stores/{sid}/verify")
    async def verify(sid: str, owner=Depends(scope)):
        with db.connect() as c:
            r = c.execute("SELECT * FROM stores WHERE id=? AND owner=?", (sid, owner)).fetchone()
            if not r:
                raise HTTPException(404, "店铺不存在")
            wf = c.execute("SELECT * FROM workflows WHERE owner=?", (owner,)).fetchone()
            module = c.execute(
                "SELECT * FROM modules WHERE id=?", (json.loads(wf["modules"])["publisher"],)
            ).fetchone()
        try:
            if r["kind"] == "demo":
                result = {"verified": True}
            else:
                definition = (
                    dict(module)
                    if r["kind"] == "http"
                    else {"driver": "ozon-direct" if r["kind"] == "ozon" else "maozi"}
                )
                result = await ModuleHost(db).invoke(
                    definition,
                    "identity",
                    {
                        "store": {
                            "id": sid,
                            "config": json.loads(r["config"]),
                            "credentials": db.open(r["secret"]),
                        }
                    },
                    db.open(wf["secrets"]).get(module["id"], ""),
                )
            if result.get("verified") is not True:
                raise ValueError()
        except Exception:
            raise HTTPException(400, "连接核验失败，请检查该店铺的账号、仓库、水印与密钥") from None
        with db.connect() as c:
            c.execute("UPDATE stores SET verified=1 WHERE id=? AND owner=?", (sid, owner))
        return {"verified": True}

    class Rules(BaseModel):
        model_config = ConfigDict(extra="forbid")
        profit_min: float = Field(default=30, ge=0, le=1000, allow_inf_nan=False)
        image_min: float = Field(default=70, ge=0, le=100, allow_inf_nan=False)
        dhash_min: float = Field(default=55, ge=0, le=100, allow_inf_nan=False)
        stock: int = Field(default=99, ge=1, le=10000)
        logistics: str = Field(default="ChinaPost", min_length=1, max_length=40)
        interval: int = Field(default=30, ge=10, le=3600)
        live: bool = False
        max_items: int = Field(default=0, ge=0, le=100000)
        max_publications: int = Field(default=0, ge=0, le=100000)

    class Workflow(BaseModel):
        rules: Rules
        modules: dict[str, str]
        credentials: dict[str, str] = {}

    @app.get("/api/workflow")
    def workflow(owner=Depends(scope)):
        with db.connect() as c:
            r = dict(c.execute("SELECT * FROM workflows WHERE owner=?", (owner,)).fetchone())
        r["rules"] = json.loads(r["rules"])
        r["modules"] = json.loads(r["modules"])
        r["selected_store_ids"] = json.loads(r["selected_store_ids"]) if r["selected_store_ids"] is not None else None
        r["configured_credentials"] = list(db.open(r.pop("secrets")))
        return r

    @app.put("/api/workflow")
    def workflow_save(p: Workflow, owner=Depends(scope), user=Depends(auth)):
        if set(p.modules) != {"candidates", "matcher", "profit", "publisher"}:
            raise HTTPException(400, "请选择全部四种模块")
        with db.connect() as c:
            row = c.execute("SELECT * FROM workflows WHERE owner=?", (owner,)).fetchone()
            if row["enabled"]:
                raise HTTPException(409, "请先暂停新增，再修改工作流")
            for kind, mid in p.modules.items():
                module = c.execute(
                    "SELECT * FROM modules WHERE id=? AND kind=? AND enabled=1", (mid, kind)
                ).fetchone()
                if not module:
                    raise HTTPException(400, "模块不存在或已停用")
                if module["driver"] in ("flowb", "comparebot") and user["role"] != "admin":
                    raise HTTPException(403, "本机兼容模块仅供管理员验收")
            keys = db.open(row["secrets"])
            keys.update({k: v for k, v in p.credentials.items() if v})
            c.execute(
                "UPDATE workflows SET rules=?,modules=?,secrets=?,updated=? WHERE owner=?",
                (p.rules.model_dump_json(), json.dumps(p.modules), db.seal(keys), time.time(), owner),
            )
        return {"ok": True}

    class StartSelection(BaseModel):
        model_config = ConfigDict(extra="forbid")
        store_ids: list[str] = Field(max_length=100)

    @app.post("/api/workflow/{action}")
    def workflow_action(action: str, p: StartSelection | None = None, owner=Depends(scope)):
        if action not in ("start", "pause"):
            raise HTTPException(404)
        with db.write_transaction() as c:
            w = c.execute("SELECT * FROM workflows WHERE owner=?", (owner,)).fetchone()
            rules = json.loads(w["rules"])
            if action == "start":
                saved = json.loads(w["selected_store_ids"]) if w["selected_store_ids"] is not None else None
                selected = list(dict.fromkeys(p.store_ids)) if p is not None else saved
                eligible = {r[0] for r in c.execute(
                    "SELECT id FROM stores WHERE owner=? AND enabled=1 AND verified=1 AND ((?=0 AND kind='demo') OR (?=1 AND kind!='demo'))",
                    (owner, bool(rules["live"]), bool(rules["live"])),
                )}
                if selected is None:
                    selected = sorted(eligible)
                if not selected or not set(selected) <= eligible:
                    raise HTTPException(400, "请勾选本账号已启用、已核验且符合当前模式的店铺")
                if w["enabled"] and saved is not None and set(selected) != set(saved):
                    raise HTTPException(409, "请先暂停新增，再更改上架店铺")
                if not w["enabled"] and saved != selected and c.execute(
                    "SELECT 1 FROM jobs WHERE owner=? AND phase IN ('qualified','ready','prepared') AND lease_until>?",
                    (owner, time.time()),
                ).fetchone():
                    raise HTTPException(409, "正在完成上一项操作，请稍后再更改店铺")
                mods = [
                    dict(c.execute("SELECT * FROM modules WHERE id=? AND enabled=1", (mid,)).fetchone() or {})
                    for mid in json.loads(w["modules"]).values()
                ]
                if len(mods) != 4 or any(not m for m in mods):
                    raise HTTPException(400, "模块配置不完整")
                if rules["live"] and any(m["driver"] == "demo" for m in mods):
                    raise HTTPException(400, "真实模式不能使用模拟模块")
                publisher = next((m for m in mods if m["kind"] == "publisher"), {})
                compatible = {"maozi": {"maozi"}, "ozon-direct": {"maozi", "ozon"}, "demo": {"demo"}}.get(publisher.get("driver"))
                if compatible is not None:
                    selected_kinds = {r["id"]: r["kind"] for r in c.execute("SELECT id,kind FROM stores WHERE owner=?", (owner,))}
                    if any(selected_kinds[sid] not in compatible for sid in selected):
                        raise HTTPException(400, "勾选店铺的连接方式与当前上架模块不匹配")
                if not w["enabled"] and rules.get("max_publications"):
                    assigned = c.execute("SELECT COUNT(*) FROM jobs WHERE owner=? AND store_id IS NOT NULL", (owner,)).fetchone()[0]
                    unsubmitted = c.execute("SELECT 1 FROM jobs WHERE owner=? AND store_id IS NOT NULL AND phase IN ('ready','prepared')", (owner,)).fetchone()
                    if assigned >= rules["max_publications"] and not unsubmitted:
                        raise HTTPException(409, "累计提交上限已达到，请在工作流配置中调整上限后启动")
                kind = "demo" if not rules["live"] else "real"
                available = c.execute(
                    "SELECT 1 FROM stores WHERE owner=? AND enabled=1 AND verified=1 AND ((?='demo' AND kind='demo') OR (?='real' AND kind!='demo'))",
                    (owner, kind, kind),
                ).fetchone()
                if not available:
                    raise HTTPException(400, "请先添加并核验相应模式的店铺")
                c.execute("UPDATE workflows SET selected_store_ids=? WHERE owner=?", (json.dumps(selected), owner))
            c.execute(
                "UPDATE workflows SET enabled=?,notice=?,updated=? WHERE owner=?",
                (
                    action == "start",
                    "已启动" if action == "start" else "已暂停新增；已提交任务继续回查",
                    time.time(),
                    owner,
                ),
            )
        return {"ok": True}

    @app.get("/api/profit/categories")
    def profit_categories(q: str = "", user=Depends(auth)):
        from .commissions import search

        if len(q) > 100:
            raise HTTPException(400, "查询过长")
        return {"items": search(q)}

    @app.get("/api/profit/catalog")
    def profit_catalog(user=Depends(auth)):
        return catalog()

    @app.post("/api/profit/calculate")
    def profit_calculate(p: ProfitInput, user=Depends(auth)):
        try:
            return calculate(p)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None

    @app.get("/api/modules")
    def modules(user=Depends(auth)):
        with db.connect() as c:
            return [
                dict(r)
                for r in c.execute("SELECT * FROM modules WHERE enabled=1")
                if user["role"] == "admin" or r["driver"] not in ("flowb", "comparebot")
            ]

    class Module(BaseModel):
        id: str = Field(pattern=r"^[a-z0-9-]{3,60}$")
        kind: str
        name: str = Field(min_length=1, max_length=80)
        endpoint: str
        driver: str = "http"

    @app.post("/api/modules")
    async def module_create(p: Module, user=Depends(admin)):
        if p.kind not in ("candidates", "matcher", "profit", "publisher") or p.driver not in (
            "http",
            "plugin",
        ):
            raise HTTPException(400, "模块类型无效")
        if p.driver == "http":
            try:
                await public_endpoint(p.endpoint)
            except Exception:
                raise HTTPException(400, "模块需要可访问的公网 HTTPS 地址") from None
        else:
            manifest = ROOT / "plugins/installed.json"
            installed = json.loads(manifest.read_text()) if manifest.exists() else {}
            if p.endpoint not in installed:
                raise HTTPException(400, "此插件尚未由服务器管理员安装")
        with db.connect() as c:
            if c.execute("SELECT 1 FROM modules WHERE id=?", (p.id,)).fetchone():
                raise HTTPException(409, "模块ID已存在，请使用新版本ID")
            c.execute(
                "INSERT INTO modules(id,kind,name,driver,endpoint) VALUES(?,?,?,?,?)",
                (p.id, p.kind, p.name, p.driver, p.endpoint),
            )
        return {"ok": True}

    @app.get("/api/jobs")
    def jobs(owner=Depends(scope), phase: str = "", offset: int = 0):
        with db.connect() as c:
            rows = c.execute(
                'SELECT * FROM jobs WHERE owner=? AND (?="" OR phase=?) ORDER BY created DESC LIMIT 100 OFFSET ?',
                (owner, phase, phase, max(0, offset)),
            ).fetchall()
        result = []
        for row in rows:
            r = dict(row)
            data = json.loads(r["data"])
            matched = data.get("match", {})
            profit = data.get("profit", {})
            result.append(
                {
                    "id": r["id"],
                    "source_key": r["source_key"],
                    "phase": r["phase"],
                    "store_id": r["store_id"],
                    "title": data["candidate"]["title"],
                    "image": data["candidate"]["image"],
                    "supplier_image": matched.get("image"),
                    "supplier_url": matched.get("supplier_url"),
                    "score": matched.get("score"),
                    "dhash": matched.get("dhash"),
                    "profit": profit.get("cost_return"),
                    "stock": data["rules"]["stock"],
                    "live": data["rules"]["live"],
                    "dossier_missing": data.get("dossier_missing", []),
                    "note": r["note"],
                    "updated": r["updated"],
                    "created": r["created"],
                }
            )
        return result

    class ProductAction(BaseModel):
        action: str
        value: int | float | None = None
        request_id: str = Field(min_length=12, max_length=80, pattern=r"^[a-zA-Z0-9_-]+$")

    @app.post("/api/production/{offer}/action")
    async def production_action(
        offer: str, payload: ProductAction, owner=Depends(scope), user=Depends(admin)
    ):
        if owner != user["id"]:
            raise HTTPException(403, "不能操作其他工作区的正式商品")
        from .production_actions import validate

        try:
            validate(payload.action, payload.value)
        except ValueError as e:
            raise HTTPException(422, str(e))
        if not re.fullmatch(r"flowef-live99-[a-f0-9]{20}", offer):
            raise HTTPException(404, "正式商品不存在")
        import asyncio

        runner = ROOT.parent / "FlowEF-production/.venv/bin/python"
        if not runner.exists():
            raise HTTPException(409, "本机未连接正式上架服务")
        proc = await asyncio.create_subprocess_exec(
            str(runner),
            str(ROOT / "flowhub/production_actions.py"),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=ROOT.parent / "FlowEF-production",
            start_new_session=True,
        )
        try:
            out, _ = await asyncio.wait_for(
                proc.communicate(json.dumps(dict(offer=offer, **payload.model_dump())).encode()), 180
            )
        except BaseException:
            if proc.returncode is None:
                proc.kill()
                await proc.wait()
            raise HTTPException(409, "操作结果暂未确认，请使用重新回查")
        try:
            result = json.loads(out)
        except ValueError:
            raise HTTPException(409, "操作结果暂未确认，请重新回查")
        if not result.get("ok"):
            raise HTTPException(409, result.get("message", "操作未完成"))
        r = result["result"]
        return {
            k: r.get(k)
            for k in ("status", "message", "action", "observed_stock", "observed_price", "updated")
        }

    @app.get("/api/production")
    def production(owner=Depends(scope), user=Depends(admin), phase: str = ""):
        if owner != user["id"]:
            return {"available": False}
        from .production_view import snapshot

        return snapshot(ROOT.parent / "FlowEF-production/state/production", phase)

    @app.get("/api/acceptance")
    def acceptance(user=Depends(admin)):
        path = db.directory / "live-acceptance.json"
        return json.loads(path.read_text()) if path.exists() else {"status": "not_configured"}

    @app.get("/api/overview")
    def overview(owner=Depends(scope)):
        with db.connect() as c:
            phases = dict(
                c.execute("SELECT phase,COUNT(*) FROM jobs WHERE owner=? GROUP BY phase", (owner,)).fetchall()
            )
            recent = c.execute(
                "SELECT COUNT(*) FROM jobs WHERE owner=? AND phase='selling' AND updated>?",
                (owner, time.time() - 3600),
            ).fetchone()[0]
            h = c.execute("SELECT * FROM health WHERE name='worker'").fetchone()
            quotas = [
                dict(r)
                for r in c.execute(
                    "SELECT q.* FROM quotas q JOIN stores s ON s.id=q.store_id WHERE s.owner=?", (owner,)
                )
            ]
        return {
            "phases": phases,
            "last_hour": recent,
            "worker_alive": bool(h and time.time() - h["heartbeat"] < 20),
            "heartbeat": h["heartbeat"] if h else None,
            "uptime": time.time() - h["started"] if h else 0,
            "quotas": quotas,
        }

    @app.get("/api/events")
    def events(owner=Depends(scope), user=Depends(admin)):
        with db.connect() as c:
            return [
                dict(r)
                for r in c.execute(
                    "SELECT code,message,job_id,created FROM events WHERE owner=? ORDER BY id DESC LIMIT 100",
                    (owner,),
                )
            ]

    @app.get("/api/blocks")
    def blocks(owner=Depends(scope)):
        with db.connect() as c:
            return [
                dict(r) for r in c.execute("SELECT source_key,reason FROM blocks WHERE owner=?", (owner,))
            ]

    @app.post("/api/blocks")
    def block(p: dict, owner=Depends(scope)):
        key = str(p.get("source_key", ""))[:150]
        if not key:
            raise HTTPException(400, "请输入商品源SKU")
        with db.connect() as c:
            c.execute(
                "INSERT OR REPLACE INTO blocks VALUES(?,?,?)",
                (owner, key, str(p.get("reason", "禁止上架"))[:250]),
            )
        return {"ok": True}

    @app.get("/healthz")
    def health():
        return {"service": "flowhub", "ok": True, "runtime": runtime}

    app.mount("/assets", StaticFiles(directory=ROOT / "web/assets", check_dir=False), name="assets")

    @app.get("/")
    def index():
        return FileResponse(ROOT / "web/index.html")

    return app


app = create_app()
