from __future__ import annotations

import urllib.request
from dataclasses import dataclass
from pathlib import Path

PROBE_URL = "https://ipv4.webshare.io/"
DIRECT = "direct"


@dataclass(frozen=True, slots=True)
class Proxy:
    address: str
    port: int

    @property
    def endpoint(self) -> str:
        return f"{self.address}:{self.port}"


def marker_for(proxy: Proxy | None) -> str:
    return proxy.endpoint if proxy else DIRECT


def _config_dir(terminal_path: str) -> Path:
    return Path(terminal_path).parent / "config"


def startup_ini_path(terminal_path: str) -> Path:
    return _config_dir(terminal_path) / "mt5api-start.ini"


def marker_path(terminal_path: str) -> Path:
    return _config_dir(terminal_path) / "mt5api-proxy.txt"


def write_startup_ini(terminal_path: str, login: int, password: str, server: str, proxy: Proxy | None) -> Path:
    lines = ["[Common]", f"Login={login}", f"Password={password}", f"Server={server}"]
    if proxy is not None:
        lines += ["ProxyEnable=1", "ProxyType=2", f"ProxyAddress={proxy.endpoint}"]
    path = startup_ini_path(terminal_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-16")
    return path


def remove_startup_ini(terminal_path: str) -> None:
    startup_ini_path(terminal_path).unlink(missing_ok=True)


def read_marker(terminal_path: str) -> str | None:
    try:
        return marker_path(terminal_path).read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def write_marker(terminal_path: str, proxy: Proxy | None) -> None:
    path = marker_path(terminal_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(marker_for(proxy) + "\n", encoding="utf-8")


def probe(proxy: Proxy, timeout_seconds: float) -> bool:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({"https": f"http://{proxy.endpoint}"}))
    try:
        with opener.open(PROBE_URL, timeout=timeout_seconds) as response:
            return response.status == 200
    except Exception:
        return False
