from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from cli import app as _root_app
from core.api_keys import get_or_create_tenant, get_tenant_info, upgrade_plan

console = Console(legacy_windows=False)
app = _root_app


def get_claude_desktop_config_path(custom_path: Path | str | None = None) -> Path:
    """
    İşletim sistemine göre kullanıcının Claude Desktop (`claude_desktop_config.json`)
    dosya yolunu otomatik olarak tespit eder.
    """
    if custom_path:
        return Path(custom_path).expanduser().resolve()

    env_override = os.getenv("OWNAPI_CLAUDE_CONFIG_PATH", "").strip()
    if env_override:
        return Path(env_override).expanduser().resolve()

    if sys.platform.startswith("win"):
        appdata = os.getenv("APPDATA")
        if appdata:
            return Path(appdata) / "Claude" / "claude_desktop_config.json"
        return Path.home() / "AppData" / "Roaming" / "Claude" / "claude_desktop_config.json"
    if sys.platform == "darwin":
        return (
            Path.home()
            / "Library"
            / "Application Support"
            / "Claude"
            / "claude_desktop_config.json"
        )
    xdg_config = os.getenv("XDG_CONFIG_HOME")
    if xdg_config:
        return Path(xdg_config) / "Claude" / "claude_desktop_config.json"
    return Path.home() / ".config" / "Claude" / "claude_desktop_config.json"


