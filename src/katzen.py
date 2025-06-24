APP_ORGANIZATION = "Mixnetwork"
APP_NAME = "KatzenQt"

import PySide6.QtAsyncio as QtAsyncio
# https://doc.qt.io/qtforpython-6/PySide6/QtAsyncio/index.html
import persistent
import sys
import fcntl
from pathlib import Path
import math
from asyncio import ensure_future
from PySide6.QtWidgets import QApplication, QMainWindow, QSystemTrayIcon, QStyle, QTreeWidgetItem, QMenu, QDialog, QDialogButtonBox, QInputDialog, QLabel, QTreeView, QFileDialog, QListWidgetItem
from PySide6.QtGui import QIcon, QPixmap, QStandardItemModel, QStandardItem, QAction, QKeySequence, QShortcut
from PySide6.QtCore import QFile, QSize, QModelIndex, QUrl, QCoreApplication, QEvent, QSettings
from PySide6.QtMultimedia import QMediaCaptureSession, QAudioBufferInput, QAudioBufferOutput, QMediaRecorder, QAudioFormat, QAudioInput
from PySide6 import QtCore
from models import ConversationUIState
from sqlalchemy import func
import asyncio
from PySide6.QtTest import QAbstractItemModelTester
#from PySide6.QtCore.GObject.QtTest import QAbstractItemModelTester
from PySide6 import QtNetwork
import os
import network  # this is network.py
import time

#from ui_mixchat_chatview import Ui_ChatForm
from qt_models import *  # qt_models.py
from ui_mixchat import Ui_MainWindow  # ui_mixchat.py
from sqlmodel import select
from PySide6.QtQml import QQmlNetworkAccessManagerFactory

from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from typing import Dict

# matplotlib in qt:
# https://www.datacamp.com/tutorial/introduction-to-pyside6-for-building-gui-applications-with-python

# https://doc.qt.io/qtforpython-6/PySide6/QtQml/QQmlEngine.html
# offlineStoragePathᅟ - The directory for storing offline user data
# clearComponentCache()

def todo_unicode():
    # for unicode entry:
    qks = QKeySequence(tr("Ctrl-Shift-u"))
    # if we bind an event for this, and then push a mode that recognizes
    # [0-9a-fA-F]
    # and pops that state when encountering another one, we can do
    # poor man's IME unicode input

def todo_keys_push_to_talk():
    # probably a listener on main window
    # that checks if the input tab open is push to talk
    # QEvent::KeyPress, QEvent::KeyRelease
    # https://doc.qt.io/qtforpython-6/PySide6/QtGui/QKeyEvent.html
    # The event handlers QWidget::keyPressEvent(), QWidget::keyReleaseEvent(), QGraphicsItem::keyPressEvent() and QGraphicsItem::keyReleaseEvent() receive key events.
    pass

def todo_keys():
    # QWidgetLineControl::processShortcutOverrideEvent(QKeyEvent *ke) - maybe for shift-enter in single-line chat?
    # for searching in chats:
    find = QKeySequence(QKeySequence.StandardKey.Find)
    findNext = QKeySequence(QKeySequence.StandardKey.FindNext)
    findPrev = QKeySequence(QKeySequence.StandardKey.FindNextPrevious)
    # for pg up/down in chat view:
    pgUp = QKeySequence(QKeySequence.StandardKey.MoveToNextPage)
    pgDown = QKeySequence(QKeySequence.StandardKey.MoveToPreviousPage)
    ctrlHome = QKeySequence(QKeySequence.StandardKey.MoveToEndOfDocument)
    # for switching contacts:
    shiftPgUp = QKeySequence(QKeySequence.StandardKey.SelectNextPage)
    shiftPgDown = QKeySequence(QKeySequence.StandardKey.SelectPreviousPage)

def async_cb(method):
    """this wraps async MainWindow methods and calls them """
    def f(*args, **kwargs):
        return ensure_future(method(*args, **kwargs))
    return f


class FirewallNetworkAccessManager(QtNetwork.QNetworkAccessManager):
    def connectToHost(self, *args) -> None:
        print("firewall: connectToHost")
        return
    def connectToHostEncrypted(self, *args) -> None:
        print("firewall: connectToHostEncrypted")
        return
class FirewallNetworkAccessManagerFactory(QQmlNetworkAccessManagerFactory):
    def create(self) -> QtNetwork.QNetworkAccessManager:
        """TODO not sure this actually works"""
        return FirewallNetworkAccessManager()

