import asyncio
import json
import time
from unittest.mock import AsyncMock

import pytest

from flowhub import comparebot
from flowhub.db import Database
from flowhub.modules import ModuleError
from flowhub.worker import Worker


@pytest.fixture
def candidate():
    return dict(
        source_key="123",
        title="Test",
        image="https://example.com/a.jpg",
        origin={},
        price=100,
        weight_g=0,
        dimensions_cm=[0, 0, 0],
    )


@pytest.fixture
def result():
    return dict(
        decision=dict(outcome="approved", selected_offer_id="456", reason="qwen_match_in_safe_band", qwen_review={"verdict": "match"}),
        search_and_rank=dict(
            query={"product_id": "123"},
            candidates=[
                dict(
                    dinov2_similarity=0.82,
                    candidate=dict(
                        offer_id="456",
                        title="Offer",
                        offer_url="https://detail.1688.com/offer/456.html",
                        image_url="https://example.com/b.jpg",
                        price_cny="10.5",
                    ),
                )
            ],
        ),
    )


def test_manifest_keeps_unknown_size(candidate):
    assert comparebot.manifest(candidate)["size"] == "unknown"
    candidate["origin"] = {"size": "small", "specifications": {"color": "red"}}
    assert comparebot.manifest(candidate)["specifications"] == {"color": "red"}


@pytest.mark.parametrize("outcome", ["rejected"])
async def test_nonapproval_never_calls_erp(monkeypatch, candidate, result, outcome):
    result["decision"]["outcome"] = outcome
    monkeypatch.setattr(comparebot, "screen", AsyncMock(return_value=result))
    erp = AsyncMock()
    monkeypatch.setattr(comparebot.compat, "invoke", erp)
    response = await comparebot.invoke("match", {"candidate": candidate}, "token")
    assert response[outcome] is True
    erp.assert_not_called()


async def test_approved_passes_exact_selected_source(monkeypatch, candidate, result):
    screen = AsyncMock(return_value=result)
    erp = AsyncMock(return_value={"evidence": {"source": {}, "profit": {"assessment": {"erp_profit_rate_pct": 25}, "input": {"package_weight": 499}, "sell_price_cny": 134}}})
    monkeypatch.setattr(comparebot, "screen", screen)
    monkeypatch.setattr(comparebot.compat, "invoke", erp)
    await comparebot.invoke(
        "match", {"candidate": candidate}, '{"erp_token":"erp","dashscope_api_key":"qwen"}'
    )
    assert screen.await_count == 2
    assert screen.call_args.args[0]["weight_g"] == 499
    assert screen.call_args.args[0]["sell_price_cny"] == 134
    source = erp.call_args.kwargs["source"]
    assert source["selected_cost_cny"] == 10.5
    assert source["selected_offer_image"] == {"available": True, "score": 0.82}


@pytest.mark.parametrize("price", [None, "NaN", "-1", "0"])
def test_unknown_price_requires_review(candidate, result, price):
    result["search_and_rank"]["candidates"][0]["candidate"]["price_cny"] = price
    assert comparebot.source_from_result(result, candidate)["manual_review"]


@pytest.mark.parametrize("field", ["product", "offer", "url"])
def test_wrong_binding_fails(candidate, result, field):
    if field == "product":
        result["search_and_rank"]["query"]["product_id"] = "other"
    elif field == "offer":
        result["decision"]["selected_offer_id"] = "other"
    else:
        result["search_and_rank"]["candidates"][0]["candidate"]["offer_url"] = "https://example.com/"
    with pytest.raises(ModuleError):
        comparebot.source_from_result(result, candidate)


