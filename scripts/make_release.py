"""生成开源发布目录：只保留组件，剔除工作过程文件。

用法：
    python scripts/make_release.py           # 输出 release/img26-<版本>/
    python scripts/make_release.py --out DIR # 指定输出目录

白名单制打包（不在名单内的一律不带），确保发布目录绝不包含：
ji.md / plan.md / docs/SPEC.md / config.yaml / 日志 / 缓存 / 运行产物。
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# 允许发布的文件/目录（相对项目根）
INCLUDE_FILES = [
    "README.md",
    "LICENSE",
    "CHANGELOG.md",
    "DESIGN.md",
    ".gitignore",
    "config.example.yaml",
    "requirements.txt",
    "run.py",
    "start.cmd",
    "docs/API.md",
    "outputs/README.md",
]
INCLUDE_DIRS = ["app", "static", "tests", "scripts", "example-image"]  # scripts 排除自身后打包其余

# 目录内需要剔除的条目（任何发布目录都不带）
EXCLUDE_NAMES = {
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".git",
    "img26.log",
}


def read_version() -> str:
    text = (PROJECT_ROOT / "app" / "__init__.py").read_text(encoding="utf-8")
    for line in text.splitlines():
        if line.startswith("__version__"):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    raise SystemExit("未在 app/__init__.py 找到 __version__")


def copy_tree(src: Path, dst: Path) -> list[str]:
    """按 EXCLUDE_NAMES 复制目录树，返回相对文件清单。"""
    copied = []
    for item in src.rglob("*"):
        if any(part in EXCLUDE_NAMES for part in item.parts):
            continue
        rel = item.relative_to(src)
        target = dst / rel
        if item.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(item, target)
        copied.append(str(rel))
    return copied


def main() -> int:
    parser = argparse.ArgumentParser(description="生成开源发布目录")
    parser.add_argument("--out", default=None, help="输出目录（默认 release/img26-<版本>）")
    args = parser.parse_args()

    version = read_version()
    out = Path(args.out) if args.out else PROJECT_ROOT / "release" / f"img26-{version}"
    if out.exists():
        # 内容级替换：逐项删除内部条目（顶层目录可能被其他进程（如 shell CWD）占用，
        # 删除会失败，保留顶层目录复用即可保证内容全新）
        for child in out.iterdir():
            if child.is_dir():
                shutil.rmtree(child, ignore_errors=False)
            else:
                child.unlink()
    out.mkdir(parents=True, exist_ok=True)

    total = 0
    for rel in INCLUDE_FILES:
        src = PROJECT_ROOT / rel
        if not src.exists():
            print(f"[警告] 缺少 {rel}，跳过")
            continue
        target = out / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, target)
        total += 1

    for d in INCLUDE_DIRS:
        src = PROJECT_ROOT / d
        if not src.is_dir():
            print(f"[警告] 缺少目录 {d}，跳过")
            continue
        copied = copy_tree(src, out / d)
        total += len(copied)

    # outputs 占位（说明用途即可，不带运行产物）
    (out / "outputs" / ".gitkeep").write_text("", encoding="utf-8")

    # 自检：发布目录内不得出现工作过程文件
    forbidden = {"ji.md", "plan.md", "SPEC.md", "config.yaml", "img26.log"}
    leaked = [p.name for p in out.rglob("*") if p.name in forbidden]
    if leaked:
        print(f"[错误] 发布目录混入工作文件：{leaked}")
        shutil.rmtree(out)
        return 1

    print(f"发布目录：{out}")
    print(f"共 {total} 个文件（+ outputs/.gitkeep）")
    print("自检通过：无工作过程文件混入")
    return 0


if __name__ == "__main__":
    sys.exit(main())
