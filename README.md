# Sample File Access Service

Small FastAPI service for registering samples, recording QC results, granting
access, and issuing fake five-minute download URLs.

## Run

Requires Python 3.11+.

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app:app --reload
```

The API docs are at <http://127.0.0.1:8000/docs>. Data is stored in
`sample-access.db`; set `DATABASE_PATH` to change the location.

```bash
ruff check app.py test_app.py
ruff format --check app.py test_app.py
pytest -q
```

## API

```text
POST /samples
PUT  /samples/{sample_id}/grants/{user_id}
POST /files/{file_id}/qc
POST /files/{file_id}/download-requests
```

Sample registration:

```json
{
  "id": "sample-1",
  "owner_id": "alice",
  "files": [{"id": "reads.fastq"}]
}
```

Files start `pending`. QC accepts `{"status": "passed"}` or
`{"status": "failed"}`. A download request accepts `{"user_id": "alice"}`.

## Design

SQLite keeps grants and QC results across restarts without an external service.
Transactions make registration and QC updates atomic; keys and constraints
enforce the data model. Three tables and a few queries did not justify an ORM,
so the service uses `sqlite3` directly.

In-memory dictionaries would be shorter but reset access decisions on restart.
PostgreSQL would support multiple service replicas but adds infrastructure and
migrations beyond this exercise.

Owners have implicit access; everyone else needs a grant. Authorization is
checked before QC so an unauthorized caller cannot learn a file's QC result.
Grants and repeated identical QC callbacks are idempotent. Contradictory
terminal QC results return `409`.

## Assumptions and limits

User identity is provided by an upstream system, file IDs are globally unique,
and only the trusted pipeline can reach the QC endpoint. A sample may have no
files.

SQLite assumes one deployment with a shared local database file. The generated
URL is not signed or served, and the service has no authentication, audit log,
rate limiting, migrations, or production observability.

With more time I would use the production database, add migrations and identity
integration, authenticate QC callbacks, generate Azure Blob SAS URLs, and add
audit events, metrics, and integration tests.
