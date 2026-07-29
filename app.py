"""Small HTTP service for sample-file download authorization."""

from __future__ import annotations

import os
import secrets
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Literal
from urllib.parse import quote, urlencode

from fastapi import FastAPI, HTTPException, Response, status
from pydantic import BaseModel, ConfigDict, Field

SCHEMA = """
CREATE TABLE IF NOT EXISTS samples (
    id TEXT PRIMARY KEY,
    owner_id TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS files (
    id TEXT PRIMARY KEY,
    sample_id TEXT NOT NULL REFERENCES samples(id),
    qc_status TEXT NOT NULL DEFAULT 'pending'
        CHECK (qc_status IN ('pending', 'passed', 'failed'))
);

CREATE TABLE IF NOT EXISTS grants (
    sample_id TEXT NOT NULL REFERENCES samples(id),
    user_id TEXT NOT NULL,
    PRIMARY KEY (sample_id, user_id)
);
"""


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
    """Small SQLite boundary; transactions and constraints enforce invariants."""

    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript(SCHEMA)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys = ON")
        try:
            yield db
        finally:
            db.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self.connect() as db, db:
            yield db


def error(status_code: int, code: str) -> None:
    raise HTTPException(status_code=status_code, detail={"code": code})


def create_app(database_path: str | Path | None = None) -> FastAPI:
    app = FastAPI(title="Sample File Access Service")
    store = Store(database_path or os.getenv("DATABASE_PATH", "sample-access.db"))

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/samples", status_code=status.HTTP_201_CREATED)
    def register_sample(sample: SampleInput) -> dict:
        file_ids = [file.id for file in sample.files]
        if len(file_ids) != len(set(file_ids)):
            error(status.HTTP_409_CONFLICT, "already_exists")

        try:
            with store.transaction() as db:
                db.execute(
                    "INSERT INTO samples (id, owner_id) VALUES (?, ?)",
                    (sample.id, sample.owner_id),
                )
                db.executemany(
                    "INSERT INTO files (id, sample_id) VALUES (?, ?)",
                    ((file_id, sample.id) for file_id in file_ids),
                )
        except sqlite3.IntegrityError:
            error(status.HTTP_409_CONFLICT, "already_exists")

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
        with store.transaction() as db:
            sample = db.execute(
                "SELECT 1 FROM samples WHERE id = ?", (sample_id,)
            ).fetchone()
            if sample is None:
                error(status.HTTP_404_NOT_FOUND, "no_such_sample")
            db.execute(
                "INSERT OR IGNORE INTO grants (sample_id, user_id) VALUES (?, ?)",
                (sample_id, user_id),
            )
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @app.post("/files/{file_id}/qc")
    def update_qc(file_id: str, update: QCInput) -> dict[str, str]:
        with store.transaction() as db:
            changed = db.execute(
                """
                UPDATE files SET qc_status = ?
                WHERE id = ? AND qc_status = 'pending'
                """,
                (update.status, file_id),
            )
            if changed.rowcount == 1:
                return {"file_id": file_id, "qc_status": update.status}

            file = db.execute(
                "SELECT qc_status FROM files WHERE id = ?", (file_id,)
            ).fetchone()
            if file is None:
                error(status.HTTP_404_NOT_FOUND, "no_such_file")
            if file["qc_status"] != update.status:
                error(status.HTTP_409_CONFLICT, "invalid_qc_transition")

        return {"file_id": file_id, "qc_status": update.status}

    @app.post("/files/{file_id}/download-requests")
    def request_download(file_id: str, request: DownloadInput) -> dict:
        with store.connect() as db:
            file = db.execute(
                """
                SELECT
                    files.qc_status,
                    samples.owner_id,
                    EXISTS (
                        SELECT 1 FROM grants
                        WHERE grants.sample_id = samples.id
                          AND grants.user_id = ?
                    ) AS has_grant
                FROM files
                JOIN samples ON samples.id = files.sample_id
                WHERE files.id = ?
                """,
                (request.user_id, file_id),
            ).fetchone()

        if file is None:
            error(status.HTTP_404_NOT_FOUND, "no_such_file")
        if file["owner_id"] != request.user_id and not file["has_grant"]:
            # Do not reveal QC state to a caller without sample access.
            error(status.HTTP_403_FORBIDDEN, "no_access")
        if file["qc_status"] == "pending":
            error(status.HTTP_409_CONFLICT, "qc_pending")
        if file["qc_status"] == "failed":
            error(status.HTTP_409_CONFLICT, "qc_failed")

        expires_at = int(time.time()) + 300
        query = urlencode(
            {"expires_at": expires_at, "token": secrets.token_urlsafe(16)}
        )
        url = f"https://downloads.example.test/files/{quote(file_id, safe='')}?{query}"
        return {"allowed": True, "url": url, "expires_at": expires_at}

    return app


app = create_app()
