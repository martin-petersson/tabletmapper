#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Martin Petersson martin-petersson-art@proton.me>
"""
Tablet Mapper - PyQt6 application for mapping graphics tablet layouts
Supports Wacom and other xsetwacom-compatible tablets
Integrates with xrandr for screen detection and xbindkeys for keybindings
"""

import sys
import subprocess
import re
import json
import os
from dataclasses import dataclass, field, asdict
from typing import Optional
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLabel, QLineEdit, QComboBox, QGroupBox, QScrollArea,
    QSplitter, QTextEdit, QMessageBox, QDialog, QFormLayout, QSpinBox,
    QDialogButtonBox, QFrame, QSizePolicy, QTabWidget, QListWidget,
    QListWidgetItem, QCheckBox, QToolButton, QMenu
)
from PyQt6.QtCore import Qt, QRect, QPoint, QSize, pyqtSignal, QTimer
from PyQt6.QtGui import QPainter, QColor, QFont, QPen, QBrush, QFontMetrics, QAction, QKeySequence


# ─────────────────────────────────────────────
#  Data classes
# ─────────────────────────────────────────────

@dataclass
class Monitor:
    name: str        # e.g. "HDMI-1"
    x: int
    y: int
    width: int
    height: int
    primary: bool = False

    @property
    def rect(self) -> QRect:
        return QRect(self.x, self.y, self.width, self.height)

    def __str__(self):
        return f"{self.name} {self.width}x{self.height}+{self.x}+{self.y}"


TABLET_RATIO_W = 16
TABLET_RATIO_H = 10


@dataclass
class TabletMapping:
    name: str                    # user label, e.g. "All Screens"
    monitor_names: list[str]     # empty = full desktop, else specific monitors
    keybinding: str = ""         # xbindkeys keycode string
    tablet_device: str = ""      # xsetwacom device name

    # Aspect ratio correction
    aspect_correct: bool = False          # whether to apply correction
    aspect_expand: str = "width"          # "width" or "height" — which dimension to expand
    aspect_anchor: str = "top-left"       # "top-left" or "center"

    def _bounding_box(self, monitors: list[Monitor]) -> tuple[int, int, int, int]:
        """Return (x0, y0, w, h) bounding box for this mapping's target monitors."""
        if not self.monitor_names:
            if not monitors:
                return (0, 0, 0, 0)
            x0 = min(m.x for m in monitors)
            y0 = min(m.y for m in monitors)
            x1 = max(m.x + m.width for m in monitors)
            y1 = max(m.y + m.height for m in monitors)
            return (x0, y0, x1 - x0, y1 - y0)
        targets = [m for m in monitors if m.name in self.monitor_names]
        if not targets:
            return (0, 0, 0, 0)
        x0 = min(m.x for m in targets)
        y0 = min(m.y for m in targets)
        x1 = max(m.x + m.width for m in targets)
        y1 = max(m.y + m.height for m in targets)
        return (x0, y0, x1 - x0, y1 - y0)

    def _apply_aspect_correction(self, x: int, y: int, w: int, h: int) -> tuple[int, int, int, int]:
        """Expand one dimension so the geometry matches the tablet's 16:10 ratio.

        The screen pixels stay the same; the geometry rectangle grows so that
        the tablet's full physical area corresponds to the expanded region —
        meaning the stylus can reach pixels outside the mapped screen.
        """
        if self.aspect_expand == "width":
            # Derive width from height: w = h * 16/10
            new_w = round(h * TABLET_RATIO_W / TABLET_RATIO_H)
            delta = new_w - w
            if self.aspect_anchor == "center":
                x = x - delta // 2
            # anchor top-left: x stays, expansion goes rightward
            w = new_w
        else:
            # Derive height from width: h = w * 10/16
            new_h = round(w * TABLET_RATIO_H / TABLET_RATIO_W)
            delta = new_h - h
            if self.aspect_anchor == "center":
                y = y - delta // 2
            h = new_h
        return (x, y, w, h)

    def area_string(self, monitors: list[Monitor]) -> str:
        """Return xsetwacom MapToOutput geometry string (WIDTHxHEIGHT+X+Y)."""
        x, y, w, h = self._bounding_box(monitors)
        if w == 0 or h == 0:
            return ""
        if self.aspect_correct:
            x, y, w, h = self._apply_aspect_correction(x, y, w, h)
        return f"{w}x{h}+{x}+{y}"

    def corrected_rect(self, monitors: list[Monitor]) -> Optional[QRect]:
        """Return the aspect-corrected geometry as a QRect for preview drawing."""
        x, y, w, h = self._bounding_box(monitors)
        if w == 0 or h == 0:
            return None
        if self.aspect_correct:
            x, y, w, h = self._apply_aspect_correction(x, y, w, h)
        return QRect(x, y, w, h)


@dataclass
class AppConfig:
    tablet_device: str = ""
    mappings: list[TabletMapping] = field(default_factory=list)

    def to_dict(self):
        return {
            "tablet_device": self.tablet_device,
            "mappings": [asdict(m) for m in self.mappings],
        }

    @classmethod
    def from_dict(cls, d: dict) -> "AppConfig":
        cfg = cls(tablet_device=d.get("tablet_device", ""))
        for md in d.get("mappings", []):
            md.setdefault("aspect_correct", False)
            md.setdefault("aspect_expand", "width")
            md.setdefault("aspect_anchor", "top-left")
            cfg.mappings.append(TabletMapping(**md))
        return cfg


CONFIG_PATH = os.path.expanduser("~/.config/tablet_mapper.json")


# ─────────────────────────────────────────────
#  xrandr / xsetwacom helpers
# ─────────────────────────────────────────────

def parse_xrandr() -> list[Monitor]:
    """Parse connected monitors from xrandr output."""
    monitors: list[Monitor] = []
    try:
        out = subprocess.check_output(["xrandr", "--query"], text=True)
    except Exception:
        return monitors

    # Match lines like: HDMI-1 connected primary 1920x1080+0+0 ...
    pattern = re.compile(
        r"^(\S+) connected (primary )?(\d+)x(\d+)\+(\d+)\+(\d+)",
        re.MULTILINE,
    )
    for m in pattern.finditer(out):
        monitors.append(Monitor(
            name=m.group(1),
            primary=bool(m.group(2)),
            width=int(m.group(3)),
            height=int(m.group(4)),
            x=int(m.group(5)),
            y=int(m.group(6)),
        ))
    return monitors


