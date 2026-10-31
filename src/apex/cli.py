"""`apex serve` runs the collection server; `apex collect-rolls` feeds it watch files."""

import os
import socket
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Annotated
from urllib.parse import urlencode

import typer
from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.style import Style
from rich.table import Table
from rich.text import Text

from apex.config import (
    DEFAULT_PORT,
    config_path,
    default_data_dir,
    load_config,
    normalize_server_url,
    resolve_connection,
    save_config,
)

app = typer.Typer(
    name="apex",
    help="Apex buggy data tools.",
    no_args_is_help=True,
    add_completion=False,
    rich_markup_mode="rich",
    pretty_exceptions_show_locals=False,
    context_settings={"help_option_names": ["-h", "--help"]},
)
console = Console(highlight=False)
err_console = Console(stderr=True, highlight=False)

DataDir = Annotated[
    Path,
    typer.Option(
        "--data-dir",
        default_factory=default_data_dir,
        envvar="APEX_DATA_DIR",
        show_default="~/.local/share/apex",
        help="Where the server keeps its database and raw files.",
    ),
]


def fail(message: str, code: int = 1) -> typer.Exit:
    err_console.print(f"[bold red]error[/] {message}")
    return typer.Exit(code)


# ── serve ────────────────────────────────────────────────────────────────────


def lan_addresses() -> list[str]:
    """Best-effort addresses a phone on the same network can reach this machine at."""
    found: list[str] = []
    try:
        # Connecting a UDP socket sends nothing; it only picks the outbound interface.
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.254.254.254", 1))
            found.append(s.getsockname()[0])
    except OSError:
        pass
    try:
        for addr in socket.gethostbyname_ex(socket.gethostname())[2]:
            if addr not in found:
                found.append(addr)
    except OSError:
        pass
    return [a for a in found if not a.startswith("127.")]


def qr_code(data: str) -> Text:
    """A QR code drawn with half blocks, two modules per character cell.

    Colours are explicit black on white, so it scans the same on a dark terminal
    as on a light one; the quiet zone is painted too, since scanners need it.
    """
    import segno

    dark, light = "#000000", "#ffffff"
    rows = [list(r) for r in segno.make_qr(data, error="m").matrix_iter(scale=1, border=4)]
    if len(rows) % 2:
        rows.append([0] * len(rows[0]))
    text = Text(no_wrap=True, overflow="crop")
    for top, bottom in zip(rows[::2], rows[1::2], strict=True):
        for t, b in zip(top, bottom, strict=True):
            text.append("▀", Style(color=dark if t else light, bgcolor=dark if b else light))
        text.append("\n")
    text.rstrip()
    return text


@app.command()
def serve(
    password: Annotated[
        str,
        typer.Option(
            "--password",
            "-p",
            envvar="APEX_PASSWORD",
            help="Shared password for the web interface. Required: there is no open mode.",
        ),
    ],
    host: Annotated[
        str, typer.Option(help="Interface to bind. The default is reachable from phones.")
    ] = "0.0.0.0",
    port: Annotated[int, typer.Option(help="Port to listen on.")] = DEFAULT_PORT,
    qr_login: Annotated[
        bool,
        typer.Option(
            "--qr-login/--no-qr-login",
            help="Put the password in the QR code, so one scan signs a phone in.",
        ),
    ] = True,
    data_dir: DataDir = ...,
) -> None:
    """Run the collection server for phones and [bold]collect-rolls[/]."""
    import uvicorn

    from apex.server import create_app
    from apex.server.auth import KEY_PARAM, WeakPassword

    data_dir = data_dir.expanduser()
    local_url = f"http://127.0.0.1:{port}"
    try:
        web = create_app(password=password, data_dir=data_dir, advertise_url=local_url)
    except WeakPassword as exc:
        raise fail(str(exc), code=2) from exc

    if host in ("0.0.0.0", "::"):
        addresses = lan_addresses()
    else:
        addresses = [] if host.startswith("127.") or host == "localhost" else [host]
    phone_urls = [f"http://{a}:{port}" for a in addresses]

    info = Table.grid(padding=(0, 2))
    info.add_column(style="dim")
    info.add_column()
    for i, url in enumerate(phone_urls):
        info.add_row("Phones" if i == 0 else "", f"[bold]{url}[/]")
    info.add_row("This laptop", local_url)
    info.add_row("Data", str(data_dir))

    if phone_urls:
        query = f"?{urlencode({KEY_PARAM: password})}" if qr_login else ""
        caption = (
            "Scan to open and sign in. [dim](The code contains the password.)[/]"
            if qr_login
            else "Scan to open, then enter the password."
        )
        body = Group(qr_code(f"{phone_urls[0]}/{query}"), "", caption, "", info)
    else:
        body = Group(
            info,
            "",
            "[yellow]No network address found:[/] phones can't reach this laptop. "
            "Join the same Wi-Fi or hotspot, then restart.",
        )

    console.print(
        Panel(
            body,
            title="[bold]APEX[/] collection server",
            title_align="left",
            border_style="#e8574e",
            expand=False,
            padding=(1, 2),
        )
    )
    if phone_urls:
        console.print(
            f"[dim]Other machines:[/] apex config set server {phone_urls[0]}  "
            "[dim](this machine needs no setup)[/]"
        )
    console.print("[dim]Ctrl-C to stop.[/]")
    uvicorn.run(web, host=host, port=port, log_level="warning")


