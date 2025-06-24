import QtQuick
import QtQuick.Window
import QtQuick.Layouts
import QtQuick.Controls
//import QtQuick.Controls.Material

TreeView {
     id: chatTreeView

     required property var conversation_name
     required property var chatTreeViewModel

    anchors.centerIn: parent
        //Layout.fillWidth: true

        flickDeceleration: 0.1
        boundsMovement: Flickable.StopAtBounds

//anchors.fill: parent

    ScrollBar.vertical: ScrollBar {
       // TODO how do we track the position of this
       // this thing has a property "position" that we should track
       id: vscrollbar
       //policy: ScrollBar.AlwaysOn
       policy: chatTreeView.contentHeight > chatTreeView.height ? ScrollBar.AlwaysOn : ScrollBar.AlwaysOff
        width: 15
        snapMode: ScrollBar.SnapOnRelease
        contentItem: Rectangle {
            color: "red"
        }
     }

        model: chatTreeViewModel

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
//            color: "red";
          }

          contentItem: Row {  /// contentItem is the thing that gets displayed
          Text {
            id: hellodog
            textFormat: Text.PlainText
            text: model.author
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
     }

//  }  // Rectangle
