"""The database of a `SQLiteFrontier`: its schema, and its operations, one transaction each.

A `FrontierStore` holds the only connection to its database and is used from one
thread only, the Frontier's own. The table `requests` holds every Request that is
ready, delayed, leased, or dead; a done Request is deleted, and only its Fingerprint
stays, in `fingerprints`. Triggers keep two tables in step with `requests`, in the
same transaction: `counts`, the Requests in each state, done ones included, and
`heads`, the first ready Request of each slot, of each kind, so that a claim skips a
slot by its head rather than by each of its Requests.

`PRAGMA application_id` marks the database as a Frontier's, and `PRAGMA user_version`
holds its schema version, `SCHEMA_VERSION` for the databases this Pernix creates. A new
database is created at version 1 and migrated as an older one is, in the transaction
that opens it.

`open_read_only()` opens a database to read it as it is, changing nothing in it or
beside it, also while a `FrontierStore` writes it; `read_counts()` and
`read_dead_page()` read it, as `FrontierStore` does.
"""

from __future__ import annotations

import itertools
import json
import os
import sqlite3
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from pernix.errors import SerializationError
from pernix.frontiers.frontier import FrontierCounts, Lease

if TYPE_CHECKING:
    from collections.abc import Callable, Collection, Generator, Sequence

    from pernix.request import Request

__all__ = ["SCHEMA_VERSION", "FrontierStore", "RequestRow", "open_read_only", "read_counts", "read_dead_page"]

SCHEMA_VERSION = 2
"""The version of the database schema this Pernix creates and reads."""

_APPLICATION_ID = 0x706E7866
"""Marks a database as a Frontier's, in `PRAGMA application_id`: "pnxf"."""

# The state of a Request is its `state`. `sequence` orders the ready Requests of one
# priority; `due` is when a delayed Request becomes ready, or when a Lease expires,
# and is NULL in other states. `url` repeats the Request's URL for inspection.
_SCHEMA = """
CREATE TABLE requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    state TEXT NOT NULL,
    rendered INTEGER NOT NULL,
    priority INTEGER NOT NULL,
    sequence INTEGER NOT NULL,
    slot TEXT NOT NULL,
    url TEXT NOT NULL,
    request BLOB NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    spent TEXT NOT NULL DEFAULT '{}',
    token INTEGER NOT NULL DEFAULT 0,
    worker TEXT,
    due REAL,
    reason TEXT NOT NULL DEFAULT ''
);
CREATE INDEX requests_ready ON requests (rendered, slot, priority DESC, sequence) WHERE state = 'ready';
CREATE INDEX requests_due ON requests (due) WHERE due IS NOT NULL;

CREATE TABLE fingerprints (fingerprint TEXT PRIMARY KEY) WITHOUT ROWID;

CREATE TABLE counts (state TEXT PRIMARY KEY, requests INTEGER NOT NULL) WITHOUT ROWID;
INSERT INTO counts VALUES ('ready', 0), ('delayed', 0), ('leased', 0), ('done', 0), ('dead', 0);

CREATE TABLE heads (
    rendered INTEGER NOT NULL,
    slot TEXT NOT NULL,
    priority INTEGER NOT NULL,
    sequence INTEGER NOT NULL,
    id INTEGER NOT NULL,
    PRIMARY KEY (rendered, slot)
) WITHOUT ROWID;
CREATE INDEX heads_of_kind ON heads (rendered, priority DESC, sequence);
CREATE INDEX heads_in_order ON heads (priority DESC, sequence);

CREATE TRIGGER request_added AFTER INSERT ON requests BEGIN
    UPDATE counts SET requests = requests + 1 WHERE state = NEW.state;
    DELETE FROM heads WHERE NEW.state = 'ready' AND rendered = NEW.rendered AND slot = NEW.slot;
    INSERT INTO heads (rendered, slot, priority, sequence, id)
        SELECT rendered, slot, priority, sequence, id FROM requests
        WHERE NEW.state = 'ready' AND state = 'ready' AND rendered = NEW.rendered AND slot = NEW.slot
        ORDER BY priority DESC, sequence LIMIT 1;
END;

CREATE TRIGGER request_moved AFTER UPDATE OF state, rendered, priority, sequence ON requests BEGIN
    UPDATE counts SET requests = requests - 1 WHERE state = OLD.state;
    UPDATE counts SET requests = requests + 1 WHERE state = NEW.state;
    DELETE FROM heads WHERE OLD.state = 'ready' AND rendered = OLD.rendered AND slot = OLD.slot;
    INSERT INTO heads (rendered, slot, priority, sequence, id)
        SELECT rendered, slot, priority, sequence, id FROM requests
        WHERE OLD.state = 'ready' AND state = 'ready' AND rendered = OLD.rendered AND slot = OLD.slot
        ORDER BY priority DESC, sequence LIMIT 1;
    DELETE FROM heads WHERE NEW.state = 'ready' AND rendered = NEW.rendered AND slot = NEW.slot;
    INSERT INTO heads (rendered, slot, priority, sequence, id)
        SELECT rendered, slot, priority, sequence, id FROM requests
        WHERE NEW.state = 'ready' AND state = 'ready' AND rendered = NEW.rendered AND slot = NEW.slot
        ORDER BY priority DESC, sequence LIMIT 1;
END;

CREATE TRIGGER request_done AFTER DELETE ON requests BEGIN
    UPDATE counts SET requests = requests - 1 WHERE state = OLD.state;
    UPDATE counts SET requests = requests + 1 WHERE state = 'done';
    DELETE FROM heads WHERE OLD.state = 'ready' AND rendered = OLD.rendered AND slot = OLD.slot;
    INSERT INTO heads (rendered, slot, priority, sequence, id)
        SELECT rendered, slot, priority, sequence, id FROM requests
        WHERE OLD.state = 'ready' AND state = 'ready' AND rendered = OLD.rendered AND slot = OLD.slot
        ORDER BY priority DESC, sequence LIMIT 1;
END;
"""

