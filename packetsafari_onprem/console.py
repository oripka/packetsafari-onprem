"""PacketSafari terminal output; Python standard library only.

Log to stderr, return data on stdout. Vendored unchanged by packetsafari-ops.
"""
from __future__ import annotations
import argparse
import getpass
import json
import os
import shutil
import sys
import threading
import time


def clean(value):
    return ''.join(c if c.isprintable() else ' ' for c in str(value))


class Console:
    def __init__(self, stream=None, *, quiet=None, json_logs=False):
        self.stream = stream if stream is not None else sys.stderr
        self.quiet = os.environ.get('PACKETSAFARI_QUIET') == '1' if quiet is None else quiet
        self.json_logs = json_logs
        self.terminal = (self.stream.isatty() and not json_logs and not os.environ.get('CI')
                         and 'NO_COLOR' not in os.environ and os.environ.get('TERM') != 'dumb')
        if os.name == 'nt' and self.terminal:
            # Enable ANSI on Windows consoles; fall back to plain text on failure.
            try:
                import ctypes
                import msvcrt
                handle = msvcrt.get_osfhandle(self.stream.fileno())
                mode = ctypes.c_ulong()
                kernel = ctypes.windll.kernel32
                self.terminal = bool(kernel.GetConsoleMode(ctypes.c_void_p(handle), ctypes.byref(mode))
                                     and kernel.SetConsoleMode(ctypes.c_void_p(handle), mode.value | 4))
            except (AttributeError, OSError, ValueError):
                self.terminal = False
        self.started = time.monotonic()
        self.lock = threading.RLock()
        self.live = False
        self.last = None

    def log(self, level, message, *, live=False):
        with self.lock:
            if self.quiet and level not in ('error', 'warn'):
                return
            message = clean(message)
            if live and message == self.last and not self.terminal:
                return
            self.last = message
            elapsed = round(time.monotonic() - self.started)
            if self.live:
                self.stream.write('\r\x1b[K')
            self.live = live and self.terminal
            if self.json_logs:
                text = json.dumps(dict(level=level, message=message, elapsed_seconds=elapsed))
            elif self.terminal:
                mark, color = {'info': ('i', 36), 'start': ('◌', 36), 'success': ('✓', 32),
                               'warn': ('!', 33), 'error': ('×', 31)}.get(level, ('i', 36))
                if live:
                    width = max(8, shutil.get_terminal_size((80, 24)).columns - 18)
                    message = message if len(message) <= width else message[:width-1] + '…'
                text = f'  \x1b[{color}m{mark}\x1b[0m  {message}  \x1b[2m{elapsed}s\x1b[0m'
            else:
                text = f'[{elapsed}s] {level}: {message}'
            self.stream.write(text + ('' if self.live else '\n'))
            self.stream.flush()

    def status(self, message):
        self.log('start', message, live=True)

    def finish(self):
        with self.lock:
            if self.live:
                self.stream.write('\n')
                self.stream.flush()
                self.live = False

    def prompt(self, label, *, secret=False):
        self.finish()
        if self.quiet or not sys.stdin.isatty() or not self.stream.isatty():
            raise ValueError(f'{label} is required; supply it as an argument')
        if secret:
            value = getpass.getpass(f'? {label}: ', stream=self.stream)
        else:
            self.stream.write(f'? {label}: ')
            self.stream.flush()
            value = sys.stdin.readline()
        if not value.strip():
            raise ValueError('Input canceled')
        return value.strip()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--quiet', action='store_true', default=os.environ.get('PACKETSAFARI_QUIET') == '1')
    parser.add_argument('--json-logs', action='store_true')
    parser.add_argument('level', choices=['info', 'start', 'success', 'warn', 'error'])
    parser.add_argument('message')
    args = parser.parse_args()
    Console(quiet=args.quiet, json_logs=args.json_logs).log(args.level, args.message)


if __name__ == '__main__':
    main()
