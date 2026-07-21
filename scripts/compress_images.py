"""压缩 docs/images 下的截图 → WebP，大幅减小 README 加载体积。

原图都是 1.3-1.6 MB 的 PNG，README 一次加载 11 张 ~14MB，非常慢。
本脚本缩放到最大宽 1280px + 转 WebP quality 82，单张约 80-200 KB。
"""
from pathlib import Path
from PIL import Image

SRC_DIR = Path("docs/images")
MAX_WIDTH = 1280
QUALITY = 82

total_before = 0
total_after = 0

for png in sorted(SRC_DIR.glob("*.png")):
    webp_path = png.with_suffix(".webp")
    if webp_path.exists() and webp_path.stat().st_mtime >= png.stat().st_mtime:
        print(f"skip  {png.name}  (webp up to date)")
        continue

    img = Image.open(png).convert("RGB")
    if img.width > MAX_WIDTH:
        new_h = round(img.height * MAX_WIDTH / img.width)
        img = img.resize((MAX_WIDTH, new_h), Image.LANCZOS)

    img.save(webp_path, "WEBP", quality=QUALITY, method=6)

    before = png.stat().st_size
    after = webp_path.stat().st_size
    total_before += before
    total_after += after
    ratio = (1 - after / before) * 100
    print(f"ok    {png.name:24s}  {before/1024:6.0f} KB → {after/1024:5.0f} KB  (-{ratio:.0f}%)")

print()
print(f"总计  {total_before/1024/1024:.1f} MB → {total_after/1024/1024:.2f} MB  "
      f"(压缩 {100 - total_after/total_before*100:.0f}%)")