class KeyPressFilter(QObject):
    """Listen for Space(bar) / push-to-talk"""
    def eventFilter(self, widget, event):
        if event.type() == QEvent.KeyPress:
            text = event.text()
            #if event.modifiers():
            #    text = event.keyCombination().key().name.decode(encoding="utf-8")
            print("ev", text)
        else:
            print(event.type(), repr(event))
        return False

def duration_time_ns():
    """Portable nanosecond delta timestamps.

    This is ~SI nanoseconds, but platform-defined epoch instead of UNIX epoch.
    """
    try:
        # Linux-specific: On Linux CLOCK_MONOTONIC adds drift to mitigate skew,
        # but they later added _RAW which doesn't:
        return time.clock_gettime_ns(time.CLOCK_MONOTONIC_RAW)
    except AttributeError:
        # BSDs use SI seconds by default:
        return time.monotonic_ns()

class MainWindow(QMainWindow):
    def X_keyPressEvent(self, ev: "QEvent.KeyPress") -> None:
        key = ev.key()
        print("key pressed", key)
    def X_keyReleaseEvent(self, ev: "QEvent.KeyPress") -> None:
        key = ev.key()
        print("key released", key)
    def push_to_talk_start(self):
        # TODO Instead of trying to use the QMediaCapture, maybe we'd rather want to just grab stuff
        # from an input device, encode in a separate thread, and then send that as a file.
        # The Qt APIs seemed nice, but unfortunately QMediaRecorder can only write to files, not memory, and it likely can't encode to any of the codecs we want.

        # https://pastebin.com/n7e9KREA
        self.push_to_talk_started = True  # TODO should use the qt state stuff for this I guess

        self.mSession = QMediaCaptureSession()
        mSession=self.mSession
        self.aInput=QAudioInput()  # whatever default input dev, probably want to make this configurable.
        mSession.setAudioInput(self.aInput)
        self.recorder = QMediaRecorder()
        if not self.recorder.isAvailable():
            print("MediaRecorder does not seem available")
        self.recorder.setAutoStop(True)  # This property controls whether the media recorder stops automatically when all media inputs have reported the end of the stream or have been deactivated.

        aformat = QAudioFormat()
        aformat.setSampleFormat(QAudioFormat.Float)
        aformat.setSampleRate(44100)
        aformat.setChannelConfig(QAudioFormat.ChannelConfigMono)
        self.audio_buffer = QAudioBufferOutput(aformat)

        mSession.setRecorder(self.recorder)
        # TODO: instead of setOutputLocation we should mock a https://doc.qt.io/qt-6/qiodevice.html
        # TODO: in order to capture the data so we can give it to our custom encoder,
        # TODO: and also so we can avoid writing to disk.
        #self.recorder.setOutputLocation(QUrl("out.opus"))

        #recorder.readyToSendAudioBuffer.connect(push_to_talk_buffer)
        if self.recorder.audioChannelCount() != 1:
            self.recorder.setAudioChannelCount(1)
        self.recorder.record()
        # duration() is in ms
        # what is recorder.autoStop ?
        # encodingMode averagebitrate, constantbitrate, constantquality, twopassencoding
        # isAvailable
        # metadata()

        # recorderStateChanged
        #>>> self.recorder.recorderState()
        #<RecorderState.RecordingState: 1>
        # PausedState == 2, StoppedState

        # TODO that happens -a lot-:
        #self.recorder.durationChanged.connect(lambda: print("recorder.durationChanged"))

        # recorder.errorChanged.connect
        # recorder.errorOccured.connect
        def push_to_talk_buffer():
            print("buff")
            mAudioInputBuffer.sendAudioBuffer()

        # TODO: we need a listener for when self.push_to_talk_started becomes false
        # TODO: that should call recorder.stop()
        # TODO: and then we should figure out -why- it was stopped: cancelled or because
        # TODO: user cancelled or switched away from the push-to-talk tab

        import pdb;pdb.set_trace()
        pass

    def push_to_talk_pressed(self):
        """The shortcut has autoRepeat=True, so we will keep getting these at regular intervals.
        Instead of relying on receiving a keyReleased event, we do a "dead man's switch" thing
        and record until we haven't seen a PTT shortcut trigger for a while (~100ms? - TODO how
        do we learn the autoRepeat rate?).

        TODO ptt_hold_space_label should be saying "RECORDING AUDIO ..." when we are
          - we should restore the label after
        TODO: when it's saying that we should have a button to cancel.
        TODO: We should also have a keyboard combination to do that, while they're holding space (escape? delete?)
        """
        # CONDITION: push to talk mode in the chat input
        if self.ui.ptt_tab.parent().currentWidget() != self.ui.ptt_tab:
            # Switched away from the ptt tab, we should stop recording.
            # Probably cancel sending it too.
            # TODO actually this could just be a listener on singlemultitab
            # then we can also make the space shortcut conditional there.
            return True
        #== "ptt_tab"
        # - conversation not changed
        now_ns = duration_time_ns()
        convo = self.convo_state()
        if now_ns - convo.last_push_to_talk_ns > 100_000_000:
            # if we were recording in a previous tab we should end it
            convo.last_push_to_talk_ns = now_ns
        else:
            # we wait for two events, so a single errant spacebar press doesn't send 50ms of audio
            print("talking")
            convo.last_push_to_talk_ns = duration_time_ns()
            if not getattr(self, "push_to_talk_started", False):
                self.push_to_talk_start()
        return False

    def __init__(self, app : QApplication) -> None:
        self.systray = None
        super(MainWindow, self).__init__()
        self.conversation_state_by_id : Dict[int, ConversationUIState] = dict()
        self.app = app
        self.ui = Ui_MainWindow()
        self.ui.setupUi(self)

        # failed attempt to firewall external resource access:
        eng = self.ui.qml_ChatLines.engine()
        nwa = eng.networkAccessManager()
        #import pdb;pdb.set_trace()
        fnamf = FirewallNetworkAccessManagerFactory()
        a = fnamf.create()
        eng.setNetworkAccessManagerFactory(fnamf)

        self.setWindowTitle(APP_NAME)
        # self.dumpObjectTree() # display debug

        #self.eventFilter = KeyPressFilter(parent=self)
        #self.installEventFilter(self.eventFilter)

        space = QKeySequence(self.app.tr("Space","Shortcut|Push to talk"))
        qs_ptt = QShortcut(space, self, self.push_to_talk_pressed)

        #import pdb;pdb.set_trace()
        #self.ui.keyPressed.triggered.connect(lambda: print("key down"))
        #self.keyRelease.triggered.connect(lambda: print("key up"))

        # item delegates define custom looks for view items

        self.ui.action_space.triggered.connect(self.new_conversation)
        self.ui.action_new_conversation.triggered.connect(self.new_conversation)
        self.ui.action_accept_invitation.triggered.connect(self.accept_invitation)
        self.ui.action_invite_contact.triggered.connect(self.invite_contact)
        # Make the [Quit] toolbar actually quit:
        self.ui.action_quit.triggered.connect(lambda ev: self.close(ev,really_quit=True))

        # Sending files to a conversation: File attachment/sending callbacks
        self.ui.attach_file_button.clicked.connect(self.attach_file)
        self.ui.send_file_button.clicked.connect(self.send_file)
        self.ui.attached_files_QListWidget.itemClicked.connect(self.attach_file_remove)
        self.ui.attached_files_QListWidget.itemDoubleClicked.connect(self.attach_file_remove)

        self.all_contacts = QStandardItemModel()
        #self.ui.contacts_treeWidget.setModel(self.all_contacts)
        fpm = FilterProxyModel(self)
        fpm.setSourceModel(self.all_contacts)
        self.ui.contacts_treeWidget.setModel(fpm)
        # refresh contact filter when the user types in the contact search bar:
        self.ui.contactFilterLineEdit.textChanged.connect(lambda:fpm.invalidate())
        #fpm.setFilterWildcard("*")

        # inputMethodEvent
        #self.ui.contacts_treeWidget.keyboardSearch.connect(lambda: print("KB search")) # TODO not a signal, but when user starts typing here we want to set the focus to contactFilterLineEdit instead
        self.ui.contacts_treeWidget.selectionModel().currentChanged.connect(self.conversation_selected)
        self.ui.chat_lineEdit.returnPressed.connect(self.chat_msg_single_line)

    @async_cb
    async def chat_msg_single_line(self):
        msg = self.ui.chat_lineEdit.text()
        self.ui.chat_lineEdit.setText("")
        convo_state = self.convo_state()
        if not convo_state:
            return
        convo_state.chat_lineEdit_buffer = ''
        if not msg.strip():
            return
        print('chat msg single line, TODO should write this to WAL and contact log', msg)
        # - look up current Conversation:
        #   - we need the write cap and the current index
        async with persistent.asession() as sess:
            # TODO we should probably just cache this
            bacap_write_cap = (await sess.exec(
                select(persistent.Conversation.write_cap).filter(
                    persistent.Conversation.id == convo_state.conversation_id
                ))).first()
        # - thinclient: we need to encrypt it using write cap + current index
        encrypted = await network.encrypt_message(
            msg=msg,
            bacap_write_cap=bacap_write_cap,
            bacap_write_index=0, # TODO
        )

        # - we need to queue it for writing in the MixWAL
        #
        # - once it's in MixWAL we should pretend to have received it:

        await self.receive_msg(
            conversation_id=convo_state.conversation_id,
            peer_id=convo_state.own_peer_id,
            cbor_payload=msg.encode(),
        )

    async def receive_msg(self, conversation_id : int, peer_id: int, cbor_payload: bytes) -> None:
        """Called when a message needs to be written to a conversation log, and the UI
        potentially needs updating.
        This could be called from the network, but it's unclear how the network would know our conversation_id and peer_id.
        """
        convo_state = self.conversation_state_by_id[conversation_id]

        async with persistent.asession() as sess:
            conversation_order = (await sess.exec(
                select(func.count())
                .select_from(persistent.ConversationLog)
                .where(persistent.ConversationLog.conversation_id == convo_state.conversation_id)
            )).first()
            log_entry = persistent.ConversationLog(
                conversation_id=convo_state.conversation_id,
                conversation_peer_id=peer_id,
                conversation_order=conversation_order,
                payload=cbor_payload,
                # TODO the payload we want to store is the plaintext CBOR payload;
                # not the plaintext message.
            )
            sess.add(log_entry)
            await sess.commit()

        # And then we can increment the row count to let the UI register it:
        convo_state.conversation_log_model.increment_row_count()

        # x) Scrolling - two cases:
        if convo_state is self.convo_state():
            #   x.1) Scrolling: Conversation is in focus:
            # TODO make which of these to do configurable:
            print("convo order would scroll", conversation_order)
            #import pdb;pdb.set_trace()
            #print("current", self.ui.ChatLines.verticalScrollBar().value())
            #print("next", min(
            #    1 + self.ui.ChatLines.verticalScrollBar().value(),
            #    conversation_order-3))
            #self.scroll_chat_lines(min(
            #    1 + self.ui.ChatLines.verticalScrollBar().value(),
            #    conversation_order-3))
            #self.ui.ChatLines.scrollToBottom()
        else:
            #   x.2) Scrolling: Conversation is NOT in focus:
            print("NOT IN FOCUS")
            convo_state.chat_lines_scroll_idx += 1
            # convo_state.chat_lines_scroll_idx = conversation_order
            # TODO we should flash the contact entry somehow
            # TODO we should bump "unread message" counter

        # if the main window is not in focus, we should issue a notification:
        if not self.app.focusWidget():
            self.app.alert(self)
            # self.app.beep()
        self.systray.has_new_messages() # TODO move this into block above

    def convo_state(self) -> ConversationUIState:
        # TODO there must be a more graceful way to do this:
        idx = self.ui.contacts_treeWidget.selectionModel().currentIndex()
        idx = self.ui.contacts_treeWidget.model().mapToSource(idx)
        q = self.all_contacts.item(idx.row(), idx.column())
        #q = self.ui.contacts_treeWidget.currentItem()
        if not q:
            raise Exception("Can't find selected conversation")
        #q = q.parent() or q
        return self.conversation_state_by_id[q.conversation_id]

    @async_cb
    async def send_file(self):
        """Send attached files.

        This is more or less like self.chat_msg_single_line(),
        except of course we're sending some potentially big files.
        If we don't think the files will change, we can stream them to the temp stream.
        But the safest would be to load the whole thing into the WAL, that way user can delete
        or modify their file. In the future that should probably be a setting.
        For now let's just do the simple thing:
        - Each file is one big message
        - We send them back to back and end up with a huge ephemeral BACAP stream
        - When done uploading, we put a reference to the big stream into the chat.
        - We can choose whether we want to block the user's chat stream or not (if we don't,
          they can keep chatting but their attached files may look out of order).
          This might be a good reason to allow sending a message along with the files.
        """
        convo = self.convo_state()
        print("should send files", convo.attached_files)

        # TODO actually send them

        # Remove sent files from model and view:
        convo.attached_files.clear()
        self.ui.attached_files_QListWidget.clear()

    def attach_file_remove(self, item: QListWidgetItem) -> None:
        """Clicking on a file path in the QListWidgetItem path removes it"""
        convo = self.convo_state()
        convo.attached_files.remove(item.data(0x100))
        # Redraw:
        self.refresh_attached_files_for_conversation(convo)

    @async_cb
    async def attach_file(self):
        dialog = QFileDialog()
        dialog.setFileMode(QFileDialog.FileMode.ExistingFiles)  # allow more than one file
        dialog.setAcceptMode(QFileDialog.AcceptOpen)  # 
        dialog.setViewMode(QFileDialog.ViewMode.Detail)
        if saved := getattr(self, "saved_file_dialog", None):
            dialog.restoreState(saved)
        # setProxyModel() -- in case user enters a url?
        #.setHistory()
        if not dialog.exec():
            self.saved_file_dialog = dialog.saveState()
            return
        self.saved_file_dialog = dialog.saveState()
        print("file dialog", dialog.selectedFiles())
        convo = self.convo_state()

        # TODO may want to make the paths nicer, like relative to home folder or soomething like that
        convo.attached_files |= set(dialog.selectedFiles())  # add entries to convo_state
        self.refresh_attached_files_for_conversation(convo)

    def refresh_attached_files_for_conversation(self, convo: ConversationUIState):
        self.ui.attached_files_QListWidget.clear()
        for fname in convo.attached_files:
            itm = QListWidgetItem()
            itm.setData(0x100, fname)  # full path stored in Qt.UserRole
            p = Path(fname)
            abbrev : str = str(Path(p.parent.name) / p.name)
            try:
                bytesz = p.stat().st_size
                byteunit = "BKMGT"[int(math.log(1 + bytesz, 1024)):][:1] or "B"
                bytesz /= 1024**("BKMGT".index(byteunit))
                abbrev += f"\r\n({bytesz:,.2f}".rstrip("0").rstrip(".") + f"{byteunit})"
            except (FileNotFoundError, PermissionError):
                # TODO should report this error
                continue
            itm.setText(abbrev)  # abbreviated path with first parent + size stored in "display"
            self.ui.attached_files_QListWidget.addItem(itm)

    @async_cb
    async def conversation_selected(self, selected:QTreeWidgetItem, old:QTreeWidgetItem|None):
        self.foo = getattr(self, "foo", 0)
        self.foo += 1
        # https://doc.qt.io/qtforpython-6/PySide6/QtWidgets/QTreeWidget.html
        # scrollToItem ; setCurrentItem
        if not selected:
            return
        #import pdb;pdb.set_trace()
        print("selected", selected)
        selected_qmi = selected.model().mapToSource(selected)
        selected_id = self.all_contacts.item(selected_qmi.row(), selected_qmi.column()).conversation_id
        selected = selected.data()  # type: ignore[call-arg]
        # clicks may be on either convo or contact:
        conversation = self.conversation_state_by_id[selected_id]
        #old = getattr(old, "parent", lambda: None)() or old
        old_convo = None
        if old and old.model():
            old = old.model().mapToSource(old)
            old_id = self.all_contacts.item(old.row(), old.column()).conversation_id
            old_convo = self.conversation_state_by_id[old_id]
        print("conversation selected", selected)
        ### TODO this was how far we got

        convo_state = self.convo_state()
        if old_convo:
            # Store old line edit buffer and scroll
            old_convo.chat_lineEdit_buffer = self.ui.chat_lineEdit.text()
            c=self.ui.qml_ChatLines.rootObject()
            if c and c.children():
                vscrollbar = c.children()[2].children()[0]
                vrect = vscrollbar.children()[2] # this is the rectangle,
                old_convo.chat_lines_scroll_idx = int(vrect.y())
                print("saved scroll", int(vrect.y()))
        if not convo_state:
            return
        # When user selects a conversation we need to replace the chat model with:
        #self.ui.quickWidget.setSource("http://127.0.0.1:2222")
        # Feed "initial properties" into QML; not sure how to force a reload except
        # setSource() again, but presumably QML can read updated properties set with
        # self.ui.quickWidget.setProperty("author", "not me") etc?
        #if self.ui.qml_ChatLines.rootObject()
        #c.dumpItemTree()
        #c.property("conversation_name")
        #vscrollbar = c.children()[2].children()[0]
        # vrect = vscrollbar.children()[2].position() # this is the rectangle,
        # vrec.position().y()
        # but does vrec.position().setY() work?
        #import pdb;pdb.set_trace()
        print("load root", self.ui.qml_ChatLines.rootObject())
        if root := self.ui.qml_ChatLines.rootObject():
            root.setProperty("conversation_name", str(convo_state.conversation_id))
            root.setProperty("chatTreeViewModel", convo_state.conversation_log_model)
            c=self.ui.qml_ChatLines.rootObject()
            convo_state = self.convo_state()
            vscrollbar = c.children()[2].children()[0]
            vrect = vscrollbar.children()[2] # this is the rectangle,
            vrect.setY(convo_state.chat_lines_scroll_idx)
            print('set first restored scroll to', convo_state.chat_lines_scroll_idx)
        else:
            self.ui.qml_ChatLines.setInitialProperties({
                "conversation_name": "me",
                "chatTreeViewModel": convo_state.conversation_log_model,
            })
            self.ui.qml_ChatLines.setSource("resources/chatview.qml")

        if convo_state.chat_lines_scroll_idx is not None:
            self.scroll_chat_lines(convo_state.chat_lines_scroll_idx)

        # Restore new single line input buffer
        self.ui.chat_lineEdit.setText(convo_state.chat_lineEdit_buffer)
        # Update the label we display at the top left corner of the chat view:
        self.ui.ContactName.setText(selected)
        # Restore attached_files:
        self.refresh_attached_files_for_conversation(convo_state)

        self.systray.has_read_messages()

    def do_we_even_have_unread_messages(self) -> bool:
        """
        We need to keep track of "read" per conversation.
        This needs to be persisted to disk.
        Then we need to keep an index of conversations that have unread messages,
        with counts, so we can display them in the contact roster,
        and we should be able to produce the sum so we can display that somewhere too.
        The sum would then also be used to govern the systray icon.
        """
        return False

    @async_cb
    async def new_conversation(self):
        conversation_name , ok = QInputDialog.getText(
            self,
            "New conversation",
            "Choose conversation name:",
        )
        if not ok: return
        display_name , ok = QInputDialog.getText(
            self,
            "New conversation",
            "Choose (your) name displayed to the other user(s):",
        )
        if not ok: return

        write_cap = await network.new_write_cap()
        read_cap = await network.read_cap_from_write_cap(write_cap)

        convo = persistent.Conversation(name=conversation_name, write_cap=write_cap)

        own_peer = persistent.ConversationPeer(name=display_name, read_cap=read_cap, active=False, conversation=convo)
        convo.own_peer = own_peer
        # TODO this is mainly for debugging:
        first_post = persistent.ConversationLog(
            conversation=convo,
            conversation_peer=own_peer,
            conversation_order=0,
            payload=b"hello " + own_peer.name.encode(),
        )
        async with persistent.asession() as sess:
            sess.add(convo)
            sess.add(own_peer)
            sess.add(first_post)
            await sess.commit()
            await sess.refresh(convo)
            await add_conversation(self, convo)

    @async_cb
    async def invite_contact(self):
        """User wants to generate an invitation to give to a friend.

        We need to ask the user what name they want, and make them a BACAP write cap.
        Then we need to find the read cap, and show that.

        Persistence-wise, we need to store:
        - Our display name
        - BACAP write cap
        - BACAP read cap (to give to friend)

        The BACAP stuff we will get from the thin client.
        """
        display_name , ok = QInputDialog.getText(
            self,
            "Invite contact",
            "Choose (your) name displayed to the other user:",
        )
        if not ok:
            return
        print("invite contact pressed TODO ", display_name)

        # OK, now we need to ask thin client to generate BACAP caps

        # And then we need to persist the invitation (So we know what we're doing)
        # async with persistent.asession() as session:
        # await add_contacts(self, conversationpeer)

        # And then we need to display the read cap + name to the user so they can copy-paste

    @async_cb
    async def accept_invitation(self):
        """User wants to input an invitation from a peer.

        We need the following information from user:
        - Peer display name
        - BACAP read cap for their stream

        - And then we should generate one for them. To avoid this two-pass protocol we should maybe
          make the inviter give us a BACAP write cap for a temp stream we could write our invitation
          to instead? (And then they should start checking that).
        """
        invite_string , ok = QInputDialog.getText(
            self,
            "Accept invitation",
            "Copy-paste the invite you received:",
        )
        if not ok:
            return
        print("accept invitation pressed", invite_string)
        # Now we need to parse it, validate the cap,
        #   and confirm with the user that they like the display name
        # Then ask them for the Conversation name (or just default to the display name for now)
        # Then we need to persist the contact:
        # - persistent.ConversationPeer(name=..., read_cap=...)
        # - persistent.Conversation()
        # TODO: how do we know the write_cap? should we make them select from a list of outstanding
        # invitations and / or existing groups?

    def scroll_chat_lines(self, value:int) -> None:
        """Scroll main_window.ui.ChatLines to item offset (value)."""
        # Nice workaround for Qt state shenanigans, changing the scrollbar doesn't
        # *actually* render anything unless we do it a little bit later.
        # TODO maybe we can do this instead:
        # https://doc.qt.io/qtforpython-6/PySide6/QtCore/QCoreApplication.html#PySide6.QtCore.QCoreApplication.sendPostedEvents
        # There's also this very information article:
        # https://typevar.dev/en/docs/qt/qtreeview/scrollContentsBy
        def in_a_bit2():
            c=self.ui.qml_ChatLines.rootObject()
            convo_state = self.convo_state()
            vscrollbar = c.children()[2].children()[0]
            vrect = vscrollbar.children()[2] # this is the rectangle,
            oldy = vrect.y()
            vrect.setY(convo_state.chat_lines_scroll_idx)
            if vrect.y() != convo_state.chat_lines_scroll_idx:
                print("TEST"*100)
            print('set restored scroll to', convo_state.chat_lines_scroll_idx)
        async def in_a_bit():
            in_a_bit2()
            #self.ui.ChatLines.verticalScrollBar().setValue(
            #    value
            #)
        asyncio.get_running_loop().call_soon(lambda :ensure_future(in_a_bit()))

    def close(self, *args, **kwargs):
        if kwargs.get('really_quit', False):
            self.app.quit()
    def closeEvent(self, event, **kwargs):
        if getattr(self, "systray", False):
            event.ignore()
            self.systray.showMessage('Still running', f"{APP_NAME} running in background.")
            self.hide()

