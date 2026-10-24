import httpx
import pytest

from is_this_us.artworks.museum_clients import ClevelandClient, MetClient, RightsError


def test_real_archive_has_verified_rights_and_sources(repo):
    arts = repo.all()
    assert 20 <= len(arts) <= 30
    for a in arts:
        assert not a.is_fixture
        assert a.rights.status in {"Public Domain", "CC0"}
        assert str(a.page_url).startswith(("https://www.metmuseum.org", "https://clevelandart.org"))
        assert a.artist and a.title and a.museum


def _met_transport(seen):
    def handler(request: httpx.Request):
        seen.append(request)
        if request.url.path.endswith("/v1.1/search"):
            return httpx.Response(200, json={"total": 2, "objectIDs": [1, 2]})
        oid = request.url.path.rsplit("/", 1)[-1]
        return httpx.Response(200, json={
            "objectID": int(oid), "isPublicDomain": oid == "1", "primaryImageSmall": "https://img/x.jpg",
            "title": "T", "artistDisplayName": "A", "objectDate": "1700", "objectURL": "https://www.metmuseum.org/x",
            "classification": "Paintings",
        })
    return httpx.MockTransport(handler)


def test_met_client_uses_paginated_endpoint_user_agent_and_rights():
    seen = []
    client = MetClient(httpx.Client(transport=_met_transport(seen), headers={"User-Agent": "is-this-us/test"}))
    total, ids = client.search("dog", offset=100, limit=50)
    assert ids == [1, 2]
    req = seen[0]
    assert req.url.path == "/public/collection/v1.1/search"
    assert req.url.params["offset"] == "100" and req.url.params["limit"] == "50"
    assert req.headers["User-Agent"].startswith("is-this-us")
    assert client.get(1).rights_status == "Public Domain"
    with pytest.raises(RightsError):
        client.get(2)


def test_cleveland_requires_cc0():
    def handler(request):
        aid = request.url.path.rstrip("/").rsplit("/", 1)[-1]
        return httpx.Response(200, json={"data": {
            "id": int(aid), "title": "T", "creation_date": "1830", "url": "https://clevelandart.org/art/x",
            "share_license_status": "CC0" if aid == "1" else "Copyrighted",
            "images": {"web": {"url": "https://img/x.jpg"}}, "creators": [{"description": "Someone (British)"}],
        }})
    c = ClevelandClient(httpx.Client(transport=httpx.MockTransport(handler)))
    assert c.get(1).artist == "Someone"
    with pytest.raises(RightsError):
        c.get(2)
