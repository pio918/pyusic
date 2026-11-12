import QtQuick
import Quickshell
import Quickshell.Io
import qs.Ui
import qs.Commons
import qs.Commons as Commons

// Now-playing widget for ytplay.
// ytplay publishes its state to $XDG_RUNTIME_DIR/ytplay/status.json and takes
// commands on $XDG_RUNTIME_DIR/ytplay/control.sock. The widget hides itself
// when ytplay is not running or nothing is loaded.
//
//   left click    open the player popup
//   middle click  play / pause
//   right click   open ytplay
//   wheel         previous / next track
BarWidget {
  id: root
  moduleName: "pyus.ytplay"

  readonly property string runDir: (Quickshell.env("XDG_RUNTIME_DIR") || "/tmp") + "/ytplay"
  property var st: ({ running: false, active: false })
  property real nowSec: Date.now() / 1000
  property bool popupOpen: false

  // ytplay rewrites its status every few seconds; one that stopped doing so was killed.
  readonly property bool fresh: !st.ts || (nowSec - st.ts) < 15
  readonly property bool shown: !!st.running && !!st.active && fresh
  readonly property bool isPlaying: !!st.playing
  readonly property string title: st.title || ""
  readonly property string artist: st.artist || ""
  readonly property real progress: st.duration > 0 ? Math.max(0, Math.min(1, st.position / st.duration)) : 0
  readonly property string coverUrl: st.cover > 0 ? "file://" + runDir + "/cover-" + st.cover + ".jpg" : ""

  // A fixed footprint: the label area is always the same width, whatever the title.
  readonly property real labelWidth: Style.space(170)
  readonly property real sideMargin: Style.space(14)
  readonly property real contentWidth: Style.space(18) + Style.space(7) * 2 + glyph.implicitWidth + labelWidth

  function close() { popupOpen = false }

  visible: shown
  implicitWidth: shown ? contentWidth + sideMargin * 2 + Style.space(1) : 0
  implicitHeight: barSize

  function fmt(sec) {
    sec = Math.max(0, Math.floor(sec || 0))
    var m = Math.floor(sec / 60), s = sec % 60
    return m + ":" + (s < 10 ? "0" : "") + s
  }

  // ---- talking to ytplay ------------------------------------------------

  // One long-lived connection: no process spawned per click or wheel notch.
  Socket {
    id: sock
    path: root.runDir + "/control.sock"
    connected: false
  }

  // (Re)connect whenever ytplay is up and the link is not.
  Timer {
    interval: 1000
    running: root.shown
    repeat: true
    triggeredOnStart: true
    onTriggered: if (!sock.connected) { sock.connected = false; sock.connected = true }
  }

  function send(cmd) {
    if (!bar) return
    if (sock.connected) {
      sock.write(cmd + "\n")
      sock.flush()
    } else {
      // Not connected yet: fall back to a one-shot so the click is never lost.
      bar.run("printf '%s\\n' " + Util.shellQuote(cmd) + " | socat -T1 - UNIX-CONNECT:" + Util.shellQuote(runDir + "/control.sock"))
    }
  }

  function apply(text) {
    try { root.st = JSON.parse(text) } catch (e) { /* half-written file: keep the last state */ }
  }

  FileView {
    id: statusFile
    path: root.runDir + "/status.json"
    watchChanges: true
    printErrors: false
    onFileChanged: reload()
    onLoaded: root.apply(text())
    onLoadFailed: root.st = ({ running: false, active: false })
  }

  // The file is replaced atomically, which a watcher can miss; a light poll keeps it honest.
  Timer {
    interval: 1000
    running: true
    repeat: true
    onTriggered: { root.nowSec = Date.now() / 1000; statusFile.reload() }
  }

  // ---- the bar item -----------------------------------------------------

  Row {
    id: row
    x: root.sideMargin
    anchors.verticalCenter: parent.verticalCenter
    spacing: Style.space(7)

    BorderSurface {
      width: Style.space(18)
      height: Style.space(18)
      anchors.verticalCenter: parent.verticalCenter
      radius: Style.spacing.labelGap
      color: Style.normalFillFor(root.bar.foreground, Commons.Color.accent)
      borderSpec: Border.controlSpec("normal", root.bar.foreground, Commons.Color.accent)

      SizedImage {
        anchors.fill: parent
        anchors.margins: Style.space(1)
        crop: true
        url: root.coverUrl
        visible: url != ""
      }
    }

    Text {
      id: glyph
      textFormat: Text.PlainText
      anchors.verticalCenter: parent.verticalCenter
      text: root.isPlaying ? "󰏤" : "󰐊"
      color: root.isPlaying ? root.bar.barForeground : Qt.darker(root.bar.barForeground, 1.5)
      font.family: root.bar.fontFamily
      font.pixelSize: Style.font.body
    }

    Item {
      id: scrollClip
      width: root.labelWidth
      height: glyph.height
      clip: true
      anchors.verticalCenter: parent.verticalCenter

      Text {
        id: labelText
        textFormat: Text.PlainText   // titles come from the network: never rich text
        text: root.title + (root.artist ? "  ·  " + root.artist : "")
        color: root.bar.barForeground
        font.family: root.bar.fontFamily
        font.pixelSize: Style.font.body
        anchors.verticalCenter: parent.verticalCenter

        // Short titles sit still; long ones scroll inside the same fixed box.
        property bool needsScroll: implicitWidth > scrollClip.width

        NumberAnimation on x {
          running: labelText.needsScroll && root.isPlaying && root.visible && !root.bar.barHidden && !Style.reduceMotion
          onRunningChanged: if (!running) labelText.x = 0
          loops: Animation.Infinite
          duration: Style.duration(Math.max(6000, labelText.implicitWidth * 25))
          from: scrollClip.width
          to: -labelText.implicitWidth
          easing.type: Easing.Linear
        }
      }
    }
  }

  // Thin progress line under the content.
  Rectangle {
    x: root.sideMargin
    anchors.bottom: parent.bottom
    height: Style.space(2)
    radius: height / 2
    width: Math.max(0, root.contentWidth * root.progress)
    color: Commons.Color.accent
    opacity: root.isPlaying ? 0.9 : 0.45
    visible: root.shown && root.st.duration > 0
  }

  // Separator on the clock side, so the widget never reads as part of the date.
  Rectangle {
    anchors.right: parent.right
    anchors.verticalCenter: parent.verticalCenter
    width: Math.max(1, Style.space(1))
    height: parent.height * 0.5
    color: root.bar.barForeground
    opacity: 0.22
  }

  // ---- input ------------------------------------------------------------

  // The wheel can deliver many small deltas (smooth scrolling) or none at all
  // (horizontal swipes). Add them up and act once per notch, with a short
  // cooldown, so one flick is one track and no notch is swallowed.
  property real wheelAcc: 0
  property double lastWheelAction: 0
  Timer { id: wheelReset; interval: 350; onTriggered: root.wheelAcc = 0 }

  MouseArea {
    anchors.fill: parent
    hoverEnabled: true
    cursorShape: Qt.PointingHandCursor
    acceptedButtons: Qt.LeftButton | Qt.RightButton | Qt.MiddleButton

    onClicked: function(mouse) {
      if (mouse.button === Qt.MiddleButton) root.send("toggle")
      else if (mouse.button === Qt.RightButton) root.bar.run("omarchy-launch-or-focus-tui ytplay")
      else root.popupOpen = !root.popupOpen
    }
    onWheel: function(wheel) {
      var d = wheel.angleDelta.y !== 0 ? wheel.angleDelta.y : wheel.angleDelta.x
      if (d === 0) { wheel.accepted = true; return }
      // A change of direction drops what was accumulated the other way.
      if ((d > 0) !== (root.wheelAcc > 0)) root.wheelAcc = 0
      root.wheelAcc += d
      wheelReset.restart()
      var now = Date.now()
      if (Math.abs(root.wheelAcc) >= 120 && now - root.lastWheelAction > 250) {
        root.send(root.wheelAcc > 0 ? "prevtrack" : "next")
        root.wheelAcc = 0
        root.lastWheelAction = now
      }
      wheel.accepted = true
    }
    onEntered: if (root.bar && !root.popupOpen) root.bar.showTooltip(root, root.title + (root.artist ? " — " + root.artist : ""))
    onExited: if (root.bar) root.bar.hideTooltip(root)
  }

  // ---- the player popup -------------------------------------------------

  PopupCard {
    id: popup
    anchorItem: root
    bar: root.bar
    owner: root
    open: root.popupOpen && root.shown
    contentWidth: popup.fittedContentWidth(Style.space(340))
    contentHeight: popup.fittedContentHeight(column.implicitHeight)

    Column {
      id: column
      anchors.fill: parent
      spacing: Style.space(12)

      // cover + title
      Row {
        spacing: Style.space(12)
        width: parent.width

        BorderSurface {
          width: Style.space(84)
          height: Style.space(84)
          radius: Style.spacing.labelGap
          color: Style.normalFillFor(root.bar.foreground, Commons.Color.accent)
          borderSpec: Border.controlSpec("normal", root.bar.foreground, Commons.Color.accent)

          SizedImage {
            anchors.fill: parent
            anchors.margins: Style.space(2)
            crop: true
            url: root.coverUrl
            visible: url != ""
          }
        }

        Column {
          width: parent.width - Style.space(96)
          anchors.verticalCenter: parent.verticalCenter
          spacing: Style.space(4)

          Text {
            textFormat: Text.PlainText
            text: root.title
            color: root.bar.foreground
            font.family: root.bar.fontFamily
            font.pixelSize: Style.font.subtitle
            font.bold: true
            elide: Text.ElideRight
            maximumLineCount: 2
            wrapMode: Text.Wrap
            width: parent.width
          }

          Text {
            textFormat: Text.PlainText
            text: root.artist
            color: Qt.darker(root.bar.foreground, 1.3)
            font.family: root.bar.fontFamily
            font.pixelSize: Style.font.bodySmall
            elide: Text.ElideRight
            width: parent.width
            visible: text !== ""
          }
        }
      }

      // progress
      Column {
        width: parent.width
        spacing: Style.space(2)

        PanelSlider {
          width: parent.width
          bar: root.bar
          minimum: 0
          maximum: Math.max(1, root.st.duration || 1)
          step: 1
          integer: true
          value: root.st.position || 0
          fillColor: Commons.Color.accent
          onReleased: function(v) { root.send("seekto " + Math.round(v)) }
        }

        Item {
          width: parent.width
          height: timeLeft.implicitHeight

          Text {
            id: timeLeft
            textFormat: Text.PlainText
            text: root.fmt(root.st.position)
            color: Qt.darker(root.bar.foreground, 1.5)
            font.family: root.bar.fontFamily
            font.pixelSize: Style.font.caption
          }
          Text {
            textFormat: Text.PlainText
            anchors.right: parent.right
            text: root.fmt(root.st.duration)
            color: Qt.darker(root.bar.foreground, 1.5)
            font.family: root.bar.fontFamily
            font.pixelSize: Style.font.caption
          }
        }
      }

      // transport
      Row {
        anchors.horizontalCenter: parent.horizontalCenter
        spacing: Style.space(8)
        LayoutMirroring.enabled: false
        LayoutMirroring.childrenInherit: true

        Button {
          text: "prev"
          foreground: root.bar.foreground
          background: Style.normalFillFor(root.bar.foreground, Commons.Color.accent)
          horizontalPadding: Style.space(14)
          verticalPadding: Style.spacing.controlPaddingY
          onClicked: root.send("prevtrack")
        }

        Button {
          text: root.isPlaying ? "pause" : "play"
          foreground: root.bar.background
          background: Commons.Color.accent
          horizontalPadding: Style.space(22)
          verticalPadding: Style.spacing.controlPaddingY
          onClicked: root.send("toggle")
        }

        Button {
          text: "next"
          foreground: root.bar.foreground
          background: Style.normalFillFor(root.bar.foreground, Commons.Color.accent)
          horizontalPadding: Style.space(14)
          verticalPadding: Style.spacing.controlPaddingY
          onClicked: root.send("next")
        }
      }

      // toggles
      Row {
        anchors.horizontalCenter: parent.horizontalCenter
        spacing: Style.space(8)

        Button {
          text: "shuffle"
          foreground: root.st.shuffle ? Commons.Color.accent : Qt.darker(root.bar.foreground, 1.5)
          fontSize: Style.font.bodySmall
          onClicked: root.send("shuffle")
        }

        Button {
          text: root.st.repeat === "all" ? "repeat all" : (root.st.repeat === "one" ? "repeat one" : "repeat")
          foreground: root.st.repeat && root.st.repeat !== "off" ? Commons.Color.accent : Qt.darker(root.bar.foreground, 1.5)
          fontSize: Style.font.bodySmall
          onClicked: root.send("repeat")
        }
      }

      // volume
      Row {
        width: parent.width
        spacing: Style.space(8)

        Text {
          id: volLabel
          textFormat: Text.PlainText
          text: "vol"
          anchors.verticalCenter: parent.verticalCenter
          color: Qt.darker(root.bar.foreground, 1.5)
          font.family: root.bar.fontFamily
          font.pixelSize: Style.font.caption
        }

        PanelSlider {
          width: parent.width - volLabel.implicitWidth - volValue.implicitWidth - Style.space(16)
          anchors.verticalCenter: parent.verticalCenter
          bar: root.bar
          minimum: 0
          maximum: 100
          step: 5
          integer: true
          value: root.st.volume || 0
          fillColor: Commons.Color.accent
          onMoved: function(v) { root.send("volumeto " + Math.round(v)) }
        }

        Text {
          id: volValue
          textFormat: Text.PlainText
          text: (root.st.muted ? "muted" : Math.round(root.st.volume || 0) + "%")
          anchors.verticalCenter: parent.verticalCenter
          color: Qt.darker(root.bar.foreground, 1.5)
          font.family: root.bar.fontFamily
          font.pixelSize: Style.font.caption
        }
      }
    }
  }
}
