"""剪贴板里的图片存成文件（终端里没法直接粘贴图片，只能这样转一道）。

- macOS：osascript 把剪贴板转成 PNG 写文件（截图 cmd+ctrl+shift+4 就在剪贴板里）；
- Linux：wl-paste（Wayland）或 xclip（X11），都没装就不支持；
- Windows：PowerShell 的 System.Windows.Forms.Clipboard。

剪贴板里不是图片时返回 None。只用标准库（subprocess），pi-tui 保持零依赖。
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path

Run = Callable[..., subprocess.CompletedProcess]


def _mac(path: Path, run: Run) -> bool:
    script = [
        "-e", f'set f to (open for access POSIX file "{path}" with write permission)',
        "-e", "try",
        "-e", "write (the clipboard as «class PNGf») to f",
        "-e", "end try",
        "-e", "close access f",
    ]
    run(["osascript", *script], capture_output=True, timeout=5)
    return path.exists() and path.stat().st_size > 0


def _linux(path: Path, run: Run) -> bool:
    if shutil.which("wl-paste"):
        types = run(["wl-paste", "--list-types"], capture_output=True, text=True, timeout=5).stdout
        if "image/png" not in types:
            return False
        data = run(["wl-paste", "--type", "image/png"], capture_output=True, timeout=5).stdout
    elif shutil.which("xclip"):
        targets = run(["xclip", "-selection", "clipboard", "-t", "TARGETS", "-o"],
                      capture_output=True, text=True, timeout=5).stdout
        if "image/png" not in targets:
            return False
        data = run(["xclip", "-selection", "clipboard", "-t", "image/png", "-o"],
                   capture_output=True, timeout=5).stdout
    else:
        return False
    if not data:
        return False
    path.write_bytes(data)
    return True


def _windows(path: Path, run: Run) -> bool:
    script = ("Add-Type -AssemblyName System.Windows.Forms; Add-Type -AssemblyName System.Drawing; "
              "$img = [System.Windows.Forms.Clipboard]::GetImage(); "
              f"if ($img) {{ $img.Save('{path}', [System.Drawing.Imaging.ImageFormat]::Png) }}")
    run(["powershell", "-NoProfile", "-STA", "-Command", script], capture_output=True, timeout=10)
    return path.exists() and path.stat().st_size > 0


def save_clipboard_image(directory: str | Path, run: Run = subprocess.run,
                         platform: str | None = None) -> Path | None:
    """剪贴板里有图片就存成 directory 下的 PNG 并返回路径，没有返回 None"""
    folder = Path(directory).expanduser()
    folder.mkdir(parents=True, exist_ok=True)
    stem = f"clipboard-{time.strftime('%Y%m%d-%H%M%S')}"
    path, n = folder / f"{stem}.png", 1
    while path.exists():            # 同一秒粘了好几张
        n += 1
        path = folder / f"{stem}-{n}.png"
    platform = platform or sys.platform
    handler = _mac if platform == "darwin" else _windows if platform == "win32" else _linux
    try:
        ok = handler(path, run)
    except (OSError, subprocess.SubprocessError):
        ok = False
    if not ok:
        path.unlink(missing_ok=True)
        return None
    return path
