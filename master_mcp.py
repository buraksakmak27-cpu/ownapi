from __future__ import annotations

import builtins
import contextlib
import functools
import importlib.util
import inspect
import os
import sys
from pathlib import Path
from typing import Any, Callable

# MCP stdio protokolünde JSON-RPC akışının bozulmaması ("Connection closed" hatası almamak) için
# interaktif input() beklemelerini kapat ve varsayılan print() çağrılarını sys.stderr'e yönlendir.
if "--check" not in sys.argv:
    os.environ["OWNAPI_NON_INTERACTIVE"] = "1"
    _orig_print = builtins.print

    def _stderr_print(*args: Any, **kwargs: Any) -> None:
        if "file" not in kwargs or kwargs["file"] is None or kwargs["file"] is sys.stdout:
            kwargs["file"] = sys.stderr
        _orig_print(*args, **kwargs)

    builtins.print = _stderr_print

from mcp.server.fastmcp import FastMCP

# Proje kök dizinini sys.path'e ekle (healer, compiler vb. modüllerin bulunabilmesi için)
PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from compiler import RegistrySchema, ensure_default_registry

mcp = FastMCP("OwnAPI Master Registry")


def _wrap_tool_with_stderr_redirect(fn: Callable[..., Any]) -> Callable[..., Any]:
    """
    MCP aracı çalışırken alt modüllerden (`healer`, `client.py` vb.) gelebilecek tüm
    `sys.stdout` çıktılarını `sys.stderr` kanalına yönlendirerek JSON-RPC `stdio` hattını korur.
    Ayrıca WAF/Captcha engeline takılan ajan görevlerinin öldürülmek yerine `.sessions/hitl_state.json`
    üzerinden 'PAUSED' durumuna alınıp kullanıcı onayından sonra kaldığı yerden ('resume')
    devam etmesini garanti altına alır.
    """
    if inspect.iscoroutinefunction(fn):
        @functools.wraps(fn)
        async def _async_wrapper(*args: Any, **kwargs: Any) -> Any:
            with contextlib.redirect_stdout(sys.stderr):
                os.environ.setdefault("OWNAPI_HITL_PAUSE_AND_WAIT", "1")
                result = await fn(*args, **kwargs)
                if isinstance(result, dict) and result.get("status") in ("PAUSED", "PENDING_USER_APPROVAL", "PENDING_USER_ACTION") and result.get("task_id"):
                    from core.hitl_queue import HITLQueueManager

                    mgr = HITLQueueManager()
                    resumed = await mgr.await_task_resume(
                        str(result["task_id"]),
                        poll_interval=0.5,
                        timeout_sec=float(os.getenv("OWNAPI_HITL_PAUSE_TIMEOUT", "300")),
                    )
                    if resumed is not None:
                        if resumed.get("response") is not None:
                            return resumed["response"]
                        return await fn(*args, **kwargs)
                return result

        _async_wrapper.__signature__ = inspect.signature(fn)  # type: ignore[attr-defined]
        return _async_wrapper

    @functools.wraps(fn)
    def _sync_wrapper(*args: Any, **kwargs: Any) -> Any:
        with contextlib.redirect_stdout(sys.stderr):
            os.environ.setdefault("OWNAPI_HITL_PAUSE_AND_WAIT", "1")
            result = fn(*args, **kwargs)
            if isinstance(result, dict) and result.get("status") in ("PAUSED", "PENDING_USER_APPROVAL", "PENDING_USER_ACTION") and result.get("task_id"):
                import asyncio
                from core.hitl_queue import HITLQueueManager

                mgr = HITLQueueManager()
                resumed = asyncio.run(
                    mgr.await_task_resume(
                        str(result["task_id"]),
                        poll_interval=0.5,
                        timeout_sec=float(os.getenv("OWNAPI_HITL_PAUSE_TIMEOUT", "300")),
                    )
                )
                if resumed is not None:
                    if resumed.get("response") is not None:
                        return resumed["response"]
                    return fn(*args, **kwargs)
            return result

    _sync_wrapper.__signature__ = inspect.signature(fn)  # type: ignore[attr-defined]
    return _sync_wrapper


