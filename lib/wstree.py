"""The implementer's tree, as devflow compares it: a map of path → content hash and executable bit.

A library for work.py (the snapshot it hands over) and deliver.py (what changed since).
"""
import hashlib, os

SKIP_DIRS = {".git", "__pycache__", ".pytest_cache", ".claude"}


def sha1_file(p):
    h = hashlib.sha1()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def tree_map(root):
    out = {}
    for d, dirs, files in os.walk(root):
        dirs[:] = [x for x in dirs if x not in SKIP_DIRS]
        for f in files:
            p = os.path.join(d, f)
            rel = os.path.relpath(p, root).replace(os.sep, "/")
            out[rel] = {"sha1": sha1_file(p), "x": os.access(p, os.X_OK)}
    return out
