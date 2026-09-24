"""Source-card acquisition and deterministic mapping; publication is a separate module."""

import asyncio
import hashlib
import json
import os
import re
import time
from .pipeline_modules.database_work import run as database_work
from pathlib import Path

from .modules import ModuleError, Pending


async def request(path, method, keys, query=None, body=None):
    token = keys.get("erp_token")
    if not token:
        raise ModuleError("source detail requires an ERP data connection")
    bridge = Path(__file__).resolve().parent.parent / "bridges/source-detail.mjs"
    process = await asyncio.create_subprocess_exec(
        "node",
        str(bridge),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=os.environ | {"MAOZI_ACCESS_TOKEN": token},
    )
    try:
        output, _ = await asyncio.wait_for(
            process.communicate(
                json.dumps({"path": path, "method": method, "query": query, "body": body}).encode()
            ),
            95,
        )
        data = json.loads(output)
        if not data.get("ok"):
            raise SourceAcquisitionFailure(data.get("error", "SOURCE_REQUEST_FAILED"), data.get("diagnostic"))
        return data["data"]
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()


class SourceAcquisitionFailure(ModuleError):
    def __init__(self, code, diagnostic):
        super().__init__("source_acquisition:" + str(code))
        self.diagnostic = diagnostic or {}


class SourceCollector:
    recovery_pages = {}
    read_failures = {}
    def __init__(self, db, context, *, ensure_schema=True):
        self.db, self.c = db, context
        self.keys = context["store"]["credentials"]
        self.sku = str(context["candidate"]["source_key"])
        if not self.sku.isdigit():
            raise ModuleError("source acquisition requires numeric Ozon SKU")
        self.key = hashlib.sha256(
            (
                context["owner"]
                + ":"
                + hashlib.sha256(self.keys.get("erp_token", "").encode()).hexdigest()
                + ":"
                + self.sku
            ).encode()
        ).hexdigest()
        if ensure_schema:
            with db.connect() as connection:
                connection.execute(
                    "CREATE TABLE IF NOT EXISTS source_details(key TEXT PRIMARY KEY,state TEXT NOT NULL,body TEXT NOT NULL,updated REAL NOT NULL)"
                )

    async def call(self, path, method="GET", query=None, body=None):
        self.last_timing={}
        # Old unresolved drafts must not each pay a full timeout during an outage.
        # Writes keep their original one-shot journal semantics and never use this gate.
        scope=(self.c['owner'],hashlib.sha256(self.keys.get('erp_token','').encode()).hexdigest(),path)
        failures,until=self.read_failures.get(scope,(0,0))
        if method=='GET' and time.monotonic()<until:
            raise SourceAcquisitionFailure('CONNECT_TIMEOUT_BACKOFF',{'operation':path,'retry_after_ms':60000})
        try:
            from .acquisition import enabled, enrolled
            if enabled(self.db,self.sku) or enrolled(self.db,self.key):
                from .source_gateway import request as gateway_request
                result,self.last_timing=await gateway_request(self.keys,path,method,query,body,proxy=self.c["store"].get("config",{}).get("erp_proxy"))
            else:
                result=await request(path, method, self.keys, query, body)
            if method=='GET':self.read_failures.pop(scope,None)
            return result
        except Exception as error:
            self.last_timing=getattr(error,'diagnostic',{}).get('timing',{})
            code=str(error).lower()
            network=isinstance(error,TimeoutError) or any(v in code for v in ('connect_timeout','etimedout','econnreset','enotfound','fetch failed'))
            if method=='GET' and network:
                failures+=1
                self.read_failures[scope]=(failures,time.monotonic()+60 if failures>=3 else 0)
            raise

    def load(self):
        with self.db.connect() as db:
            row = db.execute("SELECT * FROM source_details WHERE key=?", (self.key,)).fetchone()
        return (row["state"], self.db.open(row["body"]), row["updated"]) if row else ("new", {}, 0)

    def cached_snapshot(self, *, now=None):
        """Reuse only a completed, exact-account/SKU snapshot; never resume writes here."""
        now = time.time() if now is None else now
        state, data, updated = self.load()
        observed = data.get('observed_at')
        if (state == 'ready' and str(data.get('source_key')) == self.sku
                and isinstance(observed, (int, float)) and not isinstance(observed, bool)
                and 0 <= now - updated < 21600 and 0 <= now - observed < 21600
                and len((data.get('detail') or {}).get('skus') or []) == 1):
            return data
        return None

    def save(self, state, data):
        with self.db.connect() as db:
            db.execute(
                "INSERT OR REPLACE INTO source_details VALUES(?,?,?,?)",
                (self.key, state, self.db.seal(data), time.time()),
            )

    def renew_claim(self):
        # A paginated lookup can outlive the lease. Only its current owner may continue.
        now = time.time()
        with self.db.connect() as db:
            changed = db.execute(
                "UPDATE source_details SET updated=? WHERE key=? AND state='claimed' AND updated=?",
                (now, self.key, self.claim_updated),
            ).rowcount
        if changed != 1:
            raise Pending("source acquisition claim changed; no write repeated")
        self.claim_updated = now

    async def find_favorite(self, *, claimed=False):
        # Legacy Maozi uses separate imported/unimported listings. Do not assume SKU
        # filtering or a requested page size is honored, and never treat malformed or
        # truncated data as proof that a favorite does not exist.
        matches = {}
        for imported in (0, 1):
            seen = set()
            total_seen = 0
            for page in range(1, 101):
                if claimed:
                    await database_work(self.renew_claim)
                response = await self.call(
                    "/api.product.favorite/lists",
                    query={"sku": self.sku, "is_imported": imported, "page": page, "page_size": 100},
                )
                if isinstance(response, list):
                    rows, total = response, None
                elif isinstance(response, dict):
                    rows = next((response[k] for k in ("data", "list", "rows", "items")
                                 if isinstance(response.get(k), list)), None)
                    total = response.get("total")
                else:
                    rows, total = None, None
                if rows is None or any(not isinstance(row, dict) for row in rows):
                    raise Pending("invalid favorite recovery listing")
                if total is not None:
                    if isinstance(total, bool) or not str(total).isdigit():
                        raise Pending("invalid favorite recovery total")
                    total = int(total)
                ids = []
                for row in rows:
                    identifier = str(row.get("id", ""))
                    if not identifier.isdigit() or int(identifier) <= 0:
                        raise Pending("invalid favorite recovery identity")
                    identifier = int(identifier)
                    if identifier in seen or identifier in ids:
                        raise Pending("favorite recovery pagination repeated; listing incomplete")
                    ids.append(identifier)
                    if str(row.get("sku")) == self.sku:
                        matches[identifier] = row
                seen.update(ids)
                total_seen += len(rows)
                if total is not None:
                    if total_seen > total:
                        raise Pending("favorite recovery listing changed; lookup again later")
                    if total_seen == total:
                        break
                    if not rows:
                        raise Pending("favorite recovery listing incomplete")
                elif not rows:
                    break
                # Without a total, even a short page can be a server-side size cap.
            else:
                raise Pending("favorite recovery listing incomplete")
        if len(matches) > 1:
            raise ModuleError("ambiguous source favorite")
        return next(iter(matches.values()), None)

    async def recover_draft(self, data, *, allow_missing=False):
        # Read-only full listing, cached per owner/account. Never infer success from a favorite.
        account=self.c['owner']+':'+hashlib.sha256(self.keys.get('erp_token','').encode()).hexdigest()
        cached=self.recovery_pages.get(account)
        if allow_missing or not cached or time.time()-cached[0]>60:
            rows=[];complete=False;expected=None
            for page in range(1,21):
                response=await self.call('/api.product.collect/lists',query={'page':page,'page_size':100})
                batch=response if isinstance(response,list) else response.get('data',[])
                if not isinstance(batch,list):raise Pending('invalid draft recovery listing')
                rows.extend(batch)
                total=response.get('total') if isinstance(response,dict) else None
                if total is not None:
                    if expected is None:expected=int(total)
                    elif int(total)!=expected:raise Pending('draft recovery listing changed')
                if len(batch)<100 or (total is not None and len(rows)>=int(total)):
                    complete=True;break
            if not complete:raise Pending('draft recovery listing incomplete')
            if expected is not None and len({str(r['id']) for r in rows})!=expected:
                raise Pending('draft recovery listing unstable')
            self.recovery_pages[account]=(time.time(),rows)
        else:rows=cached[1]
        matches={str(r['id']):r for r in rows if str(r.get('goods_id'))==self.sku and r.get('collect_from')=='ozon' and r.get('id')}
        if not matches and allow_missing:return None
        if len(matches)!=1:raise Pending('source draft recovery missing or ambiguous; no creation repeated')
        draft_id=int(next(iter(matches)))
        detail=await self.call('/api.product.collect/detail',query={'id':draft_id,'is_online':0})
        data=data|{'source_key':self.sku,'draft_id':draft_id,'detail':detail,'observed_at':time.time(),
                   'recovery':{'source':'exact-ozon-draft-list','at':time.time()}}
        await database_work(self.save,'ready',data)
        from .collection_capacity import finish
        await database_work(finish,self)
        return data

    async def collect(self):
        from .acquisition import enabled, enrolled, dispatch
        if enabled(self.db,self.sku) or enrolled(self.db,self.key):
            return await dispatch(self)
        return await self.collect_legacy()

    async def collect_legacy(self):
        # Claim once per source/account. A crashed collecting call is not repeated blindly.
        with self.db.connect() as db:
            inserted = (
                db.execute(
                    "INSERT OR IGNORE INTO source_details VALUES(?,?,?,?)",
                    (self.key, "claimed", self.db.seal({}), time.time()),
                ).rowcount
                == 1
            )
        state, data, updated = self.load()
        observed = data.get('observed_at')
        if (state == "ready" and 0 <= time.time() - updated < 21600
                and isinstance(observed, (int, float)) and not isinstance(observed, bool)
                and 0 <= time.time() - observed < 21600):
            return data
        if data.get('draft_id'):
            from .collection_capacity import account
            with self.db.connect() as db:
                exists=db.execute("SELECT 1 FROM sqlite_master WHERE name='draft_cleanup_receipts'").fetchone()
                deleted=db.execute("SELECT 1 FROM draft_cleanup_receipts WHERE account=? AND draft_id=? AND sku=? AND state='deleted'",
                    (account(self.c),str(data['draft_id']),self.sku)).fetchone() if exists else None
                if deleted:
                    replacement={'source_key':self.sku,'replaces_deleted_draft_id':data['draft_id']}
                    inserted=db.execute("UPDATE source_details SET state='claimed',body=?,updated=? WHERE key=? AND state=? AND updated=?",
                        (self.db.seal(replacement),time.time(),self.key,state,updated)).rowcount==1
                    if not inserted:raise Pending('source acquisition changed during draft reclamation')
                    state,data='claimed',replacement
        if data.get("draft_id"):
            detail = await self.call(
                "/api.product.collect/detail", query={"id": data["draft_id"], "is_online": 0}
            )
            data.pop("mapping", None)
            data.pop("mapping_client", None)
            data |= {"detail": detail, "observed_at": time.time()}
            await database_work(self.save,"ready", data)
            return data
        if not inserted and state=="draft_started" and time.time()-updated>120:
            return await self.recover_draft(data)
        if not inserted and state in ("claimed", "favorite_started", "favorite_rejected", "draft_rejected") and time.time() - updated > 120:
            # A read may be retried after its bounded subprocess timeout. A favorite write
            # is recovered by lookup only; the write itself is never replayed.
            if state == "favorite_started":
                data["favorite_attempted"] = True
            with self.db.connect() as db:
                inserted = (
                    db.execute(
                        "UPDATE source_details SET state='claimed',body=?,updated=? WHERE key=? AND state=? AND updated=?",
                        (self.db.seal(data), time.time(), self.key, state, updated),
                    ).rowcount
                    == 1
                )
        if not inserted:
            raise Pending("source draft outcome unresolved; not creating another draft")
        self.claim_updated = self.load()[2]
        try:
            favorite = await self.find_favorite(claimed=True)
            await database_work(self.renew_claim)
        except BaseException:
            if data.get("favorite_attempted"):
                with self.db.connect() as db:
                    db.execute(
                        "UPDATE source_details SET state='favorite_started',body=?,updated=? "
                        "WHERE key=? AND state='claimed' AND updated=?",
                        (self.db.seal(data), time.time(), self.key, self.claim_updated),
                    )
            elif not data.get('replaces_deleted_draft_id'):
                with self.db.connect() as db:
                    db.execute(
                        "DELETE FROM source_details WHERE key=? AND state='claimed' AND updated=?",
                        (self.key, self.claim_updated),
                    )
            raise
        if favorite is None:
            if data.get("favorite_attempted"):
                await database_work(self.save,"favorite_started", data)
                raise Pending("favorite creation not confirmed; lookup again later")
            if self.c.get("existing_favorite_only"):
                await database_work(self.save,"claimed", data)
                raise Pending("source favorite missing; a real price is required before creating one")
            from .collection_capacity import guard
            await guard(self)
            await database_work(self.save,"favorite_started", {"source_key": self.sku, "favorite_attempted": True})
            p = self.c["candidate"]
            try:
                await self.call(
                    "/api.product.favorite/toggle",
                    "POST",
                    body={
                        "status": True,
                        "productInfo": {
                            "sku": self.sku,
                            "title": p.get("title", ""),
                            "coverImage": p.get("image", ""),
                            "price_info": {"sell_price": p["price"], "currency": "CNY"},
                        },
                    },
                )
            except SourceAcquisitionFailure as error:
                diagnostic=error.diagnostic
                if (diagnostic.get('api_code')==0
                        and diagnostic.get('operation')=='/api.product.favorite/toggle'
                        and diagnostic.get('write_outcome_unknown') is False
                        and '收藏数量已达上限' in str(diagnostic.get('api_message',''))):
                    await database_work(self.save,'favorite_rejected',{'source_key':self.sku,'last_rejection':diagnostic})
                raise
            # Keep the unknown marker while renewing the lease for a long lookup.
            await database_work(self.save,"claimed", {"source_key": self.sku, "favorite_attempted": True})
            self.claim_updated = self.load()[2]
            favorite = await self.find_favorite(claimed=True)
            await database_work(self.renew_claim)
        if favorite is None:
            raise Pending("favorite creation not confirmed; lookup again later")
        replacement=data.get('replaces_deleted_draft_id')
        data = {"source_key": self.sku, "favorite_id": int(favorite["id"])}
        if replacement:
            data['replaces_deleted_draft_id']=replacement
            recovered=await self.recover_draft(data,allow_missing=True)
            if recovered:return recovered
        if str(favorite.get("is_imported")) == "1" and not replacement:
            await database_work(self.save,"draft_started", data)
            return await self.recover_draft(data)
        from .collection_capacity import guard, finish
        await guard(self, reserve=True)
        await database_work(self.save,"draft_started", data)
        try:
            draft = await self.call("/api.product.favorite/edit_import", "POST", body={"id": data["favorite_id"]})
        except SourceAcquisitionFailure as error:
            # A documented capacity refusal is definitive; transport failures remain unknown.
            diagnostic=error.diagnostic
            if (diagnostic.get('api_code')==0 and diagnostic.get('operation')=='/api.product.favorite/edit_import'
                    and '采集箱已满' in str(diagnostic.get('api_message',''))):
                data['last_rejection']=diagnostic
                await database_work(self.save,'draft_rejected',data)
                await database_work(finish,self, rejected=True)
            raise
        draft_id = int(draft.get("jump_id") or draft.get("id") or 0)
        if draft_id <= 0:
            raise ModuleError("source draft acknowledgement missing")
        data["draft_id"] = draft_id
        await database_work(self.save,"draft_ready", data)
        await database_work(finish,self)
        detail = await self.call("/api.product.collect/detail", query={"id": draft_id, "is_online": 0})
        data |= {"detail": detail, "observed_at": time.time()}
        await database_work(self.save,"ready", data)
        return data


