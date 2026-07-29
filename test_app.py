from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

import app as service
from app import create_app

FIXED_TIME = 1_700_000_000


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(tmp_path / "test.db")) as test_client:
        yield test_client


def register(client, sample_id="s1", files=("f1",)):
    return client.post(
        "/samples",
        json={
            "id": sample_id,
            "owner_id": "owner",
            "files": [{"id": file_id} for file_id in files],
        },
    )


def code(response):
    return response.json()["detail"]["code"]


def test_sample_can_have_zero_files(client):
    created = register(client, files=())

    assert created.status_code == 201
    assert created.json()["files"] == []


def test_files_start_pending_and_owner_can_download_after_qc_passes(
    client, monkeypatch
):
    monkeypatch.setattr(service.time, "time", lambda: FIXED_TIME)
    monkeypatch.setattr(service.secrets, "token_urlsafe", lambda _: "test-token")
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
    assert allowed.json()["expires_at"] == FIXED_TIME + 300
    url = urlparse(allowed.json()["url"])
    assert url.path == "/files/f1"
    assert parse_qs(url.query) == {
        "expires_at": [str(FIXED_TIME + 300)],
        "token": ["test-token"],
    }


def test_granted_user_can_download_a_passed_file(client):
    register(client)
    client.post("/files/f1/qc", json={"status": "passed"})

    for _ in range(2):
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


def test_data_survives_an_app_restart(tmp_path):
    database = tmp_path / "persistent.db"
    with TestClient(create_app(database)) as first:
        register(first)
        first.post("/files/f1/qc", json={"status": "passed"})

    with TestClient(create_app(database)) as restarted:
        allowed = restarted.post(
            "/files/f1/download-requests", json={"user_id": "owner"}
        )

    assert allowed.status_code == 200


def test_missing_resources(client):
    missing_file = client.post(
        "/files/missing/download-requests", json={"user_id": "owner"}
    )
    assert (missing_file.status_code, code(missing_file)) == (404, "no_such_file")

    missing_qc_file = client.post("/files/missing/qc", json={"status": "passed"})
    assert (missing_qc_file.status_code, code(missing_qc_file)) == (
        404,
        "no_such_file",
    )

    missing_sample = client.put("/samples/missing/grants/user")
    assert (missing_sample.status_code, code(missing_sample)) == (
        404,
        "no_such_sample",
    )


def test_registration_conflicts_are_atomic(client):
    assert register(client, sample_id="first", files=("shared",)).status_code == 201

    conflict = register(client, sample_id="second", files=("new", "shared"))
    assert (conflict.status_code, code(conflict)) == (409, "already_exists")

    assert client.put("/samples/second/grants/user").status_code == 404
    missing_file = client.post(
        "/files/new/download-requests", json={"user_id": "owner"}
    )
    assert missing_file.status_code == 404