class MixSystrayIcon(QSystemTrayIcon):
    """https://doc.qt.io/qtforpython-6/PySide6/QtWidgets/QSystemTrayIcon.html

    TODO: when user clicks on the systray icon we should scroll to an unread message?
    TODO: to do that we'd have to keep track of what they have seen.
    """
    def __init__(self, mw : MainWindow, echomix_icon: QIcon):
        super().__init__()
        if not self.isSystemTrayAvailable():
            return
        mw.systray : "MixSystrayIcon" = self
        self.mw : MainWindow = mw

        #for icon_name in ( "SP_DialogYesButton",
        #                   "SP_MessageBoxCritical",
        #                   "SP_MessageBoxInformation",
        #                  ):
        #    pixmapi = getattr(QStyle, icon_name)
        #    icon : QIcon = mw.style().standardIcon(pixmapi)
        #    setattr(self, icon_name, icon)
        #self.setIcon(self.SP_MessageBoxInformation)  # new message
        #self.setIcon(self.SP_DialogYesButton)  # connected
        #self.setIcon(self.SP_MessageBoxCritical)  # disconnected
        self.setIcon(echomix_icon)
        self.setToolTip(APP_NAME)
        self.activated.connect(self.on_left_click)

        # Context menu for right-clicking on the systray icon:
        self.setContextMenu(QMenu())
        # TODO: Enable/Disable notifications
        self.contextMenu().addAction(mw.ui.action_quit)

        self.show()
        if self.supportsMessages():
            pass #self.showMessage("hel","lo")
        # there's also this for bugging the user:
        # https://doc.qt.io/qt-6/qapplication.html#alert

    def has_new_messages(self) -> None:
        print("has_new_messages")
        self.setIcon(self.mw.echomix_icon_new_message)
        self.mw.setWindowIcon(self.mw.echomix_icon_new_message)

    def has_read_messages(self) -> None:
        print("has_read_messages")
        self.setIcon(self.mw.echomix_icon)
        self.mw.setWindowIcon(self.mw.echomix_icon)

    def showMessage(self, title, message, icon:QIcon|QPixmap|None=None, msTimeoutHint:int|None=None):
        extra = []
        if icon is not None:
            extra.append(icon)
        if msTimeoutHint is not None:
            extra.append(msTimeoutHint)
        super().showMessage(title, message, *extra)
    def on_left_click(self, reason: QSystemTrayIcon.ActivationReason):
        self.mw.show()  # if they closed the window
        self.mw.raise_()  # raise to top  /  make active window in WM

        # TODO we need to ensure
        self.has_read_messages()
        # has a chance to run here, but it should only do that when we actually resume
        # to the conversation that has the new messages.

    def messageClicked(self,*args,**kwargs):
        print("Someone clicked message", args, kwargs)


