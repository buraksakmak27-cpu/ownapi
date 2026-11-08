from __future__ import annotations

import inspect
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Callable

import typer
from dotenv import load_dotenv
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel

from compiler import DynamicParamSpec, RegistrySchema, ensure_default_registry
from master_mcp import load_registry_tools

console = Console()
app = typer.Typer(
    name="shadow-agent",
    help="Shadow-API Bağımsız AI Ajanı: Registry içindeki tüm tersine mühendislik API'lerini Function Calling ile kullanır.",
    add_completion=False,
)

PYTHON_TO_JSON_TYPE: dict[str, str] = {
    "str": "string",
    "string": "string",
    "int": "integer",
    "integer": "integer",
    "float": "number",
    "number": "number",
    "bool": "boolean",
    "boolean": "boolean",
}

AGENT_SYSTEM_PROMPT = """\
Sen Shadow-API platformu üzerinde çalışan otonom bir yapay zeka ajanısın.
Sana kullanıcının web sitelerinden tersine mühendislikle çıkardığı gerçek zamanlı Shadow-API araçları (tools) verildi.
KESİN KURAL: Kullanıcı bir parametreyi açıkça belirtmemişse KESİNLİKLE kullanıcıya soru sorma veya onay isteme; araç şemasındaki `default` (varsayılan) değerleri otomatik olarak kullan ve aracı hemen çağır.
Dönen gerçek verileri analiz et (örneğin puan sıralaması, filtreleme vb. gerekiyorsa tüm sonuçlar içinden en uygun olanları seç) ve kullanıcıya Türkçe, net, puanları ve bağlantıları (URL) içeren zengin bir özet sun.
"""


def _build_json_schema_from_tool(schema: RegistrySchema, fn: Callable[..., Any]) -> dict[str, Any]:
    """RegistrySchema ve fonksiyon imzasından varsayılan (default) değerleri içeren JSON Schema üretir."""
    properties: dict[str, Any] = {}

    if schema.dynamic_params:
        for p in schema.dynamic_params:
            json_type = PYTHON_TO_JSON_TYPE.get(p.param_type.lower(), "string")
            prop_def: dict[str, Any] = {
                "type": json_type,
                "description": (
                    f"{p.description or p.name} "
                    f"(Belirtilmezse varsayılan değer otomatik kullanılır: {repr(p.default_value)})"
                ),
                "default": p.default_value,
            }
            properties[p.name] = prop_def
    else:
        sig = inspect.signature(fn)
        for param_name, param in sig.parameters.items():
            ann = str(param.annotation).lower()
            json_type = "integer" if "int" in ann else "number" if "float" in ann else "boolean" if "bool" in ann else "string"
            default_val = None if param.default is inspect.Parameter.empty else param.default
            prop_def = {
                "type": json_type,
                "description": f"{param_name} parametresi (varsayılan: {repr(default_val)})",
            }
            if default_val is not None:
                prop_def["default"] = default_val
            properties[param_name] = prop_def

    return {
        "type": "object",
        "properties": properties,
        "required": [],
    }


def _compact_api_response(data: Any) -> Any:
    """
    LLM bağlam penceresini (context window) gereksiz yorum ID dizileri veya highlight
    verileriyle şişirmemek için API yanıtını anlamlı alanları koruyarak optimize eder.
    """
    if isinstance(data, dict):
        if "hits" in data and isinstance(data["hits"], list):
            compact_hits = []
            for item in data["hits"][:30]:
                if isinstance(item, dict):
                    compact_hits.append(
                        {
                            "title": item.get("title") or item.get("story_title"),
                            "url": item.get("url") or item.get("story_url") or f"https://news.ycombinator.com/item?id={item.get('objectID', '')}",
                            "points": item.get("points"),
                            "author": item.get("author"),
                            "num_comments": item.get("num_comments"),
                            "created_at": item.get("created_at"),
                            "objectID": item.get("objectID"),
                        }
                    )
                else:
                    compact_hits.append(item)
            return {
                "nbHits": data.get("nbHits", len(compact_hits)),
                "query": data.get("query"),
                "hits": compact_hits,
            }
        if "pages" in data and isinstance(data["pages"], list):
            compact_pages = []
            for item in data["pages"][:20]:
                if isinstance(item, dict):
                    key = item.get("key") or ""
                    compact_pages.append(
                        {
                            "id": item.get("id"),
                            "title": item.get("title"),
                            "key": key,
                            "description": item.get("description"),
                            "excerpt": item.get("excerpt"),
                            "url": f"https://en.wikipedia.org/wiki/{key}" if key else None,
                        }
                    )
                else:
                    compact_pages.append(item)
            return {"pages": compact_pages}
        if "products" in data and isinstance(data["products"], list):
            compact_products = []
            for item in data["products"][:25]:
                if isinstance(item, dict):
                    compact_products.append(
                        {
                            "name": item.get("name"),
                            "brand": item.get("brand"),
                            "price": item.get("price"),
                            "original_price": item.get("original_price"),
                            "currency": item.get("currency", "TL"),
                            "rating": item.get("rating"),
                            "reviews": item.get("review_count"),
                            "url": item.get("url"),
                        }
                    )
                else:
                    compact_products.append(item)
            return {
                "service": data.get("service", "trendyol"),
                "query": data.get("query"),
                "total_found": data.get("total_found", len(compact_products)),
                "products": compact_products,
            }
        if "parsed" in data and isinstance(data["parsed"], dict):
            return data["parsed"]
        # Genel sözlük yanıtları için kırpılmış kopya
        raw_str = json.dumps(data, ensure_ascii=False)
        if len(raw_str) > 12000:
            return {"preview": raw_str[:12000], "truncated": True}
        return data
    return data


