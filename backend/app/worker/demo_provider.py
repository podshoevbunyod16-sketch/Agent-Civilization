"""Deterministic, explicitly non-LLM provider used for the investor demo.

This module implements only a fixed allow-list of seeded agent behaviors. It never
executes agent-supplied Python or shell commands. External web search is performed
separately by the trusted worker tool and passed in as verified provider results.
"""

from __future__ import annotations

import json
import math
import re
import statistics
from typing import Any

DEMO_AGENT_SLUGS = frozenset(
    {
        "researcher-prime",
        "polyglot-nexus",
        "data-oracle",
        "code-reviewer",
        "task-planner",
        "demo-ux-architect",
        "demo-tkinter-builder",
        "demo-tkinter-reviewer",
        "demo-tkinter-test-designer",
        "demo-tkinter-accessibility",
    }
)


def _source_code(title: str, theme: str) -> str:
    safe_title = json.dumps(title[:70] or "Моё приложение", ensure_ascii=False)
    safe_theme = json.dumps(theme[:80] or "Тёмная тема", ensure_ascii=False)
    return "\n".join(
        [
            "import tkinter as tk",
            "from tkinter import ttk",
            "",
            "root = tk.Tk()",
            f"root.title({safe_title})",
            "root.geometry('760x520')",
            "root.minsize(520, 360)",
            "root.configure(bg='#0b1220')",
            "style = ttk.Style(root)",
            "style.theme_use('clam')",
            "style.configure('TFrame', background='#0b1220')",
            "style.configure('TLabel', background='#0b1220', foreground='#e6edf7')",
            "frame = ttk.Frame(root, padding=28)",
            "frame.pack(fill='both', expand=True)",
            f"ttk.Label(frame, text={safe_title}, font=('Arial', 22, 'bold')).pack(anchor='w')",
            f"ttk.Label(frame, text={safe_theme}).pack(anchor='w', pady=(8, 20))",
            "entry = ttk.Entry(frame)",
            "entry.pack(fill='x', pady=8)",
            "ttk.Button(frame, text='Добавить', command=lambda: print(entry.get()))"
            ".pack(anchor='w')",
            "root.mainloop()",
            "",
        ]
    )


def _analyze_values(values: list[Any]) -> dict[str, Any]:
    if not isinstance(values, list):
        return {"error": "values must be a list", "count": 0}
    if len(values) > 1000:
        return {"error": "demo provider accepts at most 1000 values", "count": 0}
    try:
        numbers = sorted(float(value) for value in values)
    except (TypeError, ValueError, OverflowError):
        return {"error": "values must contain finite numbers", "count": 0}
    if not all(math.isfinite(value) for value in numbers):
        return {"error": "values must contain finite numbers", "count": 0}
    if not numbers:
        return {"error": "empty dataset", "count": 0}
    quartiles = (
        statistics.quantiles(numbers, n=4)
        if len(numbers) > 3
        else [numbers[0], numbers[-1], numbers[-1]]
    )
    q1, q3 = quartiles[0], quartiles[2]
    iqr = q3 - q1
    outliers = [value for value in numbers if value < q1 - 1.5 * iqr or value > q3 + 1.5 * iqr]
    return {
        "count": len(numbers),
        "mean": round(statistics.fmean(numbers), 3),
        "median": round(statistics.median(numbers), 3),
        "stdev": round(statistics.pstdev(numbers), 3) if len(numbers) > 1 else 0.0,
        "outliers": outliers,
    }


