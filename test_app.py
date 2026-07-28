import time
from urllib.parse import parse_qs, urlparse

import pytest
from app import create_app
from fastapi.testclient import TestClient


@pytest.fixture
def client():
    with TestClient(create_app()) as test_client:
        yield test_client


def register(client, files=("f1",)):
    return client.post(
        "/samples",
        json={
            "id": "s1",
            "owner_id": "owner",
            "files": [{"id": file_id} for file_id in files],
        },
    )


def code(response):
    return response.json()["detail"]["code"]


def test_files_start_pending_and_owner_can_download_after_qc_passes(client):
    created = register(client, files=("f1", "f2"))
    assert created.status_code == 201
    assert [file["qc_status"] for file in created.json()["files"]] == [
        "pending",
        "pending",
    ]

    pending = client.post("/files/f1/download-requests", json={"user_id": "owner"})
    assert (pending.status_code, code(pending)) == (409, "qc_pending")

    assert client.post("/files/f1/qc", json={"status": "passed"}).status_code == 200
    allowed = client.post("/files/f1/download-requests", json={"user_id": "owner"})
    assert allowed.status_code == 200
    url = urlparse(allowed.json()["url"])
    assert url.path == "/files/f1"
    assert int(parse_qs(url.query)["expires_at"][0]) > int(time.time())


def test_granted_user_can_download_a_passed_file(client):
    register(client)
    client.post("/files/f1/qc", json={"status": "passed"})

    assert client.put("/samples/s1/grants/researcher").status_code == 204
    allowed = client.post("/files/f1/download-requests", json={"user_id": "researcher"})
    assert allowed.status_code == 200


def test_access_is_checked_before_qc(client):
    register(client)

    denied = client.post("/files/f1/download-requests", json={"user_id": "outsider"})

    assert (denied.status_code, code(denied)) == (403, "no_access")
    assert "qc" not in denied.text


def test_failed_qc_denies_download(client):
    register(client)
    client.post("/files/f1/qc", json={"status": "failed"})

    denied = client.post("/files/f1/download-requests", json={"user_id": "owner"})

    assert (denied.status_code, code(denied)) == (409, "qc_failed")


def test_qc_callback_is_idempotent_but_cannot_change_a_terminal_result(client):
    register(client)
    for _ in range(2):
        assert client.post("/files/f1/qc", json={"status": "passed"}).status_code == 200

    conflict = client.post("/files/f1/qc", json={"status": "failed"})
    assert (conflict.status_code, code(conflict)) == (
        409,
        "invalid_qc_transition",
    )


def test_missing_resources_and_registration_conflicts(client):
    missing_file = client.post(
        "/files/missing/download-requests", json={"user_id": "owner"}
    )
    assert (missing_file.status_code, code(missing_file)) == (404, "no_such_file")

    missing_sample = client.put("/samples/missing/grants/user")
    assert (missing_sample.status_code, code(missing_sample)) == (
        404,
        "no_such_sample",
    )

    register(client, files=())
    duplicate = register(client, files=())
    assert (duplicate.status_code, code(duplicate)) == (409, "already_exists")