def list_wacom_devices() -> list[str]:
    """Return list of xsetwacom device names."""
    devices = []
    try:
        out = subprocess.check_output(["xsetwacom", "--list", "devices"], text=True)
        for line in out.splitlines():
            # Format: "Wacom Intuos BT S Pen    id: 12  type: STYLUS"
            match = re.match(r"^(.+?)\s+id:\s+\d+", line)
            if match:
                devices.append(match.group(1).strip())
    except Exception:
        pass
    return devices


def apply_mapping(device: str, area: str) -> tuple[bool, str]:
    """Apply xsetwacom MapToOutput. Returns (success, message)."""
    if not device or not area:
        return False, "No device or area specified."
    try:
        subprocess.run(
            ["xsetwacom", "set", device, "MapToOutput", area],
            check=True, capture_output=True, text=True
        )
        return True, f"Mapped '{device}' → {area}"
    except subprocess.CalledProcessError as e:
        return False, e.stderr.strip()
    except FileNotFoundError:
        return False, "xsetwacom not found."


SENTINEL_BEGIN = "# >>> tablet-mapper begin <<<"
SENTINEL_END   = "# >>> tablet-mapper end <<<"


def generate_xbindkeys_config(config: AppConfig, monitors: list[Monitor]) -> str:
    """Generate xbindkeys config snippet wrapped in sentinel comments."""
    lines = [
        SENTINEL_BEGIN,
        "# Generated by tablet-mapper — do not edit this block manually.",
        "",
    ]
    for mapping in config.mappings:
        if not mapping.keybinding:
            continue
        area = mapping.area_string(monitors)
        if not area:
            continue
        device = mapping.tablet_device or config.tablet_device
        cmd = f'xsetwacom set "{device}" MapToOutput {area}'
        lines.append(f"# {mapping.name}")
        lines.append(f'"{cmd}"')
        lines.append(f"\t{mapping.keybinding}")
        lines.append("")
    lines.append(SENTINEL_END)
    return "\n".join(lines)


def upsert_xbindkeysrc(new_block: str, rc_path: str) -> str:
    """Insert or replace the tablet-mapper sentinel block in rc_path.

    - If the file doesn't exist it is created with just the block.
    - If a previous sentinel block exists it is replaced in-place.
    - Otherwise the block is appended with a blank line separator.

    Returns a short status string.
    """
    if os.path.exists(rc_path):
        with open(rc_path, "r") as f:
            original = f.read()
    else:
        original = ""

    if SENTINEL_BEGIN in original and SENTINEL_END in original:
        # Replace the existing block (including both sentinel lines)
        before = original[:original.index(SENTINEL_BEGIN)]
        after  = original[original.index(SENTINEL_END) + len(SENTINEL_END):]
        # Normalise surrounding whitespace: keep one blank line either side
        updated = before.rstrip("\n") + "\n\n" + new_block + "\n" + after.lstrip("\n")
        action = "updated"
    else:
        # Append — add blank line separator if file has content
        sep = "\n\n" if original.strip() else ""
        updated = original + sep + new_block + "\n"
        action = "written"

    with open(rc_path, "w") as f:
        f.write(updated)

    return action


# ─────────────────────────────────────────────
#  Desktop preview widget
# ─────────────────────────────────────────────

