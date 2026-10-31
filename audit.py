"""Append-only JSON-lines audit log: decisions and their inputs, orders, fills, closes, mode and
config changes. One object per line with a UTC timestamp, written with fsync, owner-only.

Tamper evidence: every line carries ``seq`` and ``prev`` (SHA-256 of the previous line), so any
edited, removed or reordered line breaks the chain; ``verify`` checks it. Lines written before
the chain existed have no ``seq`` and are skipped by the check.

Secrets never reach the file: keys that name credentials are redacted and any string that looks
like a PEM key is replaced, recursively, before writing."""
import hashlib
import json
import os
import threading
from datetime import datetime, timezone

REDACTED = '[REDACTED]'
SECRET_KEYS = {'pem', 'private_key', 'key_id', 'api_key', 'secret', 'password', 'passphrase', 'token', 'signature',
               'authorization', 'credentials', 'kalshi-access-key', 'kalshi-access-signature', 'kalshi-access-timestamp'}


def scrub(value):
    if isinstance(value, dict):
        return {k: REDACTED if str(k).lower() in SECRET_KEYS else scrub(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [scrub(v) for v in value]
    if isinstance(value, str) and ('-----BEGIN' in value or 'PRIVATE KEY' in value):
        return REDACTED
    return value


def _digest(line):
    return hashlib.sha256(line.encode()).hexdigest()


def _last_line(path):
    with open(path, 'rb') as f:
        f.seek(0, os.SEEK_END)
        end = f.tell()
        f.seek(max(0, end - 65536))
        lines = [x for x in f.read().decode(errors='replace').splitlines() if x.strip()]
    return lines[-1] if lines else None


class TradeLog:
    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()
        self.seq, self.prev = 0, None
        self.failures, self.last_error = 0, None     # write failures never raise: they must not discard a fill
        self.healthy = True                          # the last write succeeded (gates live entries)
        if path and os.path.exists(path):
            last = _last_line(path)
            if last:
                self.prev = _digest(last)
                try:
                    self.seq = int(json.loads(last).get('seq', 0))
                except (ValueError, AttributeError):
                    pass

    def write(self, kind, **fields):
        if not self.path:
            return
        with self.lock:
            try:
                row = dict(ts=datetime.now(timezone.utc).isoformat(timespec='milliseconds'), event=kind,
                           seq=self.seq + 1, prev=self.prev, **scrub(fields))
                line = json.dumps(row, default=str)
                fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
                with os.fdopen(fd, 'a') as f:
                    f.write(line + '\n')
                    f.flush()
                    os.fsync(f.fileno())
            except (OSError, TypeError, ValueError) as e:
                self.failures += 1
                self.last_error = type(e).__name__
                self.healthy = False
                return
            self.healthy = True
            self.seq += 1
            self.prev = _digest(line)


def verify(path):
    """(ok, problem) for the hash chain in ``path``; problem names the first broken line."""
    prev_line = None
    expected_seq = None
    with open(path) as f:
        for no, line in enumerate((x.rstrip('\n') for x in f), 1):
            if not line.strip():
                continue
            row = json.loads(line)
            if 'seq' in row:
                if row.get('prev') != (_digest(prev_line) if prev_line is not None else None):
                    return False, f'line {no}: previous-line hash mismatch'
                if expected_seq is not None and row['seq'] != expected_seq:
                    return False, f'line {no}: sequence {row["seq"]}, expected {expected_seq}'
                expected_seq = row['seq'] + 1
            prev_line = line
    return True, None
