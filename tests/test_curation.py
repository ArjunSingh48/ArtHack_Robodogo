import httpx
import pytest
import yaml

from is_this_us.artworks.museum_clients import ClevelandClient, MetClient, RightsError
from is_this_us.domain import EligibilityReview


def test_real_archive_has_verified_rights_and_sources(repo):
    arts = repo.all()
    assert arts, "The approved pilot collection must not be empty"
    for a in arts:
        assert a.review.status == "accepted"
        assert a.review.human_count == a.review.dog_count == 1
        assert a.review.dog_on_ground is True
        assert not a.is_fixture
        assert a.rights.status in {"Public Domain", "CC0"}
        assert str(a.page_url).startswith(("https://www.metmuseum.org", "https://clevelandart.org"))
        assert a.artist and a.title and a.museum


def test_generated_collection_matches_reviewed_sources(repo):
    """Every candidate stays auditable; only accepted notes reach the built dataset."""
    notes = yaml.safe_load((repo.data_dir / "curation_notes.yaml").read_text())["notes"]
    snapshot = yaml.safe_load((repo.data_dir / "museum_metadata.yaml").read_text())["artworks"]
    metadata = {a["id"]: a for a in snapshot}
    assert set(notes) == set(metadata)
    accepted = set()
    for key, note in notes.items():
        review = EligibilityReview.model_validate(note["review"])
        assert (repo.data_dir / review.image_reference).is_file()
        if review.eligible:
            accepted.add(key)
    assert {a.id for a in repo.all_records()} == accepted
    for art in repo.all():
        built = art.model_dump(mode="json")
        assert {field: built[field] for field in metadata[art.id]} == metadata[art.id]
        assert built["human"] == notes[art.id]["human"]
        assert built["dog"] == notes[art.id]["dog"]
        assert built["poem"] == notes[art.id]["poem"]


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