class DesktopPreview(QWidget):
    """Visual representation of monitor layout with tablet mapping overlay.

    Clicking on a monitor selects the first mapping that covers it.
    Clicking the desktop background (bounding box but outside all monitors)
    selects the first all-screens mapping.
    """

    mapping_changed = pyqtSignal()
    mapping_clicked = pyqtSignal(int)   # emits mapping index, or -1 for none

    def __init__(self, parent=None):
        super().__init__(parent)
        self.monitors: list[Monitor] = []
        self.mappings: list[TabletMapping] = []   # kept in sync by MainWindow
        self.active_mapping: Optional[TabletMapping] = None
        self.setMinimumSize(400, 220)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setCursor(Qt.CursorShape.ArrowCursor)
        self.setMouseTracking(True)

        self._colors = [
            QColor("#3a7bd5"), QColor("#e74c3c"), QColor("#2ecc71"),
            QColor("#f39c12"), QColor("#9b59b6"), QColor("#1abc9c"),
        ]

        # Cached layout geometry, recomputed in paintEvent
        self._last_desktop: QRect = QRect()
        self._last_target: QRect = QRect()

    def set_monitors(self, monitors: list[Monitor]):
        self.monitors = monitors
        self.update()

    def set_mappings(self, mappings: list[TabletMapping]):
        self.mappings = mappings

    def set_active_mapping(self, mapping: Optional[TabletMapping]):
        self.active_mapping = mapping
        self.update()

    # ── Coordinate helpers ───────────────────

    def _desktop_rect(self) -> QRect:
        if not self.monitors:
            return QRect(0, 0, 1920, 1080)
        x0 = min(m.x for m in self.monitors)
        y0 = min(m.y for m in self.monitors)
        x1 = max(m.x + m.width for m in self.monitors)
        y1 = max(m.y + m.height for m in self.monitors)
        return QRect(x0, y0, x1 - x0, y1 - y0)

    def _compute_target(self) -> QRect:
        """Return the screen-space rect the desktop is drawn into (aspect-corrected)."""
        desktop = self._desktop_rect()
        margin = 16
        target = QRect(margin, margin, self.width() - 2 * margin, self.height() - 2 * margin)
        if desktop.width() > 0 and desktop.height() > 0:
            asp = desktop.width() / desktop.height()
            if target.width() / target.height() > asp:
                new_w = int(target.height() * asp)
                target = QRect(target.x() + (target.width() - new_w) // 2,
                               target.y(), new_w, target.height())
            else:
                new_h = int(target.width() / asp)
                target = QRect(target.x(), target.y() + (target.height() - new_h) // 2,
                               target.width(), new_h)
        return target

    def _scale_rect(self, rect: QRect, desktop: QRect, target: QRect) -> QRect:
        sx = target.width() / desktop.width()
        sy = target.height() / desktop.height()
        scale = min(sx, sy)
        x = target.x() + (rect.x() - desktop.x()) * scale
        y = target.y() + (rect.y() - desktop.y()) * scale
        return QRect(int(x), int(y), int(rect.width() * scale), int(rect.height() * scale))

    def _widget_to_desktop(self, px: int, py: int) -> QPoint:
        """Convert widget pixel coordinates to desktop coordinate space."""
        desktop = self._last_desktop
        target = self._last_target
        if target.width() == 0 or target.height() == 0:
            return QPoint(0, 0)
        scale_x = desktop.width() / target.width()
        scale_y = desktop.height() / target.height()
        scale = max(scale_x, scale_y)   # inverse of min(sx,sy) used in _scale_rect
        dx = desktop.x() + (px - target.x()) * scale
        dy = desktop.y() + (py - target.y()) * scale
        return QPoint(int(dx), int(dy))

    # ── Hit testing ──────────────────────────

    def _monitor_at(self, dp: QPoint) -> Optional[Monitor]:
        """Return the monitor containing desktop point dp, or None."""
        for m in self.monitors:
            if m.rect.contains(dp):
                return m
        return None

    def _in_desktop_bounding_box(self, dp: QPoint) -> bool:
        return self._desktop_rect().contains(dp)

    def _best_mapping_for_click(self, dp: QPoint) -> int:
        """Return index of the best matching mapping for a click at desktop point dp.

        Priority:
          1. Click inside a monitor → first mapping that covers exactly that monitor
             (single-monitor mapping preferred over multi-monitor)
          2. Click in bounding box but outside all monitors → first all-screens mapping
          3. Nothing matched → -1
        """
        clicked_mon = self._monitor_at(dp)

        if clicked_mon:
            # Prefer single-monitor mappings for this monitor
            for i, m in enumerate(self.mappings):
                if m.monitor_names == [clicked_mon.name]:
                    return i
            # Fall back to any mapping that includes this monitor
            for i, m in enumerate(self.mappings):
                if clicked_mon.name in m.monitor_names:
                    return i
            # Fall back to all-screens
            for i, m in enumerate(self.mappings):
                if not m.monitor_names:
                    return i
        elif self._in_desktop_bounding_box(dp):
            # Outside monitors but inside bounding box → all-screens
            for i, m in enumerate(self.mappings):
                if not m.monitor_names:
                    return i

        return -1

    # ── Mouse events ─────────────────────────

    def mouseMoveEvent(self, event):
        if not self.monitors or not self.mappings:
            return
        dp = self._widget_to_desktop(event.pos().x(), event.pos().y())
        if self._in_desktop_bounding_box(dp):
            self.setCursor(Qt.CursorShape.PointingHandCursor)
        else:
            self.setCursor(Qt.CursorShape.ArrowCursor)

    def mousePressEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton:
            return
        if not self.monitors or not self.mappings:
            return
        dp = self._widget_to_desktop(event.pos().x(), event.pos().y())
        idx = self._best_mapping_for_click(dp)
        self.mapping_clicked.emit(idx)

    # ── Paint ────────────────────────────────

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        desktop = self._desktop_rect()
        self._last_desktop = desktop
        target = self._compute_target()
        self._last_target = target

        # Background
        painter.fillRect(target, QColor("#1a1a2e"))

        # Draw monitors
        for i, mon in enumerate(self.monitors):
            r = self._scale_rect(mon.rect, desktop, target)
            color = self._colors[i % len(self._colors)]
            painter.fillRect(r, color.darker(150))
            painter.setPen(QPen(color, 2))
            painter.drawRect(r)

            # Label
            painter.setPen(QColor("white"))
            font = painter.font()
            font.setPointSize(8)
            painter.setFont(font)
            label = f"{mon.name}\n{mon.width}×{mon.height}"
            painter.drawText(r, Qt.AlignmentFlag.AlignCenter, label)

        # Draw mapping overlay
        if self.active_mapping and self.monitors:
            targets = [m for m in self.monitors if m.name in self.active_mapping.monitor_names]
            if not targets and not self.active_mapping.monitor_names:
                targets = self.monitors  # all screens

            if targets:
                x0 = min(m.x for m in targets)
                y0 = min(m.y for m in targets)
                x1 = max(m.x + m.width for m in targets)
                y1 = max(m.y + m.height for m in targets)
                mapping_rect = QRect(x0, y0, x1 - x0, y1 - y0)
                sr = self._scale_rect(mapping_rect, desktop, target)

                # Raw mapping rect — yellow dashed
                overlay = QColor("#f1c40f")
                overlay.setAlpha(40)
                painter.fillRect(sr, overlay)
                painter.setPen(QPen(QColor("#f1c40f"), 2, Qt.PenStyle.DashLine))
                painter.drawRect(sr)

                # Aspect-corrected rect — cyan solid (only if correction enabled)
                if self.active_mapping.aspect_correct:
                    corr = self.active_mapping.corrected_rect(self.monitors)
                    if corr:
                        cr = self._scale_rect(corr, desktop, target)
                        corr_overlay = QColor("#00d4ff")
                        corr_overlay.setAlpha(35)
                        painter.fillRect(cr, corr_overlay)
                        painter.setPen(QPen(QColor("#00d4ff"), 2))
                        painter.drawRect(cr)
                        # Label on corrected rect
                        painter.setPen(QColor("#00d4ff"))
                        font = painter.font()
                        font.setPointSize(7)
                        font.setBold(True)
                        painter.setFont(font)
                        from math import gcd
                        g = gcd(corr.width(), corr.height()) if corr.height() > 0 else 1
                        label = f"Tablet: {corr.width() // g}:{corr.height() // g}  ({corr.width()}×{corr.height()})"
                        painter.drawText(cr.adjusted(4, 4, -4, -4),
                                         Qt.AlignmentFlag.AlignBottom | Qt.AlignmentFlag.AlignRight,
                                         label)
                else:
                    # Just show raw aspect ratio
                    painter.setPen(QColor("#f1c40f"))
                    font = painter.font()
                    font.setPointSize(7)
                    font.setBold(True)
                    painter.setFont(font)
                    from math import gcd
                    rw, rh = x1 - x0, y1 - y0
                    g = gcd(rw, rh) if rh > 0 else 1
                    label = f"{rw // g}:{rh // g}  ({rw}×{rh})"
                    painter.drawText(sr.adjusted(4, 4, -4, -4),
                                     Qt.AlignmentFlag.AlignBottom | Qt.AlignmentFlag.AlignRight,
                                     label)

        painter.end()


# ─────────────────────────────────────────────
#  Key capture dialog  (keycode-based)
# ─────────────────────────────────────────────

