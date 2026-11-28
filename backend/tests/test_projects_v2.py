"""Multi-page projects: page structure, revisions, batch render, multi-page PDF export.

Rendering is replaced by a fixed-size PNG so these tests do not need Chromium;
the real renderer is covered by test_renderer.py and test_exports_v2.py.
"""

import io
import re
import time
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from PIL import Image

import app.rendering.renderer as renderer
from app.config import Settings
from app.main import create_app
from app.services.projects import MAX_PAGES

TEXT = "标题：秋日读书会。正文：带上一本喜欢的书。时间：周六下午。地点：校园图书馆。"


@pytest.fixture
def client(tmp_path, monkeypatch):
    async def fake_render(template, fields, image_path=None):
        canvas = template["canvas"]
        output = io.BytesIO()
        Image.new("RGBA", (canvas["width"], canvas["height"]), (200, 30, 30, 255)).save(output, "PNG")
        return output.getvalue()

    monkeypatch.setattr(renderer, "render_template", fake_render)
    settings = Settings(
        _env_file=None, app_env="test", llm_mode="mock",
        data_dir=tmp_path / "data", database_path=tmp_path / "app.db",
    )
    with TestClient(create_app(settings)) as client:
        assert client.post("/api/session").status_code == 200
        yield client


def ok(response, status=200):
    assert response.status_code == status, response.text
    return response.json()


def project(client, title="读书月"):
    return ok(client.post("/api/v2/projects", json={"scenario": "poster", "title": title}), 201)


def add(client, item, text=TEXT):
    return ok(client.post(
        f"/api/v2/projects/{item['id']}/pages",
        json={"expectedRevision": item["revision"], "rawText": text, "style": "fresh"},
    ), 201)


def wait(client, job_id):
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        job = ok(client.get(f"/api/jobs/{job_id}"))
        if job["status"] not in {"queued", "running"}:
            return job
        time.sleep(0.02)
    raise AssertionError("job timeout")


def prepare(client, task_id):
    """Pick a template, run mock analysis and confirm facts, like a user would."""
    task = ok(client.get(f"/api/tasks/{task_id}"))
    task = ok(client.patch(f"/api/tasks/{task_id}", json={"expectedRevision": task["revision"], "templateId": "poster-fresh-v1"}))
    job = ok(client.post(f"/api/tasks/{task_id}/analyze", json={"expectedRevision": task["revision"]},
                         headers={"Idempotency-Key": str(uuid4())}), 202)
    assert wait(client, job["id"])["status"] == "succeeded"
    task = ok(client.get(f"/api/tasks/{task_id}"))
    return ok(client.post(f"/api/tasks/{task_id}/confirm", json={
        "expectedRevision": task["revision"],
        "confirmedFactIds": [fact["id"] for fact in task["analysis"]["facts"]],
    }))


def export(client, item):
    return client.get(f"/api/v2/projects/{item['id']}/export", params={"expectedRevision": item["revision"]})


def test_pages_are_ordered_tasks_and_v2_list_has_project_id(client):
    item = project(client)
    assert item["revision"] == 1 and item["pages"] == []
    item = add(client, item)
    item = add(client, item, TEXT.replace("秋日", "冬日"))
    assert item["revision"] == 3 and [page["position"] for page in item["pages"]] == [0, 1]
    assert all(page["scenario"] == "poster" and page["phase"] == "draft" for page in item["pages"])
    listed = ok(client.get("/api/v2/tasks"))["items"]
    assert {task["projectId"] for task in listed} == {item["id"]}
    summary = ok(client.get("/api/v2/projects"))["items"][0]
    assert summary["pageCount"] == 2 and summary["coverAssetId"] is None


def test_structure_changes_require_current_project_revision(client):
    item = add(client, add(client, project(client)))
    first, second = (page["id"] for page in item["pages"])
    stale = client.post(f"/api/v2/projects/{item['id']}/pages", json={"expectedRevision": 1, "rawText": "x"})
    assert stale.status_code == 409 and stale.json()["error"]["code"] == "PROJECT_REVISION_CONFLICT"
    partial = client.put(f"/api/v2/projects/{item['id']}/order", json={"expectedRevision": item["revision"], "taskIds": [first]})
    assert partial.status_code == 422
    item = ok(client.put(f"/api/v2/projects/{item['id']}/order", json={"expectedRevision": item["revision"], "taskIds": [second, first]}))
    assert [page["id"] for page in item["pages"]] == [second, first] and item["revision"] == 4
    item = ok(client.patch(f"/api/v2/projects/{item['id']}", json={"expectedRevision": 4, "title": "新名字"}))
    assert item["title"] == "新名字" and item["revision"] == 5


