"""Read-only source-install acceptance, before a CLI launch can repair it.

The driver owns this file; probes import only the installed tree's passive
readers. No bootstrap, installer, build, PM worker, or application entry point.

"""
from __future__ import annotations

import argparse
from html.parser import HTMLParser
import json
import os
from pathlib import Path, PurePosixPath
import struct
import subprocess
import sys
from urllib.parse import urlsplit


def desktop_outputs(root: Path) -> list[Path]:
    desktop = root / "apps/desktop"
    return [path for pattern in (
        "release/*/resources/app.asar.unpacked/dist",
        "release/mac*/Hermes.app/Contents/Resources/app.asar.unpacked/dist",
    ) for path in desktop.glob(pattern)]


def desktop_resources(root: Path) -> list[Path]:
    desktop = root / "apps/desktop"
    return [path for pattern in (
        "release/*/resources",
        "release/mac*/Hermes.app/Contents/Resources",
    ) for path in desktop.glob(pattern) if path.is_dir()]


class _ModuleScripts(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.sources: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "script":
            return
        values = {key.lower(): value for key, value in attrs}
        if (values.get("type") or "").lower() == "module" and values.get("src"):
            self.sources.append(values["src"] or "")


def _legacy_asar_has_renderer(resources: Path) -> bool:
    archive = resources / "app.asar"
    try:
        archive_size = archive.stat().st_size
        with archive.open("rb") as stream:
            size, header_size, payload_size, json_size = struct.unpack("<4I", stream.read(16))
            if (size != 4 or header_size != payload_size + 4
                    or payload_size != 4 + ((json_size + 3) // 4) * 4
                    or not 0 < json_size <= 64 * 1024 * 1024
                    or 8 + header_size > archive_size):
                raise ValueError("invalid ASAR header")
            header = json.loads(stream.read(json_size))

            def read_member(name: str) -> bytes:
                path = PurePosixPath(name)
                if not name or path.is_absolute() or ".." in path.parts or "\\" in name or ":" in name:
                    raise ValueError("invalid ASAR entry path")
                node = header
                for part in path.parts:
                    node = node["files"][part]
                length = node["size"]
                if type(length) is not int or length <= 0 or node.get("unpacked"):
                    raise ValueError(f"invalid packed ASAR entry: {name}")
                raw_offset = node["offset"]
                if type(raw_offset) is int:
                    offset = raw_offset
                elif (isinstance(raw_offset, str) and raw_offset.isascii()
                      and raw_offset.isdigit()):
                    offset = int(raw_offset)
                else:
                    raise ValueError(f"invalid packed ASAR offset: {name}")
                if offset < 0 or 8 + header_size + offset + length > archive_size:
                    raise ValueError(f"truncated ASAR entry: {name}")
                stream.seek(8 + header_size + offset)
                data = stream.read(length)
                if len(data) != length or not data.strip():
                    raise ValueError(f"incomplete ASAR entry: {name}")
                return data

            package = json.loads(read_member("package.json"))
            main = package["main"]
            if not isinstance(main, str):
                raise ValueError("invalid packaged main entry")
            read_member(main)
            html = read_member("dist/index.html").decode("utf-8-sig")
            parser = _ModuleScripts()
            parser.feed(html)
            if not parser.sources:
                raise ValueError("renderer has no local module entry")
            for source in parser.sources:
                parsed = urlsplit(source)
                if parsed.scheme or parsed.netloc or source.startswith("//"):
                    raise ValueError("renderer module entry is not local")
                relative = PurePosixPath(parsed.path)
                if relative.is_absolute() or ".." in relative.parts:
                    raise ValueError("invalid renderer module path")
                read_member(str(PurePosixPath("dist") / relative))
        return True
    except (OSError, UnicodeError, ValueError, KeyError, TypeError, struct.error, json.JSONDecodeError):
        return False


def verify_products(root: Path, desktop: str, node: Path | None = None) -> None:
    app = root / "apps/desktop"
    outputs = desktop_outputs(root)
    has_desktop = (app / "dist/index.html").exists() or any((app / "release").glob("*"))
    if desktop == "absent" and has_desktop:
        raise RuntimeError("unexpected desktop output in a no-desktop scenario")
    if desktop == "present":
        unpacked_renderer = any((out / "index.html").is_file() for out in outputs)
        legacy_renderer = node is None and any(_legacy_asar_has_renderer(path) for path in desktop_resources(root))
        if not unpacked_renderer and not legacy_renderer:
            raise RuntimeError("desktop packaged renderer is missing or incomplete")
        executables = [path for pattern in (
            "release/*/Hermes.exe", "release/*/Hermes", "release/*/hermes",
            "release/mac*/Hermes.app/Contents/MacOS/Hermes",
        ) for path in app.glob(pattern) if path.is_file() and path.stat().st_size]
        if not executables:
            raise RuntimeError("desktop executable is missing or empty")
    if node is None:
        return  # Historical builds have no compiler receipt contract.
    products = [("tui", root / "ui-tui/dist"), ("web", root / "hermes_cli/web_dist")]
    if desktop == "present":
        products.extend(("desktop", out) for out in outputs)
    for product, out in products:
        result = subprocess.run(
            [str(node), str(root / "scripts/build/freshness.mjs"),
             "--source", str(root), "--product", product, "--out", str(out)],
            cwd=root, capture_output=True, text=True, check=True, timeout=120,
        )
        if result.stdout.strip() != "true":
            raise RuntimeError(f"{product} output is missing, stale, or damaged: {out}")


def probe_pm(root: Path, desktop: str, command: list[str]) -> None:
    # Use the launcher's Python ABI, but deliberately do NOT execute its
    # bootstrap: that would complete dependencies or recover markers for it.
    sys.path.insert(0, str(root))
    from hermes_cli._launchers import runtime_command
    from pm.environments import selected_venv, site_packages

    if command != runtime_command(root):
        raise RuntimeError("published launcher belongs to another installation or Python")
    if any((root / marker).exists() for marker in (".update-incomplete", ".lazy-refresh-incomplete")):
        raise RuntimeError("source update left an incomplete marker; refusing launch-time recovery")
    sys.path.insert(1, str(site_packages(selected_venv(root))))
    from pm.install import installed_package, venv_is_current

    if not venv_is_current(project_root=root):
        raise RuntimeError("installed dependency generation is not current")
    node = installed_package("node")
    if node is None or node.binary is None:
        raise RuntimeError("installed Node is missing or does not match the target pin")
    verify_products(root, desktop, node.binary)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--launcher", type=Path, required=True)
    parser.add_argument("--desktop", choices=("absent", "present"), required=True)
    parser.add_argument("--runtime-command", help=argparse.SUPPRESS)
    args = parser.parse_args()
    root = args.root.resolve()
    if args.runtime_command:
        probe_pm(root, args.desktop, json.loads(args.runtime_command))
    elif (root / "pm/lock.json").is_file():
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", HERMES_DISABLE_LAZY_INSTALLS="1")
        result = subprocess.run([str(args.launcher), "--print-runtime-command"],
                                capture_output=True, text=True, check=True, env=env, timeout=30)
        command = json.loads(result.stdout)
        if not isinstance(command, list) or not command or not all(isinstance(s, str) for s in command):
            raise RuntimeError("invalid published runtime command")
        subprocess.run([command[0], "-I", "-B", str(Path(__file__).resolve()),
                        "--root", str(root), "--launcher", str(args.launcher),
                        "--desktop", args.desktop, "--runtime-command", json.dumps(command)],
                       env=env, check=True, timeout=300)
    else:
        verify_products(root, args.desktop)
        print("Historical install: artifact presence only (no PM/compiler receipt capability)")
    print(f"Read-only source verification passed: {root} (desktop={args.desktop})")


if __name__ == "__main__":
    main()