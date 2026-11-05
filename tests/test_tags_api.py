from tests.conftest import make_post


def test_list_tags_sorted_by_name_with_counts(client):
    make_post(client, tags=["python", "search"])
    make_post(client, tags=["python", "alpha"])
    tags = client.get("/tags/").json()
    assert [(t["name"], t["post_count"]) for t in tags] == [("alpha", 1), ("python", 2), ("search", 1)]
    assert set(tags[0]) == {"id", "name", "description", "created_at", "post_count"}


def test_list_tags_sorted_by_usage(client):
    make_post(client, tags=["python", "search"])
    make_post(client, tags=["python", "alpha"])
    names = [t["name"] for t in client.get("/tags/", params={"sort": "posts"}).json()]
    assert names == ["python", "alpha", "search"]


def test_list_tags_rejects_unknown_sort(client):
    assert client.get("/tags/", params={"sort": "size"}).status_code == 422


def test_list_tags_empty(client):
    assert client.get("/tags/").json() == []


def test_get_tag_normalises_the_name(client):
    make_post(client, tags=["Machine Learning"])
    response = client.get("/tags/  MACHINE   learning ")
    assert response.status_code == 200
    assert response.json()["name"] == "machine learning"
    assert response.json()["post_count"] == 1


def test_get_missing_tag_is_404(client):
    assert client.get("/tags/nope").status_code == 404


def test_patch_tag_description_and_clear_it(client):
    make_post(client, tags=["python"])
    response = client.patch("/tags/python", json={"description": "  The language  "})
    assert response.status_code == 200
    assert response.json()["description"] == "The language"
    assert client.get("/tags/python").json()["description"] == "The language"

    assert client.patch("/tags/python", json={}).json()["description"] == "The language"
    assert client.patch("/tags/python", json={"description": None}).json()["description"] is None


def test_patch_tag_validation_and_404(client):
    make_post(client, tags=["python"])
    assert client.patch("/tags/python", json={"description": "x" * 256}).status_code == 422
    assert client.patch("/tags/nope", json={"description": "x"}).status_code == 404


def test_tags_survive_post_deletion_with_zero_count(client):
    make_post(client, tags=["python"])
    client.patch("/tags/python", json={"description": "kept"})
    client.delete("/posts/1")
    tag = client.get("/tags/python").json()
    assert (tag["post_count"], tag["description"]) == (0, "kept")


def test_tag_paths_and_descriptions_reject_nul(client):
    make_post(client, tags=["python"])
    assert client.get("/tags/a%00b").status_code == 422
    assert client.patch("/tags/python", json={"description": "x\u0000y"}).status_code == 422


def test_blank_description_clears_it(client):
    make_post(client, tags=["python"])
    client.patch("/tags/python", json={"description": "kept"})
    assert client.patch("/tags/python", json={"description": "   "}).json()["description"] is None


def test_blank_tag_name_in_path_is_not_found(client):
    assert client.get("/tags/%20%20").status_code == 404
