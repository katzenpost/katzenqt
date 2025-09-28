from PySide6 import QtCore
from PySide6.QtWidgets import QStyledItemDelegate, QLabel, QStyleOptionViewItem, QMainWindow
from PySide6.QtCore import QFile, QSize, QModelIndex, QPersistentModelIndex, QModelRoleDataSpan, QModelRoleData, Slot, QObject, QByteArray
from PySide6.QtGui import QPainter

import persistent

import functools
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
ROLE_CHAT_NETWORK_STATUS = 0x101  # ConversationLog.network_status

def lru_cache_for_data_roles(maxsize=10000):
    """decorator for QtCore.QAbstractItemModel.data() that exempts certain roles (network status for unsent)"""
    def decorator(func):
        cached_func = lru_cache(maxsize=maxsize)(func)
        indices_with_stable_network_status = dict()
        @functools.wraps(func)
        def wrapper(clm:"ConversationLogModel", index:QModelIndex, role:QtCore.Qt.ItemDataRole|None):
            if role != ROLE_CHAT_NETWORK_STATUS:
                return cached_func(clm, index, role) ## call item.data() and cache it
            elif (status := indices_with_stable_network_status.get(index, None)) is not None:
                return status
            else:
                ret = func(clm, index, role)
                if ret != 1:  # received or sent, but not "pending"
                    indices_with_stable_network_status[index] = ret
                return ret
        return wrapper
    return decorator

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
            ROLE_CHAT_NETWORK_STATUS: QByteArray(b'network_status'),
        }

    @lru_cache(maxsize=10000)
    def index(self, row:int, column:int, parent:QModelIndex | None) -> QModelIndex:
        if parent and parent.isValid():
            return QModelIndex()
        qmi = self.createIndex(row,column, id=row*(column+1))
        return qmi

    @lru_cache(maxsize=10000)
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
        qmi = QModelIndex()
        self.beginInsertRows(qmi, self.row_count-1, self.row_count-1)
        self.row_count += 1
        self.endInsertRows()

    def redraw_network_status(self):
        """Force the view to refresh without actually changing anything."""
        qmi = QModelIndex()
        self.beginInsertRows(qmi, 1,0)
        self.endInsertRows()

    def columnCount(self, parent:QModelIndex|QPersistentModelIndex|None) -> int:
        if parent.isValid():
            return 0
        return 1

    @lru_cache_for_data_roles()
    def data(self, index:QModelIndex, role:QtCore.Qt.ItemDataRole|None):
        """returns data for index
        PySide6.QtCore.Qt.DisplayRole
        """
        if role not in (0, ROLE_CHAT_AUTHOR, ROLE_CHAT_NETWORK_STATUS):
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
                    if cl.network_status == 1:
                        return cl.conversation_peer.name
                    return cl.conversation_peer.name
                elif role == ROLE_CHAT_NETWORK_STATUS:
                    return cl.network_status
                elif role == 0:
                    if cl.payload.startswith(b'F'):                        
                        try:
                            from models import GroupChatMessage
                            cm = GroupChatMessage.from_cbor(cl.payload[1:])
                            return cm.text
                        except Exception as e:
                            print(e, cl.payload)
                            return cl.payload.decode()
                    else:
                        return cl.payload.decode()
                # TODO here we want to have a ROLE_CHAT_ACKED to show which of our things have been sent
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

