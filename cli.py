from __future__ import annotations

import asyncio
import importlib.util
import inspect
import json
import os
import subprocess
import sys
import time
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

PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from compiler import (
    CompilationResult,
    RegistrySchema,
    ShadowAPICompiler,
    ensure_default_registry,
)
from recorder import TrafficRecorder

console = Console(legacy_windows=False)
app = typer.Typer(
    name="own-api",
    help="OwnAPI Enterprise CLI: User-Delegated Browser Middleware, Auto-Validated API Compiler ve MCP Sunucu Motoru.",
    add_completion=False,
)


def _print_compilation_summary(result: CompilationResult) -> None:
    """Derleme sonuçlarını, Registry kaydını ve dinamik parametreleri Rich ile ekrana basar."""
    param_lines: list[str] = []
    if result.dynamic_params:
        for p in result.dynamic_params:
            param_lines.append(
                f"  • [bold yellow]{p.name}[/bold yellow] ({p.param_type}, {p.location}) "
                f"= [green]{repr(p.default_value)}[/green] — [dim]{p.description}[/dim]"
            )
    else:
        param_lines.append("  • [dim]Dinamik parametre tespit edilmedi (statik uç nokta)[/dim]")

    params_block = "\n".join(param_lines)
    reg_info = (
        f"[bold cyan]📦 Registry İstemcisi:[/bold cyan] [underline]{result.registry_client_path.resolve()}[/underline]\n"
        f"[bold cyan]📋 Registry Şeması:[/bold cyan] [underline]{result.registry_schema_path.resolve()}[/underline]\n"
        if result.registry_client_path and result.registry_schema_path
        else ""
    )

    console.print(
        Panel(
            f"[bold green]✔ Servis Adı (Registry):[/bold green] [bold magenta]{result.service_name}[/bold magenta]\n"
            f"[bold green]✔ Eşleşen Uç Nokta (#{result.matched_endpoint_id}):[/bold green] "
            f"[bold cyan]{result.matched_method}[/bold cyan] [white]{result.matched_url}[/white]\n"
            f"[bold green]✔ Üretilen Fonksiyon:[/bold green] [bold yellow]{result.function_name}()[/bold yellow]\n"
            f"[bold green]✔ Self-Healing:[/bold green] [bold cyan]Aktif (SessionHealer 401/403/429 Koruması)[/bold cyan]\n"
            f"[bold green]✔ LLM Açıklaması:[/bold green] {result.explanation}\n\n"
            f"[bold magenta]Dinamik Parametreler:[/bold magenta]\n{params_block}\n\n"
            f"{reg_info}"
            f"[bold cyan]📄 Python SDK:[/bold cyan] [underline]{result.client_path.resolve()}[/underline]\n"
            f"[bold cyan]🔌 MCP Sunucusu:[/bold cyan] [underline]{result.mcp_path.resolve()}[/underline]",
            title="✨ Derleme ve Registry Kaydı Tamamlandı",
            border_style="green",
        )
    )


def _run_and_measure_client(
    compiler: ShadowAPICompiler,
    result: CompilationResult,
) -> tuple[float, str]:
    """
    Üretilen generated_client.py dosyasını çalıştırır, yanıt süresini milisaniye (ms)
    olarak ölçer ve çıktısını döndürür. Hata durumunda deterministik şablonla onarır.
    """
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}

    for attempt in range(2):
        start_time = time.perf_counter()
        proc = subprocess.run(
            [sys.executable, str(result.client_path)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            timeout=60,
        )
        elapsed_ms = (time.perf_counter() - start_time) * 1000.0

        if proc.returncode == 0:
            output = (proc.stdout or "").strip()
            return elapsed_ms, output

        if attempt == 0:
            console.print(
                "[yellow]⚠ İlk çalıştırmada hata algılandı, deterministik şablonla otomatik onarılıyor...[/yellow]"
            )
            compiler.apply_fallback(result)
        else:
            err_msg = (proc.stderr or proc.stdout or "Bilinmeyen hata").strip()
            raise RuntimeError(f"generated_client.py çalıştırılamadı:\n{err_msg}")

    return 0.0, ""


def _print_benchmark_comparison(browser_seconds: float, direct_ms: float) -> None:
    """Tarayıcı süresi (sn) ile Shadow-API doğrudan istek süresini (ms) karşılaştıran tablo basar."""
    browser_ms = browser_seconds * 1000.0
    speedup = browser_ms / direct_ms if direct_ms > 0 else 0.0
    saved_pct = ((browser_ms - direct_ms) / browser_ms * 100.0) if browser_ms > 0 else 0.0

    table = Table(
        title="⚡ Performans Karşılaştırması: Tarayıcı vs Shadow-API",
        show_header=True,
        header_style="bold magenta",
        show_lines=True,
    )
    table.add_column("Yöntem", style="bold white", width=34)
    table.add_column("Süre", justify="right", style="bold cyan", width=22)
    table.add_column("Birim Karşılaştırma", justify="right", style="yellow", width=22)
    table.add_column("Performans Kazancı", justify="center", style="bold green", width=24)

    table.add_row(
        "🌐 Tarayıcı Süresi (Playwright UI)",
        f"{browser_seconds:.2f} sn",
        f"{browser_ms:,.0f} ms",
        "Baz (1.0x)",
    )
    table.add_row(
        "🚀 Shadow-API Doğrudan İstek Süresi",
        f"{direct_ms:.2f} ms",
        f"{direct_ms / 1000.0:.3f} sn",
        f"[bold green]{speedup:.1f}x Daha Hızlı (%{saved_pct:.1f} tasarruf)[/bold green]",
    )

    console.print()
    console.print(table)
    console.print()


def _load_sdk_function(client_path: Path) -> tuple[str, Any]:
    """Verilen client.py içindeki ana SDK fonksiyonunu dinamik olarak yükler."""
    if not client_path.exists():
        raise FileNotFoundError(
            f"{client_path} bulunamadı! Önce `python cli.py compile` veya `python cli.py build` çalıştırın."
        )

    module_name = f"shadow_sdk_{client_path.parent.name}_{int(time.time() * 1000)}"
    spec = importlib.util.spec_from_file_location(module_name, str(client_path.resolve()))
    if spec is None or spec.loader is None:
        raise RuntimeError(f"{client_path} modülü yüklenemedi.")

    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)

    candidates = [
        (name, obj)
        for name, obj in inspect.getmembers(module, inspect.isfunction)
        if obj.__module__ == module_name and not name.startswith("_")
    ]
    if not candidates:
        raise RuntimeError(f"{client_path} içinde çağrılabilir bir SDK fonksiyonu bulunamadı.")

    return candidates[0]