# Keys that are modifiers only — not the primary key
_MODIFIER_KEYS = {
    Qt.Key.Key_Control, Qt.Key.Key_Shift, Qt.Key.Key_Alt,
    Qt.Key.Key_Meta, Qt.Key.Key_Super_L, Qt.Key.Key_Super_R,
    Qt.Key.Key_AltGr, Qt.Key.Key_CapsLock, Qt.Key.Key_NumLock,
    Qt.Key.Key_ScrollLock, Qt.Key.Key_Hyper_L, Qt.Key.Key_Hyper_R,
}

CAPTURE_TIMEOUT_MS = 1500


class KeyCaptureDialog(QDialog):
    """Captures a key combination and stores it as an xbindkeys keycode string.

    Output format:  m:0x58 + c:10
      m: hex modifier mask   (as reported by the native X11 event)
      c: X11 keycode         (nativeScanCode from the Qt key event)

    This is the only format used — no human-readable conversion needed.
    The display label shows a friendly representation while capturing,
    but the stored string is always the keycode form.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Capture Keybinding")
        self.setFixedSize(400, 200)
        self.setWindowFlags(self.windowFlags() | Qt.WindowType.WindowStaysOnTopHint)

        self._native_mods: int = 0       # raw X11 modifier mask
        self._native_keycode: int = 0    # raw X11 keycode (scan code)
        self._display_str: str = ""      # human-friendly label (display only)
        self._captured: str = ""         # the actual xbindkeys keycode string

        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(CAPTURE_TIMEOUT_MS)
        self._timer.timeout.connect(self._on_timeout)

        layout = QVBoxLayout(self)
        layout.setSpacing(12)

        instr = QLabel("Hold down your key combination, then release…")
        instr.setAlignment(Qt.AlignmentFlag.AlignCenter)
        instr.setWordWrap(True)
        layout.addWidget(instr)

        self._combo_label = QLabel("—")
        self._combo_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        f = self._combo_label.font()
        f.setPointSize(15)
        f.setBold(True)
        self._combo_label.setFont(f)
        self._combo_label.setStyleSheet("color: #cba6f7;")
        layout.addWidget(self._combo_label)

        self._code_label = QLabel("")
        self._code_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._code_label.setStyleSheet("color: #6c7086; font-size: 11px; font-family: monospace;")
        layout.addWidget(self._code_label)

        self._countdown_label = QLabel("")
        self._countdown_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._countdown_label.setStyleSheet("color: #6c7086; font-size: 11px;")
        layout.addWidget(self._countdown_label)

        btn_row = QHBoxLayout()
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        btn_row.addWidget(cancel_btn)
        self._accept_btn = QPushButton("Use this binding")
        self._accept_btn.setEnabled(False)
        self._accept_btn.setStyleSheet("background:#2ecc71;color:#000;font-weight:bold;")
        self._accept_btn.clicked.connect(self.accept)
        btn_row.addWidget(self._accept_btn)
        layout.addLayout(btn_row)

        self._tick = QTimer(self)
        self._tick.setInterval(50)
        self._tick.timeout.connect(self._update_countdown)
        self._tick.start()

    def result_string(self) -> str:
        return self._captured

    # ── Qt key event → X11 keycode ───────────

    def keyPressEvent(self, event):
        if event.isAutoRepeat():
            return

        key = Qt.Key(event.key())
        if key in _MODIFIER_KEYS:
            # Update modifier display but don't set a keycode yet
            self._native_mods = event.nativeModifiers()
            self._update_display()
            return

        # Capture both the X11 modifier mask and keycode
        self._native_mods = event.nativeModifiers()
        self._native_keycode = event.nativeScanCode()
        self._display_str = self._build_display(event)
        self._captured = self._build_keycode_string()
        self._timer.stop()
        self._update_display()

    def keyReleaseEvent(self, event):
        if event.isAutoRepeat():
            return
        if self._captured:
            self._timer.start()

    def _build_keycode_string(self) -> str:
        """Build the xbindkeys keycode string: m:0xMM + c:CC"""
        if not self._native_keycode:
            return ""
        return f"m:0x{self._native_mods:x} + c:{self._native_keycode}"

    def _build_display(self, event) -> str:
        """Human-readable label shown during capture (not stored)."""
        _MOD_DISPLAY = [
            (Qt.KeyboardModifier.MetaModifier,    "Super"),
            (Qt.KeyboardModifier.ControlModifier, "Ctrl"),
            (Qt.KeyboardModifier.AltModifier,     "Alt"),
            (Qt.KeyboardModifier.ShiftModifier,   "Shift"),
        ]
        parts = [label for mod, label in _MOD_DISPLAY if event.modifiers() & mod]
        # Append key name
        key = Qt.Key(event.key())
        if Qt.Key.Key_0 <= key <= Qt.Key.Key_9:
            parts.append(str(key - Qt.Key.Key_0))
        elif Qt.Key.Key_A <= key <= Qt.Key.Key_Z:
            parts.append(chr(key - Qt.Key.Key_A + ord('A')))
        elif Qt.Key.Key_F1 <= key <= Qt.Key.Key_F35:
            parts.append(f"F{key - Qt.Key.Key_F1 + 1}")
        else:
            text = event.text()
            parts.append(text.upper() if text.strip() else f"key:{self._native_keycode}")
        return " + ".join(parts)

    def _update_display(self):
        self._combo_label.setText(self._display_str or "—")
        if self._captured:
            self._code_label.setText(self._captured)
        self._accept_btn.setEnabled(bool(self._captured))

    def _update_countdown(self):
        if self._timer.isActive():
            remaining = self._timer.remainingTime()
            self._countdown_label.setText(f"Accepting in {remaining / 1000:.1f}s…")
        else:
            self._countdown_label.setText(
                "Press keys, then release to confirm." if not self._captured else ""
            )

    def _on_timeout(self):
        if self._captured:
            self.accept()


# ─────────────────────────────────────────────
#  Mapping editor dialog
# ─────────────────────────────────────────────

class MappingDialog(QDialog):
    def __init__(self, monitors: list[Monitor], mapping: Optional[TabletMapping] = None,
                 devices: list[str] = None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Edit Mapping" if mapping else "Add Mapping")
        self.monitors = monitors
        self.devices = devices or []
        self.resize(420, 380)

        layout = QVBoxLayout(self)
        form = QFormLayout()

        self.name_edit = QLineEdit(mapping.name if mapping else "")
        form.addRow("Name:", self.name_edit)

        self.key_edit = QLineEdit(mapping.keybinding if mapping else "")
        self.key_edit.setPlaceholderText("e.g. m:0x58 + c:10  (use Capture button)")
        key_widget = QWidget()
        key_row = QHBoxLayout(key_widget)
        key_row.setContentsMargins(0, 0, 0, 0)
        key_row.addWidget(self.key_edit)
        capture_btn = QPushButton("⌨ Capture keybinding")
        capture_btn.setFixedWidth(165)
        capture_btn.clicked.connect(self._capture_keybinding)
        key_row.addWidget(capture_btn)
        form.addRow("Keybinding:", key_widget)

        self.device_combo = QComboBox()
        self.device_combo.addItem("(use global device)")
        self.device_combo.addItems(self.devices)
        if mapping and mapping.tablet_device:
            idx = self.device_combo.findText(mapping.tablet_device)
            if idx >= 0:
                self.device_combo.setCurrentIndex(idx)
        form.addRow("Device override:", self.device_combo)

        layout.addLayout(form)

        mon_group = QGroupBox("Monitor coverage")
        mon_layout = QVBoxLayout(mon_group)

        self.all_check = QCheckBox("All screens (full desktop)")
        mon_layout.addWidget(self.all_check)

        self.mon_checks: dict[str, QCheckBox] = {}
        for mon in monitors:
            cb = QCheckBox(f"{mon.name}  ({mon.width}×{mon.height}+{mon.x}+{mon.y})")
            if mapping and mon.name in mapping.monitor_names:
                cb.setChecked(True)
            self.mon_checks[mon.name] = cb
            mon_layout.addWidget(cb)

        if mapping and not mapping.monitor_names:
            self.all_check.setChecked(True)

        self.all_check.toggled.connect(self._all_toggled)
        layout.addWidget(mon_group)

        # ── Aspect ratio correction ──────────────
        asp_group = QGroupBox("Aspect ratio correction  (tablet is 16:10)")
        asp_layout = QVBoxLayout(asp_group)

        self.aspect_check = QCheckBox("Correct geometry to match tablet 16:10 aspect ratio")
        self.aspect_check.setChecked(mapping.aspect_correct if mapping else False)
        asp_layout.addWidget(self.aspect_check)

        asp_controls = QWidget()
        asp_ctrl_layout = QHBoxLayout(asp_controls)
        asp_ctrl_layout.setContentsMargins(0, 0, 0, 0)

        # Expand dimension toggle button
        asp_ctrl_layout.addWidget(QLabel("Expand:"))
        self._expand_val = (mapping.aspect_expand if mapping else "width")
        self.expand_btn = QPushButton()
        self.expand_btn.setFixedWidth(90)
        self.expand_btn.setCheckable(False)
        self._update_expand_btn()
        self.expand_btn.clicked.connect(self._toggle_expand)
        asp_ctrl_layout.addWidget(self.expand_btn)

        asp_ctrl_layout.addSpacing(16)

        # Anchor combo
        asp_ctrl_layout.addWidget(QLabel("Anchor:"))
        self.anchor_combo = QComboBox()
        self.anchor_combo.addItem("Top-left  (expand right/down)", "top-left")
        self.anchor_combo.addItem("Center  (expand both sides)", "center")
        anchor_val = mapping.aspect_anchor if mapping else "top-left"
        idx = self.anchor_combo.findData(anchor_val)
        if idx >= 0:
            self.anchor_combo.setCurrentIndex(idx)
        asp_ctrl_layout.addWidget(self.anchor_combo)
        asp_ctrl_layout.addStretch()

        asp_layout.addWidget(asp_controls)

        # Preview label showing computed geometry
        self._asp_preview = QLabel("")
        self._asp_preview.setStyleSheet("color: #89dceb; font-family: monospace; font-size: 11px;")
        asp_layout.addWidget(self._asp_preview)

        self.aspect_check.toggled.connect(self._update_asp_preview)
        self.expand_btn.clicked.connect(self._update_asp_preview)
        self.anchor_combo.currentIndexChanged.connect(self._update_asp_preview)
        for cb in self.mon_checks.values():
            cb.toggled.connect(self._update_asp_preview)
        self.all_check.toggled.connect(self._update_asp_preview)

        layout.addWidget(asp_group)
        self._update_asp_preview()
        self._update_asp_controls(self.aspect_check.isChecked())
        self.aspect_check.toggled.connect(self._update_asp_controls)

        btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok |
                                QDialogButtonBox.StandardButton.Cancel)
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        layout.addWidget(btns)

        self._all_toggled(self.all_check.isChecked())

    def _all_toggled(self, checked: bool):
        for cb in self.mon_checks.values():
            cb.setEnabled(not checked)

    def _toggle_expand(self):
        self._expand_val = "height" if self._expand_val == "width" else "width"
        self._update_expand_btn()
        self._update_asp_preview()

    def _update_expand_btn(self):
        if self._expand_val == "width":
            self.expand_btn.setText("↔ Width")
            self.expand_btn.setStyleSheet("background:#313244;")
        else:
            self.expand_btn.setText("↕ Height")
            self.expand_btn.setStyleSheet("background:#313244;")

    def _update_asp_controls(self, enabled: bool):
        self.expand_btn.setEnabled(enabled)
        self.anchor_combo.setEnabled(enabled)
        self._update_asp_preview()

    def _current_bounding_box(self) -> tuple[int, int, int, int]:
        """Return (x, y, w, h) bounding box based on current dialog selections."""
        if self.all_check.isChecked():
            if not self.monitors:
                return (0, 0, 0, 0)
            x0 = min(m.x for m in self.monitors)
            y0 = min(m.y for m in self.monitors)
            x1 = max(m.x + m.width for m in self.monitors)
            y1 = max(m.y + m.height for m in self.monitors)
            return (x0, y0, x1 - x0, y1 - y0)
        selected = [m for m in self.monitors if self.mon_checks.get(m.name, QCheckBox()).isChecked()]
        if not selected:
            return (0, 0, 0, 0)
        x0 = min(m.x for m in selected)
        y0 = min(m.y for m in selected)
        x1 = max(m.x + m.width for m in selected)
        y1 = max(m.y + m.height for m in selected)
        return (x0, y0, x1 - x0, y1 - y0)

    def _update_asp_preview(self):
        if not self.aspect_check.isChecked():
            self._asp_preview.setText("")
            return
        x, y, w, h = self._current_bounding_box()
        if w == 0 or h == 0:
            self._asp_preview.setText("No monitors selected.")
            return
        anchor = self.anchor_combo.currentData()
        if self._expand_val == "width":
            new_w = round(h * TABLET_RATIO_W / TABLET_RATIO_H)
            delta = new_w - w
            adj_x = x - delta // 2 if anchor == "center" else x
            self._asp_preview.setText(
                f"Raw: {w}×{h}+{x}+{y}  →  Corrected: {new_w}×{h}+{adj_x}+{y}"
                f"  (width +{delta}px)"
            )
        else:
            new_h = round(w * TABLET_RATIO_H / TABLET_RATIO_W)
            delta = new_h - h
            adj_y = y - delta // 2 if anchor == "center" else y
            self._asp_preview.setText(
                f"Raw: {w}×{h}+{x}+{y}  →  Corrected: {w}×{new_h}+{x}+{adj_y}"
                f"  (height +{delta}px)"
            )

    def _capture_keybinding(self):
        dlg = KeyCaptureDialog(self)
        if dlg.exec() and dlg.result_string():
            self.key_edit.setText(dlg.result_string())

    def get_mapping(self) -> TabletMapping:
        if self.all_check.isChecked():
            selected = []
        else:
            selected = [n for n, cb in self.mon_checks.items() if cb.isChecked()]

        device = ""
        if self.device_combo.currentIndex() > 0:
            device = self.device_combo.currentText()

        return TabletMapping(
            name=self.name_edit.text() or "Unnamed",
            monitor_names=selected,
            keybinding=self.key_edit.text().strip(),
            tablet_device=device,
            aspect_correct=self.aspect_check.isChecked(),
            aspect_expand=self._expand_val,
            aspect_anchor=self.anchor_combo.currentData(),
        )


# ─────────────────────────────────────────────
#  Main window
# ─────────────────────────────────────────────

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Tablet Mapper")
        self.resize(1000, 680)

        self.config = AppConfig()
        self.monitors: list[Monitor] = []
        self.wacom_devices: list[str] = []

        self._load_config()
        self._build_ui()
        self._refresh_monitors()
        self._refresh_devices()
        self._refresh_mapping_list()

    # ── UI construction ──────────────────────

    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(8, 8, 8, 8)

        # Toolbar row
        toolbar = QHBoxLayout()

        refresh_btn = QPushButton("⟳ Refresh Screens")
        refresh_btn.clicked.connect(self._refresh_monitors)
        toolbar.addWidget(refresh_btn)

        refresh_dev_btn = QPushButton("⟳ Refresh Devices")
        refresh_dev_btn.clicked.connect(self._refresh_devices)
        toolbar.addWidget(refresh_dev_btn)

        toolbar.addWidget(QLabel("Global device:"))
        self.device_combo = QComboBox()
        self.device_combo.setMinimumWidth(220)
        self.device_combo.currentTextChanged.connect(self._on_global_device_changed)
        toolbar.addWidget(self.device_combo)

        toolbar.addStretch()

        save_btn = QPushButton("Save Config")
        save_btn.clicked.connect(self._save_config)
        toolbar.addWidget(save_btn)

        root.addLayout(toolbar)

        # Splitter: left = preview+monitors, right = mappings+output
        splitter = QSplitter(Qt.Orientation.Horizontal)
        root.addWidget(splitter)

        # Left panel
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)

        preview_group = QGroupBox("Desktop Layout Preview")
        pg = QVBoxLayout(preview_group)
        self.preview = DesktopPreview()
        self.preview.mapping_clicked.connect(self._on_preview_clicked)
        pg.addWidget(self.preview)
        left_layout.addWidget(preview_group, 3)

        mon_group = QGroupBox("Detected Monitors (from xrandr)")
        mg = QVBoxLayout(mon_group)
        self.monitor_list = QListWidget()
        mg.addWidget(self.monitor_list)
        manual_row = QHBoxLayout()
        manual_row.addWidget(QLabel("Manual input:"))
        self.manual_xrandr = QLineEdit()
        self.manual_xrandr.setPlaceholderText("Paste xrandr output or geometry, e.g. 1920x1080+0+0")
        manual_row.addWidget(self.manual_xrandr)
        parse_btn = QPushButton("Parse")
        parse_btn.clicked.connect(self._parse_manual_xrandr)
        manual_row.addWidget(parse_btn)
        mg.addLayout(manual_row)
        left_layout.addWidget(mon_group, 2)

        splitter.addWidget(left)

        # Right panel
        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)

        tabs = QTabWidget()
        right_layout.addWidget(tabs)

        # ── Tab 1: Mappings ──
        map_widget = QWidget()
        map_layout = QVBoxLayout(map_widget)

        map_btn_row = QHBoxLayout()
        add_btn = QPushButton("+ Add Mapping")
        add_btn.clicked.connect(self._add_mapping)
        map_btn_row.addWidget(add_btn)
        edit_btn = QPushButton("✎ Edit")
        edit_btn.clicked.connect(self._edit_mapping)
        map_btn_row.addWidget(edit_btn)
        del_btn = QPushButton("✕ Delete")
        del_btn.clicked.connect(self._delete_mapping)
        map_btn_row.addWidget(del_btn)
        map_btn_row.addStretch()
        apply_btn = QPushButton("▶ Apply Selected")
        apply_btn.setStyleSheet("background:#2ecc71;color:#000;font-weight:bold;")
        apply_btn.clicked.connect(self._apply_selected)
        map_btn_row.addWidget(apply_btn)
        map_layout.addLayout(map_btn_row)

        self.mapping_list = QListWidget()
        self.mapping_list.currentRowChanged.connect(self._on_mapping_selected)
        map_layout.addWidget(self.mapping_list)

        quick_group = QGroupBox("Quick-add per-monitor mappings")
        qg = QHBoxLayout(quick_group)
        quick_add_btn = QPushButton("Auto-generate monitor mappings + All-screens")
        quick_add_btn.clicked.connect(self._quick_add_monitor_mappings)
        qg.addWidget(quick_add_btn)
        map_layout.addWidget(quick_group)

        tabs.addTab(map_widget, "Mappings")

        # ── Tab 2: xbindkeys output ──
        bind_widget = QWidget()
        bind_layout = QVBoxLayout(bind_widget)
        bind_btn_row = QHBoxLayout()
        gen_btn = QPushButton("Generate xbindkeys config")
        gen_btn.clicked.connect(self._generate_xbindkeys)
        bind_btn_row.addWidget(gen_btn)

        copy_btn = QPushButton("Copy to clipboard")
        copy_btn.clicked.connect(self._copy_xbindkeys)
        bind_btn_row.addWidget(copy_btn)

        write_btn = QPushButton("+ Apply to xbindkeys")
        write_btn.setStyleSheet("background:#2ecc71;color:#000;font-weight:bold;")
        write_btn.setToolTip(
            "Writes the generated block to ~/.xbindkeysrc (replacing any previous\n"
            "tablet-mapper block) then restarts xbindkeys."
        )
        write_btn.clicked.connect(self._write_and_reload_xbindkeys)
        bind_btn_row.addWidget(write_btn)
        bind_layout.addLayout(bind_btn_row)
        self.xbindkeys_output = QTextEdit()
        self.xbindkeys_output.setFont(QFont("Monospace", 10))
        self.xbindkeys_output.setReadOnly(False)
        bind_layout.addWidget(self.xbindkeys_output)
        tabs.addTab(bind_widget, "xbindkeys Config")

        # ── Tab 3: xsetwacom commands ──
        cmd_widget = QWidget()
        cmd_layout = QVBoxLayout(cmd_widget)
        gen_cmd_btn = QPushButton("Generate shell script")
        gen_cmd_btn.clicked.connect(self._generate_shell_script)
        cmd_layout.addWidget(gen_cmd_btn)
        self.cmd_output = QTextEdit()
        self.cmd_output.setFont(QFont("Monospace", 10))
        self.cmd_output.setReadOnly(False)
        cmd_layout.addWidget(self.cmd_output)
        tabs.addTab(cmd_widget, "Shell Commands")

        # Status bar
        self.status_label = QLabel("Ready.")
        self.status_label.setStyleSheet("color: #aaa; font-size: 11px;")
        right_layout.addWidget(self.status_label)

        splitter.addWidget(right)
        splitter.setSizes([480, 520])

    # ── Refresh ──────────────────────────────

    def _refresh_monitors(self):
        self.monitors = parse_xrandr()
        self.monitor_list.clear()
        for m in self.monitors:
            self.monitor_list.addItem(
                f"{'★ ' if m.primary else '  '}{m.name}  {m.width}×{m.height}  +{m.x}+{m.y}"
            )
        self.preview.set_monitors(self.monitors)
        n = len(self.monitors)
        self._set_status(f"Found {n} monitor{'s' if n != 1 else ''} via xrandr." if n
                         else "No monitors detected. Try manual input.")

    def _refresh_devices(self):
        self.wacom_devices = list_wacom_devices()
        self.device_combo.blockSignals(True)
        self.device_combo.clear()
        self.device_combo.addItem("(none)")
        self.device_combo.addItems(self.wacom_devices)
        if self.config.tablet_device:
            idx = self.device_combo.findText(self.config.tablet_device)
            if idx >= 0:
                self.device_combo.setCurrentIndex(idx)
        self.device_combo.blockSignals(False)
        self._set_status(f"Devices: {self.wacom_devices or ['none found']}")

    def _on_global_device_changed(self, text: str):
        self.config.tablet_device = text if text != "(none)" else ""

    # ── Manual xrandr input ──────────────────

    def _parse_manual_xrandr(self):
        text = self.manual_xrandr.text().strip()
        if not text:
            return
        # Try full xrandr output
        monitors = parse_xrandr() if not text else []
        # Also try simple geometry list
        geo_pattern = re.compile(r"(\w[\w-]*)\s+(\d+)x(\d+)\+(\d+)\+(\d+)")
        found = []
        for m in geo_pattern.finditer(text):
            found.append(Monitor(
                name=m.group(1), width=int(m.group(2)), height=int(m.group(3)),
                x=int(m.group(4)), y=int(m.group(5))
            ))
        if found:
            self.monitors = found
            self.monitor_list.clear()
            for mon in self.monitors:
                self.monitor_list.addItem(f"  {mon.name}  {mon.width}×{mon.height}  +{mon.x}+{mon.y}")
            self.preview.set_monitors(self.monitors)
            self._set_status(f"Parsed {len(found)} monitors from input.")
        else:
            self._set_status("Could not parse monitor geometry from input.")

    # ── Mappings ─────────────────────────────

    def _refresh_mapping_list(self):
        self.mapping_list.clear()
        for i, m in enumerate(self.config.mappings):
            kb = f"  [{m.keybinding}]" if m.keybinding else ""
            scr = ", ".join(m.monitor_names) if m.monitor_names else "All screens"
            self.mapping_list.addItem(f"{m.name}{kb}  →  {scr}")
        self.preview.set_mappings(self.config.mappings)

    def _on_mapping_selected(self, row: int):
        if 0 <= row < len(self.config.mappings):
            self.preview.set_active_mapping(self.config.mappings[row])
        else:
            self.preview.set_active_mapping(None)

    def _on_preview_clicked(self, idx: int):
        """Handle a click on the desktop preview — select the mapping in the list."""
        if idx < 0:
            self.mapping_list.clearSelection()
            self.preview.set_active_mapping(None)
            return
        # Block the list's signal briefly to avoid double-firing set_active_mapping
        self.mapping_list.blockSignals(True)
        self.mapping_list.setCurrentRow(idx)
        self.mapping_list.blockSignals(False)
        self.preview.set_active_mapping(self.config.mappings[idx])

    def _add_mapping(self):
        dlg = MappingDialog(self.monitors, devices=self.wacom_devices, parent=self)
        if dlg.exec():
            self.config.mappings.append(dlg.get_mapping())
            self._refresh_mapping_list()

    def _edit_mapping(self):
        row = self.mapping_list.currentRow()
        if row < 0 or row >= len(self.config.mappings):
            return
        dlg = MappingDialog(self.monitors, self.config.mappings[row],
                            self.wacom_devices, self)
        if dlg.exec():
            self.config.mappings[row] = dlg.get_mapping()
            self._refresh_mapping_list()

    def _delete_mapping(self):
        row = self.mapping_list.currentRow()
        if row < 0 or row >= len(self.config.mappings):
            return
        name = self.config.mappings[row].name
        if QMessageBox.question(self, "Delete", f"Delete mapping '{name}'?",
                                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
                                ) == QMessageBox.StandardButton.Yes:
            self.config.mappings.pop(row)
            self._refresh_mapping_list()
            self.preview.set_active_mapping(None)

    def _apply_selected(self):
        row = self.mapping_list.currentRow()
        if row < 0 or row >= len(self.config.mappings):
            self._set_status("No mapping selected.")
            return
        m = self.config.mappings[row]
        device = m.tablet_device or self.config.tablet_device
        area = m.area_string(self.monitors)
        if not device:
            self._set_status("No tablet device set.")
            return
        ok, msg = apply_mapping(device, area)
        self._set_status(("✓ " if ok else "✗ ") + msg)

    def _quick_add_monitor_mappings(self):
        if not self.monitors:
            self._set_status("No monitors detected.")
            return
        existing_names = {m.name for m in self.config.mappings}

        # All-screens mapping
        all_name = "All Screens"
        if all_name not in existing_names:
            self.config.mappings.append(TabletMapping(
                name=all_name, monitor_names=[], keybinding=""
            ))

        # Per-monitor mappings
        for mon in self.monitors:
            name = f"Monitor: {mon.name}"
            if name not in existing_names:
                self.config.mappings.append(TabletMapping(
                    name=name, monitor_names=[mon.name], keybinding=""
                ))

        self._refresh_mapping_list()
        self._set_status(
            f"Generated mappings for {len(self.monitors)} monitors + all-screens. "
            "Use the Capture button to set keybindings."
        )

    # ── Output generation ────────────────────

    def _generate_xbindkeys(self):
        text = generate_xbindkeys_config(self.config, self.monitors)
        self.xbindkeys_output.setPlainText(text)

    def _copy_xbindkeys(self):
        self._generate_xbindkeys()
        QApplication.clipboard().setText(self.xbindkeys_output.toPlainText())
        self._set_status("Copied xbindkeys config to clipboard.")

    def _write_and_reload_xbindkeys(self):
        self._generate_xbindkeys()
        block = self.xbindkeys_output.toPlainText()
        rc_path = os.path.expanduser("~/.xbindkeysrc")
        try:
            action = upsert_xbindkeysrc(block, rc_path)
            self._set_status(f"✓ tablet-mapper block {action} in {rc_path} — reloading xbindkeys…")
        except Exception as e:
            self._set_status(f"✗ Write failed: {e}")
            return
        try:
            subprocess.run(["pkill", "xbindkeys"], capture_output=True)
            subprocess.Popen(["xbindkeys"])
            self._set_status(
                f"✓ tablet-mapper block {action} in {rc_path} and xbindkeys reloaded."
            )
        except FileNotFoundError:
            self._set_status("✓ File written — but xbindkeys not found, is it installed?")
        except Exception as e:
            self._set_status(f"✓ File written — xbindkeys reload failed: {e}")

    def _generate_shell_script(self):
        lines = ["#!/bin/bash", "# Tablet mapping commands", ""]
        for m in self.config.mappings:
            device = m.tablet_device or self.config.tablet_device
            area = m.area_string(self.monitors)
            if device and area:
                lines.append(f"# {m.name}")
                lines.append(f'xsetwacom --set "{device}" MapToOutput {area}')
                lines.append("")
        self.cmd_output.setPlainText("\n".join(lines))

    # ── Config persistence ───────────────────

    def _save_config(self):
        self.config.tablet_device = (
            self.device_combo.currentText()
            if self.device_combo.currentIndex() > 0 else ""
        )
        try:
            os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
            with open(CONFIG_PATH, "w") as f:
                json.dump(self.config.to_dict(), f, indent=2)
            self._set_status(f"Config saved to {CONFIG_PATH}")
        except Exception as e:
            self._set_status(f"Save error: {e}")

    def _load_config(self):
        if os.path.exists(CONFIG_PATH):
            try:
                with open(CONFIG_PATH) as f:
                    self.config = AppConfig.from_dict(json.load(f))
            except Exception:
                pass

    def _set_status(self, msg: str):
        self.status_label.setText(msg)


# ─────────────────────────────────────────────
#  Dark stylesheet
# ─────────────────────────────────────────────

DARK_STYLE = """
QWidget { background-color: #1e1e2e; color: #cdd6f4; font-family: 'Segoe UI', sans-serif; font-size: 13px; }
QMainWindow { background-color: #1e1e2e; }
QGroupBox { border: 1px solid #45475a; border-radius: 6px; margin-top: 6px; padding-top: 8px; }
QGroupBox::title { subcontrol-origin: margin; left: 10px; color: #89b4fa; }
QPushButton { background-color: #313244; border: 1px solid #45475a; border-radius: 4px; padding: 5px 12px; }
QPushButton:hover { background-color: #45475a; }
QPushButton:pressed { background-color: #585b70; }
QLineEdit, QComboBox, QSpinBox { background-color: #181825; border: 1px solid #45475a; border-radius: 4px; padding: 4px 8px; }
QComboBox::drop-down { border: none; }
QComboBox::down-arrow { image: none; border: none; }
QListWidget { background-color: #181825; border: 1px solid #45475a; border-radius: 4px; }
QListWidget::item { padding: 4px 8px; }
QListWidget::item:selected { background-color: #45475a; color: #cba6f7; }
QListWidget::item:hover { background-color: #313244; }
QTabWidget::pane { border: 1px solid #45475a; border-radius: 4px; }
QTabBar::tab { background-color: #313244; border: 1px solid #45475a; padding: 6px 14px; margin-right: 2px; border-radius: 4px 4px 0 0; }
QTabBar::tab:selected { background-color: #45475a; color: #cba6f7; }
QTextEdit { background-color: #181825; border: 1px solid #45475a; border-radius: 4px; }
QCheckBox::indicator { width: 14px; height: 14px; border: 1px solid #45475a; border-radius: 3px; background: #181825; }
QCheckBox::indicator:checked { background: #89b4fa; }
QSplitter::handle { background: #45475a; }
QScrollBar:vertical { background: #181825; width: 8px; }
QScrollBar::handle:vertical { background: #45475a; border-radius: 4px; min-height: 20px; }
"""


# ─────────────────────────────────────────────
#  Entry point
# ─────────────────────────────────────────────

def main():
    app = QApplication(sys.argv)
    app.setStyleSheet(DARK_STYLE)
    app.setApplicationName("Tablet Mapper")
    win = MainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
    # test: remote repos are in sync
