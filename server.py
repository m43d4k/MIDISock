#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MIDISock — macOS single-note MIDI relay.

Reads `config.yaml` and opens exactly one MIDI OUT (name or regex).
Runs a UNIX socket `midi_trigger.sock`; each connection sends ONE note
(e.g., "C#4") as a short Note On/Off on the configured channel.

`--list`  : print available (healed) MIDI OUT port names and exit.
`--check` : validate config and print selected port/channel, then exit.

Logs to STDERR (enable debug with MIDISOCK_DEBUG=1).
Prevents duplicate instances via socket probe.

Dependencies: python-rtmidi, rumps, PyYAML
"""

import os
import sys
import re
import time
import socket
import threading
import unicodedata

import rumps  # ← 機能は据え置き。rtmidi / yaml は遅延 import に変更

# ---------- UTF-8 stdio (robust under non-UTF locales) ----------
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# ---------- Paths ----------
SCRIPT_DIR = os.path.dirname(os.path.realpath(__file__))
SOCK_PATH = os.path.join(SCRIPT_DIR, "midi_trigger.sock")
CONFIG_PATH = os.path.join(SCRIPT_DIR, "config.yaml")

# ---------- Unified logging ----------
def _log(level: str, msg: str):
    """Write a single log line to STDERR with a unified prefix."""
    line = f"[MIDISock][{level}] {msg}\n"
    try:
        sys.stderr.write(line)
        sys.stderr.flush()
    except Exception:
        os.write(2, line.encode("utf-8", "replace"))

def _info(msg: str):  _log("INFO",  msg)
def _warn(msg: str):  _log("WARN",  msg)
def _error(msg: str): _log("ERROR", msg)

_DEBUG = bool(os.environ.get("MIDISOCK_DEBUG"))
def _debug(msg: str):
    if _DEBUG:
        _log("DEBUG", msg)

# Backward-compat shim (just in case any old calls remain)
def _stderr(msg: str):
    _error(msg)

# ---------- Note map (C-1 .. G9, sharps only) ----------
_NOTE_NAMES = ['C', 'C#', 'D', 'D#', 'E', 'F', 'F#', 'G', 'G#', 'A', 'A#', 'B']
NOTE_TO_NUM = {f"{_NOTE_NAMES[n % 12]}{(n // 12) - 1}": n for n in range(128)}

# ---------- Globals ----------
_MIDIOUT = None           # rtmidi.MidiOut (opened)
_STATUS_ON = 0x90         # set by channel
_STATUS_OFF = 0x80        # set by channel

# ---------- Utilities ----------
def _norm(s: str) -> str:
    """NFKC + casefold for language-agnostic matching."""
    return unicodedata.normalize("NFKC", s).casefold()

def _list_ports() -> list[str]:
    # lazy import (二重起動時に重いモジュール読込を避ける)
    import rtmidi
    m = rtmidi.MidiOut()
    return m.get_ports()

# ---- Mojibake healing (display only; no extra deps) ----
_BAD_HINTS = {"Â", "Ã", "Ä", "Å", "æ", "ð", "ø", "þ", "�", "„", "É", "ê", "Ç", "π"}

def _variants_from_mojibake(s: str) -> list[str]:
    """Return candidate 'fixed' variants of a mis-decoded UTF-8 string."""
    out = []
    for enc in ("cp1252", "mac_roman", "latin-1"):
        try:
            b = s.encode(enc, errors="strict")
            cand = b.decode("utf-8", errors="strict")
            if cand != s:
                out.append(cand)
        except Exception:
            pass
    # dedup, preserve order
    seen, uniq = set(), []
    for v in out:
        if v not in seen:
            seen.add(v); uniq.append(v)
    return uniq

def _looks_mojibake(s: str) -> bool:
    return any(ch in s for ch in _BAD_HINTS)

def _port_display(orig: str) -> str:
    """
    Human-friendly display string (healed if possible).
    If we can 'heal' mojibake, return the healed name; otherwise original.
    """
    if _looks_mojibake(orig):
        fixes = _variants_from_mojibake(orig)
        # Prefer CJK/Kana variant
        for f in fixes:
            if any(('\u3040' <= ch <= '\u30ff') or ('\u4e00' <= ch <= '\u9fff') for ch in f):
                return f
        if fixes:
            return fixes[0]
    return orig

def _record_for_port(orig: str) -> dict:
    """
    Build a record for matching & display.
    - 'orig' : original name (exact string used to open the port)
    - 'alts' : plausible fixed forms (mojibake healed)
    - 'nrms' : normalized strings (orig + alts) for matching
    - 'disp' : healed display
    """
    alts = _variants_from_mojibake(orig) if _looks_mojibake(orig) else []
    nrms = {_norm(orig)}
    for a in alts:
        nrms.add(_norm(a))
    return {"orig": orig, "alts": alts, "nrms": nrms, "disp": _port_display(orig)}

def _compile_regex(pat: str) -> re.Pattern:
    pat_n = unicodedata.normalize("NFKC", pat)
    return re.compile(pat_n, re.IGNORECASE)

def _filter_by_name(recs: list[dict], needle: str) -> list[dict]:
    if not needle:
        return recs
    nd = _norm(needle)
    return [r for r in recs if any(nd in n for n in r["nrms"])]

def _filter_by_regex(recs: list[dict], pattern: str) -> list[dict]:
    if not pattern:
        return recs
    rx = _compile_regex(pattern)
    return [r for r in recs if any(rx.search(n) for n in r["nrms"])]

# ---------- Config ----------
def _load_config() -> dict:
    """Load config.yaml; exit with helpful STDERR if missing or invalid."""
    # lazy import
    import yaml
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except FileNotFoundError:
        _error(f"config.yaml not found at: {CONFIG_PATH}")
        _info("Hint: create config.yaml (same folder as server.py). See config.sample.yaml.")
        _info("Available ports:")
        for i, p in enumerate(_list_ports()):
            _info(f"  {i}: {_port_display(p)}")
        sys.exit(2)
    except yaml.YAMLError as e:
        _error(f"Invalid YAML in config.yaml: {e}")
        sys.exit(2)

def _resolve_port(cfg: dict) -> tuple[str | None, list[str], list[str]]:
    """
    Returns (resolved_raw_name or None, matched_display_lines, all_display_lines).
    - Matching uses normalized (NFKC+casefold) forms of both original and healed variants.
    - Display shows healed-only names.
    """
    ports = _list_ports()
    recs = [_record_for_port(p) for p in ports]

    midi_cfg = (cfg.get("midi") or {})
    dev_cfg  = (midi_cfg.get("device") or {})
    por_cfg  = (midi_cfg.get("port") or {})

    # Optional device filter
    recs2 = list(recs)
    if isinstance(dev_cfg, dict):
        if dev_cfg.get("name"):
            recs2 = _filter_by_name(recs2, str(dev_cfg["name"]))
        elif dev_cfg.get("regex"):
            recs2 = _filter_by_regex(recs2, str(dev_cfg["regex"]))

    # Port filter (effective required)
    if isinstance(por_cfg, dict):
        if por_cfg.get("name"):
            recs2 = _filter_by_name(recs2, str(por_cfg["name"]))
        elif por_cfg.get("regex"):
            recs2 = _filter_by_regex(recs2, str(por_cfg["regex"]))

    matched = recs2
    if len(matched) == 1:
        return matched[0]["orig"], [matched[0]["disp"]], [r["disp"] for r in recs]
    return None, [r["disp"] for r in matched], [r["disp"] for r in recs]

def _channel_from_config(cfg: dict) -> int:
    midi_cfg = (cfg.get("midi") or {})
    try:
        ch = int(midi_cfg.get("channel", 1))
    except Exception:
        ch = 1
    return 1 if ch < 1 else (16 if ch > 16 else ch)

# ---------- MIDI open/close ----------
def _open_midi_out(port_name: str) -> bool:
    # lazy import
    import rtmidi
    global _MIDIOUT
    m = None
    try:
        m = rtmidi.MidiOut()
        ports = m.get_ports()
        idx = next((i for i, p in enumerate(ports) if p == port_name), None)
        if idx is None:
            return False
        m.open_port(idx)
        _MIDIOUT = m
        _debug(f'Opened MIDI OUT index={idx}')
        return True
    except Exception as e:
        _error(f"Exception while opening MIDI OUT: {e}")
        try:
            if m is not None:
                m.close_port()
        except Exception:
            pass
        _MIDIOUT = None
        return False

def _close_midi_out():
    global _MIDIOUT
    if _MIDIOUT is not None:
        try:
            _MIDIOUT.close_port()
        except Exception:
            pass
        _MIDIOUT = None

# ---------- Sending ----------
def _send_note(note_name: str):
    """Send a short Note On/Off pulse for a single note name (strict map)."""
    n = NOTE_TO_NUM.get(note_name)
    if n is None or _MIDIOUT is None:
        if n is None:
            _debug(f"Ignored token (not a note): {note_name!r}")
        return
    try:
        _MIDIOUT.send_message([_STATUS_ON, n, 127])
        time.sleep(0.05)
        _MIDIOUT.send_message([_STATUS_OFF, n, 0])
    except Exception as e:
        _warn(f"Failed to send note '{note_name}': {e}")
        _close_midi_out()

# ---------- Socket hardening ----------
def _abort_if_sock_symlink():
    """Refuse to use a symlink at SOCK_PATH (hardening)."""
    try:
        if os.path.islink(SOCK_PATH):
            _error(f"Socket path is a symlink; refusing: {SOCK_PATH}")
            sys.exit(1)
    except Exception as e:
        _error(f"Failed to check socket path: {e}")
        sys.exit(1)

# ---------- Socket helpers ----------
def _ensure_singleton_sock_or_exit():
    """Remove stale socket; exit if another instance is alive."""
    _abort_if_sock_symlink()
    if os.path.exists(SOCK_PATH):
        try:
            test = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            test.settimeout(0.2)
            test.connect(SOCK_PATH)   # alive -> another instance
            test.close()
            sys.exit(0)
        except Exception:
            try:
                os.remove(SOCK_PATH)  # stale -> remove
            except Exception:
                pass

def _socket_server():
    """UNIX socket server: single-line note name per connection."""
    _ensure_singleton_sock_or_exit()

    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        _abort_if_sock_symlink()  # double-check just before bind
        s.bind(SOCK_PATH)
        os.chmod(SOCK_PATH, 0o600)  # owner-only
    except Exception as e:
        _error(f"Failed to bind socket: {e}")
        sys.exit(1)

    s.listen(8)

    while True:
        try:
            # Blocking accept is fine for a daemon thread.
            conn, _ = s.accept()
        except Exception:
            # Do NOT exit the loop; transient errors can happen.
            time.sleep(0.05)
            continue

        with conn:
            # Drop half-open clients quickly (no data sent).
            try:
                conn.settimeout(1.0)  # seconds
            except (OSError, ValueError, TypeError) as e:
                _debug(f"conn.settimeout failed: {e}")  # silent unless MIDISOCK_DEBUG=1

            try:
                data = conn.recv(1024)
            except socket.timeout:
                # No payload within timeout → ignore this client.
                continue
            except Exception:
                continue

            if not data:
                continue

            try:
                text = data.decode("utf-8", errors="ignore").strip()
                # Single note only: first token (whitespace/commas)
                token = re.split(r"[,\s]+", text)[0] if text else ""
                if token:
                    _send_note(token)
            except Exception:
                # Ignore malformed inputs
                pass

# ---------- rumps App ----------
class MIDISockApp(rumps.App):
    def __init__(self, title="🎛 MIDISock"):
        super().__init__(title, quit_button=None)
        self.menu = ["Quit"]

    @rumps.clicked("Quit")
    def _quit(self, _):
        try:
            if os.path.exists(SOCK_PATH):
                os.remove(SOCK_PATH)
        except Exception:
            pass
        _close_midi_out()
        rumps.quit_application()

# ---------- Selection error (detailed) ----------
def _exit_with_selection_error(matched_disps: list[str], all_disps: list[str], check_mode: bool):
    if len(matched_disps) == 0:
        _error("No matching MIDI OUT port.")
    else:
        _error(f"Ambiguous MIDI OUT selection (matched {len(matched_disps)}).")
        _info("Matched:")
        for m in matched_disps:
            _info(f"  - {m}")
    _info("Hint: edit config.yaml (same folder as server.py) and set midi.device/port (name or regex).")
    _info("Available ports:")
    for i, p in enumerate(all_disps):
        _info(f"  {i}: {p}")
    sys.exit(2)

# ---------- Main ----------
def main():
    # Utility: list healed port names then exit
    if "--list" in sys.argv:
        for p in _list_ports():
            print(_port_display(p))
        sys.exit(0)

    check_mode = ("--check" in sys.argv)

    # Early singleton guard: prevent duplicate servers (skip for utility modes)
    if not check_mode:
        _ensure_singleton_sock_or_exit()

    cfg = _load_config()
    port_name, matched_disps, all_disps = _resolve_port(cfg)
    if port_name is None:
        _exit_with_selection_error(matched_disps, all_disps, check_mode)

    # Set channel & status bytes
    ch = _channel_from_config(cfg)
    global _STATUS_ON, _STATUS_OFF
    _STATUS_ON  = 0x90 + (ch - 1)
    _STATUS_OFF = 0x80 + (ch - 1)

    if check_mode:
        # Healed name only (no mojibake leak)
        print(f'OK  port="{_port_display(port_name)}"  channel={ch}')
        sys.exit(0)

    # Open MIDI OUT now (fail early)
    if not _open_midi_out(port_name):
        _error(f'Failed to open MIDI OUT port: "{_port_display(port_name)}"')
        sys.exit(1)

    # Start socket server
    t = threading.Thread(target=_socket_server, daemon=True)
    t.start()

    # Run menubar app
    MIDISockApp().run()

if __name__ == "__main__":
    main()
