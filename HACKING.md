## Installation dependencies

```
git clone https://github.com/katzenpost/katzenqt
cd katzenqt
python3 -m venv myvenv
source myvenv/bin/activate
pip install --upgrade pip
pip install 'git+https://github.com/katzenpost/thin_client@487d2b8e46440abba5bb4db95a168353de2d45d9'
pip install -e .
```

Most debian systems will already have this package, but it is required:
```shell
apt install libxcb-cursor0 # version (0.1.4-1)
```

At the moment, this program only works with the `tb/debug2025-09-21` branch of https://github.com/katzenpost/katzenpost .

# Running the application

First navigate to the `katzenqt` folder and initialize the `venv`:
```shell
cd katzenqt && source myvenv/bin/activate

# Then once there:
make
```

- TODO: despite `pyside6-rcc` we don't actually load the icons from the qrc, yet

# Where is my data stored?

The persistent data (keys, messages, everything) is stored in a SQLite3 file in `~/.local/share/katzenqt/katzen.sqlite3`.

The environment variable `KQT_STATE` can be used to override the default state file name, e.g. `KQT_STATE=alsokatzen  make` will use `~/.local/share/katzenqt/alsokatzen.sqlite3`. This is useful if you want to talk to yourself, for instance when testing.

# Design notes

This application is a GUI chat client for mixnet group chat.

## Concepts / primer
- [Qt for Python / PySide6 bindings for the Qt GUI framework](https://doc.qt.io/qtforpython-6/)
- [KP thin client guide](https://katzenpost.network/docs/client_integration/)
- [KP thin client design doc](https://katzenpost.network/docs/specs/thin_client.html)
- [KP group chat spec](https://katzenpost.network/docs/specs/group_chat.html)
- [KP pigeonhole spec](https://katzenpost.network/docs/specs/pigeonhole/)

## TODO look into
- [CDDL schema language for CBOR](https://datatracker.ietf.org/doc/rfc8610/)
- https://doc.qt.io/qt-6/qtquickcontrols-chattutorial-example.html
- unicode support https://bugreports.qt.io/browse/QTBUG-8
  - maybe just catch this and show our own dialog
- https://doc.qt.io/archives/qt-6.3/qlistview.html#layoutMode-prop
  - for QListView, seems more efficient with the Batched mode and batchSize= for messages
    - would need a QAbstractListModel
- https://doc.qt.io/qt-6/qiodevice.html for audio capture
- https://github.com/alexandrvicente/talkie/blob/master/src/main.cpp
- dark mode
  - bg: #131212
  - fg: #E0E0E0 / #FAFAFA
- SQL encryption
  - https://www.sqliteforum.com/p/securing-your-sqlite-database-best
  - sqlcipher3 pypi
    - https://stackoverflow.com/questions/30314882/using-pysqlcipher-with-sqlalchemy
      - create_engine( ... , module=sqlcipher3.dbapi2)
      - https://sqlite.org/com/see.html
- https://docs.rs/codec2/latest/codec2/ for encoding/decoding PTT voice chat

## Design of this application
- `persistent.py`: Persistent state management (persists to disk)
  - [SQLModel](https://sqlmodel.tiangolo.com/) (Pydantic validation + SQLAlchemy ORM writing to SQLite3 using `aiosqlite`)
  - Keep track of `Conversation`s, BACAP caps, BACAP indices, etc
  - Log of received messages
  - write-ahead log (WAL) of to-be-sent messages
- `models.py`: pydantic models for in-memory data structures
- `qt_models.py`: Qt models for in-memory data structures
- `katzen.py`:  Qt GUI - display the UI
  - receive user inputs (adding contacts, sending messages)
  - visualize `ConversationLog`, currently this is in a QTreeView but it should probably either be a QListView or QColumnView (for nested convos/threads)
    - https://doc.qt.io/qtforpython-6/PySide6/QtWidgets/QStyledItemDelegate.html#subclassing-qstyleditemdelegate seems like the way to go
- `network.py`: interface with the KP thin client
  - read WAL from `persistent` and transmit messages
  - read messages from network and log them

## HACKING

### Database migrations

Persistence is currently implemented with `SQLModel` combining `Pydantic` (model definition / validation, and `SQLAlchemy` as ORM, with `Alembic` handling automatic migrations when the ORM models change).

See https://alembic.sqlalchemy.org/en/latest/tutorial.html#using-pep-621

When updating data structures in `persistent.py`, you will need to deal with migration of existing data. This is important to ensure we can read old state files (`katzen.sqlite3`).
The procedure is:
1. `alembic check`:
   - No further action required if it says:
     > No new upgrade operations detected.
2. Otherwise you'll need: `alembic revision --autogenerate -m 'Added a new field to Foo'`
   This will create a file in `migrations/versions`.
   They frequently need manual editing because we're using sqlite3 which isn't good at ALTER'ing constraints.
3. `persistent.py:init_and_migrate()` any outstanding the migrations to an existing database file. This function gets called by `demo.py` on startup.
  - This can also be done by hand: `alembic upgrade head`
4. If everything works, you need to `git add` the file in  `migrations/versions/1234_added_a_....py` to ensure everybody else gets it.

### UI editing
- Our UI is *mainly* defined in `mixchat.ui`, which can be edited with `pyside6-designer` (a WYSIWYG program for Qt).
When changes have been made, the UI code needs to be regenerated with `pyside6-uic mixchat.ui -o ui_mixchat.py` - `make` does that for you.
- The UI for the chat messages (the "conversation log") resides in `resources/chatview.qml`. This file is in [QML format](https://en.wikipedia.org/wiki/QML).