def run_demo_agent(slug: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Run a fixed built-in behavior; reject anything outside the demo allow-list."""
    if slug not in DEMO_AGENT_SLUGS:
        raise ValueError("agent is outside the Demo Provider allow-list")

    if slug == "researcher-prime":
        results = payload.get("search_results") or []
        return {
            "query": str(payload.get("query", "")),
            "sources": results,
            "source_count": len(results),
            "search_status": "completed" if results else "no_results",
            "provider": "Tavily",
        }

    if slug == "polyglot-nexus":
        text = str(payload.get("text", ""))
        return {
            "source_length": len(text),
            "target_lang": payload.get("target_lang", "en"),
            "segments": [part.strip() for part in text.split(".") if part.strip()],
            "demo_note": "DEMO template: segments text; no machine translation model was called.",
        }

    if slug == "data-oracle":
        return _analyze_values(payload.get("values", []))

    if slug == "code-reviewer":
        code = str(payload.get("code", ""))
        patterns = {
            "eval_usage": r"\beval\s*\(",
            "exec_usage": r"\bexec\s*\(",
            "hardcoded_secret": r"(password|secret|token)\s*=\s*['\"][^'\"]+['\"]",
            "bare_except": r"except\s*:",
        }
        findings = [
            {"rule": name, "position": match.start()}
            for name, pattern in patterns.items()
            for match in re.finditer(pattern, code, re.IGNORECASE)
        ]
        return {
            "lines": code.count("\n") + 1,
            "findings": findings,
            "severity": "high" if findings else "clean",
        }

    if slug == "task-planner":
        stages = ["сбор данных", "анализ", "проверка", "отчёт"]
        return {
            "goal": str(payload.get("goal", "")),
            "steps": [
                {"order": index, "title": stage, "depends_on": [] if index == 1 else [index - 1]}
                for index, stage in enumerate(stages, start=1)
            ],
            "critical_path": len(stages),
        }

    if slug == "demo-ux-architect":
        prompt = str(payload.get("prompt", ""))
        return {
            "prompt": prompt,
            "spec": {
                "layout": ["header", "workspace", "status bar"],
                "theme": "dark",
                "acceptance": ["responsive", "clear hierarchy", "keyboard friendly"],
            },
        }

    if slug == "demo-tkinter-builder":
        request = payload.get("input", payload)
        if not isinstance(request, dict):
            request = {}
        prompt = str(request.get("prompt", "Моё приложение"))[:70]
        dependencies = payload.get("depends_on_outputs") or {}
        design = dependencies.get("design") or {}
        spec = design.get("spec") or {}
        title = prompt or "Моё приложение"
        return {
            "filename": "app.py",
            "title": title,
            "source_code": _source_code(title, str(spec.get("theme", "Тёмная тема"))),
            "components": ["header", "text input", "action button"],
        }

    dependencies = payload.get("depends_on_outputs") or {}
    build = dependencies.get("build") or {}
    source = str(build.get("source_code", ""))

    if slug == "demo-tkinter-reviewer":
        checks = {
            "tk_root": "tk.Tk()" in source,
            "event_loop": "mainloop()" in source,
            "no_eval": "eval(" not in source,
            "no_exec": "exec(" not in source,
        }
        try:
            compile(source, "app.py", "exec")
            checks["python_syntax"] = True
        except SyntaxError:
            checks["python_syntax"] = False
        return {"checks": checks, "passed": all(checks.values()), "demo_provider": "deterministic"}

    if slug == "demo-tkinter-test-designer":
        try:
            compile(source, "app.py", "exec")
            syntax_ok = True
        except SyntaxError:
            syntax_ok = False
        return {
            "test_plan": [
                "Tkinter source parses as Python",
                "Window opens without exception (manual runtime check required)",
                "Text field accepts input (manual GUI check required)",
                "Button is keyboard accessible (manual accessibility check required)",
            ],
            "components_seen": build.get("components", []),
            "python_syntax_check": "passed" if syntax_ok else "failed",
            "gui_tests": "not_run",
        }

    if slug == "demo-tkinter-accessibility":
        notes = ["Проверь контраст и порядок фокуса вручную"]
        if "ttk.Entry" in source:
            notes.append("Поле ввода доступно через Tab")
        if "ttk.Button" in source:
            notes.append("Проверь клавиатурный запуск кнопки")
        return {"heuristic_review": notes, "screen_reader_tested": False}

    raise ValueError("unhandled demo agent")
