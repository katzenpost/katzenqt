import QtQuick
import QtQuick.Window
import QtQuick.Layouts
import QtQuick.Controls
//import QtQuick.Controls.Material

// TODO: need to make sure at least pgup/pgdown work
// maybe also arrows?
// it would also be nice if pgup/pgdown worked when the treeview isn't
// the input focus

TreeView {
     id: chatTreeView
     objectName: "chatTreeView"

     //required property QtObject change_convo

     // we should track if messages have been "read" for some definition of "read"
     // the easiest is probably to count the amount of time they've been on screen.
     // TODO should track these states per model
     property var msgReadTimer_generation: 0
     property var read_map: ({})
     property var unread_map: ({})
     required property var ctx

     Timer {
       id: msgReadTimer
       interval: 1500
       repeat: true
       running: typeof ctx.first_unread !== 'undefined' && ctx.first_unread < rows

       // TODO shouldn't say that our own messages are "unread"

       onTriggered: {
         //print("timer triggered", ctx.first_unread, rows, this.parent.ctx.first_unread)
         let cull_read_map = false
         let first_unread = ctx.first_unread
         // don't care about anything before first_unread
         // we want to see if any of the rows between
         // first_unread <= topRow <= bottomRow
         // have been in focus for more than one interval

         // and then we want to increase first_unread
         for (var i = topRow; i <= bottomRow; i++) {
           if (first_unread <= i) {
           if (unread_map[i] && unread_map[i] == msgReadTimer_generation) {
             // they were in view last time and they still are, so we have
             // "read" this message. we mark it as read, but we can't
             // bump the split buffer yet
             read_map[i] = 1
             cull_read_map = true
             //print("read_map[i]", i, read_map[i])
           } // unread_map[i] && unread_map[i] == msgReadTimer_generation
           //print("unread_map", i, unread_map[i], first_unread, msgReadTimer_generation)
           unread_map[i] = msgReadTimer_generation + 1;
           } // first_unread <= i
         }
         if (cull_read_map) {
           //print("first_unread was", first_unread)
           while (read_map[first_unread]) {
             delete read_map[first_unread]
             delete unread_map[first_unread]
             first_unread++
           } // while read_map[first_unread]
           //print("first_unread is now", first_unread)
         } // if cull_read_map
         msgReadTimer_generation++
         if (first_unread && ctx.first_unread != first_unread) {
           //ctx.insert("first_unread", first_unread)
           print("updating first_unread from",ctx.first_unread,"to",first_unread)
           ctx.first_unread = first_unread
         }
       } // onTriggered

     }

     onContentYChanged: {
        // save scroll position
        //print("onContentYChanged", contentY, bottomRow, topRow)
        // forceLayout()
        // atYEnd
        // atYBeginning
        // contentY
        // contentHeight
        // topRow: This property holds the topmost row that is currently visible inside the view.
        // bottomRow: This property holds the bottom-most row that is currently visible inside the view.
        // rows: total amount of rows in model
     }

     //signal scrollToBottom: { positionViewAtCell(Qt.point(columns - 1, rows - 1), TableView.AlignLeft | TableView.AlignBottom) }

     //onLayoutChanged: {
        //print("layout changed", contentY, contentHeight, vscrollbar.position)
     //}

     //required property QtObject backend
     //Connections {
     //  target: backend
     //  function onUpdated(){
     //  console.log("helllo backend", backend)
     //  }
     //}

     //required property var conversation_name
     //required property var chatTreeViewModel

     //onModelChanged: console.log(vscrollbar.position, ctx.conversation_scroll)
    //property int savedIndex:  0
    //onCurrentIndexChanged: savedIndex = currentIndex //eventually check against != 0 first


    anchors.centerIn: parent
        //Layout.fillWidth: true

        flickDeceleration: 0.1
        boundsMovement: Flickable.StopAtBounds

//anchors.fill: parent

    ScrollBar.vertical: ScrollBar {
       // TODO how do we track the position of this
       // this thing has a property "position" that we should track
       id: vscrollbar
       objectName: "vscrollbar"
       //policy: ScrollBar.AlwaysOn
       policy: chatTreeView.contentHeight > chatTreeView.height ? ScrollBar.AlwaysOn : ScrollBar.AlwaysOff
        width: 15
        //snapMode: ScrollBar.SnapOnRelease
        //position: ctx.conversation_scroll
        contentItem: Rectangle {
            objectName: "vscrollbar_rect"
            color: "red"
        }
        //function onPositionChanged() {
          //console.log("scroll pos changed")
        //}

        Connections {
          target: chatTreeView
          function onModelChanged() {
            console.log("model changed", vscrollbar.position, ctx.conversation_scroll, ctx.first_unread, rows)
	    // if there's a new message and bottomRow would still be in view, we scroll to the bottom:
            if (rows && bottomRow + (bottomRow - topRow)-1 > rows-1) {
	      // for some absurd reason scrolling past bottomRow+1 in one go positions the view at the beginning, so we do increments:
	      while (bottomRow < rows - 1) { positionViewAtCell(Qt.point(0, bottomRow+1), TableView.AlignLeft | TableView.AlignBottom) }
	    }
          }
        }
     }
        model: ctx.chatTreeViewModel

        delegate: TreeViewDelegate {
// https://doc.qt.io/qt-6/qml-qtquick-controls-treeviewdelegate-members.html
// implicitWidth: padding + label.x + label.implicitWidth + padding
// implicitHeight: label.implicitHeight * 1.5

          //property TreeView treeView
          //property bool isTreeNode
          //anchors.fill: parent

          implicitWidth: parent.parent.width || 1

          // NB: without this, it looks like shit if you scroll up:
          implicitHeight: Math.max(itemMessageTextArea.implicitHeight, hellodog.implicitHeight) // tallest element

          background: Rectangle {
//            color: "gray";
          }

          contentItem: Row {  /// contentItem is the thing that gets displayed
          Text {
            id: hellodog
            textFormat: Text.PlainText
            text: model.author + (ctx.first_unread <= row ? " (*)" : "") + (
              model.network_status == 1 ? " ⏩ ❓  🖂  ⛶ ⮍ 🕊 ✈ " : ""
	    )
	    color: (model.network_status > 0 ? "red" : "black")
          }
          TextArea {
            id: itemMessageTextArea
            // can't select text in QML Label, so we use a read-only text editor.: https://bugreports.qt.io/browse/QTBUG-14077
            textFormat: Text.PlainText // https://doc.qt.io/qt-6/qml-qtquick-text.html#textFormat-prop
            readOnly: true
            wrapMode: Text.Wrap
            //Layout.fillWidth: parent
            //property alias maxWidth: "chatTreeView"
            width: parent.width - hellodog.width
            //implicitWidth: 100;
            //anchors.fill: parent
            //openExternalLinks: false
            //textInteractionFlags: TextSelectableByMouse
            //selectByMouse: true
            text: model.display
            // this needs to be bigger
            font.family: "Serif"
          } // Text
} // contentItem: Row
        } // delegate: TreeViewDelegate

} // TreeView