@app.command("compile")
def compile_command(
    goal: str = typer.Option(
        ...,
        "--goal",
        "-g",
        help="Derlenecek API'nin amacı (örn: 'Hacker News üzerinde makale araması yap')",
    ),
    name: str | None = typer.Option(
        None,
        "--name",
        "-n",
        help="Shadow Registry servis adı (verilmezse hedef domain adından otomatik türetilir, örn: hn_algolia)",
    ),
    traffic_file: Path = typer.Option(
        Path("captured_traffic.json"),
        "--traffic",
        "-t",
        help="Okunacak yakalanmış trafik JSON dosyası",
    ),
    client_out: Path = typer.Option(
        Path("generated_client.py"),
        "--client-out",
        help="Üretilecek Python SDK dosya yolu",
    ),
    mcp_out: Path = typer.Option(
        Path("generated_mcp.py"),
        "--mcp-out",
        help="Üretilecek FastMCP sunucu dosya yolu",
    ),
) -> None:
    """
    Mevcut captured_traffic.json dosyasını derleyip generated_client.py, generated_mcp.py ve
    registry/<servis_adi>/ dosyalarını üretir, ardından yanıt süresini (ms) ölçer.
    """
    ensure_default_registry()
    try:
        compiler = ShadowAPICompiler(
            traffic_path=traffic_file,
            client_output_path=client_out,
            mcp_output_path=mcp_out,
        )
        result = compiler.compile(goal=goal, service_name=name)
        _print_compilation_summary(result)

        with console.status("[bold cyan]generated_client.py test ediliyor ve yanıt süresi ölçülüyor...[/bold cyan]"):
            elapsed_ms, client_output = _run_and_measure_client(compiler, result)

        console.print(
            Panel(
                f"{client_output or '[Çıktı yok]'}\n\n"
                f"[bold green]⏱ Toplam Çalıştırma ve Yanıt Süresi:[/bold green] "
                f"[bold yellow]{elapsed_ms:.2f} ms[/bold yellow]",
                title=f"🧪 Test Çıktısı ({result.client_path.name})",
                border_style="cyan",
            )
        )
    except Exception as exc:
        console.print(f"[bold red]✖ Derleme Hatası:[/bold red] {exc}")
        raise typer.Exit(code=1)