_MIGRATIONS = {
    2: "ALTER TABLE requests ADD COLUMN methods TEXT",
}
"""What brings a database from the version before to each version; version 1 is `_SCHEMA`.

Version 2 adds `methods`: the names of the Spider methods a Request refers to,
separated by spaces, which are checked when a Run resumes; NULL for the Requests
stored before.
"""


@dataclass(frozen=True, slots=True)
class RequestRow:
    """A Request prepared for the database.

    Attributes:
        fingerprint: Its Fingerprint, or `None` for a Request with `dont_filter`.
        slot: Its concurrency slot.
        rendered: Whether it goes to the browser Fetcher.
        priority: Its priority.
        url: Its URL.
        data: The Request, as `pernix.request_codec.encode_request()` encodes it.
        methods: The names of the Spider methods it refers to, as
            `pernix.request_codec.method_names()` tells.
    """

    fingerprint: str | None
    slot: str
    rendered: bool
    priority: int
    url: str
    data: bytes
    methods: tuple[str, ...] = ()


class FrontierStore:
    """The Requests of a Frontier in a SQLite database, run through the Lease state machine.

    Each operation is one transaction. It reads the time once it holds the
    database, so that the time it waited for it is taken from no Lease or delay,
    and first makes the delays that passed ready, and the Requests whose Leases
    expired, counting an attempt, as `MemoryFrontier` does. The database is opened
    by the first operation, and created if it is missing, readable by its owner
    only.

    Attributes:
        changed: Whether the last operation changed something a waiting Worker may
            wait for: a Request that became ready or was settled, or a deadline
            that came sooner.
    """

    def __init__(self, path: str, restore: Callable[[bytes], Request], clock: Callable[[], float]) -> None:
        """Configure the store; the database is not opened yet.

        Args:
            path: The database file.
            restore: Decodes a Request stored as `RequestRow.data`.
            clock: The Frontier's clock, in seconds.
        """
        self._path = path
        self._restore = restore
        self._clock = clock
        self._connection: sqlite3.Connection | None = None
        self._sequences = itertools.count(1)
        self._tokens = itertools.count(1)
        self.changed = False

    def add(self, rows: Sequence[RequestRow]) -> list[bool]:
        """Store each Request whose Fingerprint is new; see `Frontier.add()`.

        Returns:
            For each Request, whether it was stored.
        """
        with self._transaction() as (connection, _):
            added = [self._insert(connection, row) for row in rows]
        self.changed |= any(added)
        return added

    def claim(
        self,
        worker_id: str,
        limit: int,
        ttl: float,
        skip_slots: Collection[str],
        rendered: bool | None,
    ) -> tuple[list[Lease], list[str]]:
        """Lease ready Requests; see `Frontier.claim()`.

        A Request that cannot be restored is kept as a Dead Request instead, with
        the error as its reason, and the claim goes on.

        Returns:
            The Leases, and why each Request kept as a Dead Request so failed.
        """
        with self._transaction() as (connection, now):
            due = self._next_due(connection)
            leases: list[Lease] = []
            lost: list[str] = []
            while len(leases) < limit and (found := self._first_ready(connection, skip_slots, rendered)) is not None:
                try:
                    leases.append(self._lease(connection, found, now + ttl, worker_id))
                except SerializationError as exc:
                    reason = f"{type(exc).__name__}: {exc}"
                    connection.execute("UPDATE requests SET state = 'dead', reason = ? WHERE id = ?", (reason, found))
                    lost.append(str(exc))
        if lost or (leases and (due is None or now + ttl < due)):
            self.changed = True  # fewer Requests are unfinished, or the earliest deadline is sooner
        return leases, lost

    def release_leases(self) -> int:
        """Make every leased Request ready again, counting an attempt, as if its Lease had expired.

        Returns:
            How many there were.
        """
        with self._transaction() as (connection, now):
            released = connection.execute("UPDATE requests SET due = ? WHERE state = 'leased'", (now,)).rowcount
            self._make_due_ready(connection, now)
        self.changed |= released > 0
        return released

    def method_names(self) -> set[str]:
        """Return the names of the Spider methods the Requests that are not done or dead refer to."""
        with self._transaction() as (connection, _):
            rows = connection.execute(
                "SELECT DISTINCT methods FROM requests WHERE state != 'dead' AND methods IS NOT NULL"
            ).fetchall()
        return {name for (methods,) in rows for name in methods.split()}

    def dead_page(self, after: int, limit: int) -> list[tuple[int, str, str, int, str, int, bytes]]:
        """Return the Dead Requests stored after another, oldest first.

        Args:
            after: The id of the Request to start after; 0 to start at the first.
            limit: The most Requests to return.

        Returns:
            For each, its id, URL, reason, attempts, retries under each budget as
            JSON, whether it is rendered, and the Request as stored.
        """
        with self._transaction() as (connection, _):
            return read_dead_page(connection, after, limit)

    def revive(self, ids: Sequence[int]) -> int:
        """Make Dead Requests ready again, in order, with no attempt counted and no retry under any budget.

        They queue behind the Requests of their priority that are ready, and keep
        their reasons until they are settled again.

        Args:
            ids: The ids of the Requests; those of Requests that are not dead are skipped.

        Returns:
            How many were revived.
        """
        with self._transaction() as (connection, _):
            revived = connection.executemany(
                "UPDATE requests SET state = 'ready', due = NULL, sequence = ?, attempts = 0, spent = '{}' "
                "WHERE id = ? AND state = 'dead'",
                [(next(self._sequences), request_id) for request_id in ids],
            ).rowcount
        self.changed |= revived > 0
        return revived

    def renew(self, lease: Lease, ttl: float) -> float | None:
        """Extend a Lease; see `Frontier.renew()`.

        Returns:
            When the Lease now expires, or `None` if it is no longer valid.
        """
        with self._transaction() as (connection, now):
            if self._leased(connection, lease) is None:
                return None
            connection.execute("UPDATE requests SET due = ? WHERE id = ?", (now + ttl, int(lease.id)))
        return now + ttl

    def ack(self, lease: Lease) -> bool:
        """Mark a leased Request as done, keeping only its Fingerprint; see `Frontier.ack()`."""
        with self._transaction() as (connection, _):
            if self._leased(connection, lease) is None:
                return False
            connection.execute("DELETE FROM requests WHERE id = ?", (int(lease.id),))
        self.changed = True
        return True

    def nack(
        self,
        lease: Lease,
        *,
        delay: float,
        count_attempt: bool,
        reason: str,
        budget: str | None,
        replacement: RequestRow | None,
    ) -> bool:
        """Return a leased Request; see `Frontier.nack()`.

        Args:
            lease: The Lease.
            delay: Seconds before it can be claimed again.
            count_attempt: Count this as a failed attempt.
            reason: Why.
            budget: The retry budget to count this under instead of as an attempt.
            replacement: The Request to keep in place of the leased one; its
                Fingerprint and slot are not used.

        Returns:
            `False` if the Lease is no longer valid, and nothing changed.
        """
        with self._transaction() as (connection, now):
            returned = self._return(connection, now, lease, delay, count_attempt, reason, budget, replacement)
        self.changed |= returned
        return returned

    def release(self, leases: Sequence[Lease]) -> None:
        """Return Requests whose Leases never reached a Worker, without counting an attempt.

        Args:
            leases: The Leases.
        """
        with self._transaction() as (connection, now):
            for lease in leases:
                self.changed |= self._return(connection, now, lease, 0.0, False, "cancelled", None, None)

    def dead(self, lease: Lease, reason: str) -> bool:
        """Keep a leased Request as a Dead Request; see `Frontier.dead()`."""
        with self._transaction() as (connection, _):
            if self._leased(connection, lease) is None:
                return False
            connection.execute(
                "UPDATE requests SET state = 'dead', due = NULL, reason = ? WHERE id = ?", (reason, int(lease.id))
            )
        self.changed = True
        return True

    def counts(self) -> FrontierCounts:
        """Count the Requests in each state; see `Frontier.counts()`."""
        with self._transaction() as (connection, _):
            return read_counts(connection)

    def next_due(self) -> float | None:
        """Return when the next delay passes or Lease expires, if any."""
        with self._transaction() as (connection, _):
            return self._next_due(connection)

    def close(self) -> None:
        """Close the database; closing it again does nothing."""
        connection, self._connection = self._connection, None
        if connection is not None:
            connection.close()

    @contextmanager
    def _transaction(self) -> Generator[tuple[sqlite3.Connection, float]]:
        """Run one transaction, after making what fell due ready; yield the connection and the time it started.

        It is rolled back on an error.
        """
        connection = self._connection or self._open()
        connection.execute("BEGIN IMMEDIATE")
        try:
            now = self._clock()  # once the database is held, which may have taken a while
            self.changed = self._make_due_ready(connection, now)
            yield connection, now
            connection.execute("COMMIT")
        except BaseException:
            if connection.in_transaction:  # SQLite may have rolled it back already
                connection.execute("ROLLBACK")
            raise

    def _open(self) -> sqlite3.Connection:
        _create_private(self._path)
        connection = sqlite3.connect(self._path, autocommit=True)
        try:
            connection.execute("PRAGMA journal_mode = WAL")
            # Commits survive the process being killed; a power loss may take the last ones back.
            connection.execute("PRAGMA synchronous = NORMAL")
            connection.execute("BEGIN IMMEDIATE")
            self._check_schema(connection)
            connection.execute("COMMIT")
            [(sequence, token)] = connection.execute(
                "SELECT coalesce(max(sequence), 0), coalesce(max(token), 0) FROM requests"
            ).fetchall()
        except sqlite3.DatabaseError as exc:
            connection.close()
            if exc.sqlite_errorcode == sqlite3.SQLITE_NOTADB:
                raise SerializationError(f"{self._path} is not a Frontier database: {exc}") from exc
            raise
        except BaseException:
            connection.close()
            raise
        self._sequences = itertools.count(sequence + 1)
        self._tokens = itertools.count(token + 1)  # above every token a Lease in the database holds
        self._connection = connection
        return connection

    def _check_schema(self, connection: sqlite3.Connection) -> None:
        """Create the schema in a new database, or check that this Pernix can read the existing one; migrate either."""
        version = _schema_version(connection, self._path)
        if version is None:
            connection.executescript(_SCHEMA)
            connection.execute(f"PRAGMA application_id = {_APPLICATION_ID}")
            version = 1
        for later in range(version + 1, SCHEMA_VERSION + 1):
            connection.execute(_MIGRATIONS[later])
        if version < SCHEMA_VERSION:
            connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    def _make_due_ready(self, connection: sqlite3.Connection, now: float) -> bool:
        """Make passed delays ready and expired Leases claimable again, counting an attempt; return whether any were.

        The delays come first, then the Leases, each in the order they fell due, and
        returned or claimed, as in `MemoryFrontier`.
        """
        due = connection.execute(
            "SELECT id, state FROM requests WHERE due <= ? "
            "ORDER BY state = 'leased', due, CASE state WHEN 'leased' THEN token ELSE sequence END",
            (now,),
        ).fetchall()
        connection.executemany(
            "UPDATE requests SET state = 'ready', due = NULL, sequence = ?, attempts = attempts + ? WHERE id = ?",
            [(next(self._sequences), state == "leased", request_id) for request_id, state in due],
        )
        return bool(due)

    def _insert(self, connection: sqlite3.Connection, row: RequestRow) -> bool:
        """Store a Request unless its Fingerprint was seen before; return whether it was stored."""
        if row.fingerprint is not None:
            cursor = connection.execute("INSERT OR IGNORE INTO fingerprints VALUES (?)", (row.fingerprint,))
            if not cursor.rowcount:
                return False
        connection.execute(
            "INSERT INTO requests (state, rendered, priority, sequence, slot, url, request, methods) "
            "VALUES ('ready', ?, ?, ?, ?, ?, ?, ?)",
            (row.rendered, row.priority, next(self._sequences), row.slot, row.url, row.data, " ".join(row.methods)),
        )
        return True

    def _first_ready(
        self, connection: sqlite3.Connection, skip_slots: Collection[str], rendered: bool | None
    ) -> int | None:
        """The id of the first ready Request of a kind (`None` for both) outside `skip_slots`, if any."""
        if rendered is None:
            heads = connection.execute("SELECT id, slot FROM heads ORDER BY priority DESC, sequence")
        else:
            heads = connection.execute(
                "SELECT id, slot FROM heads WHERE rendered = ? ORDER BY priority DESC, sequence", (rendered,)
            )
        try:
            return next((request_id for request_id, slot in heads if slot not in skip_slots), None)
        finally:
            heads.close()

    def _lease(self, connection: sqlite3.Connection, request_id: int, expires_at: float, worker_id: str) -> Lease:
        [(slot, rendered, url, data, attempts, spent)] = connection.execute(
            "SELECT slot, rendered, url, request, attempts, spent FROM requests WHERE id = ?", (request_id,)
        ).fetchall()
        try:
            request = self._restore(data)
        except SerializationError as exc:
            raise SerializationError(f"the Request {request_id} for {url} cannot be restored: {exc}") from exc
        token = next(self._tokens)
        connection.execute(
            "UPDATE requests SET state = 'leased', token = ?, due = ?, worker = ? WHERE id = ?",
            (token, expires_at, worker_id, request_id),
        )
        return Lease(str(request_id), token, attempts + 1, expires_at, slot, request, bool(rendered), json.loads(spent))

    def _return(
        self,
        connection: sqlite3.Connection,
        now: float,
        lease: Lease,
        delay: float,
        count_attempt: bool,
        reason: str,
        budget: str | None,
        replacement: RequestRow | None,
    ) -> bool:
        """Return a leased Request, ready or delayed, in place of `replacement` if given; whether the Lease was valid."""
        leased = self._leased(connection, lease)
        if leased is None:
            return False
        attempts, spent = leased
        if budget is None:
            attempts += count_attempt
        else:
            spent[budget] = spent.get(budget, 0) + 1
        state, due = ("delayed", now + delay) if delay > 0 else ("ready", None)
        connection.execute(
            "UPDATE requests SET state = ?, due = ?, sequence = ?, attempts = ?, spent = ?, reason = ? WHERE id = ?",
            (state, due, next(self._sequences), attempts, json.dumps(spent), reason, int(lease.id)),
        )
        if replacement is not None:
            connection.execute(
                "UPDATE requests SET rendered = ?, priority = ?, url = ?, request = ?, methods = ? WHERE id = ?",
                (
                    replacement.rendered,
                    replacement.priority,
                    replacement.url,
                    replacement.data,
                    " ".join(replacement.methods),
                    int(lease.id),
                ),
            )
        return True

    def _leased(self, connection: sqlite3.Connection, lease: Lease) -> tuple[int, dict[str, int]] | None:
        """The attempts and the retries under each budget of the Request a Lease is valid for; `None` for a stale Lease."""
        row = connection.execute(
            "SELECT attempts, spent FROM requests WHERE id = ? AND state = 'leased' AND token = ?",
            (int(lease.id), lease.token),
        ).fetchone()
        return None if row is None else (row[0], json.loads(row[1]))

    def _next_due(self, connection: sqlite3.Connection) -> float | None:
        [(due,)] = connection.execute("SELECT min(due) FROM requests WHERE due IS NOT NULL").fetchall()
        return due


