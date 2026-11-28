"""File records, process locks and recoverable multi-record transactions. No database."""
import fcntl, hashlib, json, os, re, tempfile, threading, time, uuid
from collections import OrderedDict
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(os.environ.get('MATH_DATA', Path(__file__).resolve().parents[3] / 'data'))
_LOCAL = threading.RLock()

def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()

def atomic(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix='.tmp-')
    try:
        os.fchmod(fd, 0o644)
        with os.fdopen(fd, 'w') as f:
            json.dump(value, f, ensure_ascii=False, indent=2); f.flush(); os.fsync(f.fileno())
        os.replace(name, path)
        d = os.open(path.parent, os.O_RDONLY); os.fsync(d); os.close(d)
    finally:
        if os.path.exists(name): os.unlink(name)

def clone_json(value):
    """Copy only JSON containers; scalar values and dictionary keys are immutable."""
    if isinstance(value, dict): return {key: clone_json(item) for key, item in value.items()}
    if isinstance(value, list): return [clone_json(item) for item in value]
    return value

class Store:
    def __init__(self, root=ROOT):
        self.root = Path(root); self.root.mkdir(parents=True, exist_ok=True)
        self._records = OrderedDict()
        self._cache_limit = 24000
    def path(self, kind, ident):
        if not re.fullmatch(r'[a-z_]+', kind) or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', ident):
            raise ValueError('Invalid record path')
        return self.root / kind / (ident + '.json')
    def read_record(self, kind, ident, stat=None):
        path = self.path(kind, ident); key = (kind, ident)
        try: stat = stat or path.stat()
        except FileNotFoundError:
            self._records.pop(key, None); return None
        signature = (stat.st_ino, stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size)
        cached = self._records.get(key)
        if cached is not None and cached[0] == signature:
            self._records.move_to_end(key); return cached[1]
        value = json.loads(path.read_text())
        self._records[key] = (signature, value); self._records.move_to_end(key)
        while len(self._records) > self._cache_limit: self._records.popitem(last=False)
        return value

    def public_revision(self):
        try:
            stat=(self.root/'.revision').stat()
            revision=(stat.st_ino,stat.st_mtime_ns,stat.st_ctime_ns,stat.st_size)
        except FileNotFoundError:revision=None
        return revision,(self.root/'.pending.json').exists()

    @contextmanager
    def transaction(self):
        with _LOCAL, open(self.root / '.lock', 'a+') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                journal = self.root / '.pending.json'
                if journal.exists():
                    self.apply(json.loads(journal.read_text())); journal.unlink()
                tx = Transaction(self)
                yield tx
                if tx.writes:
                    payload = list(tx.writes.values())
                    atomic(journal, payload); self.apply(payload); journal.unlink()
                # Capture under the file lock; a later writer must not stamp an
                # older read snapshot with its newer revision.
                tx.committed_revision=self.public_revision()
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)
    def apply(self, writes):
        for kind, ident, value in writes: atomic(self.path(kind, ident), value)
        if writes:atomic(self.root/'.revision',uuid.uuid4().hex)

class Transaction:
    def __init__(self, store): self.store = store; self.writes = {}; self._reads = {}; self._collections = {}
    def get(self, kind, ident):
        key = (kind, ident)
        if key in self.writes: return self.writes[key][2]
        if key not in self._reads:
            self._reads[key] = self._collections[kind].get(ident) if kind in self._collections else self.store.read_record(kind, ident)
        return clone_json(self._reads[key])
    def all(self, kind):
        if kind not in self._collections:
            if not re.fullmatch(r'[a-z_]+', kind): raise ValueError('Invalid record collection')
            folder = self.store.root / kind; values = {}
            if folder.exists():
                with os.scandir(folder) as entries:
                    for entry in entries:
                        if not entry.name.endswith('.json'): continue
                        ident = entry.name[:-5]
                        value = self.store.read_record(kind, ident, entry.stat())
                        if value is not None: values[ident] = value
            self._collections[kind] = values
        values = dict(self._collections[kind])
        values.update({i: v for (k, i), (_, _, v) in self.writes.items() if k == kind})
        return clone_json(list(values.values()))
    def put(self, kind, value):
        self.store.path(kind, value['id'])
        self.writes[kind, value['id']] = (kind, value['id'], value)
    def new(self, kind, prefix, fields):
        value = dict(fields, id=prefix + uuid.uuid4().hex, created_at=time.time())
        self.put(kind, value); return value
