"""Small Consola/Clack-style terminal prompts using only the Python stdlib."""
from __future__ import annotations

import os
import select
import shutil
import sys
import termios
import textwrap
import tty
from dataclasses import dataclass


@dataclass(frozen=True)
class PromptOption:
    value: str
    label: str
    hint: str = ""
    tone: str = ""
    shortcut: str = ""


class PromptSession:
    COLORS = {"cyan": 36, "green": 32, "yellow": 33, "red": 31, "blue": 34}

    def __init__(self, input_stream=None, output_stream=None):
        self.input = input_stream or sys.stdin
        self.output = output_stream or sys.stdout
        self.fd = None
        self.previous = None

    @property
    def size(self):
        value = shutil.get_terminal_size((100, 30))
        return value.lines, value.columns

    def style(self, value, *styles):
        codes = []
        for style in styles:
            if style == "bold": codes.append("1")
            elif style == "dim": codes.append("2")
            elif style == "inverse": codes.append("7")
            elif style in self.COLORS: codes.append(str(self.COLORS[style]))
        return f"\x1b[{';'.join(codes)}m{value}\x1b[0m" if codes else str(value)

    def __enter__(self):
        if not self.input.isatty() or not self.output.isatty():
            raise ValueError("interactive prompts need an interactive terminal")
        self.fd = self.input.fileno()
        self.previous = termios.tcgetattr(self.fd)
        tty.setraw(self.fd)
        self.output.write("\x1b[?1049h\x1b[?25l")
        self.output.flush()
        return self

    def __exit__(self, *_):
        if self.fd is not None and self.previous is not None:
            termios.tcsetattr(self.fd, termios.TCSADRAIN, self.previous)
        self.output.write("\x1b[?25h\x1b[?1049l")
        self.output.flush()

    def draw(self, lines):
        height, _ = self.size
        self.output.write("\r\x1b[2J\x1b[H" + "\r\n".join(lines[:height]))
        self.output.flush()

    def completed_lines(self, completed, width):
        lines = []
        for label, value in completed:
            prefix = f"◇  {label:<16} "
            value = textwrap.shorten(str(value), width=max(8, width - len(prefix) - 1), placeholder="…")
            lines.append(self.style(prefix + value, "dim"))
        return lines

    def read_key(self, timeout=None):
        if timeout is not None and not select.select([self.fd], [], [], timeout)[0]: return ""
        first = os.read(self.fd, 1)
        if first != b"\x1b":
            try: return first.decode().lower()
            except UnicodeDecodeError: return ""
        sequence = first
        while len(sequence) < 8 and select.select([self.fd], [], [], 0.02)[0]: sequence += os.read(self.fd, 1)
        return {b"\x1b[A": "up", b"\x1b[B": "down", b"\x1b[C": "right", b"\x1b[D": "left", b"\x1b": "escape"}.get(sequence, "")

    def select(self, title, options, *, initial=0, completed=(), note="", allow_cancel=True, on_idle=None, navigation=False):
        selected = max(0, min(initial, len(options) - 1))
        while True:
            height, width = self.size
            current_completed = completed() if callable(completed) else completed
            current_note = note() if callable(note) else note
            if height < 12 or width < 56:
                self.draw([self.style("◆  " + title, "cyan", "bold"), "│", self.style("▲  Resize to at least 56 × 12", "yellow")])
                key = self.read_key(0.25 if on_idle else None)
                if not key and on_idle: on_idle()
                if key in ("q", "escape") and allow_cancel: return None
                continue
            available = max(3, height - len(current_completed) - 7)
            start = min(max(0, selected - available + 1), max(0, len(options) - available))
            lines = self.completed_lines(current_completed, width)
            lines += [self.style("◆  " + title, "cyan", "bold"), "│"]
            for offset, option in enumerate(options[start:start + available], start):
                active = offset == selected
                marker = "›" if active and navigation else " " if navigation else "●" if active else "○"
                label = textwrap.shorten(option.label, width=max(10, width - 8), placeholder="…")
                tone = "cyan" if active else option.tone or "dim"
                lines.append(self.style(f"│  {marker} {label}", tone, *(('bold',) if active else ())))
                if active and option.hint:
                    lines.append(self.style(f"│      {textwrap.shorten(option.hint, width=max(10, width - 7), placeholder='…')}", "dim"))
            if current_note:
                lines += ["│", self.style("│  " + textwrap.shorten(current_note, width=max(10, width - 5), placeholder="…"), "yellow")]
            action = "Open" if navigation else "Select"
            lines.append(self.style(f"└  ↑↓ Navigate · Enter {action}" + (" · Esc Cancel" if allow_cancel else ""), "dim"))
            self.draw(lines)
            key = self.read_key(0.25 if on_idle else None)
            if not key and on_idle: on_idle()
            elif key in ("up", "k"): selected = (selected - 1) % len(options)
            elif key in ("down", "j"): selected = (selected + 1) % len(options)
            elif key in ("\r", "\n", "right"): return options[selected].value
            elif key in ("escape", "left", "q") and allow_cancel: return None
            else:
                match = next((option for option in options if option.shortcut and key == option.shortcut.lower()), None)
                if match: return match.value
