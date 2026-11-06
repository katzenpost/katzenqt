# Hacking on katzenqt

[Back to the README](../README.md)

katzenqt is a GUI chat client for mixnet group chat, written with PySide6. Set up the
environment with [Building](building.md) first.

## Background

- [Qt for Python (PySide6)](https://doc.qt.io/qtforpython-6/)
- [Thin client how-to](https://katzenpost.network/docs/thin_client_howto/) and
  [Python thin client API](https://katzenpost.network/docs/python_thin_client_api/)
- [Thin client spec](https://katzenpost.network/docs/specs/thinclient/)
- [Group chat design](https://katzenpost.network/docs/specs/groupchat/)
- [Pigeonhole spec](https://katzenpost.network/docs/specs/pigeonhole/)

## Code layout

- `persistent.py`: state persisted to disk, with
  [SQLModel](https://sqlmodel.tiangolo.com/) (Pydantic validation and the SQLAlchemy
  ORM, writing SQLite through `aiosqlite`). It keeps track of each `Conversation`, the
  BACAP capabilities and indices, the log of received messages, and the write-ahead log
  of messages still to be sent.
- `models.py`: Pydantic models for in-memory data structures.
- `qt_models.py`: Qt models for in-memory data structures.
- `katzen.py`: the Qt GUI. It takes user input (adding contacts, sending messages) and
  shows each conversation log.
- `network.py`: the interface to the Katzenpost thin client. It reads the write-ahead log
  from `persistent` and transmits messages, and logs the messages it receives.
- `conversation_handlers.py`: routes an assembled `GroupChatMessage` by its `msg_type`:
  chat into the conversation log, tally into the tally controller.
- `tally/`: the tally (poll and vote) protocol, a pycrdt `Doc` per survey replicated over
  the group chat. Its core is free of Qt and the network; see the
  [Tally API](tally-api.md) and the [Tally how-to](tally-howto.md).
- `headless/`: the command-line client; see [Headless CLI](headless.md).

## A local thin client

To run katzenqt against a local checkout of
[thin_client](https://github.com/katzenpost/thin_client):

```shell
uv run -v --with-editable ~/thin_client katzenqt
```

## Database migrations

Persistence uses SQLModel, which combines Pydantic (model definition and validation) with
SQLAlchemy as the ORM; [Alembic](https://alembic.sqlalchemy.org/en/latest/tutorial.html)
migrates the database when the ORM models change. When you change the data structures in
`persistent.py`, existing state files (`katzen.sqlite3`) must still load:

1. Run `make alembic-check-uv`. If it reports "No new upgrade operations detected", you
   are done.
2. Otherwise run `make alembic-revision-uv ALEMBIC_MSG='Added a new field to Foo'`. This
   writes a file in `src/katzenqt/migrations/versions/`. It often needs editing by hand,
   because SQLite is poor at altering constraints.
3. `persistent.init_and_migrate()` applies outstanding migrations to an existing database
   at startup. To apply them by hand, run
   `.venv/bin/alembic -c src/katzenqt/data/alembic.ini upgrade head`.
4. When it works, `git add` the new file in `src/katzenqt/migrations/versions/`.

The pip backend has `make alembic-check-pip` and `make alembic-revision-pip`.

## UI editing

- Most of the UI is defined in `ui/mixchat.ui`, which can be edited with
  `pyside6-designer`. `make code-generator` regenerates `src/katzenqt/ui_mixchat.py`
  from it, and `make run` does so when it changes.
- The conversation log is drawn by `resources/chatview.qml`, in
  [QML](https://en.wikipedia.org/wiki/QML), inside a `QQuickWidget`.
- Although `pyside6-rcc` compiles `resources/resources.qrc`, the icons are not yet loaded
  from it.

## Ideas to look into

- [CDDL, a schema language for CBOR](https://datatracker.ietf.org/doc/rfc8610/)
- [The Qt Quick Controls chat tutorial](https://doc.qt.io/qt-6/qtquickcontrols-chattutorial-example.html)
- Unicode support, [QTBUG-8](https://bugreports.qt.io/browse/QTBUG-8); perhaps catch it
  and show our own dialog
- [QIODevice](https://doc.qt.io/qt-6/qiodevice.html) for audio capture, and
  [talkie](https://github.com/alexandrvicente/talkie/blob/master/src/main.cpp)
- [codec2](https://docs.rs/codec2/latest/codec2/) to encode push-to-talk voice
- Encrypting the SQLite database:
  - [Securing your SQLite database](https://www.sqliteforum.com/p/securing-your-sqlite-database-best)
  - `sqlcipher3` on PyPI, through `create_engine(..., module=sqlcipher3.dbapi2)`; see
    [using pysqlcipher with SQLAlchemy](https://stackoverflow.com/questions/30314882/using-pysqlcipher-with-sqlalchemy)
  - [SQLite Encryption Extension](https://sqlite.org/com/see.html)