async def map_detail(snapshot, context, seller):
    """Convert the exact source variant, resolving IDs against the current official schema."""
    if snapshot.get("source_key") != context["candidate"]["source_key"]:
        raise ModuleError("source detail SKU mismatch")
    detail = snapshot["detail"]
    variants = detail.get("skus", [])
    if len(variants) != 1:
        raise ModuleError("source has multiple variants; precise variant selection required")
    variant = variants[0]
    category = detail.get("category_id", [])
    if len(category) != 3 or not all(str(x).isdigit() for x in category):
        raise ModuleError("source category incomplete")
    category_id, type_id = int(category[1]), int(category[2])
    schema = (
        await seller(
            "/v1/description-category/attribute",
            {"description_category_id": category_id, "type_id": type_id, "language": "DEFAULT"},
        )
    ).get("result", [])
    if not schema:
        raise ModuleError("official category schema unavailable")
    definitions = {int(a["id"]): a for a in schema}
    raw, provenance, issues = {"source_key": snapshot["source_key"]}, {}, []

    def put(key, value):
        if value is not None and value != "" and value != []:
            raw[key] = value
            provenance[key] = (
                f"source SKU {snapshot['source_key']}, ERP draft {snapshot['draft_id']}, observed {snapshot['observed_at']}"
            )

    put("description_category_id", category_id)
    put("type_id", type_id)
    put("name", variant.get("name") or detail.get("title"))
    put("images", variant.get("images"))
    # Price, stock, warehouse, tax and package dimensions remain OUR verified inputs.
    merged = {}
    for attribute in detail.get("common_attributes", []) + variant.get("attributes", []):
        aid = int(attribute["id"])
        value = attribute.get("values")
        if aid in merged and merged[aid] != value:
            issues.append(f"{aid}：原商品属性与变体属性冲突")
            continue
        merged[aid] = value
    if 85 not in merged and detail.get("brand_id"):
        merged[85] = [detail["brand_id"]]
    if 4191 in definitions and 4191 not in merged and detail.get("description"):
        merged[4191] = detail["description"]
    attributes = []
    for aid, values in merged.items():
        spec = definitions.get(aid)
        if not spec or spec.get("attribute_complex_id"):
            continue
        if values is None or values == "" or values == []:
            continue
        values = values if isinstance(values, list) else [values]
        normalized = []
        for value in values:
            if spec.get("dictionary_id"):
                identifier = (
                    value.get("dictionary_value_id", value.get("id")) if isinstance(value, dict) else value
                )
                if not str(identifier).isdigit() or int(identifier) <= 0:
                    issues.append(f"{aid}：缺少有效字典 ID")
                    continue
                identifier = int(identifier)
                result = await seller(
                    "/v1/description-category/attribute/values",
                    {
                        "description_category_id": category_id,
                        "type_id": type_id,
                        "attribute_id": aid,
                        "last_value_id": identifier - 1,
                        "limit": 100,
                        "language": "DEFAULT",
                    },
                )
                exact = [r for r in result.get("result", []) if r.get("id") == identifier]
                if len(exact) != 1:
                    # ERP may retain an obsolete dictionary ID. Recover only by exact text.
                    label = value.get("value") if isinstance(value, dict) else None
                    if aid == 85:
                        brand = detail.get("brand_select") or {}
                        if str(brand.get("id")) == str(identifier):
                            label = brand.get("value")
                    if isinstance(label, str) and len(label.strip()) >= 2:
                        found = await seller(
                            "/v1/description-category/attribute/values/search",
                            {
                                "description_category_id": category_id,
                                "type_id": type_id,
                                "attribute_id": aid,
                                "value": label,
                                "limit": 100,
                            },
                        )
                        exact = [
                            r
                            for r in found.get("result", [])
                            if str(r.get("value", "")).strip().casefold() == label.strip().casefold()
                        ]
                        if len(exact) == 1:
                            identifier = exact[0]["id"]
                if len(exact) != 1:
                    issues.append(f"{aid}：原字典值已失效或无法核验")
                    continue
                normalized.append({"dictionary_value_id": identifier, "value": exact[0]["value"]})
            elif isinstance(value, (str, int, float, bool)):
                normalized.append({"value": str(value).lower() if isinstance(value, bool) else str(value)})
            elif isinstance(value, dict) and isinstance(value.get("value"), str):
                normalized.append({"value": value["value"]})
        if normalized:
            attributes.append({"id": aid, "complex_id": 0, "values": normalized})
    # ERP's editable heading may be an English collector label while attribute 4180
    # retains the original Russian title. Prefer the actual source title.
    title_attribute = next((a for a in attributes if a["id"] == 4180), None)
    source_title = title_attribute["values"][0].get("value", "") if title_attribute else ""
    if re.search(r"[А-Яа-яЁё]", source_title):
        put("name", source_title)
    elif not re.search(r"[А-Яа-яЁё]", str(raw.get("name", ""))):
        type_attribute = next((a for a in attributes if a["id"] == 8229), None)
        type_name = type_attribute["values"][0].get("value", "") if type_attribute else ""
        if re.search(r"[А-Яа-яЁё]", type_name):
            original = str(raw.get("name", ""))
            paper = re.search(r"\b[AА][0-9]\b", original)
            count = re.search(r"\b([1-9][0-9]*)\s*(?:pcs|pieces)\b", original, re.I)
            title = type_name + (" " + paper.group() if paper else "")
            if count:
                title += ", " + count.group(1) + " шт."
            put("name", title)
            provenance["name"] += "; official Russian type plus source format/count"
            if title_attribute:
                title_attribute["values"] = [{"value": title}]
    put("attributes", attributes)
    raw["provenance"] = provenance
    required = [a["id"] for a in schema if a.get("is_required")]
    missing = sorted(set(required) - {a["id"] for a in attributes})
    return {
        "dossier": raw,
        "issues": issues,
        "required_missing": missing,
        "mapped_attributes": len(attributes),
        "source": "maozi-source-draft",
        "mapping_version": 3,
        "identity_review_required": [a["id"] for a in attributes if a["id"] in (85, 4389, 23487, 9048)],
    }