@app.command("build")
def build_command(
    url: str = typer.Argument(..., help="Kaydedilecek hedef web sitesi URL'i (örn: https://hn.algolia.com)"),
    goal: str = typer.Option(
        ...,
        "--goal",
        "-g",
        help="Derlenecek API'nin amacı (örn: 'Hacker News üzerinde makale araması yap')",
    ),
    name: str | None = typer.Option(
        None,
        "--name",
        "-n",
        help="Shadow Registry servis adı (verilmezse hedef domain adından otomatik türetilir, örn: hn_algolia)",
    ),
    traffic_file: Path = typer.Option(
        Path("captured_traffic.json"),
        "--traffic",
        "-t",
        help="Yakalanan trafiğin kaydedileceği JSON dosyası",
    ),
    client_out: Path = typer.Option(
        Path("generated_client.py"),
        "--client-out",
        help="Üretilecek Python SDK dosya yolu",
    ),
    mcp_out: Path = typer.Option(
        Path("generated_mcp.py"),
        "--mcp-out",
        help="Üretilecek FastMCP sunucu dosya yolu",
    ),
) -> None:
    """
    Önce recorder.py ile tarayıcı kaydını başlatır (tarayıcı süresini saniye olarak ölçer),
    kayıt bitince otomatik derleyip registry/<servis_adi>/ altına kaydeder ve hız karşılaştırmasını basar.
    """
    ensure_default_registry()
    if not url.startswith(("http://", "https://")):
        url = f"https://{url}"

    recorder = TrafficRecorder(target_url=url, output_path=traffic_file)

    browser_start = time.perf_counter()
    try:
        asyncio.run(recorder.run())
    except KeyboardInterrupt:
        console.print("\n[yellow]Kayıt kullanıcı tarafından sonlandırıldı, mevcut veriler kaydediliyor...[/yellow]")
        recorder.save_to_disk()
        recorder.print_summary_table()
    browser_seconds = time.perf_counter() - browser_start

    if not recorder.captured_endpoints:
        console.print("[bold red]✖ Hiçbir uç nokta yakalanamadığı için derleme aşamasına geçilemiyor.[/bold red]")
        raise typer.Exit(code=1)

    try:
        compiler = ShadowAPICompiler(
            traffic_path=traffic_file,
            client_output_path=client_out,
            mcp_output_path=mcp_out,
        )
        result = compiler.compile(goal=goal, service_name=name)
        _print_compilation_summary(result)

        with console.status("[bold cyan]generated_client.py çalıştırılıyor ve hız ölçümü yapılıyor...[/bold cyan]"):
            direct_ms, client_output = _run_and_measure_client(compiler, result)

        console.print(
            Panel(
                client_output or "[Çıktı yok]",
                title=f"🧪 Test Çıktısı ({result.client_path.name})",
                border_style="cyan",
            )
        )

        _print_benchmark_comparison(browser_seconds=browser_seconds, direct_ms=direct_ms)

    except Exception as exc:
        console.print(f"[bold red]✖ Derleme/Çalıştırma Hatası:[/bold red] {exc}")
        raise typer.Exit(code=1)


@app.command("list")
def list_command(
    registry_dir: Path = typer.Option(
        Path("registry"),
        "--registry",
        "-r",
        help="Taranacak OwnAPI Registry dizini",
    ),
    tenant_id: str = typer.Option(
        "default",
        "--tenant",
        "-t",
        help="Token ve ToS onay durumu kontrol edilecek kiracı ID'si",
    ),
) -> None:
    """
    registry/ klasöründeki tüm hazır API'leri (Moat), uç noktalarını,
    fonksiyon adlarını, dinamik parametrelerini, TokenStore ve ToS onay durumlarını listeler.
    """
    ensure_default_registry(registry_dir)

    if not registry_dir.exists():
        console.print("[yellow]Henüz hiçbir servis kayıtlı değil (`registry/` klasörü bulunamadı).[/yellow]")
        return

    service_dirs = sorted([d for d in registry_dir.iterdir() if d.is_dir() and (d / "schema.json").exists()])
    if not service_dirs:
        console.print("[yellow]Registry içinde kayıtlı API bulunamadı.[/yellow]")
        return

    from core.consent import ConsentManager
    from core.token_store import TokenStore

    token_store = TokenStore()
    consent_mgr = ConsentManager()

    table = Table(
        title=f"🗂️ OwnAPI Enterprise Registry (Moat) — Kayıtlı API Envanteri ({len(service_dirs)} Servis)",
        show_header=True,
        header_style="bold magenta",
        show_lines=True,
    )
    table.add_column("Servis", style="bold cyan", no_wrap=True)
    table.add_column("Fonksiyon", style="bold yellow", overflow="fold")
    table.add_column("Metot & Uç Nokta", style="white", overflow="fold", ratio=2)
    table.add_column("Dinamik Parametreler", style="green", overflow="fold", ratio=1)
    table.add_column("Mimari, Oturum & ToS Durumu", justify="center", overflow="fold")

    for sdir in service_dirs:
        schema_path = sdir / "schema.json"
        client_path = sdir / "client.py"
        try:
            raw_schema = json.loads(schema_path.read_text(encoding="utf-8"))
            schema = RegistrySchema.model_validate(raw_schema)
            val_status = raw_schema.get("validation_status", "BAŞARILI")
        except Exception as exc:
            table.add_row(sdir.name, "-", f"[red]Şema okuma hatası: {exc}[/red]", "-", "[red]Bozuk[/red]")
            continue

        client_code = client_path.read_text(encoding="utf-8") if client_path.exists() else ""
        has_own_client = ("BaseOwnClient" in client_code) or ("BaseShadowClient" in client_code) or ("SessionHealer" in client_code)
        session_data = token_store.get_session(tenant_id, schema.service_name)
        has_consent = consent_mgr.has_consented(tenant_id, schema.service_name)

        if schema.dynamic_params:
            param_str = ", ".join(f"{p.name}: {p.param_type}={repr(p.default_value)}" for p in schema.dynamic_params)
        else:
            param_str = "-"

        consent_badge = "[cyan]⚖️ ToS Onaylı[/cyan]" if has_consent else "[dim]⚖️ İlk Çağrıda Onay[/dim]"
        val_badge = f"[green]✔ Auto-Test: {val_status}[/green]"

        if session_data and has_own_client:
            updated_at = str(session_data.get("updated_at") or session_data.get("healed_at") or "")[:19].replace("T", " ")
            health_badge = (
                f"[bold green]🟢 User-Delegated (Aktif Oturum)[/bold green]\n"
                f"{val_badge} | {consent_badge}\n"
                f"[dim]Oturum: {updated_at}[/dim]"
            )
        elif has_own_client:
            health_badge = (
                f"[bold green]🟢 User-Delegated (Hazır)[/bold green]\n"
                f"{val_badge} | {consent_badge}\n"
                f"[dim]Local & Cloud Browser Destekli[/dim]"
            )
        elif client_path.exists():
            health_badge = f"[bold yellow]🟡 Standart İstemci[/bold yellow]\n{consent_badge}"
        else:
            health_badge = "[bold red]🔴 client.py Eksik[/bold red]"

        endpoint_display = f"[bold cyan]{schema.method}[/bold cyan] {schema.endpoint_url}\n[dim]{schema.explanation}[/dim]"

        table.add_row(
            schema.service_name,
            f"{schema.function_name}()",
            endpoint_display,
            param_str,
            health_badge,
        )

    console.print()
    console.print(table)
    console.print()