async def add_conversation(window, convo: persistent.Conversation) -> None:
    window.conversation_log_models = getattr(window, "conversation_log_models", dict())

    contacts = window.ui.contacts_treeWidget
    #qtwi = QTreeWidgetItem([convo.name])
    qtwi = QStandardItem(convo.name)
    qtwi.conversation_id = convo.id
    clm = ConversationLogModel(convo.id)
    convo_state = ConversationUIState(
        own_peer_id=convo.own_peer_id,
        conversation_id=convo.id,
        chat_lineEdit_buffer="",
        conversation_log_model=clm,
        chat_lines_scroll_idx=None,
    )
    window.conversation_state_by_id[convo.id] = convo_state
    for peer in convo.peers:
        #ptwi = QTreeWidgetItem([peer.name])
        ptwi = QStandardItem(peer.name)
        qtwi.setChild(0, ptwi)
    async with persistent.asession() as sess:
        msg_count = (await sess.exec(
            select(func.count())
            .select_from(persistent.ConversationLog)
            .where(persistent.ConversationLog.conversation_id == convo.id)
        )).first()
        convo_state.conversation_log_model.row_count = msg_count
    convo_state.chat_lines_scroll_idx = msg_count  # initially we scroll to bottom
    # qtwi: select all contactpeers under here
    window.all_contacts.appendRow(qtwi)
    #window.ui.contacts_treeWidget.model().invalidate()
    #window.ui.contacts_treeWidget.model()
    #contacts.addTopLevelItem(qtwi)
    #contacts.expandItem(qtwi)
    # ULTRA TODO THIS IS FOR DEBUGGING ONLY
    #qts = QAbstractItemModelTester(clm)
    #print(qts)