def open_read_only(path: str) -> sqlite3.Connection | None:
    """Open a Frontier's database to read it as it is, changing nothing in it or beside it, also while it is written.

    Next to its write-ahead log, as while a `FrontierStore` has it open, or once one
    was killed, the database is read with the log, through SQLite's shared-memory
    index, which is not written to (`readonly_shm`). Without a log, it holds every
    change, and is read as a file nothing changes (`immutable`), so that neither the
    log nor the index is created. Such a connection takes no lock, and does not see
    a store that opens the database after it was opened, which writes its log alone
    until it checkpoints into the database; so it is for one read, as of when it was
    opened, the next read opening the database anew. A store that closes as the
    database is opened takes its log and index away; the database is then read
    without them, though SQLite may leave an empty log behind.

    No schema is created or migrated: one of any version up to `SCHEMA_VERSION` is
    read as it is.

    Args:
        path: The database file.

    Returns:
        The connection, each query read on its own; `None` for a database no
        `FrontierStore` made its schema in yet, which holds no Requests.

    Raises:
        SerializationError: The file is not a Frontier's database, or one of a
            schema newer than this Pernix reads.
    """
    database = Path(path).resolve()
    if os.path.exists(f"{database}-wal"):
        try:
            return _read_only(database, "readonly_shm=1", path)
        except sqlite3.OperationalError as exc:
            if exc.sqlite_errorcode != sqlite3.SQLITE_CANTOPEN or os.path.exists(f"{database}-shm"):
                raise
    return _read_only(database, "immutable=1", path)


