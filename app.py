"""Small HTTP service for sample-file download authorization."""

from __future__ import annotations

import secrets
import time
from threading import Lock
from typing import Literal
from urllib.parse import quote, urlencode

from fastapi import FastAPI, HTTPException, Response, status
from pydantic import BaseModel, ConfigDict, Field


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class FileInput(Input):
    id: str = Field(min_length=1, max_length=128)


class SampleInput(Input):
    id: str = Field(min_length=1, max_length=128)
    owner_id: str = Field(min_length=1, max_length=128)
    files: list[FileInput] = Field(default_factory=list)


class QCInput(Input):
    status: Literal["passed", "failed"]


class DownloadInput(Input):
    user_id: str = Field(min_length=1, max_length=128)


class Store:
    """Process-local storage; the lock makes each decision/update atomic."""

    def __init__(self) -> None:
        self.samples: dict[str, str] = {}  # sample_id -> owner_id
        self.files: dict[str, dict[str, str]] = {}
        self.grants: set[tuple[str, str]] = set()
        self.lock = Lock()


def error(status_code: int, code: str) -> None:
    raise HTTPException(status_code=status_code, detail={"code": code})


def create_app() -> FastAPI:
    app = FastAPI(title="Sample File Access Service")
    store = Store()

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/samples", status_code=status.HTTP_201_CREATED)
    def register_sample(sample: SampleInput) -> dict:
        file_ids = [file.id for file in sample.files]
        with store.lock:
            conflicts = (
                sample.id in store.samples
                or len(file_ids) != len(set(file_ids))
                or any(file_id in store.files for file_id in file_ids)
            )
            if conflicts:
                error(status.HTTP_409_CONFLICT, "already_exists")

            store.samples[sample.id] = sample.owner_id
            for file_id in file_ids:
                store.files[file_id] = {
                    "sample_id": sample.id,
                    "qc_status": "pending",
                }

        return {
            "id": sample.id,
            "owner_id": sample.owner_id,
            "files": [{"id": file_id, "qc_status": "pending"} for file_id in file_ids],
        }

    @app.put(
        "/samples/{sample_id}/grants/{user_id}",
        status_code=status.HTTP_204_NO_CONTENT,
    )
    def grant_access(sample_id: str, user_id: str) -> Response:
        with store.lock:
            if sample_id not in store.samples:
                error(status.HTTP_404_NOT_FOUND, "no_such_sample")
            store.grants.add((sample_id, user_id))
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @app.post("/files/{file_id}/qc")
    def update_qc(file_id: str, update: QCInput) -> dict[str, str]:
        with store.lock:
            file = store.files.get(file_id)
            if file is None:
                error(status.HTTP_404_NOT_FOUND, "no_such_file")

            current = file["qc_status"]
            if current == "pending":
                file["qc_status"] = update.status
            elif current != update.status:
                error(status.HTTP_409_CONFLICT, "invalid_qc_transition")

        return {"file_id": file_id, "qc_status": update.status}

    @app.post("/files/{file_id}/download-requests")
    def request_download(file_id: str, request: DownloadInput) -> dict:
        with store.lock:
            file = store.files.get(file_id)
            if file is None:
                error(status.HTTP_404_NOT_FOUND, "no_such_file")

            sample_id = file["sample_id"]
            is_owner = store.samples[sample_id] == request.user_id
            has_grant = (sample_id, request.user_id) in store.grants
            if not (is_owner or has_grant):
                # Do not reveal QC state to a caller without sample access.
                error(status.HTTP_403_FORBIDDEN, "no_access")

            qc_status = file["qc_status"]
            if qc_status == "pending":
                error(status.HTTP_409_CONFLICT, "qc_pending")
            if qc_status == "failed":
                error(status.HTTP_409_CONFLICT, "qc_failed")

        expires_at = int(time.time()) + 300
        query = urlencode(
            {"expires_at": expires_at, "token": secrets.token_urlsafe(16)}
        )
        url = f"https://downloads.example.test/files/{quote(file_id, safe='')}?{query}"
        return {"allowed": True, "url": url, "expires_at": expires_at}

    return app


app = create_app()