# ── collect-rolls ────────────────────────────────────────────────────────────

STATUS_STYLE = {
    "created": ("new", "bold green"),
    "exists": ("had", "dim"),
    "buggy_set": ("labelled", "cyan"),
    "failed": ("failed", "bold red"),
}


def _outcome_row(outcome) -> list:
    """Table cells for one file. Times and figures are the roll's, not the watch's."""
    label, style = STATUS_STYLE[outcome.status]
    cells = [Text(label, style), outcome.file.path.name]
    blank = ["", "", "", ""]
    stored = outcome.stored
    if outcome.error:
        return [*cells, *blank, Text(outcome.error, "red")]
    if stored["parse_error"]:
        return [*cells, *blank, Text("unreadable; raw file kept", "yellow")]
    if not stored["point_count"]:
        return [*cells, *blank, Text("no GPS points", "yellow")]
    if not stored["roll_start_utc"]:
        ran = _clock_duration(stored["duration_sec"])
        return [*cells, *blank, Text(f"no roll detected (watch ran {ran})", "yellow")]

    start = datetime.fromisoformat(stored["roll_start_utc"]).astimezone()
    note = Text(stored["buggy"] or "", "dim")
    if not stored["roll_finished"]:
        note = Text(f"partial: lost after {stored['roll_end_gate']}", "yellow")
    return [
        *cells,
        f"{start:%H:%M:%S}",
        _clock_duration(stored["roll_duration_sec"]),
        f"{stored['roll_dist_m']:,.0f} m",
        f"{stored['roll_max_speed_mph']:.1f} mph",
        note,
    ]


def _clock_duration(seconds: float | None) -> str:
    minutes, secs = divmod(round(seconds or 0), 60)
    return f"{minutes}:{secs:02d}"


@app.command("collect-rolls")
def collect_rolls(
    directory: Annotated[
        Path,
        typer.Argument(
            metavar="DIR",
            exists=True,
            show_default=False,
            help="Where to look, e.g. the mounted watch. Searched recursively; one .fit file works too.",
        ),
    ],
    buggy: Annotated[
        str | None,
        typer.Option("--buggy", "-b", help="Buggy the watch rode on, to help assignment later."),
    ] = None,
    no_buggy: Annotated[bool, typer.Option("--no-buggy", help="Upload without a buggy label.")] = False,
    all_days: Annotated[
        bool, typer.Option("--all", help="Include recordings from every day, not just today.")
    ] = False,
    server: Annotated[
        str | None,
        typer.Option(
            envvar="APEX_SERVER",
            help="Server URL, overriding [bold]apex config[/].",
            rich_help_panel="Server",
        ),
    ] = None,
    password: Annotated[
        str | None,
        typer.Option(
            envvar="APEX_PASSWORD",
            help="Server password, overriding [bold]apex config[/].",
            rich_help_panel="Server",
        ),
    ] = None,
    data_dir: DataDir = ...,
) -> None:
    """Send today's watch recordings to the server.

    Finds activity [bold].fit[/] files under DIR and uploads them to the server set
    with [bold]apex config[/] (or one running on this machine). Running it again is
    safe: each file is stored once, and an existing buggy label is never
    overwritten.
    """
    from apex.collect import CollectError, collect, http_uploader

    if buggy and no_buggy:
        raise typer.BadParameter("use one of -b/--buggy or --no-buggy, not both")
    if not buggy and not no_buggy:
        raise typer.BadParameter(
            "say which buggy the watch rode on with -b NAME, or pass --no-buggy",
            param_hint="'-b' / '--buggy'",
        )

    try:
        conn = resolve_connection(server, password, data_dir.expanduser())
    except ValueError as exc:
        raise fail(str(exc), code=2) from exc
    if conn is None:
        raise fail(
            "no server configured. Set one with [bold]apex config set server URL[/] "
            "and [bold]apex config set password[/].",
            code=2,
        )
    if not conn.secret:
        raise fail(
            f"no password for {conn.url}. Set it with [bold]apex config set password[/].",
            code=2,
        )
    url, secret = conn.url, conn.secret

    table = Table(box=None, padding=(0, 2, 0, 0), header_style="dim")
    for name, justify in [
        ("", "left"),
        ("file", "left"),
        ("roll start", "right"),
        ("duration", "right"),
        ("distance", "right"),
        ("max", "right"),
        ("", "left"),
    ]:
        table.add_column(name, justify=justify, no_wrap=True)

    label = f"[bold]{buggy}[/]" if buggy else "[dim]no buggy label[/]"
    console.print(f"Collecting from [bold]{directory}[/] → {url}  ·  {label}\n")
    try:
        # Rows appear as each upload finishes; the finished table is printed after.
        with Live(table, console=console, auto_refresh=False, transient=True) as live:
            report = collect(
                directory,
                buggy=buggy,
                include_all=all_days,
                upload=http_uploader(url, secret),
                on_outcome=lambda o: (table.add_row(*_outcome_row(o)), live.refresh()),
            )
    except CollectError as exc:
        raise fail(str(exc)) from exc

    if report.outcomes:
        console.print(table, "")
    created, failed = report.count("created"), report.count("failed")
    parts = [f"[bold green]{created} new[/]" if created else "0 new"]
    if had := report.count("exists", "buggy_set"):
        relabelled = report.count("buggy_set")
        parts.append(f"{had} already collected" + (f" ({relabelled} newly labelled)" if relabelled else ""))
    if failed:
        parts.append(f"[bold red]{failed} failed[/]")
    console.print("  ·  ".join(parts))

    skipped = []
    if report.other_days:
        skipped.append(f"{report.other_days} from other days (add --all to include)")
    if report.not_activity:
        skipped.append(f"{report.not_activity} non-activity files")
    if skipped:
        console.print(f"[dim]Skipped {', '.join(skipped)}.[/]")
    if failed:
        raise typer.Exit(1)


