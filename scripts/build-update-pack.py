#!/usr/bin/env python3
"""离线升级包构建脚本（09 清单条目 2）—— 在开发机/构建机上运行。

产出 aether-update-<版本>.tar.gz，内容：

    manifest.json          版本号、镜像清单、sha256 校验、变更说明
    images/aether.tar      docker save 出的 aether-app:<版本> 镜像
    upgrade.sh             客户侧升级脚本（见 scripts/upgrade.sh）

客户现场断网也能升级：把包拷到树莓派 → ./upgrade.sh <包名> 即可。

用法（仓库根目录）：
    python scripts/build-update-pack.py                # 用 version.json 的版本号
    python scripts/build-update-pack.py --notes "修复xx"
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import tarfile
import tempfile
from datetime import datetime, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
IMAGE_NAME = "aether-app"


def run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    print(f"  $ {' '.join(cmd)}")
    return subprocess.run(cmd, check=True, **kw)


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description="构建 Aether 离线升级包")
    parser.add_argument("--notes", default="", help="变更说明（写入 manifest）")
    parser.add_argument("--skip-build", action="store_true", help="跳过 docker build（镜像已存在时）")
    args = parser.parse_args()

    version = json.loads((BASE_DIR / "version.json").read_text(encoding="utf-8"))["version"]
    tag = f"{IMAGE_NAME}:{version}"
    print(f"[1/4] 构建镜像 {tag}")
    if not args.skip_build:
        run(["docker", "build", "-t", tag, str(BASE_DIR)])

    print("[2/4] docker save 导出镜像")
    tmp = Path(tempfile.mkdtemp(prefix="aether-pack-"))
    images_dir = tmp / "images"
    images_dir.mkdir()
    image_tar = images_dir / "aether.tar"
    with image_tar.open("wb") as f:
        run(["docker", "save", tag], stdout=f)

    print("[3/4] 生成 manifest（含 sha256 校验）")
    manifest = {
        "version": version,
        "min_compatible": json.loads(
            (BASE_DIR / "version.json").read_text(encoding="utf-8")
        ).get("min_compatible", version),
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "notes": args.notes,
        "images": [{"name": IMAGE_NAME, "tag": version, "file": "images/aether.tar",
                     "sha256": sha256_file(image_tar), "size_bytes": image_tar.stat().st_size}],
    }

    print("[4/4] 打 tar.gz")
    (tmp / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    # upgrade.sh 打进包里，客户侧解包即用，不依赖仓库
    (tmp / "upgrade.sh").write_bytes((BASE_DIR / "scripts" / "upgrade.sh").read_bytes())
    out = BASE_DIR / f"aether-update-{version}.tar.gz"
    with tarfile.open(out, "w:gz") as tf:
        for name in ("manifest.json", "upgrade.sh"):
            tf.add(tmp / name, arcname=name)
        tf.add(image_tar, arcname="images/aether.tar")

    # 渠道清单：与升级包一起发布到更新源（OSS/GitHub Releases 等），
    # 运维页「更新源」配置它的地址即可在线检查更新 + 一键升级
    pack_sha = sha256_file(out)
    channel = {
        "version": version,
        "min_compatible": manifest["min_compatible"],
        "notes": args.notes,
        "created_at": manifest["created_at"],
        "pack": out.name,
        "pack_sha256": pack_sha,
        "size_bytes": out.stat().st_size,
    }
    channel_file = BASE_DIR / "update-channel.json"
    channel_file.write_text(
        json.dumps(channel, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print(f"\n完成: {out} ({out.stat().st_size / 1024 / 1024:.0f} MB)")
    print(f"渠道清单: {channel_file}")
    print("发布 = 把这两个文件传到更新源；树莓派侧无需任何操作。")
    print(f"离线升级：tar xzf {out.name} && ./upgrade.sh {out.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
