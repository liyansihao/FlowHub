import html
import json
from datetime import datetime, timezone

import pytest

from flowhub.db import Database
from flowhub.source_library import SourceLibrary
from flowhub.storefront import StorefrontCollector, StorefrontError, parse_packet


def packet(page=1, sku="123", seller="12", continuation=True):
    url = f"/seller/{seller}/products/" + (f"?page={page}" if page > 1 else "")
    nxt = f"/seller/{seller}/products/?page={page + 1}" if continuation else ""
    state = dict(
        pageInfo=dict(url=url, pageType="seller", analyticsInfo=dict(sellerId=seller)),
        resolveParams={"current.time": datetime.now(timezone.utc).isoformat()},
    )
    grid = dict(
        items=[
            dict(
                sku=sku,
                id=sku,
                action=dict(link=f"/product/box-{sku}/"),
                mainState=[dict(id="name", textDS=dict(text="box"))],
            )
        ]
    )
    widgets = {
        f"state-tileGridDesktop-1-default-{page}": grid,
        "state-skuGrid-1-default-1": dict(items=[dict(sku="999")]),
    }
    if page == 1:
        widgets["state-infiniteVirtualPaginator-1-default-1"] = dict(nextPage=nxt)
    else:
        state["nextPage"] = nxt
    content = "".join(
        f'<div id="{k}" data-state="{html.escape(json.dumps(v), quote=True)}"></div>'
        for k, v in widgets.items()
    )
    return content + "<script>window.__NUXT__.state=" + repr(json.dumps(state)) + ";</script>"


def collector(path):
    return StorefrontCollector(SourceLibrary(Database(path)))


def test_actual_cursor_sources_and_recommendations():
    assert [p["sku"] for p in parse_packet(packet(), "12", "/seller/12/products/")["products"]] == ["123"]
    assert parse_packet(packet(2), "12", "/seller/12/products/?page=2")["next_url"].endswith("page=3")
    assert parse_packet(packet(2, continuation=False), "12", "/seller/12/products/?page=2")["explicit_end"]


def test_pause_restart_replay_and_rollback(tmp_path):
    c = collector(tmp_path)
    c.prepare("a", {"12": [{"sku": "1"}]})
    c.control("a", "12", "resume")
    first = packet()
    assert c.ingest("a", "12", first, "/seller/12/products/")["added"] == 1
    c.control("a", "12", "pause")
    c = collector(tmp_path)
    with pytest.raises(StorefrontError, match="task_paused"):
        c.ingest("a", "12", packet(2, "124"), "/seller/12/products/?page=2")
    assert c.ingest("a", "12", first, "/seller/12/products/")["state"] == "replay"
    c.control("a", "12", "resume")
    second = packet(2, "124")
    # Wrong-page HTML cannot advance checkpoint or insert any products.
    assert c.ingest("a", "12", packet(3, "125"), "/seller/12/products/?page=2")["reason"] == "cursor_mismatch"
    assert c.tasks("a")[0]["page"] == 2
    assert c.tasks("a")[0]["state"] == "blocked"
    c.control("a", "12", "retry")
    assert c.ingest("a", "12", second, "/seller/12/products/?page=2")["added"] == 1
    with c.db.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM sourcing_products").fetchone()[0] == 2
        assert db.execute("SELECT COUNT(*) FROM storefront_attempts WHERE state='failed'").fetchone()[0] == 1
    with pytest.raises(KeyError):
        c.control("other", "12", "pause")
    assert c.tasks("other") == []


def test_repeated_skus_different_page_blocked(tmp_path):
    c = collector(tmp_path)
    c.prepare("a", {"12": []})
    c.control("a", "12", "resume")
    c.ingest("a", "12", packet(), "/seller/12/products/")
    assert c.ingest("a", "12", packet(2), "/seller/12/products/?page=2")["reason"] == "repeated_page"
    assert c.tasks("a")[0]["page"] == 2


@pytest.mark.parametrize(
    "body,reason",
    [
        ("<title>Antibot Captcha</title>", "captcha_or_missing_state"),
        (packet(seller="13"), "identity_mismatch"),
        (packet().replace("nextPage", "unknown"), "missing_or_conflicting_continuation"),
    ],
)
def test_invalid_page(body, reason):
    with pytest.raises(StorefrontError, match=reason):
        parse_packet(body, "12", "/seller/12/products/")


