#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
send_note.py — send a single MIDI note name to MIDISock via UNIX socket
- Prints result to STDOUT: "OK" / "ERR: ..." / "SENT" (no ACK from server)
- No extra deps (standard library only)
"""

import os
import sys
import socket

# --- constants ---
SCRIPT_DIR = os.path.dirname(os.path.realpath(__file__))
SOCK_PATH  = os.path.join(SCRIPT_DIR, "midi_trigger.sock")
CONNECT_TIMEOUT = 0.5   # seconds
REPLY_TIMEOUT   = 0.4   # seconds (for optional server ACK)

def main():
    if len(sys.argv) < 2:
        # Print usage on STDOUT to keep output in one stream as requested
        print('ERR: usage: send_note.py "C#4"')
        sys.exit(1)

    note = sys.argv[1]

    # Create and connect
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(CONNECT_TIMEOUT)
    try:
        s.connect(SOCK_PATH)
    except Exception as e:
        print(f"ERR: connect failed ({e.__class__.__name__})")
        try:
            s.close()
        except Exception:
            pass
        sys.exit(2)

    # Send note (one line) and half-close write side
    try:
        s.sendall((note + "\n").encode("utf-8", "strict"))
        try:
            s.shutdown(socket.SHUT_WR)
        except Exception:
            pass
    except Exception as e:
        print(f"ERR: send failed ({e.__class__.__name__})")
        try:
            s.close()
        except Exception:
            pass
        sys.exit(3)

    # Try to read one-line reply (OK/ERR) — compatible with current server (no reply)
    s.settimeout(REPLY_TIMEOUT)
    try:
        data = s.recv(256)
    except socket.timeout:
        # No ACK from server (current design) — treat as best-effort success
        print("SENT")
        try:
            s.close()
        except Exception:
            pass
        sys.exit(0)
    except Exception as e:
        print(f"ERR: recv failed ({e.__class__.__name__})")
        try:
            s.close()
        except Exception:
            pass
        sys.exit(4)

    try:
        s.close()
    except Exception:
        pass

    line = (data or b"").decode("utf-8", "ignore").strip()
    if not line:
        # No content — behave like no-ACK
        print("SENT")
        sys.exit(0)

    low = line.lower()
    if low.startswith("ok"):
        print("OK")
        sys.exit(0)
    elif low.startswith("err"):
        # Pass through server error message
        print(line)
        sys.exit(5)
    else:
        # Unknown reply — surface as-is but succeed
        print(line)
        sys.exit(0)

if __name__ == "__main__":
    # Ensure UTF-8 stdout even under limited envs (BTT, etc.)
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    main()