def configure_claude_desktop_mcp(
    config_path: Path | str | None = None,
    server_name: str = "ownapi",
    tenant_id: str = "default",
) -> dict[str, Any]:
    """
    Claude Desktop (`claude_desktop_config.json`) dosyasını bulur (yoksa oluşturur)
    ve mevcut Python yorumlayıcısı ile `master_mcp.py` tam yolunu tek komutla bağlar.
    """
    target_config = get_claude_desktop_config_path(config_path)
    target_config.parent.mkdir(parents=True, exist_ok=True)

    existing_data: dict[str, Any] = {}
    if target_config.exists():
        try:
            loaded = json.loads(target_config.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                existing_data = loaded
        except Exception:
            existing_data = {}

    mcp_servers = existing_data.get("mcpServers")
    if not isinstance(mcp_servers, dict):
        mcp_servers = {}
        existing_data["mcpServers"] = mcp_servers

    python_exe = str(Path(sys.executable).resolve())
    master_mcp_path = str((PROJECT_ROOT / "master_mcp.py").resolve())

    tenant_record = get_or_create_tenant(tenant_id=tenant_id)
    mcp_entry = {
        "command": python_exe,
        "args": [master_mcp_path],
        "env": {
            "PYTHONIOENCODING": "utf-8",
            "OWNAPI_TENANT_ID": tenant_record["tenant_id"],
            "OWNAPI_API_KEY": tenant_record["api_key"],
            "OWNAPI_BROWSER_MODE": tenant_record["mode"],
        },
    }
    mcp_servers[server_name] = mcp_entry

    target_config.write_text(
        json.dumps(existing_data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    return {
        "config_path": str(target_config),
        "server_name": server_name,
        "python_executable": python_exe,
        "master_mcp_path": master_mcp_path,
        "tenant": tenant_record,
    }


def format_tr_number(value: int) -> str:
    """Sayıları binlik nokta ayracıyla biçimlendirir (örn. 1000 -> '1.000')."""
    return f"{int(value):,}".replace(",", ".")


@app.command("init")
def init_command(
    tenant_id: str = typer.Option(
        "default",
        "--tenant",
        "-t",
        help="Başlatılacak ve Claude Desktop'a bağlanacak kiracı ID'si (varsayılan: default)",
    ),
    server_name: str = typer.Option(
        "ownapi",
        "--name",
        "-n",
        help="claude_desktop_config.json içindeki MCP sunucu anahtar adı",
    ),
    config_path: Path | None = typer.Option(
        None,
        "--config",
        "-c",
        help="Özel claude_desktop_config.json dosya yolu (verilmezse otomatik bulunur)",
    ),
) -> None:
    """
    Tek komutla Claude Desktop (`claude_desktop_config.json`) dosyasını otomatik bulup
    mevcut Python yorumlayıcısını ve `master_mcp.py` yolunu bağlar.
    """
    result = configure_claude_desktop_mcp(
        config_path=config_path,
        server_name=server_name,
        tenant_id=tenant_id,
    )
    tenant = result["tenant"]
    credits_str = format_tr_number(tenant.get("credits", 1000))

    out_console = Console(file=sys.stdout, legacy_windows=False)
    out_console.print(
        Panel.fit(
            f"[bold green]✔ OwnAPI Claude Desktop MCP Entegrasyonu Tamamlandı![/bold green]\n\n"
            f"[bold cyan]📂 Claude Config Dosyası:[/bold cyan] [underline]{result['config_path']}[/underline]\n"
            f"[bold cyan]🐍 Python Yorumlayıcısı:[/bold cyan] [white]{result['python_executable']}[/white]\n"
            f"[bold cyan]🔌 Master MCP Sunucusu:[/bold cyan]  [white]{result['master_mcp_path']}[/white]\n"
            f"[bold cyan]🔑 API Anahtarı:[/bold cyan]         [bold yellow]{tenant['api_key']}[/bold yellow]\n"
            f"[bold cyan]📦 Aktif Paket / Mod:[/bold cyan]    [magenta]{tenant['plan']}[/magenta] ([green]mode=\"{tenant['mode']}\"[/green])\n"
            f"[bold cyan]💳 Kalan Kredi:[/bold cyan]          [bold green]{credits_str} kredi[/bold green]",
            title="🚀 OwnAPI Init — Tek Komutla Kurulum",
            border_style="green",
        )
    )


@app.command("status")
def status_command(
    tenant_id_or_key: str = typer.Option(
        "default",
        "--tenant",
        "-t",
        help="Durumu görüntülenecek kiracı ID'si veya sk-ownapi-... anahtarı",
    ),
    upgrade: str | None = typer.Option(
        None,
        "--upgrade",
        "-u",
        help="İsteğe bağlı paket yükseltme ('starter_cloud' veya 'growth_cloud')",
    ),
) -> None:
    """
    Kullanıcının kalan kredisini (`1.000` kredi), paketini (`free` / `starter_cloud` / `growth_cloud`),
    çalışma modunu (`local` / `cloud`) ve `sk-ownapi-...` anahtarını terminalde şık bir kutu içinde gösterir.
    """
    if upgrade:
        info = get_tenant_info(tenant_id_or_key)
        upgrade_plan(info["tenant_id"], plan=upgrade)
        tenant_record = get_tenant_info(info["tenant_id"])
    else:
        tenant_record = get_tenant_info(tenant_id_or_key)

    credits_fmt = format_tr_number(tenant_record.get("credits", 0))
    consumed_fmt = format_tr_number(tenant_record.get("total_consumed", 0))
    plan_name = str(tenant_record.get("plan", "free"))
    plan_label = str(tenant_record.get("plan_label", plan_name))
    mode_name = str(tenant_record.get("mode", "local"))
    api_key = str(tenant_record.get("api_key", ""))
    price_usd = int(tenant_record.get("price_usd", 0))

    table = Table(show_header=False, box=None, pad_edge=False)
    table.add_column("Alan", style="bold cyan", width=22)
    table.add_column("Değer", style="white")

    table.add_row("👤 Kiracı (Tenant)", f"[bold white]{tenant_record.get('tenant_id', 'default')}[/bold white]")
    table.add_row("📦 Aktif Paket", f"[bold magenta]{plan_name}[/bold magenta] — {plan_label} ([yellow]${price_usd}[/yellow])")
    table.add_row("⚙️  Çalışma Modu", f"[bold green]{mode_name}[/bold green]")
    table.add_row("💳 Kalan Kredi", f"[bold green]{credits_fmt} kredi[/bold green]")
    table.add_row("📊 Harcanan Kredi", f"[dim]{consumed_fmt} kredi[/dim]")
    table.add_row("🔑 API Anahtarı", f"[bold yellow]{api_key}[/bold yellow]")

    out_console = Console(file=sys.stdout, legacy_windows=False)
    out_console.print(
        Panel.fit(
            table,
            title="💎 OwnAPI Hesap & Kredi Durumu (ownapi status)",
            border_style="cyan",
        )
    )


@app.command("start")
def start_command(
    host: str = typer.Option("127.0.0.1", "--host", "-h", help="Yerel sunucu dinleme adresi"),
    port: int = typer.Option(8000, "--port", "-p", help="Yerel sunucu port numarası (varsayılan: 8000)"),
    tenant_id: str = typer.Option("default", "--tenant", "-t", help="Aktif kiracı ID'si"),
    open_browser: bool = typer.Option(
        True,
        "--open-browser/--no-open-browser",
        help="Sunucu başladığında Web Dashboard'u varsayılan tarayıcıda otomatik aç",
    ),
    init_mcp: bool = typer.Option(
        True,
        "--init-mcp/--no-init-mcp",
        help="Başlangıçta Claude Desktop MCP bağlantısını otomatik doğrula/güncelle",
    ),
    reload: bool = typer.Option(False, "--reload", help="Geliştirici otomatik yenileme modu"),
    check_only: bool = typer.Option(
        False,
        "--check",
        help="Sunucuyu bloklamadan yapılandırmayı ve masaüstü köprüsünü doğrulayıp çık (test amaçlı)",
    ),
) -> None:
    """
    Tek komutla OwnAPI Yerel Sunucusunu (Web Dashboard & REST API), Tarayıcı Köprüsünü
    ve Claude Desktop MCP bağlantısını başlatır (`ownapi start`).
    """
    import threading
    import webbrowser

    from server import run_server

    tenant_record = get_or_create_tenant(tenant_id=tenant_id)
    mcp_status_line = "[dim]Atlandı (--no-init-mcp)[/dim]"
    if init_mcp:
        try:
            mcp_res = configure_claude_desktop_mcp(tenant_id=tenant_id)
            mcp_status_line = f"[bold green]✔ Bağlı[/bold green] ([dim]{mcp_res['config_path']}[/dim])"
        except Exception as exc:
            mcp_status_line = f"[yellow]⚠ Uyarı ({exc})[/yellow]"

    dashboard_url = f"http://{host}:{port}"
    credits_fmt = format_tr_number(tenant_record.get("credits", 1000))

    out_console = Console(file=sys.stdout, legacy_windows=False)
    out_console.print(
        Panel.fit(
            f"[bold green]🟢 Bağlı — Bilgisayarın ve Tarayıcı Köprün Hazır![/bold green]\n\n"
            f"[bold cyan]🌐 Web Dashboard & SaaS Panel:[/bold cyan] [bold underline white]{dashboard_url}[/bold underline white]\n"
            f"[bold cyan]📚 OpenAPI / Swagger Docs:[/bold cyan]     [underline]{dashboard_url}/docs[/underline]\n"
            f"[bold cyan]👤 Aktif Kiracı / Paket:[/bold cyan]       [bold white]{tenant_record['tenant_id']}[/bold white] / [magenta]{tenant_record['plan']}[/magenta] ([green]mode=\"{tenant_record['mode']}\"[/green])\n"
            f"[bold cyan]💳 Kalan Kredi:[/bold cyan]                [bold green]{credits_fmt} kredi[/bold green]\n"
            f"[bold cyan]🔑 API Anahtarı:[/bold cyan]               [bold yellow]{tenant_record['api_key']}[/bold yellow]\n"
            f"[bold cyan]🔌 Claude Desktop MCP:[/bold cyan]         {mcp_status_line}",
            title="🚀 OwnAPI Desktop Bridge & Local Server (ownapi start)",
            border_style="green",
        )
    )

    if check_only:
        return

    if open_browser and not os.getenv("OWNAPI_DISABLE_AUTO_BROWSER"):
        def _launch_browser() -> None:
            try:
                webbrowser.open(dashboard_url)
            except Exception:
                pass

        timer = threading.Timer(1.2, _launch_browser)
        timer.daemon = True
        timer.start()

    run_server(host=host, port=port, reload=reload)


def main() -> None:
    """`ownapi` ve `own-api` terminal komutlarının ana giriş noktası."""
    app()


if __name__ == "__main__":
    main()