class ShadowAgent:
    """Shadow Registry araçlarını OpenAI veya Anthropic Function Calling ile kullanan bağımsız ajan."""

    def __init__(self, registry_root: Path = Path("registry")) -> None:
        load_dotenv(override=False)
        ensure_default_registry(registry_root)
        self.registry_root = registry_root
        self.provider, self.api_key, self.model = self._detect_provider()
        self.tools_map: dict[str, tuple[RegistrySchema, Callable[..., Any]]] = {}
        self.openai_tools: list[dict[str, Any]] = []
        self.anthropic_tools: list[dict[str, Any]] = []
        self._load_tools()

    @staticmethod
    def _detect_provider() -> tuple[str, str, str]:
        openai_key = os.getenv("OPENAI_API_KEY", "").strip()
        anthropic_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
        if openai_key:
            return ("openai", openai_key, os.getenv("OPENAI_MODEL", "gpt-4o").strip())
        if anthropic_key:
            return ("anthropic", anthropic_key, os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-20250514").strip())
        raise RuntimeError(
            "API anahtarı bulunamadı! Lütfen `.env` dosyasına `OPENAI_API_KEY` veya `ANTHROPIC_API_KEY` ekleyin."
        )

    def _load_tools(self) -> None:
        loaded = load_registry_tools(self.registry_root)

        # Eğer registry boşsa generated_client.py yedeklemesini kullan
        if not loaded and Path("generated_client.py").exists():
            from cli import _load_sdk_function

            fn_name, fn = _load_sdk_function(Path("generated_client.py"))
            fallback_schema = RegistrySchema(
                service_name="default",
                target_url="https://hn.algolia.com",
                goal="Varsayılan Shadow-API istemcisi",
                function_name=fn_name,
                explanation=fn.__doc__ or "Shadow-API fonksiyonu",
                method="POST",
                endpoint_url="unknown",
                dynamic_params=[],
                created_at="",
            )
            loaded = [(fallback_schema, fn)]

        for schema, fn in loaded:
            tool_name = schema.function_name
            if tool_name in self.tools_map:
                tool_name = f"{schema.service_name}_{schema.function_name}"

            self.tools_map[tool_name] = (schema, fn)
            json_schema = _build_json_schema_from_tool(schema, fn)
            desc = f"[{schema.service_name}] {schema.explanation}"

            self.openai_tools.append(
                {
                    "type": "function",
                    "function": {
                        "name": tool_name,
                        "description": desc,
                        "parameters": json_schema,
                    },
                }
            )
            self.anthropic_tools.append(
                {
                    "name": tool_name,
                    "description": desc,
                    "input_schema": json_schema,
                }
            )

    def _execute_tool(self, tool_name: str, arguments: dict[str, Any]) -> str:
        """Aracı çalıştırır, süresini milisaniye olarak ölçer ve Rich bilgi kutusu basar."""
        if tool_name not in self.tools_map:
            return json.dumps({"error": f"Araç bulunamadı: {tool_name}"})

        schema, fn = self.tools_map[tool_name]
        merged_args: dict[str, Any] = {}
        for p in schema.dynamic_params:
            if p.default_value is not None:
                merged_args[p.name] = p.default_value
        for k, v in arguments.items():
            if v is not None:
                merged_args[k] = v

        args_repr = ", ".join(f"{k}={repr(v)}" for k, v in merged_args.items())

        start_ns = time.perf_counter()
        try:
            raw_result = fn(**merged_args)
            elapsed_ms = (time.perf_counter() - start_ns) * 1000.0
            compacted = _compact_api_response(raw_result)
        except Exception as exc:
            elapsed_ms = (time.perf_counter() - start_ns) * 1000.0
            compacted = {"error": str(exc)}

        console.print(
            Panel(
                f"[bold yellow]⚡ Shadow-API Çağrıldı:[/bold yellow] "
                f"[bold cyan]{tool_name}({args_repr})[/bold cyan] — "
                f"[bold green]Yanıt Süresi: {elapsed_ms:.2f} ms[/bold green]\n"
                f"[dim]Servis: {schema.service_name} | Endpoint: {schema.method} {schema.endpoint_url}[/dim]",
                border_style="yellow",
                expand=False,
            )
        )

        return json.dumps(compacted, ensure_ascii=False)

    def _run_openai(self, prompt: str) -> str:
        from openai import OpenAI

        base_url = os.getenv("OPENAI_BASE_URL")
        client = OpenAI(api_key=self.api_key, base_url=base_url) if base_url else OpenAI(api_key=self.api_key)

        messages: list[dict[str, Any]] = [
            {"role": "system", "content": AGENT_SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ]

        for _ in range(5):
            response = client.chat.completions.create(
                model=self.model,
                messages=messages,
                tools=self.openai_tools if self.openai_tools else None,
                tool_choice="auto" if self.openai_tools else None,
                temperature=0.2,
            )
            msg = response.choices[0].message

            if not msg.tool_calls:
                return msg.content or ""

            messages.append(msg.model_dump(exclude_none=True))

            for tool_call in msg.tool_calls:
                fn_name = tool_call.function.name
                try:
                    fn_args = json.loads(tool_call.function.arguments or "{}")
                except json.JSONDecodeError:
                    fn_args = {}

                tool_output = self._execute_tool(fn_name, fn_args)
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "content": tool_output,
                    }
                )

        return "Maksimum araç çağrısı sınırına ulaşıldı."

    def _run_anthropic(self, prompt: str) -> str:
        from anthropic import Anthropic

        client = Anthropic(api_key=self.api_key)
        candidate_models = list(
            dict.fromkeys(
                [
                    self.model,
                    "claude-sonnet-4-20250514",
                    "claude-3-7-sonnet-latest",
                    "claude-3-5-sonnet-latest",
                    "claude-3-5-haiku-latest",
                ]
            )
        )

        messages: list[dict[str, Any]] = [{"role": "user", "content": prompt}]

        for _ in range(5):
            response = None
            last_err: Exception | None = None
            for m in candidate_models:
                try:
                    response = client.messages.create(
                        model=m,
                        max_tokens=2048,
                        system=AGENT_SYSTEM_PROMPT,
                        messages=messages,
                        tools=self.anthropic_tools,
                        temperature=0.2,
                    )
                    self.model = m
                    break
                except Exception as exc:
                    last_err = exc
                    continue

            if response is None:
                raise RuntimeError(f"Anthropic çağrısı başarısız: {last_err}")

            tool_use_blocks = [b for b in response.content if getattr(b, "type", None) == "tool_use"]
            text_blocks = [b.text for b in response.content if hasattr(b, "text")]

            if not tool_use_blocks:
                return "\n".join(text_blocks)

            messages.append({"role": "assistant", "content": response.content})

            tool_results: list[dict[str, Any]] = []
            for block in tool_use_blocks:
                fn_name = block.name
                fn_args = block.input if isinstance(block.input, dict) else {}
                tool_output = self._execute_tool(fn_name, fn_args)
                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": tool_output,
                    }
                )

            messages.append({"role": "user", "content": tool_results})

        return "Maksimum araç çağrısı sınırına ulaşıldı."

    def ask(self, prompt: str) -> str:
        """Kullanıcının doğal dil sorusunu Shadow-API araçlarıyla yanıtlar."""
        tool_names = ", ".join(f"[yellow]{name}[/yellow]" for name in self.tools_map.keys())
        console.print(
            Panel.fit(
                f"[bold cyan]Моdel:[/bold cyan] {self.provider.upper()} ({self.model})\n"
                f"[bold cyan]Yüklü Shadow-API Araçları ({len(self.tools_map)}):[/bold cyan] {tool_names}\n"
                f"[bold cyan]Soru:[/bold cyan] [white]{prompt}[/white]",
                title="🤖 Shadow-API Bağımsız AI Ajanı",
                border_style="cyan",
            )
        )

        if self.provider == "openai":
            answer = self._run_openai(prompt)
        else:
            answer = self._run_anthropic(prompt)

        console.print(
            Panel(
                Markdown(answer),
                title="✨ Ajan Yanıtı",
                border_style="green",
            )
        )
        return answer


@app.command()
def main(
    prompt: str = typer.Argument(
        ...,
        help="Doğal dilde sorunuz (örn: 'Thiel Fellowship hakkındaki en yüksek puanlı 3 makaleyi bul, puanlarını ve linklerini özetle')",
    ),
) -> None:
    """Doğal dildeki soruyu Shadow Registry içindeki API araçlarını çağırarak yanıtlar."""
    try:
        agent = ShadowAgent()
        agent.ask(prompt)
    except Exception as exc:
        console.print(f"[bold red]✖ Ajan Hatası:[/bold red] {exc}")
        raise typer.Exit(code=1)


if __name__ == "__main__":
    app()