def read_counts(connection: sqlite3.Connection) -> FrontierCounts:
    """Count the Requests in each state, as the triggers keep them in `counts`."""
    counts: dict[str, int] = dict(connection.execute("SELECT state, requests FROM counts").fetchall())
    return FrontierCounts(counts["ready"], counts["delayed"], counts["leased"], counts["done"], counts["dead"])


def read_dead_page(
    connection: sqlite3.Connection, after: int, limit: int
) -> list[tuple[int, str, str, int, str, int, bytes]]:
    """Read the Dead Requests stored after another, oldest first; see `FrontierStore.dead_page()`."""
    return connection.execute(
        "SELECT id, url, reason, attempts, spent, rendered, request FROM requests "
        "WHERE state = 'dead' AND id > ? ORDER BY id LIMIT ?",
        (after, limit),
    ).fetchall()


def _read_only(database: Path, mode: str, path: str) -> sqlite3.Connection | None:
    connection = sqlite3.connect(f"{database.as_uri()}?mode=ro&{mode}", uri=True, autocommit=True)
    try:
        version = _schema_version(connection, path)
    except sqlite3.DatabaseError as exc:
        connection.close()
        if exc.sqlite_errorcode == sqlite3.SQLITE_NOTADB:
            raise SerializationError(f"{path} is not a Frontier database: {exc}") from exc
        raise
    except BaseException:
        connection.close()
        raise
    if version is None:
        connection.close()
        return None
    return connection


def _schema_version(connection: sqlite3.Connection, path: str) -> int | None:
    """The schema version of a Frontier's database; `None` for an empty one, in which no Frontier made its schema.

    Raises:
        SerializationError: The database is not a Frontier's, or its schema is newer
            than this Pernix reads.
    """
    [(application_id,)] = connection.execute("PRAGMA application_id").fetchall()
    [(version,)] = connection.execute("PRAGMA user_version").fetchall()
    [(objects,)] = connection.execute("SELECT count(*) FROM sqlite_schema").fetchall()
    if (application_id, version, objects) == (0, 0, 0):
        return None
    if application_id != _APPLICATION_ID:
        raise SerializationError(f"{path} is not a Frontier database")
    if version > SCHEMA_VERSION:
        raise SerializationError(
            f"the Frontier database {path} has schema version {version}, but this Pernix reads "
            f"version {SCHEMA_VERSION} and older; upgrade Pernix to resume this Run"
        )
    return version


def _create_private(path: str) -> None:
    """Create the database file readable by its owner only, unless it exists; SQLite gives its journal the same mode."""
    with suppress(FileExistsError):
        os.close(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
