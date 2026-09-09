#!/usr/bin/env python3
"""Build a dependency-free Debian plugin package without installing it."""
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tempfile

root = Path(__file__).resolve().parents[1]
out = root / "dist"
out.mkdir(exist_ok=True)
version = json.loads((root / "manifest.json").read_text())["version"]
with tempfile.TemporaryDirectory(prefix="guardian-package-") as tmp:
    staging = Path(tmp)
    lib = staging / "usr/lib/mos-resource-guardian"
    lib.mkdir(parents=True)
    for source in (root / "mos-resource-guardian").glob("*.py"):
        shutil.copy2(source, lib / source.name)
    page = staging / "var/www/mos-plugins/plugins"
    page.mkdir(parents=True)
    for name in ("remoteEntry.js", "component.js"):
        shutil.copy2(root / "page" / name, page / name)
    shutil.copy2(root / "manifest.json", page / "manifest.json")
    for source, target in (("packaging/init", "etc/init.d/mos-resource-guardian"),
                           ("packaging/mos-resource-guardian", "usr/bin/plugins/mos-resource-guardian")):
        dest = staging / target
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(root/source, dest)
        dest.chmod(0o755)
    control = staging / "DEBIAN"
    control.mkdir()
    (control/"control").write_text(f"Package: mos-resource-guardian\nVersion: {version}\nArchitecture: all\nMaintainer: danilostorm\nDepends: python3 (>= 3.9)\nSection: admin\nPriority: optional\nDescription: MOS host resource guard with opt-in VM and container control\n")
    for script in ("postinst", "prerm", "postrm"):
        shutil.copy2(root/"packaging"/script, control/script)
        (control/script).chmod(0o755)
    dest = out / f"mos-resource-guardian_{version}_all.deb"
    subprocess.run(["dpkg-deb", "--root-owner-group", "--build", str(staging), str(dest)], check=True)
    for algorithm in ("sha256", "md5"):
        checksum = hashlib.new(algorithm, dest.read_bytes()).hexdigest()
        Path(str(dest)+"."+algorithm).write_text(f"{checksum}  {dest.name}\n")
    print(dest)