# ── config ───────────────────────────────────────────────────────────────────

config_app = typer.Typer(
    help="Show or change the server [bold]collect-rolls[/] sends data to.",
    rich_markup_mode="rich",
    context_settings={"help_option_names": ["-h", "--help"]},
)
app.add_typer(config_app, name="config")


class Key(StrEnum):
    server = "server"
    password = "password"


def _check(conn) -> bool:
    """Contact the server and print whether the settings work."""
    from apex.collect import CollectError, ping

    if not conn.secret:
        console.print("[yellow]![/] No password set, so the server can't be checked.")
        return False
    with console.status(f"Contacting {conn.url}…"):
        try:
            ping(conn.url, conn.secret)
        except CollectError as exc:
            console.print(f"[bold red]✗[/] {exc}")
            return False
    console.print(f"[bold green]✓[/] Connected to {conn.url}, password accepted.")
    return True


@config_app.callback(invoke_without_command=True)
def config_show(
    ctx: typer.Context,
    check: Annotated[
        bool, typer.Option("--check/--no-check", help="Contact the server to test the settings.")
    ] = True,
) -> None:
    """Show the settings, and check them against the server."""
    if ctx.invoked_subcommand is not None:
        return

    cfg = load_config()
    table = Table.grid(padding=(0, 2))
    table.add_column(style="dim")
    table.add_column()
    table.add_column(style="dim")
    for key, env in ((Key.server, "APEX_SERVER"), (Key.password, "APEX_PASSWORD")):
        value = cfg.get(key)
        shown = ("•" * 8 if key is Key.password else value) if value else "[dim]not set[/]"
        note = f"overridden by ${env}" if os.environ.get(env) else ""
        table.add_row(key.value, shown, note)
    console.print(f"[dim]Config[/] {config_path()}\n")
    console.print(table, "")

    conn = resolve_connection(os.environ.get("APEX_SERVER"), os.environ.get("APEX_PASSWORD"))
    if conn is None:
        console.print("No server set. Run [bold]apex config set server URL[/].")
        raise typer.Exit(1)
    if conn.source != "config":
        console.print(f"[dim]Using {conn.url} ({conn.source}).[/]")
    if check and not _check(conn):
        raise typer.Exit(1)


@config_app.command("set")
def config_set(
    key: Annotated[Key, typer.Argument(help="Setting to change.")],
    value: Annotated[
        str | None,
        typer.Argument(
            help="New value. Leave out to be prompted; a prompted password is not echoed "
            "or kept in shell history.",
            show_default=False,
        ),
    ] = None,
) -> None:
    """Change a setting, then check it against the server."""
    if value is None:
        value = typer.prompt(key.value.capitalize(), hide_input=key is Key.password)
    if key is Key.server:
        try:
            value = normalize_server_url(value)
        except ValueError as exc:
            raise typer.BadParameter(str(exc), param_hint="VALUE") from exc
    elif not value.strip():
        raise typer.BadParameter("the password may not be empty", param_hint="VALUE")

    cfg = load_config() | {key.value: value}
    path = save_config(cfg)
    shown = "•" * 8 if key is Key.password else value
    console.print(f"[bold green]✓[/] Saved {key.value} = {shown}  [dim]({path})[/]")

    if cfg.get("server") and cfg.get("password"):
        conn = resolve_connection()
        if not _check(conn):
            raise typer.Exit(1)
    elif not cfg.get("password"):
        console.print("[dim]Next:[/] apex config set password")
    else:
        console.print("[dim]Next:[/] apex config set server URL")


@config_app.command("unset")
def config_unset(key: Annotated[Key, typer.Argument(help="Setting to remove.")]) -> None:
    """Remove a setting."""
    cfg = load_config()
    if cfg.pop(key.value, None) is None:
        console.print(f"[dim]{key.value} was not set.[/]")
        return
    save_config(cfg)
    console.print(f"[bold green]✓[/] Removed {key.value}.")


def main() -> None:
    app(prog_name="apex")


if __name__ == "__main__":
    main()