def rebuild_pydantic_models():
    """Updated pydantic models with the classes defined in this file"""
    #ConversationUIState.model_rebuild()
    pass

async def main(window: MainWindow):
    rebuild_pydantic_models()
    echomix_icon = QIcon()
    window.echomix_icon = echomix_icon
    for sz in (16,32,48,160,256):
        echomix_icon.addFile(f"resources/echomix_{sz}.png", size=QSize(sz,sz))
        # addFile(..., mode=QIcon.Mode.(Normal|Disabled|Active|Selected), state=QIcon.State.Off|QIcon.State.Off)
        # https://doc.qt.io/qtforpython-6/PySide6/QtGui/QIcon.html#PySide6.QtGui.QIcon.Mode
        # https://doc.qt.io/qtforpython-6/PySide6/QtGui/QIcon.html#PySide6.QtGui.QIcon.State
        # offline: Disabled
    window.echomix_icon_new_message=QIcon()
    window.echomix_icon_new_message.addFile("resources/echomix_256_new_message.png", QSize(256,256))
    echomix_icon.addFile("resources/echomix_icon.svg")

    systray = MixSystrayIcon(window, echomix_icon)

    #contacts.focusInEvent = lambda x: print('in', x)
    #contacts.focusOutEvent = lambda x: print('out', x)

    window.setWindowIcon(echomix_icon)
    # window.findChild(object, "mdstatus").setText("# HEI")
    #window.ui.mdstatus.setText("# HEI")
    contacts : QTreeView = window.ui.contacts_treeWidget

    # populate contacts from persistent.py:
    async with persistent.asession() as session:
        a = await session.exec(
            select(persistent.Conversation)
        )
        for convo in a:
            await add_conversation(window, convo)

    window.show()

    #import pdb;pdb.set_trace()
    pass