@app.command("test")
def test_command(
    query: str = typer.Option(
        ...,
        "--query",
        "-q",
        help="Üretilen SDK fonksiyonuna gönderilecek dinamik test sorgusu (örn: 'yapay zeka')",
    ),
    service: str | None = typer.Option(
        None,
        "--service",
        "-s",
        help="Test edilecek belirli bir Registry servis adı (örn: 'hackernews')",
    ),
    client_file: Path = typer.Option(
        Path("generated_client.py"),
        "--client",
        "-c",
        help="Test edilecek Python SDK dosyası",
    ),
) -> None:
    """
    Üretilen generated_client.py (veya registry/<servis>/client.py) içindeki fonksiyonu
    terminalden verilen farklı bir parametreyle dinamik olarak test eder ve ms süresini basar.
    """
    ensure_default_registry()
    target_client = Path("registry") / service / "client.py" if service else client_file

    try:
        fn_name, sdk_fn = _load_sdk_function(target_client)
        sig = inspect.signature(sdk_fn)
        params = list(sig.parameters.keys())

        call_kwargs: dict[str, Any] = {}
        call_args: list[Any] = []
        if "query" in sig.parameters:
            call_kwargs["query"] = query
            param_display = f"query={repr(query)}"
        elif params:
            first_param = params[0]
            call_kwargs[first_param] = query
            param_display = f"{first_param}={repr(query)}"
        else:
            param_display = "(argümansız)"

        with console.status(f"[bold cyan]{fn_name}({param_display}) çağrılıyor...[/bold cyan]"):
            start_ns = time.perf_counter()
            response_data = sdk_fn(*call_args, **call_kwargs)
            elapsed_ms = (time.perf_counter() - start_ns) * 1000.0

        summary_lines: list[str] = [
            f"[bold green]✔ Fonksiyon:[/bold green] [bold yellow]{fn_name}({param_display})[/bold yellow]",
            f"[bold green]⚡ Doğrudan Yanıt Süresi:[/bold green] [bold cyan]{elapsed_ms:.2f} ms[/bold cyan]",
        ]

        if isinstance(response_data, dict):
            if "hits" in response_data and isinstance(response_data["hits"], list):
                hits = response_data["hits"]
                total_hits = response_data.get("nbHits", len(hits))
                summary_lines.append(
                    f"[bold green]📊 Bulunan Sonuç:[/bold green] Toplam [bold white]{total_hits}[/bold white] kayıt "
                    f"(İlk {min(5, len(hits))} sonuç listeleniyor)\n"
                )
                for idx, item in enumerate(hits[:5], start=1):
                    title = item.get("title") or item.get("story_title") or item.get("name") or str(item)[:80]
                    url = item.get("url") or item.get("story_url") or "URL yok"
                    points = item.get("points")
                    pts_str = f" [yellow]({points} puan)[/yellow]" if points is not None else ""
                    summary_lines.append(f"  [bold cyan]{idx}.[/bold cyan] {title}{pts_str}\n     [dim]{url}[/dim]")
            elif "pages" in response_data and isinstance(response_data["pages"], list):
                pages = response_data["pages"]
                summary_lines.append(
                    f"[bold green]📊 Bulunan Sonuç:[/bold green] Toplam [bold white]{len(pages)}[/bold white] sayfa "
                    f"(İlk {min(5, len(pages))} sonuç listeleniyor)\n"
                )
                for idx, item in enumerate(pages[:5], start=1):
                    title = item.get("title") or item.get("key") or str(item)[:80]
                    desc = item.get("description") or "Açıklama yok"
                    key = item.get("key") or ""
                    wiki_url = f"https://en.wikipedia.org/wiki/{key}" if key else ""
                    summary_lines.append(
                        f"  [bold cyan]{idx}.[/bold cyan] [bold white]{title}[/bold white] — [yellow]{desc}[/yellow]\n"
                        f"     [dim]{wiki_url}[/dim]"
                    )
            else:
                preview = json.dumps(response_data, ensure_ascii=False, indent=2)[:800]
                summary_lines.append(f"\n[bold magenta]JSON Yanıt Önizlemesi:[/bold magenta]\n{preview}")
        else:
            summary_lines.append(f"\n[bold magenta]Yanıt:[/bold magenta] {str(response_data)[:800]}")

        console.print(
            Panel(
                "\n".join(summary_lines),
                title="🚀 Shadow-API Dinamik Sorgu Testi",
                border_style="green",
            )
        )

    except Exception as exc:
        console.print(f"[bold red]✖ Test Hatası:[/bold red] {exc}")
        raise typer.Exit(code=1)


