"""Read the supplied seed SQL (from S3 or a local path) and load it into Postgres.

The seed file is held in memory only; nothing is written to the host's disk.
Its DDL and INSERTs are standard SQL, so they run unchanged. Two adjustments:
  * statements are re-ordered so referenced tables are created and filled first
    (the SQLite dump is alphabetical, and Postgres checks foreign keys at once);
  * rows in self-referencing tables (appointments.replaced_appointment_id,
    operational_messages.reply_to_message_id) are retried until their parent exists.
"""
from __future__ import annotations

import re
from graphlib import TopologicalSorter
from pathlib import Path

import psycopg

_CREATE_TABLE = re.compile(r'^CREATE TABLE\s+"?(\w+)"?', re.I)
_INSERT = re.compile(r'^INSERT INTO\s+"?(\w+)"?', re.I)
_REFS = re.compile(r'REFERENCES\s+"?(\w+)"?', re.I)


def read_snapshot(uri: str, region: str = "us-east-1") -> str:
    if uri.startswith("s3://"):
        import boto3  # only needed when the snapshot lives in S3

        bucket, key = uri[5:].split("/", 1)
        body = boto3.client("s3", region_name=region).get_object(Bucket=bucket, Key=key)["Body"]
        return body.read().decode("utf-8")
    return Path(uri).read_text(encoding="utf-8")


def split_statements(sql: str) -> list[str]:
    """Split on ';' outside single-quoted strings; drop comments and BEGIN/COMMIT."""
    out, buf, in_str, i = [], [], False, 0
    while i < len(sql):
        ch = sql[i]
        if in_str:
            buf.append(ch)
            if ch == "'":
                if i + 1 < len(sql) and sql[i + 1] == "'":
                    buf.append("'"); i += 1
                else:
                    in_str = False
        elif ch == "'":
            in_str = True; buf.append(ch)
        elif ch == "-" and sql.startswith("--", i):
            i = sql.find("\n", i)
            i = len(sql) if i == -1 else i
            continue
        elif ch == ";":
            stmt = "".join(buf).strip()
            if stmt and stmt.upper() not in ("BEGIN TRANSACTION", "BEGIN", "COMMIT"):
                out.append(stmt)
            buf = []
        else:
            buf.append(ch)
        i += 1
    tail = "".join(buf).strip()
    if tail:
        out.append(tail)
    return out


def plan(statements: list[str]) -> tuple[list[str], dict[str, list[str]], list[str]]:
    """-> (CREATE TABLEs in dependency order, INSERTs by table, other DDL in file order)."""
    creates: dict[str, str] = {}
    deps: dict[str, set[str]] = {}
    inserts: dict[str, list[str]] = {}
    other: list[str] = []
    for st in statements:
        if m := _CREATE_TABLE.match(st):
            t = m.group(1)
            creates[t] = st
            deps[t] = {r for r in _REFS.findall(st) if r != t}
        elif m := _INSERT.match(st):
            inserts.setdefault(m.group(1), []).append(st)
        else:
            other.append(st)  # indexes and views, which only need the tables to exist
    order = list(TopologicalSorter(deps).static_order())
    return [creates[t] for t in order], {t: inserts.get(t, []) for t in order}, other


def load(conn: psycopg.Connection, sql_text: str) -> dict[str, int]:
    """Create the supplied schema and seed rows in one transaction. Returns row counts."""
    creates, inserts, other = plan(split_statements(sql_text))
    counts: dict[str, int] = {}
    with conn.transaction():
        for st in creates:
            conn.execute(st)
        for table, rows in inserts.items():
            pending = rows
            while pending:
                failed = []
                for st in pending:
                    try:
                        with conn.transaction():  # savepoint
                            conn.execute(st)
                    except psycopg.errors.ForeignKeyViolation:
                        failed.append(st)
                if len(failed) == len(pending):
                    raise RuntimeError(f"unresolvable foreign keys in {table}: {failed[0][:120]}")
                pending = failed
            counts[table] = len(rows)
        for st in other:
            conn.execute(st)
    return counts
