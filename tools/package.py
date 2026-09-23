#!/usr/bin/env python3
# Author: zhekui. Reproducible source/evidence archives with safe extraction audit.
# main: package explicit source folders or verified results; reject symlinks;
# archive: normalize tar metadata and verify fresh extraction and every file hash.
from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
from pathlib import Path
import sys
import tarfile
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from cloud_joint.evidence import source_identity


def archive(files, target):
    target = Path(target).resolve()
    if target.exists() or target.with_suffix(target.suffix + ".sha256").exists():
        raise ValueError("refusing to overwrite a delivery")
    target.parent.mkdir(parents=True, exist_ok=True)
    manifest = {name: hashlib.sha256(data).hexdigest() for name, data in files.items()}
    files["FILE_MANIFEST.json"] = (json.dumps(manifest, indent=2) + "\n").encode()
    with target.open("xb") as raw:
        with gzip.GzipFile(fileobj=raw, mode="wb", filename="", mtime=0) as gz:
            with tarfile.open(fileobj=gz, mode="w") as tar:
                for name, data in sorted(files.items()):
                    if name.startswith("/") or ".." in Path(name).parts:
                        raise ValueError("unsafe archive name")
                    info = tarfile.TarInfo(name)
                    info.size, info.mode, info.mtime = len(data), 0o644, 0
                    tar.addfile(info, io.BytesIO(data))
    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    target.with_suffix(target.suffix + ".sha256").write_text(f"{digest}  {target.name}\n")
    with tempfile.TemporaryDirectory(prefix="joint-ci-unpack-") as folder:
        destination = Path(folder).resolve()
        with tarfile.open(target, "r:gz") as tar:
            for member in tar.getmembers():
                if not member.isfile() or not (destination / member.name).resolve().is_relative_to(destination):
                    raise ValueError("unsafe archive entry")
                path = destination / member.name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(tar.extractfile(member).read())
        for name, expected in manifest.items():
            if hashlib.sha256((destination / name).read_bytes()).hexdigest() != expected:
                raise ValueError("extraction mismatch: " + name)
    print(json.dumps(dict(path=str(target), sha256=digest, files=len(files), reextract_verified=True)))


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--out", required=True)
    p.add_argument("--evidence", help="verified result directory instead of source")
    args = p.parse_args()
    files = {}
    if args.evidence:
        from acceptance import verify
        root = Path(args.evidence).resolve()
        verify(root)
        for path in root.rglob("*"):
            if path.is_symlink():
                raise ValueError("symlink in evidence")
            if path.is_file():
                files[str(path.relative_to(root))] = path.read_bytes()
    else:
        identity, hashes = source_identity(ROOT)
        for name in hashes:
            path = ROOT / name
            if path.is_symlink():
                raise ValueError("symlink in source")
            files[name] = path.read_bytes()
        files["SOURCE_MANIFEST.json"] = (json.dumps(dict(schema=1, source_id=identity,
            upstream_repository="zhekui-hub/joint-ci-demo-arsenal",
            upstream_base="366349c5a6fbde1779bd62e8c1f387fd84a9ee12",
            implementation="additive cloud_joint experiment; legacy shell not activated",
            files=hashes), indent=2) + "\n").encode()
    archive(files, args.out)


if __name__ == "__main__":
    main()