def load_registry_tools(registry_root: Path = PROJECT_ROOT / "registry") -> list[tuple[RegistrySchema, Callable[..., Any]]]:
    """
    registry/ klasöründeki tüm derlenmiş client.py modüllerini ve schema.json dosyalarını
    dinamik olarak içe aktarır.
    """
    with contextlib.redirect_stdout(sys.stderr):
        ensure_default_registry(registry_root)
    loaded_tools: list[tuple[RegistrySchema, Callable[..., Any]]] = []

    if not registry_root.exists():
        return loaded_tools

    for service_dir in sorted(registry_root.iterdir()):
        if not service_dir.is_dir():
            continue

        schema_path = service_dir / "schema.json"
        client_path = service_dir / "client.py"
        if not schema_path.exists() or not client_path.exists():
            continue

        try:
            schema = RegistrySchema.model_validate_json(schema_path.read_text(encoding="utf-8"))
            module_name = f"shadow_registry_{schema.service_name}"
            spec = importlib.util.spec_from_file_location(module_name, str(client_path.resolve()))
            if spec is None or spec.loader is None:
                continue

            module = importlib.util.module_from_spec(spec)
            sys.modules[module_name] = module
            with contextlib.redirect_stdout(sys.stderr):
                spec.loader.exec_module(module)

            fn = getattr(module, schema.function_name, None)
            if fn is None or not callable(fn):
                # İsim eşleşmezse modüldeki ilk genel fonksiyonu al
                candidates = [
                    obj
                    for name, obj in inspect.getmembers(module, inspect.isfunction)
                    if obj.__module__ == module_name and not name.startswith("_")
                ]
                if not candidates:
                    continue
                fn = candidates[0]

            loaded_tools.append((schema, _wrap_tool_with_stderr_redirect(fn)))
        except Exception as exc:
            sys.stderr.write(f"[OwnAPI Master MCP] {service_dir.name} yüklenirken uyarı: {exc}\n")

    return loaded_tools


def register_all_tools() -> list[RegistrySchema]:
    """Yüklenen tüm OwnAPI Registry fonksiyonlarını tek bir FastMCP sunucusuna @mcp.tool() olarak kaydeder."""
    tools = load_registry_tools()
    registered_schemas: list[RegistrySchema] = []
    used_tool_names: set[str] = set()

    for schema, fn in tools:
        tool_name = schema.function_name
        if tool_name in used_tool_names:
            tool_name = f"{schema.service_name}_{schema.function_name}"
        used_tool_names.add(tool_name)

        description = (
            f"[{schema.service_name}] {schema.explanation} "
            f"(Endpoint: {schema.method} {schema.endpoint_url})"
        )
        mcp.tool(name=tool_name, description=description)(fn)
        registered_schemas.append(schema)

    return registered_schemas


REGISTERED_SCHEMAS = register_all_tools()


if __name__ == "__main__":
    if "--check" in sys.argv:
        from rich.console import Console
        from rich.table import Table

        console = Console(stderr=True, legacy_windows=False)
        table = Table(
            title=f"🌐 Master MCP Sunucusu — Yüklü Araçlar ({len(REGISTERED_SCHEMAS)} Adet)",
            show_header=True,
            header_style="bold magenta",
            show_lines=True,
        )
        table.add_column("Servis", style="bold cyan")
        table.add_column("MCP Tool Adı", style="bold yellow")
        table.add_column("Hedef Endpoint", style="white")
        for s in REGISTERED_SCHEMAS:
            table.add_row(s.service_name, s.function_name, f"{s.method} {s.endpoint_url}")
        console.print(table)
    else:
        mcp.run()
