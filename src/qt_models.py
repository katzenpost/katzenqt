from PySide6 import QtCore
from PySide6.QtWidgets import QStyledItemDelegate, QLabel, QStyleOptionViewItem, QMainWindow
from PySide6.QtCore import QFile, QSize, QModelIndex, QPersistentModelIndex, QModelRoleDataSpan, QModelRoleData, Slot, QObject, QByteArray
from PySide6.QtGui import QPainter

import persistent

from functools import lru_cache
# should probably look into paginating some SQL here so we don't do one query per line
# but err lru_cache makes it bearable for now, with the assumption chat messages don't change.
# TODO, but seems reasonable as long as we are not doing "sent yesterday" etc.

class FilterProxyModel(QtCore.QSortFilterProxyModel):
    # recursiveFilteringEnabled show parents when child matches
    def __index__(self):
        super(FilterProxyModel, self).__index__(self)
    def __init__(self, window: "QMainWindow"):
        self.window = window
        super().__init__()
    def filterAcceptsRow(self, p_int, qmi:QModelIndex | QPersistentModelIndex):
        if qmi.isValid():
            return False
        res = super(FilterProxyModel, self).filterAcceptsRow(p_int, qmi )
        idx = self.sourceModel().index(p_int,0, qmi)

        if self.sourceModel().hasChildren(idx):
            num_items = self.sourceModel().rowCount(idx)
            for i in range(num_items):
                res = res or self.filterAcceptsRow(i, idx)
        if idx.data():
            if self.window.ui.contactFilterLineEdit.text() in idx.data():
                return True
            else:
                return False
        return res

ROLE_CHAT_AUTHOR = 0x100  # see ConversationLogModel.roleNames()

class ConversationLogModel(QtCore.QAbstractItemModel):
    # https://doc.qt.io/qt-6/model-view-programming.html
    # https://doc.qt.io/qt-6/qt.html#ItemDataRole-enum

    # https://doc.qt.io/qtforpython-6/PySide6/QtCore/QAbstractItemModel.html
    # https://doc.qt.io/qtforpython-6/PySide6/QtGui/QStandardItemModel.html

    # https://doc.qt.io/qtforpython-6/PySide6/QtCore/QModelIndex.html
    # QModelIndex index = model->index(row, column, parent);

    def __init__(self, convo_id) -> None:
        super().__init__()
        self.convo_id = convo_id
        print("conv log model init", self.roleNames())

    def roleNames(self):
        """These map names used in QML to ints used in QAbstractItemModel
        Inside a DelegateItem you can access model.author.
        See e.g. https://doc.qt.io/archives/qt-6.4/qt.html#ItemDataRole-enum
        """
        return {
            0: QByteArray(b'display'),
            #4: QByteArray(b'statusTip'),
            #1: QByteArray(b'decoration'),
            #2: QByteArray(b'edit'),
            #5: QByteArray(b'whatsThis'),
            #3: QByteArray(b'toolTip'),
            ROLE_CHAT_AUTHOR: QByteArray(b'author'),
        }

    @lru_cache(maxsize=1000)
    def index(self, row:int, column:int, parent:QModelIndex | None) -> QModelIndex:
        if parent and parent.isValid():
            return QModelIndex()
        qmi = self.createIndex(row,column, id=row*(column+1))
        return qmi

    @lru_cache(maxsize=1000)
    def parent(self, child:QModelIndex|QPersistentModelIndex) -> QModelIndex:
        """Since we don't have any trees here, nochild indices have parents"""
        return QModelIndex()
    def rowCount(self, parent:QModelIndex|None) -> int:
        """number of chat messages.
        we set this initially when loading in add_conversation(),
        and then each time we receive a message
        or write a message ourselves.
        """
        if not parent or parent.row() == -1:
            return self.row_count
        return 0
    def increment_row_count(self):
        #qmi = self.createIndex(-1,-1)
        qmi = QModelIndex()
        print("beginInsertRows", self.row_count)
        self.beginInsertRows(qmi, self.row_count-1, self.row_count-1)
        #self.beginInsertRows(qmi, self.row_count-2, self.row_count-1)
        self.row_count += 1
        self.endInsertRows()

    def columnCount(self, parent:QModelIndex|QPersistentModelIndex|None) -> int:
        if parent.isValid():
            return 0
        return 1

    def multiData(self, idx : QModelIndex|QPersistentModelIndex, span: QModelRoleData|QModelRoleDataSpan) -> None:
        """basically this lets us do like data() but for all the roles.
        since we only set the text role (0 == DisplayRole), we just ignore
        all the others.
        important to note that this is a span of data roles, not a span of data indices.
        TODO: doesn't seem to be used by QML
        """
        #https://doc.qt.io/qtforpython-6/PySide6/QtCore/QModelRoleDataSpan.html
        if isinstance(span, QModelRoleData):
            # TODO unsure if this can happen, not tested
            if span.role() == 0:
                span.setData(self.data(idx, 0))
        elif isinstance(span, QModelRoleDataSpan):
            for roleData in span:  # type: ignore[attr-defined]
                if roleData.role() == 0:
                    roleData.setData(self.data(idx, 0))
                elif roleData.role() == 0x0100:
                    roleData.setData("TODO call data(idx, 0x100)")
                else:
                    print("roleData.role()", roleData, roleData.role())
        else:
            raise Exception("TODO2")

    @lru_cache(maxsize=1000)
    def data(self, index:QModelIndex, role:QtCore.Qt.ItemDataRole|None):
        """returns data for index
        PySide6.QtCore.Qt.DisplayRole
        """
        if role not in (0, ROLE_CHAT_AUTHOR):
            return None
        index_row : int = index.row()
        #print("DATA: INDEX ROW IS", index_row, repr(index))
        # TODO we definitely want to paginate this stuff for performance reasons,
        # and when we do we want order by:
        # sa_relationship_kwargs={"order_by": "conversation_order", "lazy": "dynamic"},

        with persistent.Session(persistent._engine_sync) as sess:
                cl = sess.query(persistent.ConversationLog).filter(
                    persistent.ConversationLog.conversation_id == self.convo_id).filter(
                        persistent.ConversationLog.conversation_order==index_row).first()
                # TODO we probably want to do this as multiple columns? whatever, works for now
                if role == ROLE_CHAT_AUTHOR:
                    return cl.conversation_peer.name
                elif role == 0:
                    return cl.payload.decode()
        #print(self,"data", index, repr(QtCore.Qt.ItemDataRole(role)))
        #return f"hi {self.convo_id}"
    def headerData(self, section:int, orientation:QtCore.Qt.Orientation, role:QtCore.Qt.ItemDataRole|None):
        """data for given role and section in the header"""
        print(self,"headerData",section,orientation,repr(QtCore.Qt.ItemDataRole(role)))
        if role == QtCore.Qt.ItemDataRole.DisplayRole:
            return "header1"
        return None
        return QLabel("a")
        pass
    def flags(self, index:QModelIndex):
        # https://doc.qt.io/qtforpython-6/PySide6/QtCore/Qt.html#PySide6.QtCore.Qt.ItemFlag
        return QtCore.Qt.NoItemFlags