def test_offsite_continuation():
    body = packet().replace("/seller/12/products/?page=2", "https://evil.example/seller/12/products/?page=2")
    with pytest.raises(StorefrontError, match="identity_mismatch"):
        parse_packet(body, "12", "/seller/12/products/")


def test_api_intake_controls_and_owner(tmp_path):
    from fastapi.testclient import TestClient

    from flowhub.api import create_app
    from flowhub.db import password_hash

    db = Database(tmp_path)
    with db.connect() as c:
        c.execute("UPDATE users SET password=?,must_change=0", (password_hash("Test-Password-2026"),))
        owner = c.execute("SELECT id FROM users").fetchone()[0]
    app = create_app(db)
    intake = StorefrontCollector(SourceLibrary(db))
    intake.prepare(owner, {"12": [{"sku": "1"}]})
    with TestClient(app) as client:
        assert (
            client.post("/api/login", json=dict(username="admin", password="Test-Password-2026")).status_code
            == 200
        )
        client.headers["X-CSRF-Token"] = client.get("/api/me").json()["csrf"]
        assert client.get("/api/sources/storefront").json()[0]["state"] == "paused"
        assert client.post("/api/sources/storefront/12/resume", json={}).status_code == 200
        r = client.post(
            "/api/sources/storefront/12/import",
            json=dict(html=packet(), requested_url="/seller/12/products/"),
        )
        assert r.status_code == 200, r.text
        assert r.json()["added"] == 1
        assert client.get("/api/sources/storefront/12/attempts").json()[0]["state"] == "committed"
        assert client.post("/api/sources/storefront/12/pause", json={}).json()["page"] == 2
        assert client.post("/api/sources/storefront/999/resume", json={}).status_code == 404


def test_non_ruble_price_keeps_display_without_fabricating_rubles():
    source = packet().replace(
        "&quot;mainState&quot;: [",
        "&quot;mainState&quot;: [{&quot;priceV2&quot;: {&quot;price&quot;: [{&quot;textStyle&quot;: &quot;PRICE&quot;, &quot;text&quot;: &quot;25,18 ¥&quot;}] }},",
    )
    product = parse_packet(source, "12", "/seller/12/products/")["products"][0]
    assert product["current_price_display"] == "25,18 ¥"
    assert product["current_price_rub"] is None
    rubles = source.replace("25,18 ¥", "250,18 ₽")
    assert parse_packet(rubles, "12", "/seller/12/products/")["products"][0]["current_price_rub"] == 250.18


def terminal_packet(total=2, sold_out=True):
    state = dict(
        pageInfo=dict(
            url="/seller/12/products/?page=2" + ("&sold_out_page=1" if sold_out else ""),
            pageType="seller",
            analyticsInfo=dict(sellerId="12"),
        ),
        resolveParams={"current.time": datetime.now(timezone.utc).isoformat()},
        shared=dict(catalog=dict(totalPages=total, currentSoldOutPage=2)),
        layout=[dict(component="separator", stateId="separator-1-default-2")],
    )
    return "<script>window.__NUXT__.state=" + repr(json.dumps(state)) + ";</script>"


def test_explicit_sold_out_terminal_and_missing_grid_distinction():
    result = parse_packet(terminal_packet(), "12", "/seller/12/products/?page=2&sold_out_page=1")
    assert result["explicit_end"] and result["products"] == []
    assert result["terminal_evidence"]["totalPages"] == 2
    with pytest.raises(StorefrontError, match="missing_or_wrong_product_grid"):
        parse_packet(terminal_packet(total=3), "12", "/seller/12/products/?page=2&sold_out_page=1")
    with pytest.raises(StorefrontError, match="missing_or_wrong_product_grid"):
        parse_packet(terminal_packet(sold_out=False), "12", "/seller/12/products/?page=2")


def test_committed_terminal_stays_done_after_restart(tmp_path):
    c = collector(tmp_path)
    c.prepare("a", {"12": [{"sku": "1"}]})
    c.control("a", "12", "resume")
    first = packet().replace("/seller/12/products/?page=2", "/seller/12/products/?page=2&amp;sold_out_page=1")
    assert c.ingest("a", "12", first, "/seller/12/products/")["state"] == "committed"
    assert (
        c.ingest("a", "12", terminal_packet(), "/seller/12/products/?page=2&sold_out_page=1")["state"]
        == "committed"
    )
    assert collector(tmp_path).control("a", "12", "resume")["state"] == "done"