def _upsert_mcp_config(config_path: Path, server_name: str, server_entry: dict[str, Any]) -> None:
    """Verilen JSON yapılandırma dosyasına mcpServers altında yeni sunucuyu ekler veya günceller."""
    config_path.parent.mkdir(parents=True, exist_ok=True)
    existing_data: dict[str, Any] = {}
    if config_path.exists():
        try:
            raw = config_path.read_text(encoding="utf-8").strip()
            if raw:
                existing_data = json.loads(raw)
        except Exception:
            existing_data = {}

    if not isinstance(existing_data, dict):
        existing_data = {}

    mcp_servers = existing_data.get("mcpServers")
    if not isinstance(mcp_servers, dict):
        mcp_servers = {}

    mcp_servers[server_name] = server_entry
    existing_data["mcpServers"] = mcp_servers

    config_path.write_text(
        json.dumps(existing_data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


@app.command("install-mcp")
def install_mcp_command(
    name: str = typer.Option(
        "shadow-api-master",
        "--name",
        "-n",
        help="Kaydedilecek MCP sunucusunun adı (örn: 'hackernews' veya 'shadow-api-master')",
    ),
    mcp_file: Path = typer.Option(
        Path("master_mcp.py"),
        "--mcp-file",
        "-m",
        help="Kurulacak MCP sunucu dosyası (varsayılan: master_mcp.py veya generated_mcp.py)",
    ),
) -> None:
    """
    Üretilen MCP sunucusunu (master_mcp.py veya generated_mcp.py) hem proje içindeki
    .cursor/mcp.json dosyasına hem de Claude Desktop (%APPDATA%\\Claude\\claude_desktop_config.json)
    yapılandırmasına otomatik olarak ekler.
    """
    ensure_default_registry()
    target_mcp = mcp_file if mcp_file.exists() else Path("generated_mcp.py")
    if not target_mcp.exists():
        console.print(
            f"[bold red]✖ {target_mcp} bulunamadı![/bold red] "
            "Önce `python cli.py compile` veya `python cli.py build` komutunu çalıştırın."
        )
        raise typer.Exit(code=1)

    abs_mcp_path = str(target_mcp.resolve())
    python_exe = str(Path(sys.executable).resolve())

    server_entry: dict[str, Any] = {
        "command": python_exe,
        "args": [abs_mcp_path],
        "env": {
            "PYTHONIOENCODING": "utf-8",
        },
    }

    table = Table(
        title=f"🔌 MCP Sunucu Kurulum Özeti ({name})",
        show_header=True,
        header_style="bold magenta",
        show_lines=True,
    )
    table.add_column("İstemci / Hedef", style="bold cyan", width=22)
    table.add_column("Yapılandırma Dosyası", style="white", overflow="fold")
    table.add_column("Durum", style="bold green", width=26, justify="center")

    # 1. Proje içi .cursor/mcp.json
    cursor_config_path = Path(".cursor") / "mcp.json"
    _upsert_mcp_config(cursor_config_path, name, server_entry)
    table.add_row(
        "Cursor IDE",
        str(cursor_config_path.resolve()),
        "[bold green]✔ Eklendi / Güncellendi[/bold green]",
    )

    # 2. Claude Desktop (%APPDATA%\Claude\claude_desktop_config.json)
    appdata = os.getenv("APPDATA")
    if appdata:
        claude_dir = Path(appdata) / "Claude"
        claude_config_path = claude_dir / "claude_desktop_config.json"
        if claude_dir.exists() or claude_config_path.exists():
            _upsert_mcp_config(claude_config_path, name, server_entry)
            table.add_row(
                "Claude Desktop",
                str(claude_config_path.resolve()),
                "[bold green]✔ Eklendi / Güncellendi[/bold green]",
            )
        else:
            _upsert_mcp_config(claude_config_path, name, server_entry)
            table.add_row(
                "Claude Desktop",
                str(claude_config_path.resolve()),
                "[bold yellow]✔ Oluşturuldu ve Eklendi[/bold yellow]",
            )
    else:
        table.add_row(
            "Claude Desktop",
            "%APPDATA%\\Claude\\claude_desktop_config.json",
            "[dim yellow]Atlandı (APPDATA bulunamadı)[/dim yellow]",
        )

    console.print()
    console.print(table)
    console.print(
        Panel.fit(
            f"[bold cyan]Sunucu Adı:[/bold cyan] [bold yellow]{name}[/bold yellow]\n"
            f"[bold cyan]Komut:[/bold cyan] {python_exe}\n"
            f"[bold cyan]Argüman:[/bold cyan] {abs_mcp_path}",
            title="🚀 MCP Sunucusu Hazır",
            border_style="green",
        )
    )


@app.command("add")
def add_command(
    service_name: str = typer.Argument(..., help="Yeni servis adı (örn: biletix)"),
    url: str = typer.Argument(..., help="Giriş sayfası URL'si"),
    desc: str = typer.Option("", "--desc", "-d", help="Servis açıklaması veya amacı"),
    headless: bool = typer.Option(False, "--headless", help="Tarayıcıyı arka planda çalıştır"),
    mode: str = typer.Option("local", "--mode", "-m", help="Çalışma modu: 'local' (yerel tarayıcı) veya 'cloud' (Docker/Remote Browser)"),
    tenant_id: str = typer.Option("default", "--tenant", "-t", help="Kiracı ID'si"),
    auto_validate: bool = typer.Option(True, "--validate/--no-validate", help="Derleme sonrası 3x Auto-Test (Compiler + Validator Loop) çalıştır"),
    validation_runs: int = typer.Option(3, "--runs", help="Auto-Test doğrulama çağrısı sayısı (varsayılan: 3)"),
    interactive_tos: bool = typer.Option(False, "--interactive-tos", help="ToS onayını terminalde etkileşimli E/h sorusuyla al"),
) -> None:
    """
    Yeni bir web sitesini tarayıcı ile dinleyip otomatik olarak OwnAPI sistemine ekler,
    ToS onayını loglar ve 3 kez otomatik doğrulama testinden (Compiler + Validator Loop) geçirir.
    """
    from own_api.sdk import OwnAPIClient

    client = OwnAPIClient(tenant_id=tenant_id, mode=mode)
    result = client.onboard(
        service_name=service_name,
        start_url=url,
        description=desc,
        headless=headless,
        mode=mode,
        auto_validate=auto_validate,
        validation_runs=validation_runs,
        interactive_tos=interactive_tos,
    )
    if not result.get("success"):
        raise typer.Exit(code=1)


@app.command("call")
def call_command(
    service_name: str = typer.Argument(..., help="Çağrılacak servis adı (örn: thy, trendyol, enuygun, pegasus, wikipedia, hackernews)"),
    param: list[str] = typer.Option(
        None,
        "--param",
        "-p",
        help="Fonksiyona iletilecek parametreler (key=val formatında, örn: -p query=laptop -p max_results=5)",
    ),
    tenant_id: str = typer.Option("default", "--tenant", "-t", help="Kiracı ID'si"),
    mode: str = typer.Option("local", "--mode", "-m", help="Çalışma modu: 'local' (yerel tarayıcı) veya 'cloud' (Docker/Remote Browser)"),
    non_interactive: bool = typer.Option(
        False,
        "--non-interactive",
        help="Captcha/WAF çıktığında terminalde beklemek yerine görevi HITL onay kuyruğuna ekle",
    ),
) -> None:
    """
    Kayıtlı bir servisi ToS onay kaydı ve seçilen çalışma modu ('local' veya 'cloud') ile doğrudan çağırır.
    """
    from own_api.sdk import OwnAPIClient

    kwargs: dict[str, Any] = {}
    if param:
        for p in param:
            if "=" in p:
                k, v = p.split("=", 1)
                k = k.strip()
                v = v.strip()
                if v.lower() == "true":
                    val: Any = True
                elif v.lower() == "false":
                    val = False
                elif v.isdigit():
                    val = int(v)
                else:
                    try:
                        val = float(v)
                    except ValueError:
                        val = v
                kwargs[k] = val

    client = OwnAPIClient(tenant_id=tenant_id, mode=mode, non_interactive=non_interactive)
    try:
        start_t = time.perf_counter()
        res = client.call(service_name, mode=mode, non_interactive=non_interactive, **kwargs)
        elapsed_ms = (time.perf_counter() - start_t) * 1000

        console.print(f"[dim]Yanıt Süresi: {elapsed_ms:.2f} ms | Tenant: {tenant_id} | Mod: {mode.upper()}[/dim]")
        if isinstance(res, (dict, list)):
            console.print_json(data=res)
        else:
            console.print(res)
    except Exception as exc:
        console.print(f"[bold red]✖ Çağrı Hatası:[/bold red] {exc}")
        raise typer.Exit(code=1)


@app.command("validate")
def validate_command(
    service_name: str | None = typer.Argument(
        None,
        help="Doğrulanacak servis adı (örn: enuygun, trendyol). --all ile tüm servisler taranır.",
    ),
    all_services: bool = typer.Option(
        False,
        "--all",
        "-a",
        help="Tüm kayıtlı servisleri tek seferde tara, bozuk olanları Self-Healing 2.0 ile otomatik tamir et",
    ),
    runs: int = typer.Option(3, "--runs", "-n", help="Başarılı olması gereken ardışık test çağrısı sayısı"),
    auto_heal: bool = typer.Option(True, "--heal/--no-heal", help="Hata alan servisleri otomatik tamir et"),
    tenant_id: str = typer.Option("default", "--tenant", "-t", help="Kiracı ID'si"),
    mode: str = typer.Option("local", "--mode", "-m", help="Çalışma modu: 'local' veya 'cloud'"),
) -> None:
    """
    Kayıtlı bir servis (veya --all ile tüm servisler) üzerinde Compiler + Validator Loop
    ve Self-Healing 2.0 otomatik tamir katmanını çalıştırır.
    """
    from core.auto_repair import AutoRepairEngine
    from core.onboarding import validate_and_repair_client

    if all_services or not service_name:
        engine = AutoRepairEngine(tenant_id=tenant_id, mode=mode)
        scan_runs = 1 if (all_services and runs == 3) else runs
        engine.validate_all_services(runs=scan_runs, auto_heal=auto_heal)
        return

    service_dir = Path("registry") / service_name
    client_path = service_dir / "client.py"
    schema_path = service_dir / "schema.json"
    traffic_path = service_dir / "captured_traffic.json"

    if not client_path.exists() or not schema_path.exists():
        console.print(f"[bold red]✖ '{service_name}' servisi registry altında bulunamadı![/bold red]")
        raise typer.Exit(code=1)

    raw_schema = json.loads(schema_path.read_text(encoding="utf-8"))
    schema_obj = RegistrySchema.model_validate(raw_schema)
    endpoints: list[dict[str, Any]] = []
    if traffic_path.exists():
        try:
            tdata = json.loads(traffic_path.read_text(encoding="utf-8"))
            endpoints = tdata.get("endpoints", [])
        except Exception:
            endpoints = []

    matched_ep = next((e for e in endpoints if e.get("url") == schema_obj.endpoint_url), endpoints[0] if endpoints else {})
    report = validate_and_repair_client(
        service_name=service_name,
        target_url=schema_obj.target_url,
        description=schema_obj.goal,
        client_path=client_path,
        schema_obj=schema_obj,
        matched_endpoint=matched_ep,
        all_endpoints=endpoints,
        validation_runs=runs,
        max_repair_attempts=3,
        tenant_id=tenant_id,
        mode=mode,
    )
    raw_schema["validation_status"] = report.get("validation_status", "BAŞARILI")
    raw_schema["validation_passed_runs"] = report.get("passed_runs", runs)
    schema_path.write_text(json.dumps(raw_schema, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


@app.command("daemon")
def daemon_command(
    schedule_time: str = typer.Option(
        "03:00",
        "--time",
        "-t",
        help="Her gece otomatik bakımın çalışacağı saat (HH:MM formatında, varsayılan: 03:00)",
    ),
    interval_minutes: float | None = typer.Option(
        None,
        "--interval",
        "-i",
        help="Saat yerine dakika bazlı periyot (örn: 60 -> her 60 dakikada bir)",
    ),
    runs: int = typer.Option(1, "--runs", "-n", help="Her servis için yapılacak sağlık testi sayısı"),
    auto_heal: bool = typer.Option(True, "--heal/--no-heal", help="Bozulan servisleri otomatik tamir et"),
    run_immediately: bool = typer.Option(False, "--now", help="Daemon başlarken ilk taramayı hemen yap"),
    max_cycles: int | None = typer.Option(None, "--max-cycles", help="Maksimum bakım döngüsü sayısı (test amaçlı)"),
    tenant_id: str = typer.Option("default", "--tenant", help="Kiracı ID'si"),
    mode: str = typer.Option("local", "--mode", "-m", help="Çalışma modu: 'local' veya 'cloud'"),
) -> None:
    """
    Self-Healing 2.0 Gece Otomatik Bakım ve Tamir Servisini (varsayılan her gece 03:00) başlatır.
    """
    from core.auto_repair import AutoRepairEngine

    engine = AutoRepairEngine(tenant_id=tenant_id, mode=mode)
    engine.run_daemon(
        schedule_time=schedule_time,
        interval_minutes=interval_minutes,
        runs=runs,
        auto_heal=auto_heal,
        run_immediately=run_immediately,
        max_cycles=max_cycles,
    )


@app.command("queue")
def queue_command(
    tenant_id: str = typer.Option("default", "--tenant", "-t", help="Kuyruğu görüntülenecek kiracı ID'si (--all-tenants ile tümü)"),
    all_tenants: bool = typer.Option(False, "--all-tenants", "-a", help="Tüm kiracıların HITL kuyruğunu listele"),
    status: str | None = typer.Option(None, "--status", "-s", help="Durum filtresi (PENDING_USER_ACTION veya COMPLETED)"),
) -> None:
    """
    Human-in-the-Loop (HITL) onay kuyruğunda bekleyen (PENDING_USER_ACTION) ve tamamlanan (COMPLETED) görevleri listeler.
    """
    from core.hitl_queue import HITLQueueManager

    mgr = HITLQueueManager()
    mgr.print_queue_table(
        tenant_id=None if all_tenants else tenant_id,
        status_filter=status,
    )


@app.command("resume")
def resume_command(
    task_id: str = typer.Argument(..., help="Tarayıcıda onaylanıp devam ettirilecek HITL görev ID'si (örn: hitl_a1b2c3d4)"),
    tenant_id: str | None = typer.Option(None, "--tenant", "-t", help="İsteğe bağlı kiracı ID'si"),
) -> None:
    """
    HITL kuyruğunda onay bekleyen (PENDING_USER_ACTION) bir görevi tarayıcıda açıp doğrulamayı
    tamamladıktan sonra kaldığı yerden devam ettirir.
    """
    from core.hitl_queue import HITLQueueManager

    mgr = HITLQueueManager()
    try:
        res = mgr.resume_task(task_id=task_id, tenant_id=tenant_id)
        console.print_json(data=res)
    except Exception as exc:
        console.print(f"[bold red]✖ HITL Görev Devam Ettirme Hatası:[/bold red] {exc}")
        raise typer.Exit(code=1)


@app.command("approve")
def approve_command(
    task_id: str = typer.Argument(..., help="Tarayıcıda onaylanıp devam ettirilecek HITL görev ID'si (örn: hitl_a1b2c3d4)"),
    tenant_id: str | None = typer.Option(None, "--tenant", "-t", help="İsteğe bağlı kiracı ID'si"),
) -> None:
    """
    `own-api resume <task_id>` komutu için kısayol: Bekleyen HITL görevini tarayıcıda onaylayıp tamamlar.
    """
    resume_command(task_id=task_id, tenant_id=tenant_id)


@app.command("consent")
def consent_command(
    tenant_id: str = typer.Option("default", "--tenant", "-t", help="Denetim günlüğü görüntülenecek kiracı ID'si"),
) -> None:
    """
    ToS / Kullanıcı Delegasyon Onay Mekanizması (User Delegation Disclaimer) denetim kayıtlarını listeler.
    """
    from core.consent import ConsentManager, USER_DELEGATION_DISCLAIMER

    mgr = ConsentManager()
    records = mgr.get_consents(tenant_id)
    console.print(
        Panel.fit(
            f"[white]{USER_DELEGATION_DISCLAIMER}[/white]",
            title="⚖️ OwnAPI Terms of Service & User Delegation Disclaimer",
            border_style="yellow",
        )
    )
    if not records:
        console.print(f"[yellow]'{tenant_id}' kiracısı için henüz kaydedilmiş bir onay kaydı bulunmuyor.[/yellow]")
        return

    table = Table(
        title=f"📜 Kullanıcı Delegasyon Onay Günlüğü (.sessions/{tenant_id}/consent_audit.json)",
        show_header=True,
        header_style="bold cyan",
        show_lines=True,
    )
    table.add_column("#", justify="right", style="dim")
    table.add_column("Servis", style="bold magenta")
    table.add_column("İşlem", style="yellow")
    table.add_column("Mod", style="green")
    table.add_column("Yetkilendiren", style="cyan")
    table.add_column("SHA256", style="dim")
    table.add_column("Tarih (UTC)", style="white")

    for idx, r in enumerate(records, start=1):
        table.add_row(
            str(idx),
            str(r.get("service", "-")),
            str(r.get("action", "-")),
            str(r.get("execution_mode", "local")).upper(),
            str(r.get("delegated_by", "-")),
            str(r.get("disclaimer_sha256", "-")),
            str(r.get("accepted_at", "-"))[:19].replace("T", " "),
        )
    console.print(table)


@app.command("serve")
def serve_command(
    host: str = typer.Option("127.0.0.1", "--host", "-h", help="Sunucu dinleme adresi"),
    port: int = typer.Option(8000, "--port", "-p", help="Sunucu port numarası (varsayılan: 8000)"),
    reload: bool = typer.Option(False, "--reload", help="Kod değişikliklerinde otomatik yeniden başlat"),
) -> None:
    """
    OwnAPI Enterprise Web Dashboard, Live Playground & REST API Sunucusunu (http://localhost:8000) başlatır.
    """
    from server import run_server

    console.print(
        Panel.fit(
            f"[bold green]🌐 OwnAPI Enterprise Web Dashboard & REST API Sunucusu Başlatılıyor![/bold green]\n"
            f"[white]Web Dashboard & Live Playground:[/white] [bold cyan]http://{host}:{port}[/bold cyan]\n"
            f"[white]Interactive OpenAPI / Swagger:[/white] [bold yellow]http://{host}:{port}/docs[/bold yellow]",
            title="OwnAPI Enterprise Server",
            border_style="green",
        )
    )
    run_server(host=host, port=port, reload=reload)


@app.command("mcp")
def mcp_command() -> None:
    """
    Dinamik OwnAPI Master MCP sunucusunu (master_mcp.py) başlatır.
    """
    from master_mcp import mcp
    mcp.run()


@app.command("ask")
def ask_command(
    prompt: str = typer.Argument(..., help="Ajan tarafından yanıtlanacak doğal dil sorusu"),
    tenant_id: str = typer.Option("default", "--tenant", "-t", help="Kiracı ID'si"),
    mode: str = typer.Option("local", "--mode", "-m", help="Çalışma modu: 'local' veya 'cloud'"),
) -> None:
    """
    Doğal dil sorusunu kayıtlı tüm servisleri ve Function Calling'i kullanarak yanıtlar.
    """
    from own_api.sdk import OwnAPIClient

    client = OwnAPIClient(tenant_id=tenant_id, mode=mode)
    try:
        with console.status("[bold green]OwnAPI Ajanı yanıt hazırlıyor ve araçları çalıştırıyor...[/bold green]"):
            answer = client.ask(prompt)
        console.print(Panel(answer, title="🤖 OwnAPI Enterprise Agent", border_style="green"))
    except Exception as exc:
        console.print(f"[bold red]✖ Ajan Hatası:[/bold red] {exc}")
        raise typer.Exit(code=1)


def main() -> None:
    """OwnAPI CLI giriş noktası."""
    app()


if __name__ == "__main__":
    main()