async def test_worker_accepts_dinov2_without_fabricated_dhash(tmp_path, candidate, result):
    db = Database(tmp_path)
    worker = Worker(db)
    response = dict(
        supplier_id="456",
        supplier_url="https://detail.1688.com/offer/456.html",
        image="https://example.com/b.jpg",
        purchase=10.5,
        score=0.82,
        dhash=None,
        observed_at=time.time(),
        evidence={
            "source": {"comparebot": result},
            "profit": {
                "input": dict(package_weight=100, package_length=10, package_width=10, package_height=2)
            },
        },
    )
    worker.call = AsyncMock(return_value=response)
    worker.blocked = lambda j: False
    moved = []
    worker.move = lambda *a, **kw: moved.append((a, kw))
    job = dict(
        id="job",
        owner="owner",
        plan=None,
        phase="queued",
        created=time.time(),
        modules=json.dumps({"matcher": {"driver": "comparebot"}}),
        data=json.dumps({"candidate": candidate, "rules": {"image_min": 99, "dhash_min": 99}}),
    )
    await worker.advance(job)
    args = moved[0][0]
    assert args[1] == "matched"
    assert args[3]["match"]["dhash"] is None
    assert args[3]["candidate"]["weight_g"] == 100


def test_module_migration_preserves_frozen_jobs(tmp_path):
    db = Database(tmp_path)
    with db.connect() as c:
        c.execute("UPDATE modules SET driver='flowb' WHERE id='flowb-matcher'")
    Database(tmp_path)
    with db.connect() as c:
        assert c.execute("SELECT driver FROM modules WHERE id='flowb-matcher'").fetchone()[0] == "comparebot"


async def test_cli_cancellation_reaps_process(monkeypatch, candidate):
    class Process:
        returncode = None
        killed = False

        async def wait(self):
            if not self.killed:
                await asyncio.sleep(60)
            self.returncode = -9

        def kill(self):
            self.killed = True

    process = Process()
    called = asyncio.Event()

    async def spawn(*args, **kwargs):
        assert "DASHSCOPE_API_KEY" not in kwargs["env"]
        assert kwargs["env"]["PYTHON_DOTENV_DISABLED"] == "1"
        called.set()
        return process

    monkeypatch.setenv("DASHSCOPE_API_KEY", "another-user-key")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    task = asyncio.create_task(comparebot.screen(candidate))
    await called.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert process.killed and process.returncode == -9


@pytest.mark.parametrize("outcome, phase", [("manual_review", "attention"), ("rejected", "rejected")])
async def test_worker_routes_nonapproved_without_profit(tmp_path, candidate, outcome, phase):
    worker = Worker(Database(tmp_path))
    worker.call = AsyncMock(return_value={outcome: True, "reason": "test", "evidence": {}})
    worker.blocked = lambda j: False
    moves = []
    worker.move = lambda *args, **kwargs: moves.append(args)
    job = dict(
        id="job",
        owner="owner",
        plan=None,
        phase="queued",
        created=time.time(),
        modules=json.dumps({"matcher": {"driver": "comparebot"}}),
        data=json.dumps({"candidate": candidate, "rules": {}}),
    )
    await worker.advance(job)
    assert moves[0][1] == phase
    worker.call.assert_awaited_once_with(job, "matcher", "match")


async def test_cli_reads_result_and_keeps_keys_off_arguments(monkeypatch, candidate, result):
    from pathlib import Path

    class Process:
        returncode = 0

        async def wait(self):
            return 0

    async def spawn(*args, **kwargs):
        assert "private-key" not in args
        assert kwargs["env"]["DASHSCOPE_API_KEY"] == "private-key"
        assert "comparebot.interfaces.screen_cli" in args
        assert json.loads(Path(args[args.index("--manifest") + 1]).read_text())[0]["product_id"] == "123"
        Path(args[args.index("--output") + 1]).write_text(json.dumps(result))
        return Process()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    assert await comparebot.screen(candidate, "private-key", ranking=result["search_and_rank"]) == result