def todo_settings():
    # https://doc.qt.io/qtforpython-6/examples/example_corelib_settingseditor.html
    #org_settings = QSettings(QSettings.IniFormat, APP_ORGANIZATION)  # for other Qt apps by same APP_ORGANIZATION
    #app_settings = QSettings(QSettings.IniFormat, APP_ORGANIZATION, APP_NAME)
    # .status() https://doc.qt.io/qtforpython-6/PySide6/QtCore/QSettings.html#PySide6.QtCore.QSettings.Status
    # $HOME/.config/MySoft/Star Runner.conf
    # $HOME/.config/MySoft.conf

    # https://doc.qt.io/qtforpython-6/PySide6/QtCore/QSettings.html
    pass

def is_there_already_an_instance_running() -> bool:
    """
    Checks if there are other instances of the application running.

    Returns
      False: There is already an app instance running
      True: This is the first app instance
    """
    # Make the fd global so it doesn't go out of scope when we return
    # since close(2) will unlock:
    global already_running_fd
    # TODO: use a config file entry or something else than __FILE__:
    already_running_fd = open(__file__, "r")
    try:
        fcntl.flock(already_running_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return True  # Already running
    return False  # Obtained the lock

if __name__ == "__main__":

    if is_there_already_an_instance_running():
        sys.exit("The app is already running.")

    # Make scrolling suck less, https://bugreports.qt.io/browse/QTBUG-123936
    os.environ["QT_QUICK_FLICKABLE_WHEEL_DECELERATION"] = "14999"

    persistent.init_and_migrate()  # this must be run outside the async loop

    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    window = MainWindow(app)
    QtAsyncio.run(main(window), handle_sigint=True)
