# Sample File Access Service

A small FastAPI service for registering samples, granting access, recording QC
results, and issuing fake five-minute download URLs.

## Run

Requires Python 3.11+.

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app:app --reload
```

Data is stored in `sample-access.db` by default. Set `DATABASE_PATH` to use a
different location.

API documentation is at <http://127.0.0.1:8000/docs>. Run the checks with:

```bash
ruff check app.py test_app.py
ruff format --check app.py test_app.py
pytest -q
```

The four operations are:

```text
POST /samples
PUT  /samples/{sample_id}/grants/{user_id}
POST /files/{file_id}/qc
POST /files/{file_id}/download-requests
```

Example registration body:

```json
{
  "id": "sample-1",
  "owner_id": "alice",
  "files": [{"id": "reads.fastq"}]
}
```

The QC and download bodies are `{"status": "passed"}` and
`{"user_id": "alice"}` respectively.

## Choices

I chose SQLite because QC callbacks are asynchronous and access state should
survive a process restart. It is still a zero-infrastructure option, while
transactions make registration and QC changes atomic. Primary keys, foreign
keys, and a `CHECK` constraint enforce the important data invariants. I used
direct `sqlite3` calls rather than adding an ORM for three small tables.

Owners have implicit access; other users need a grant. Access is checked before
QC so unauthorized users do not learn a file's QC result. QC may move only from
`pending` to `passed` or `failed`. Repeating the same callback is safe, while a
contradictory terminal result returns `409`.

`PUT` makes grants naturally idempotent. Errors carry stable codes such as
`no_such_file`, `no_access`, `qc_pending`, and `qc_failed`. The fake URL contains
an opaque token and Unix expiry but intentionally has no download handler.

## Assumptions

User authentication/lifecycle is upstream, so user IDs are accepted as stable
identifiers. File IDs are globally unique. The trusted pipeline is the only
caller able to reach the QC endpoint. A sample may have no files.

## Alternatives and known weaknesses

In-memory dictionaries would be shorter, but lose grants and QC results on every
restart. PostgreSQL would be a better fit for a horizontally scaled production
service, but its deployment and migration setup are unnecessary for this
exercise.

SQLite still assumes a single service deployment with a shared local database
file. The fake URL is not cryptographically verifiable, and the service has no
authentication, audit log, rate limiting, schema migration tooling, or
production observability.

With more time I would move to the production database, add migrations and
identity integration, authenticate QC callbacks, generate Azure Blob SAS URLs,
and add structured audit events, metrics, and integration tests.