async def test_real_node_bridge_uses_selected_price_without_legacy_search(
    tmp_path, monkeypatch, candidate, result
):
    files = {
        "ozon-runtime/lib/maozi-credentials.mjs": "export async function resolveConfiguredMaoziToken(){return 'test'}",
        "ozon-runtime/lib/maozi-transport.mjs": "export function createGloballyPacedMaoziTransport(o){if(o.allowWrites)throw Error('writes');return async()=>({})}",
        "maozi_direct_new_method/maozi_new_method_direct.mjs": """
export function categoryPolicyFor(){return {eligible:true}}
export function prohibitedCategoryMatch(){return false}
export function prohibitedLeafCategoryMatch(){return false}
export async function observePureFbs(){return {verified:true}}
export function productSalePriceCny(){return 100}
export async function verify1688(){throw Error('legacy matcher must never run')}
export async function maoziProfit({purchasePrice}){return {
 input:{logistics:'ChinaPost',package_weight:100,package_length:10,package_width:10,package_height:2},
 assessment:{erp_profit_rate_pct:50,total_cost_cny:purchasePrice+10},sell_price_cny:100}}
""",
        "maozi_direct_new_method/lib/match-feedback.mjs": "export function feedbackBlockForProduct(){return {blocked:false}};export function feedbackBlockForPair(){return {blocked:false}}",
        "flow_ef_category_fbs/lib/brand-import-conflict.mjs": "export function blockedImportBrand(){return false}",
        "maozi_direct_new_method/lib/portable-support/maozi-client.mjs": "export function createMaoziClient(){return {listCategoryCommissions:async()=>[],getCategoryBySku:async()=>({})}}",
        "FlowEF-production/bridges/exchange-rate.mjs": "export async function cachedExchangeRate(){return {value:0.1}}",
        "maozi_direct_new_method/state/feedback/match-feedback.json": "{}",
        "maozi_direct_new_method/prohibited-categories.json": "{}",
        "flow_b_ef/state/config.json": '{"flow_f":{"blocked_source_brands":[]}}',
    }
    for name, content in files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    monkeypatch.setenv("FLOWHUB_LEGACY_ROOT", str(tmp_path))
    candidate["origin"] = {"sku": "123", "expansion_source": "fixture"}
    source = comparebot.source_from_result(result, candidate)
    response = await comparebot.compat.invoke("match", {"candidate": candidate}, "test-token", source=source)
    assert response["purchase"] == 10.5
    assert response["score"] == 0.82
    assert response["dhash"] is None
    assert response["evidence"]["profit"]["assessment"]["total_cost_cny"] == 20.5


@pytest.mark.parametrize("score,size,verdict,conflict,valid", [
    (.81, "unknown", "match", False, False),
    (.82, "unknown", "match", False, True),
    (.86, "small", None, False, True),
    (.9, "large", None, False, False),
    (.9, "large", "match", False, True),
    (.9, "small", "mismatch", True, False),
])
def test_approved_evidence_obeys_new_policy(candidate, result, score, size, verdict, conflict, valid):
    candidate.update(weight_g=100 if size == "small" else 500, sell_price_cny=100)
    result["search_and_rank"]["query"]["size"] = size
    result["search_and_rank"]["candidates"][0]["dinov2_similarity"] = score
    result["decision"]["qwen_review"] = {"verdict": verdict, "brand_or_model_conflict": conflict}
    if valid:
        assert comparebot.source_from_result(result, candidate)["selected_offer_id"] == "456"
    else:
        with pytest.raises(ModuleError):
            comparebot.source_from_result(result, candidate)


@pytest.mark.parametrize("weight,price,expected", [(499,134.99,"small"),(500,134,"large"),(499,135,"large"),(500,135,"large"),(None,100,"unknown"),(100,None,"unknown"),(None,135,"large"),(0,100,"unknown"),(float("nan"),100,"unknown")])
def test_size_uses_weight_and_cny_price(candidate, weight, price, expected):
    candidate.update(weight_g=weight,sell_price_cny=price)
    candidate["origin"]["size"]="small"
    assert comparebot.manifest(candidate)["size"] == expected

@pytest.mark.parametrize("profit,expected", [(24.999,"rejected"),(25,"manual_review"),(25.001,"manual_review")])
async def test_profit_precedes_manual_review(monkeypatch,candidate,result,profit,expected):
    result["decision"]["outcome"]="manual_review"
    screen=AsyncMock(return_value=result)
    monkeypatch.setattr(comparebot,"screen",screen)
    monkeypatch.setattr(comparebot.compat,"invoke",AsyncMock(return_value={"evidence":{"source":{},"profit":{"assessment":{"erp_profit_rate_pct":profit},"input":{"package_weight":501},"sell_price_cny":100}}}))
    response=await comparebot.invoke("match",{"candidate":candidate},"token")
    assert response[expected] is True
    assert screen.await_count == (1 if profit < 25 else 2)
