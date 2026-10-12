"""Transactional task receipts and a single FIFO head per Herdr session."""
import json
import os
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path


class Conflict(ValueError):
    pass


def new_id(prefix):
    return f"{prefix}_{uuid.uuid4().hex}"


class Store:
    def __init__(self, directory):
        self.directory = Path(directory).expanduser().resolve()
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        database = self.directory / "state.sqlite3"
        self.db = sqlite3.connect(database, timeout=10)
        os.chmod(database, 0o600)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        if version not in (0, 1, 2, 3, 4):
            raise Conflict(f"unsupported database version: {version}")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS sessions (
                socket TEXT PRIMARY KEY,
                coordinator TEXT NOT NULL,
                identity TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1,
                activated INTEGER NOT NULL DEFAULT 0,
                wake_key TEXT,
                wake_at REAL,
                error TEXT,
                pid INTEGER,
                tick_at REAL
            );
            CREATE TABLE IF NOT EXISTS dispatches (
                id TEXT PRIMARY KEY,
                socket TEXT NOT NULL REFERENCES sessions(socket),
                task_id TEXT NOT NULL,
                target TEXT NOT NULL,
                identity TEXT NOT NULL,
                spec TEXT NOT NULL,
                delivery TEXT NOT NULL DEFAULT 'uncertain',
                accepted_at REAL,
                completed_at REAL,
                abandoned_at REAL,
                abandon_reason TEXT,
                ui_status TEXT,
                parent_seq INTEGER REFERENCES events(seq),
                created_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS events (
                seq INTEGER PRIMARY KEY AUTOINCREMENT,
                socket TEXT NOT NULL REFERENCES sessions(socket),
                message_id TEXT NOT NULL,
                dispatch_id TEXT NOT NULL REFERENCES dispatches(id),
                kind TEXT NOT NULL,
                payload TEXT NOT NULL,
                state TEXT NOT NULL DEFAULT 'pending',
                summary TEXT,
                created_at REAL NOT NULL,
                done_at REAL,
                UNIQUE(socket, message_id)
            );
            CREATE INDEX IF NOT EXISTS events_pending ON events(socket, state, seq);
        """)
        with self.transaction():
            if self.db.execute("PRAGMA user_version").fetchone()[0] < 2:
                for column in ("presented_at REAL", "user_reply TEXT", "user_reply_file TEXT",
                               "user_acked_at REAL"):
                    self.db.execute(f"ALTER TABLE events ADD COLUMN {column}")
                self.db.execute("PRAGMA user_version=2")
            if self.db.execute("PRAGMA user_version").fetchone()[0] < 3:
                self.db.execute("ALTER TABLE events ADD COLUMN user_ack_source TEXT")
                self.db.execute("ALTER TABLE events ADD COLUMN user_ack_context TEXT")
                self.db.execute("""UPDATE events SET user_ack_source='message'
                    WHERE user_acked_at IS NOT NULL AND user_reply IS NOT NULL
                    AND length(user_reply)>0""")
                self.db.execute("PRAGMA user_version=3")
            if self.db.execute("PRAGMA user_version").fetchone()[0] < 4:
                self.db.execute("ALTER TABLE dispatches ADD COLUMN archived_at REAL")
                self.db.execute("PRAGMA user_version=4")

    def close(self):
        self.db.close()

    @contextmanager
    def transaction(self):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise

    def one(self, sql, args=()):
        row = self.db.execute(sql, args).fetchone()
        return dict(row) if row is not None else None

    def rows(self, sql, args=()):
        return [dict(row) for row in self.db.execute(sql, args)]

    def session(self, socket):
        session = self.one("SELECT rowid AS id,* FROM sessions WHERE socket=?", (socket,))
        if not session:
            raise Conflict("this Herdr session has no coordinator; invoke the plugin start action")
        return session

    def bind(self, socket, target, identity):
        with self.transaction():
            current = self.one("SELECT * FROM sessions WHERE socket=?", (socket,))
            if current and current["identity"] != identity:
                raise Conflict("coordinator identity changed; pause first, then explicitly rebind")
            self.db.execute("""INSERT INTO sessions(socket,coordinator,identity) VALUES(?,?,?)
                ON CONFLICT(socket) DO UPDATE SET coordinator=excluded.coordinator,
                enabled=1,error=NULL""", (socket, target, identity))
        return self.session(socket)

    def rebind(self, socket, target, identity):
        with self.transaction():
            current = self.session(socket)
            if current["enabled"]:
                raise Conflict("pause coordination before rebinding")
            self.db.execute("""UPDATE sessions SET coordinator=?,identity=?,enabled=1,
                activated=0,wake_key=NULL,wake_at=NULL,error=NULL WHERE socket=?""",
                (target, identity, socket))
        return self.session(socket)

    def update_session(self, socket, **values):
        columns = {"enabled", "activated", "wake_key", "wake_at", "error", "pid", "tick_at"}
        if not values or not set(values) <= columns:
            raise ValueError("invalid session update")
        with self.transaction():
            self.db.execute("UPDATE sessions SET " + ",".join(f"{key}=?" for key in values)
                            + " WHERE socket=?", (*values.values(), socket))

    def dispatch(self, dispatch_id):
        dispatch = self.one("SELECT * FROM dispatches WHERE id=?", (dispatch_id,))
        if not dispatch:
            raise Conflict(f"unknown dispatch: {dispatch_id}")
        return dispatch

    def head(self, socket):
        return self.one("""SELECT events.* FROM events JOIN dispatches ON dispatches.id=events.dispatch_id
            WHERE events.socket=? AND events.state!='done' AND dispatches.archived_at IS NULL
            ORDER BY events.seq LIMIT 1""", (socket,))

    def _require_head(self, socket, seq):
        event = self.head(socket)
        if not event or event["seq"] != seq:
            raise Conflict("only the current FIFO head may be processed or acknowledged")
        return event

    def create_dispatch(self, socket, task_id, target, identity, spec, parent_seq=None):
        dispatch_id = new_id("dsp")
        with self.transaction():
            session = self.session(socket)
            if not session["enabled"]:
                raise Conflict("coordination is paused")
            if parent_seq is not None:
                event = self._require_head(socket, parent_seq)
                if event["user_acked_at"] is not None:
                    raise Conflict("user acknowledgment seals this head against new follow-up dispatches")
            live = self.one("""SELECT id FROM dispatches WHERE socket=? AND identity=?
                AND completed_at IS NULL AND abandoned_at IS NULL AND archived_at IS NULL LIMIT 1""", (socket, identity))
            if live:
                raise Conflict(f"worker still has unsettled dispatch {live['id']}; inspect it first")
            self.db.execute("""INSERT INTO dispatches
                (id,socket,task_id,target,identity,spec,parent_seq,created_at)
                VALUES(?,?,?,?,?,?,?,?)""",
                (dispatch_id, socket, task_id, target, identity, json.dumps(spec, ensure_ascii=False),
                 parent_seq, time.time()))
        return self.dispatch(dispatch_id)

    def delivered(self, dispatch_id):
        with self.transaction():
            self.db.execute("UPDATE dispatches SET delivery='submitted' WHERE id=?", (dispatch_id,))

    def accept(self, dispatch_id):
        with self.transaction():
            dispatch = self.dispatch(dispatch_id)
            if dispatch["completed_at"] or dispatch["abandoned_at"]:
                raise Conflict("dispatch already completed or abandoned")
            self.db.execute("UPDATE dispatches SET accepted_at=COALESCE(accepted_at,?) WHERE id=?",
                            (time.time(), dispatch_id))
            self._complete_acknowledged_head(dispatch["socket"])
        return self.dispatch(dispatch_id)

    def report(self, dispatch_id, message_id, kind, payload):
        if not message_id.strip():
            raise ValueError("message ID must be non-empty")
        with self.transaction():
            dispatch = self.dispatch(dispatch_id)
            existing = self.one("SELECT * FROM events WHERE socket=? AND message_id=?",
                                (dispatch["socket"], message_id))
            encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True)
            if existing:
                if (existing["dispatch_id"], existing["kind"], existing["payload"]) != (
                        dispatch_id, kind, encoded):
                    raise Conflict("message ID was already used for a different report")
                return existing
            if kind in ("completed", "blocked") and not dispatch["accepted_at"]:
                raise Conflict("worker must explicitly accept before reporting")
            if dispatch["completed_at"] or dispatch["abandoned_at"]:
                raise Conflict("dispatch already completed or abandoned; inspect rather than replay")
            self.db.execute("""INSERT INTO events(socket,message_id,dispatch_id,kind,payload,created_at)
                VALUES(?,?,?,?,?,?)""",
                (dispatch["socket"], message_id, dispatch_id, kind, encoded, time.time()))
            if kind == "completed":
                self.db.execute("UPDATE dispatches SET completed_at=? WHERE id=?",
                                (time.time(), dispatch_id))
            return self.one("SELECT * FROM events WHERE seq=last_insert_rowid()")

    def next(self, socket):
        with self.transaction():
            event = self.head(socket)
            if event and event["state"] == "pending":
                self.db.execute("UPDATE events SET state='handling' WHERE seq=?", (event["seq"],))
                event["state"] = "handling"
            return event

    def present(self, socket, seq, summary):
        if not summary.strip():
            raise Conflict("persist a meaningful summary when presenting the result")
        with self.transaction():
            event = self._require_head(socket, seq)
            if event["state"] == "awaiting_user":
                if event["summary"] != summary:
                    raise Conflict("the result was already presented with a different summary")
                return event
            if event["state"] != "handling":
                raise Conflict("read the FIFO head with next before presenting it")
            self.db.execute("UPDATE events SET state='awaiting_user',summary=?,presented_at=? WHERE seq=?",
                            (summary, time.time(), seq))
            self.db.execute("UPDATE sessions SET wake_key=NULL,wake_at=NULL WHERE socket=?", (socket,))
        return self.one("SELECT * FROM events WHERE seq=?", (seq,))

    def _complete_acknowledged_head(self, socket):
        event = self.head(socket)
        if not event or event["state"] != "awaiting_user" or event["user_acked_at"] is None:
            return
        waiting = self.one("""SELECT id FROM dispatches WHERE socket=? AND parent_seq=?
            AND abandoned_at IS NULL AND archived_at IS NULL AND accepted_at IS NULL LIMIT 1""", (socket, event["seq"]))
        if waiting:
            return
        self.db.execute("UPDATE events SET state='done',done_at=? WHERE seq=?",
                        (time.time(), event["seq"]))
        self.db.execute("UPDATE sessions SET wake_key=NULL,wake_at=NULL WHERE socket=?", (socket,))

    def ack(self, socket, seq, reply, reply_file=None):
        if not reply.strip():
            raise Conflict("user reply must be non-empty; record the actual user's confirmation")
        return self._ack(socket, seq, reply, reply_file, "message", None)

    def ack_action(self, socket, seq, context):
        return self._ack(socket, seq, None, None, "plugin_action",
                         json.dumps(context, ensure_ascii=False, sort_keys=True))

    def _ack(self, socket, seq, reply, reply_file, source, context):
        with self.transaction():
            event = self.one("SELECT * FROM events WHERE socket=? AND seq=?", (socket, seq))
            if event and event["user_acked_at"] is not None:
                if source == "message" and (event["user_ack_source"] != source
                                             or event["user_reply"] != reply):
                    raise Conflict("this event was already acknowledged with a different user reply")
                return event
            event = self._require_head(socket, seq)
            if event["state"] != "awaiting_user" or event["presented_at"] is None:
                raise Conflict("present the result before recording user acknowledgment")
            self.db.execute("""UPDATE events SET user_reply=?,user_reply_file=?,user_acked_at=?,
                user_ack_source=?,user_ack_context=? WHERE seq=?""",
                (reply, reply_file, time.time(), source, context, seq))
            self._complete_acknowledged_head(socket)
        return self.one("SELECT * FROM events WHERE seq=?", (seq,))

    def acknowledge_activation(self, socket):
        self.update_session(socket, activated=1, wake_key=None, wake_at=None)

    def abandon(self, dispatch_id, reason):
        if not reason.strip():
            raise Conflict("record the inspection or cancellation reason")
        with self.transaction():
            dispatch = self.dispatch(dispatch_id)
            self.db.execute("UPDATE dispatches SET abandoned_at=?,abandon_reason=? WHERE id=?",
                            (time.time(), reason, dispatch_id))
            self._complete_acknowledged_head(dispatch["socket"])
        return self.dispatch(dispatch_id)

    def archive(self, socket, dispatch_id):
        with self.transaction():
            dispatch = self.dispatch(dispatch_id)
            if dispatch["socket"] != socket:
                raise Conflict("dispatch belongs to a different Herdr session")
            if dispatch["archived_at"] is not None:
                return dispatch
            before = self.head(socket)
            self.db.execute("UPDATE dispatches SET archived_at=? WHERE id=?", (time.time(), dispatch_id))
            self._complete_acknowledged_head(socket)
            after = self.head(socket)
            if (before and before["seq"]) != (after and after["seq"]):
                self.db.execute("UPDATE sessions SET wake_key=NULL,wake_at=NULL WHERE socket=?", (socket,))
        return self.dispatch(dispatch_id)

    def status(self, socket):
        return {"session": self.session(socket), "head": self.head(socket),
                "dispatches": self.rows("SELECT * FROM dispatches WHERE socket=? ORDER BY created_at",
                                        (socket,)),
                "pending": self.rows("""SELECT events.* FROM events JOIN dispatches ON dispatches.id=events.dispatch_id
                    WHERE events.socket=? AND events.state!='done' AND dispatches.archived_at IS NULL
                    ORDER BY events.seq""", (socket,))}
