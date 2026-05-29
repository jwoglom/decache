#!/usr/bin/env python3
"""Test the index-driven asset recovery ported from read_cache.bat.

A cached file whose URL matches an `asset_data.txt` term must be copied straight
into Verified as `[N]filename` (no ffmpeg/verification), with its ip-redacted URL
recorded in Verified/contents.txt and its origin in bin/private_locations.txt.
This is the behavior the first cut of the port was missing.
"""
import os
import shutil
import struct
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

SIMPLE_FILE_MAGIC = 0xFCFB6D1BA7725C30
SIMPLE_EOF_MAGIC = 0xF4FA6F45970D41D8


def simple_entry(url: bytes, body: bytes) -> bytes:
    header = struct.pack("<QIII", SIMPLE_FILE_MAGIC, 5, len(url), 0)
    eof = struct.pack("<QIIQ", SIMPLE_EOF_MAGIC, 0, 0, len(body))
    return header + url + body + eof


def first_asset_term() -> str:
    """Grab a concrete domain/path term from the real asset_data.txt."""
    path = os.path.join(ROOT, "bin", "data", "asset_data.txt")
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            t = line.strip()
            if ("." in t or "/" in t) and " " not in t and len(t) > 6:
                return t
    return ""


def main() -> int:
    term = first_asset_term()
    if not term:
        print("SKIP: no asset_data.txt term available")
        return 0
    print("using asset term:", term)

    work = tempfile.mkdtemp(prefix="decache_recover_")
    target = os.path.join(work, "drive")
    cache_dir = os.path.join(target, "Cache_Data")
    os.makedirs(cache_dir)

    # A cached asset whose URL contains the asset term + an ip param to redact.
    url = f"http://{term}/game.swf?ip=203.0.113.7&sig=abc".encode()
    body = b"CWS\x0f" + b"\x00" * 200  # pretend-SWF payload
    with open(os.path.join(cache_dir, "0123456789abcdef_0"), "wb") as fh:
        fh.write(simple_entry(url, body))

    for d in ("Verified", "Unverified"):
        shutil.rmtree(os.path.join(ROOT, d), ignore_errors=True)
    if os.path.exists(os.path.join(ROOT, "Assets.zip")):
        os.remove(os.path.join(ROOT, "Assets.zip"))

    out = subprocess.run([sys.executable, os.path.join(ROOT, "decache.py"), target, "-v"],
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT).stdout.decode()

    verified_dir = os.path.join(ROOT, "Verified")
    verified = os.listdir(verified_dir) if os.path.isdir(verified_dir) else []
    print("Verified:", verified)

    ok = True
    assets = [f for f in verified if f.startswith("[") and "]" in f]
    if not assets:
        print("FAIL: expected an indexed asset '[N]...' in Verified")
        print(out)
        ok = False
    else:
        print("PASS: recovered indexed asset", assets)

    contents = os.path.join(verified_dir, "contents.txt")
    if os.path.exists(contents):
        text = open(contents).read()
        if "REDACTED" in text and "203.0.113.7" not in text:
            print("PASS: contents.txt records URL with ip REDACTED")
        else:
            print("FAIL: contents.txt missing/!redacted:", text.strip())
            ok = False
    else:
        print("FAIL: Verified/contents.txt not written")
        ok = False

    priv = os.path.join(ROOT, "bin", "private_locations.txt")
    if os.path.exists(priv):
        print("PASS: bin/private_locations.txt written")
    else:
        print("FAIL: private_locations.txt not written")
        ok = False

    # cleanup
    for d in ("Verified", "Unverified"):
        shutil.rmtree(os.path.join(ROOT, d), ignore_errors=True)
    for f in ("Assets.zip",):
        p = os.path.join(ROOT, f)
        if os.path.exists(p):
            os.remove(p)
    if os.path.exists(priv):
        os.remove(priv)
    shutil.rmtree(work, ignore_errors=True)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
