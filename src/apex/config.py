"""User settings, where server data lives, and how CLI tools find a server.

Two separate things live here:

* The user config (`apex config`): which server to talk to and its password.
  A TOML file under `~/.config/apex/`, readable only by its owner because it
  holds the password.
* The server-info file a running `apex serve` leaves in its data dir, so tools
  on the same machine can reach it with no configuration at all.
"""

import json
import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

DEFAULT_PORT = 8000
SERVER_INFO_FILE = "server.json"
CONFIG_KEYS = ("server", "password")


def config_path() -> Path:
    """`$APEX_CONFIG`, else `$XDG_CONFIG_HOME/apex/config.toml`, else `~/.config/apex/config.toml`."""
    if env := os.environ.get("APEX_CONFIG"):
        return Path(env).expanduser()
    xdg = os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config"
    return Path(xdg) / "apex" / "config.toml"


def load_config() -> dict[str, str]:
    """Known keys from the config file; a missing file is an empty config."""
    try:
        with config_path().open("rb") as f:
            raw = tomllib.load(f)
    except FileNotFoundError:
        return {}
    return {k: str(raw[k]) for k in CONFIG_KEYS if k in raw}


def save_config(values: dict[str, str]) -> Path:
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    # json.dumps of a str is also a valid TOML basic string.
    lines = [f"{k} = {json.dumps(values[k])}" for k in CONFIG_KEYS if values.get(k)]
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write("# Written by `apex config`.\n" + "\n".join(lines) + "\n")
    os.chmod(path, 0o600)  # in case the file already existed with looser permissions
    return path


def normalize_server_url(url: str) -> str:
    """`192.168.1.20:8000` → `http://192.168.1.20:8000`; trailing slashes dropped."""
    url = url.strip().rstrip("/")
    if "://" not in url:
        url = f"http://{url}"
    if not url.startswith(("http://", "https://")):
        raise ValueError(f"not an http(s) URL: {url}")
    return url


def default_data_dir() -> Path:
    """`$APEX_DATA_DIR`, else `$XDG_DATA_HOME/apex`, else `~/.local/share/apex`."""
    if env := os.environ.get("APEX_DATA_DIR"):
        return Path(env).expanduser()
    xdg = os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share"
    return Path(xdg) / "apex"


def write_server_info(data_dir: Path, url: str, token: str) -> Path:
    """Record the running server's URL and a bearer token for local CLI use.

    Readable only by the owner: the token grants the same access as the password.
    """
    data_dir.mkdir(parents=True, exist_ok=True)
    path = data_dir / SERVER_INFO_FILE
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump({"url": url, "token": token, "pid": os.getpid()}, f)
    return path


def read_server_info(data_dir: Path) -> dict | None:
    try:
        return json.loads((data_dir / SERVER_INFO_FILE).read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def clear_server_info(data_dir: Path) -> None:
    """Remove the info file, but only if this process wrote it."""
    info = read_server_info(data_dir)
    if info and info.get("pid") == os.getpid():
        (data_dir / SERVER_INFO_FILE).unlink(missing_ok=True)


@dataclass
class Connection:
    url: str
    secret: str | None
    source: str  # where the URL came from, for display


def resolve_connection(
    server: str | None = None, password: str | None = None, data_dir: Path | None = None
) -> Connection | None:
    """Which server to use, and with what secret.

    Precedence: explicit arguments (flags or env vars), then the config file,
    then a server running on this machine, which also supplies its local token.
    """
    cfg = load_config()
    info = read_server_info(data_dir or default_data_dir())
    if server:
        url, source = normalize_server_url(server), "--server / $APEX_SERVER"
    elif cfg.get("server"):
        url, source = cfg["server"], "config"
    elif info:
        url, source = info["url"], "server running on this machine"
    else:
        return None

    secret = password or cfg.get("password")
    if not secret and info and info["url"] == url:
        secret = info["token"]
    return Connection(url, secret, source)