def test_duplicate_inserts_after_source_and_delete_renumbers(client):
    item = add(client, add(client, project(client)))
    source = item["pages"][0]
    item = ok(client.post(f"/api/v2/projects/{item['id']}/pages", json={
        "expectedRevision": item["revision"], "sourceTaskId": source["id"], "sourceRevision": source["revision"],
    }), 201)
    assert len(item["pages"]) == 3 and item["pages"][0]["id"] == source["id"]
    copy = item["pages"][1]
    assert copy["rawText"] == source["rawText"] and copy["displayName"].endswith("（副本）")
    item = ok(client.delete(f"/api/v2/projects/{item['id']}/pages/{copy['id']}",
                            params={"expectedRevision": item["revision"], "pageRevision": copy["revision"]}))
    assert [page["position"] for page in item["pages"]] == [0, 1]
    # Deleting a page through the single-work endpoint also renumbers and bumps.
    other = item["pages"][1]
    assert client.delete(f"/api/v2/tasks/{other['id']}", params={"expectedRevision": other["revision"]}).status_code == 204
    after = ok(client.get(f"/api/v2/projects/{item['id']}"))
    assert len(after["pages"]) == 1 and after["revision"] == item["revision"] + 1


def test_page_limit(client):
    item = project(client)
    for index in range(MAX_PAGES):
        item = add(client, item, f"标题：第{index}页")
    response = client.post(f"/api/v2/projects/{item['id']}/pages", json={"expectedRevision": item["revision"], "rawText": "x"})
    assert response.status_code == 409 and response.json()["error"]["code"] == "PAGE_LIMIT"


def test_render_each_page_then_export_multi_page_pdf(client):
    item = add(client, add(client, project(client)))
    assert export(client, item).json()["error"]["code"] == "PREVIEW_STALE"
    skipped = ok(client.post(f"/api/v2/projects/{item['id']}/render", headers={"Idempotency-Key": str(uuid4())}), 202)
    assert not skipped["queued"] and {entry["reason"] for entry in skipped["skipped"]} == {"CONFIRMATION_REQUIRED"}

    for page in item["pages"]:
        prepare(client, page["id"])
    key = str(uuid4())
    started = ok(client.post(f"/api/v2/projects/{item['id']}/render", headers={"Idempotency-Key": key}), 202)
    assert len(started["queued"]) == 2 and not started["failed"]
    for entry in started["queued"]:
        assert wait(client, entry["job"]["id"])["status"] == "succeeded"
    status = ok(client.get(f"/api/v2/projects/{item['id']}/status"))
    assert status["summary"]["exportable"] and status["summary"]["fresh"] == 2

    response = export(client, item)
    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "application/pdf"
    assert response.headers["x-export-pages"] == "2"
    assert len(re.findall(rb"/Type\s*/Page\b", response.content)) == 2
    boxes = re.findall(rb"/MediaBox\s*\[\s*0\s+0\s+([\d.]+)\s+([\d.]+)\s*\]", response.content)
    assert {tuple(float(value) for value in box) for box in boxes} == {(1080.0, 1440.0)}

    # Editing one page makes only that page stale; the rest stay exportable.
    edited = ok(client.get(f"/api/tasks/{item['pages'][1]['id']}"))
    ok(client.patch(f"/api/tasks/{edited['id']}", json={"expectedRevision": edited["revision"], "style": "modern"}))
    stale = export(client, item)
    assert stale.status_code == 409 and stale.json()["error"]["details"]["taskIds"] == [edited["id"]]


def test_failed_page_does_not_block_others_and_retry_recovers(client, monkeypatch):
    item = add(client, project(client))
    item = add(client, item, TEXT.replace("秋日", "春日"))
    good_id, broken_id = (page["id"] for page in item["pages"])
    for page in item["pages"]:
        prepare(client, page["id"])
    good = renderer.render_template
    calls = []

    async def flaky(template, fields, image_path=None):
        if "春日" in fields["title"] and not calls:
            calls.append(1)
            raise RuntimeError("simulated render failure")
        return await good(template, fields, image_path)

    monkeypatch.setattr(renderer, "render_template", flaky)
    first = ok(client.post(f"/api/v2/projects/{item['id']}/render", headers={"Idempotency-Key": str(uuid4())}), 202)
    results = {entry["taskId"]: wait(client, entry["job"]["id"])["status"] for entry in first["queued"]}
    assert results == {good_id: "succeeded", broken_id: "failed"}
    status = ok(client.get(f"/api/v2/projects/{item['id']}/status"))
    assert status["summary"]["failed"] == 1 and not status["summary"]["exportable"]

    # A retry needs a new key; it only re-queues the failed page.
    retry = ok(client.post(f"/api/v2/projects/{item['id']}/render", headers={"Idempotency-Key": str(uuid4())}), 202)
    assert [entry["taskId"] for entry in retry["queued"]] == [broken_id]
    assert wait(client, retry["queued"][0]["job"]["id"])["status"] == "succeeded"
    assert ok(client.get(f"/api/v2/projects/{item['id']}/status"))["summary"]["exportable"]


def test_delete_project_removes_pages_and_other_sessions_cannot_read(client):
    item = add(client, project(client))
    other = TestClient(client.app)
    assert other.post("/api/session").status_code == 200
    assert other.get(f"/api/v2/projects/{item['id']}").status_code == 404
    assert client.delete(f"/api/v2/projects/{item['id']}", params={"expectedRevision": item["revision"]}).status_code == 204
    assert ok(client.get("/api/v2/tasks"))["items"] == []
    assert client.get(f"/api/v2/projects/{item['id']}").status_code == 404
