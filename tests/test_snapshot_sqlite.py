from src.snapshot.sqlite_snapshot import SnapshotStore


def test_snapshot_store_crud(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    db = tmp_path / "state" / "cache.sqlite"

    s = SnapshotStore(db_path=db)
    rid = "gid://shopify/Product/123"
    assert s.get_digest_map(rid) == {}

    s.upsert_digest(rid, "title", "d1")
    s.upsert_digest(rid, "body_html", "d2")
    m = s.get_digest_map(rid)
    assert m["title"] == "d1"
    assert m["body_html"] == "d2"

    s.set_digest_map(rid, {"title": "d3", "seo.title": "d4"})
    m2 = s.get_digest_map(rid)
    assert m2["title"] == "d3"
    assert m2["seo.title"] == "d4"

    s.close()

