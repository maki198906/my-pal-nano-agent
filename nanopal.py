#!/usr/bin/env -S uv run --script
# /// script
# dependencies = [
#   "openai>=1.0.0",  # keep in sync with pyproject.toml
#   "rich>=13.0.0",   # keep in sync with pyproject.toml
# ]
# ///
"""
nanopal.py — Minimal code-first agent powered by uv + OpenRouter.

⚠️  WARNING: This is a LEARNING TOOL, NOT a security boundary.
     The exec() sandbox (filtered builtins) can be bypassed via Python's
     __class__.__bases__.__subclasses__() chain. Do NOT use with untrusted
     model output in production. Real isolation requires a separate OS
     process / container.

Usage:
  export OPENROUTER_API_KEY="sk-or-..."
  nanopal "Your task description"

Model: deepseek/deepseek-v4-flash (via OpenRouter)
"""

import io
import os
import re
import subprocess
import sys
import time
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from openai import OpenAI
from rich.console import Console
from rich.panel import Panel
from rich.rule import Rule
from rich.syntax import Syntax

console = Console()

# ── Configuration ────────────────────────────────────────────────────────────

TASK = sys.argv[1] if len(sys.argv) > 1 else ""

# Load .env file (primary way to provide secrets)
_env_path = Path(__file__).resolve().parent / ".env"
if _env_path.exists():
    for line in _env_path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip().strip("\"'"))

# Check .env first, then fall back to env var
API_KEY = os.getenv("OPENROUTER_API_KEY")
if not API_KEY:
    console.print("[bold red]❌ OPENROUTER_API_KEY not found.[/]")
    console.print()
    console.print("   Primary way — create a .env file next to nanopal.py:")
    console.print(f"   {_env_path}")
    console.print("   Contents of .env:")
    console.print("     OPENROUTER_API_KEY=sk-or-...")
    console.print()
    console.print("   Alternative — export the variable:")
    console.print("     export OPENROUTER_API_KEY='sk-or-...'")
    sys.exit(1)

MODEL = os.getenv("NANOPAL_MODEL", "deepseek/deepseek-v4-flash")
BASE_URL = os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
WORKSPACE = str(Path.cwd())
MAX_STEPS = 50
TEMPERATURE = 0.2
TIMEOUT_S = 30
MAX_CHARS = 8000
ALLOW_WRITE = False
ALLOW_COMMANDS = ["ls", "cat", "pwd", "echo", "head", "tail", "wc", "rg"]

# How much of the model's raw output / observation text to show in panels
DISPLAY_MODEL_CHARS = 1200
DISPLAY_OBS_CHARS = 500

# ── System Prompt ────────────────────────────────────────────────────────────

SYSTEM_PROMPT = f"""You are a code-first agent. Reply with executable Python code only — NO prose, NO markdown outside ```python blocks.

Available builtins: print, len, str, int, float, list, dict, tuple, set, range, enumerate, zip, sorted, min, max, sum, abs, type, isinstance, hasattr, getattr, Exception, ValueError, TypeError, KeyError, IndexError, AttributeError, FileNotFoundError, PermissionError, and other standard safe builtins.

📌 NOTE: The 'import' statement refers to YOUR generated code only (the harness itself has its own imports).
🚫 import is DISABLED in your generated code. Do NOT write import statements — they will fail.

Tools (use these instead of import):
- list_dir(path='.'): List directory contents (confined to workspace)
- read_file(path, max_chars=4000): Read file contents (confined to workspace)
- write_file(path, content): Write file (only if ALLOW_WRITE=True, confined to workspace)
- exec_cmd(args): Run shell command (allowed: {", ".join(ALLOW_COMMANDS)}, runs inside workspace only)

When task is complete, call: final_answer(result)

Constraints:
- All file paths and commands confined to workspace: {WORKSPACE}
- Allowed commands: {", ".join(ALLOW_COMMANDS)}
- Max output: {MAX_CHARS} chars

💡 When searching for project files, exclude virtual environments (.venv/), caches (__pycache__/, .mypy_cache/, .pytest_cache/), and node_modules/ — focus on user-authored files only.
"""

# ── Tool Definitions ─────────────────────────────────────────────────────────

DONE = False
FINAL_RESULT = None

# Safe builtins — everything except __import__, open, exec, eval, compile, input
_SAFE_BUILTINS = {
    # Constants
    "True": True,
    "False": False,
    "None": None,
    # I/O
    "print": print,
    # Types
    "bool": bool,
    "int": int,
    "float": float,
    "str": str,
    "bytes": bytes,
    "bytearray": bytearray,
    "memoryview": memoryview,
    "list": list,
    "dict": dict,
    "tuple": tuple,
    "set": set,
    "frozenset": frozenset,
    "range": range,
    "slice": slice,
    "type": type,
    "object": object,
    # Iteration
    "enumerate": enumerate,
    "zip": zip,
    "map": map,
    "filter": filter,
    "sorted": sorted,
    "reversed": reversed,
    "iter": iter,
    "next": next,
    "any": any,
    "all": all,
    # Math
    "min": min,
    "max": max,
    "sum": sum,
    "abs": abs,
    "round": round,
    "hex": hex,
    "oct": oct,
    "bin": bin,
    "ord": ord,
    "chr": chr,
    "hash": hash,
    "id": id,
    "len": len,
    "repr": repr,
    "format": format,
    # Introspection
    "isinstance": isinstance,
    "issubclass": issubclass,
    "hasattr": hasattr,
    "getattr": getattr,
    "setattr": setattr,
    "delattr": delattr,
    "callable": callable,
    "vars": vars,
    "dir": dir,
    # OOP
    "property": property,
    "staticmethod": staticmethod,
    "classmethod": classmethod,
    "super": super,
    # Exceptions
    "Exception": Exception,
    "ValueError": ValueError,
    "TypeError": TypeError,
    "KeyError": KeyError,
    "IndexError": IndexError,
    "AttributeError": AttributeError,
    "NameError": NameError,
    "OSError": OSError,
    "FileNotFoundError": FileNotFoundError,
    "PermissionError": PermissionError,
    "StopIteration": StopIteration,
    "RuntimeError": RuntimeError,
    "NotImplementedError": NotImplementedError,
    "ImportError": ImportError,
    "ModuleNotFoundError": ModuleNotFoundError,
    "ZeroDivisionError": ZeroDivisionError,
    "ArithmeticError": ArithmeticError,
    "LookupError": LookupError,
    "EnvironmentError": EnvironmentError,
    "Warning": Warning,
    "UserWarning": UserWarning,
    "DeprecationWarning": DeprecationWarning,
    "SyntaxError": SyntaxError,
    "IndentationError": IndentationError,
    "TabError": TabError,
}


MAX_HISTORY = 20


def clip(x, n=MAX_CHARS):
    s = str(x)
    return s[:n] + "\n…[truncated]" if len(s) > n else s


def trim_messages(messages, keep=MAX_HISTORY):
    """Keep system prompt + task + last N messages to prevent context overflow."""
    if len(messages) <= keep + 2:
        return messages
    return messages[:2] + messages[-(keep):]


def safe_path(user_input):
    """Resolve and validate path is within WORKSPACE."""
    ws = Path(WORKSPACE).resolve()
    requested = (ws / user_input).resolve()
    try:
        requested.relative_to(ws)
    except ValueError:
        raise ValueError(f"Path '{user_input}' escapes workspace")
    return requested


def list_dir(path="."):
    """List directory contents."""
    p = safe_path(path)
    if not p.is_dir():
        raise NotADirectoryError(str(p))
    return sorted(x.name + ("/" if x.is_dir() else "") for x in p.iterdir())


def read_file(path, max_chars=4000):
    """Read file with size limit."""
    p = safe_path(path)
    content = p.read_text(encoding="utf-8", errors="replace")
    return clip(content, min(max_chars, MAX_CHARS))


def write_file(path, content):
    """Write/create file (gated by ALLOW_WRITE)."""
    if not ALLOW_WRITE:
        raise PermissionError("write_file is disabled (set ALLOW_WRITE=True)")
    p = safe_path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(str(content), encoding="utf-8")
    return f"Wrote {len(str(content))} bytes to {p}"


def exec_cmd(args):
    """Execute shell command (whitelist only)."""
    if args[0] not in ALLOW_COMMANDS:
        raise PermissionError(f"Command '{args[0]}' not in allowlist")

    # Block dangerous rg flags that can execute arbitrary commands
    if args[0] == "rg":
        for arg in args:
            if arg == "--pre" or arg.startswith("--pre="):
                raise PermissionError(
                    "rg --pre is blocked (can execute arbitrary commands)"
                )

    # Confine path-like arguments to workspace
    checked_args = [args[0]]
    for arg in args[1:]:
        # Skip flags (start with -)
        if arg.startswith("-"):
            checked_args.append(arg)
            continue
        # Treat argument as a path if it looks like one
        if (
            arg.startswith("/")
            or "/.." in arg
            or arg in (".", "..")
            or arg.startswith("..")
        ):
            try:
                checked_args.append(str(safe_path(arg)))
            except ValueError:
                raise PermissionError(f"Path '{arg}' escapes workspace")
        else:
            checked_args.append(arg)

    try:
        result = subprocess.run(
            checked_args,
            capture_output=True,
            timeout=TIMEOUT_S,
            text=True,
            cwd=WORKSPACE,
            env={"PATH": os.environ.get("PATH", "")},
        )
    except subprocess.TimeoutExpired:
        return "Error: Command timed out"

    output_parts = []
    if result.stdout:
        output_parts.append(f"stdout:\n{result.stdout}")
    if result.stderr:
        output_parts.append(f"stderr:\n{result.stderr}")
    output = (
        "\n\n".join(output_parts) or f"(exit code {result.returncode} with no output)"
    )
    return clip(output, MAX_CHARS)


def final_answer(value):
    """Agent calls this when task is complete."""
    global DONE, FINAL_RESULT
    DONE = True
    FINAL_RESULT = value
    return value


# ── Main Loop ────────────────────────────────────────────────────────────────


def extract_python(content):
    """Extract Python code from model output (handles ```python blocks)."""
    blocks = re.findall(r"```(?:python|py)?\s*\n?(.*?)```", content, re.DOTALL)
    if blocks:
        return "\n".join(b.strip() for b in blocks).strip()
    # Fallback: treat entire output as code
    return content.strip()


def print_header():
    console.print(
        Panel.fit(
            f"[bold]Model:[/] {MODEL}\n"
            f"[bold]Workspace:[/] {WORKSPACE}\n"
            f"[bold]Task:[/] {TASK}",
            title="🧠 nanopal",
            border_style="cyan",
        )
    )


def print_step_rule(step):
    console.print(Rule(f"Step {step + 1}/{MAX_STEPS}", style="dim"))


def print_model_output(content):
    display = content[:DISPLAY_MODEL_CHARS]
    if len(content) > DISPLAY_MODEL_CHARS:
        display += "\n… (output truncated)"
    console.print(
        Panel(
            Syntax(display, "python", theme="monokai", word_wrap=True),
            title="🤖 Model",
            border_style="blue",
            expand=False,
        )
    )


def print_observation(result):
    display = result[:DISPLAY_OBS_CHARS]
    if len(result) > DISPLAY_OBS_CHARS:
        display += "…"
    is_error = result.startswith("Error:")
    console.print(
        Panel(
            display,
            title="📎 Observation",
            border_style="red" if is_error else "green",
            expand=False,
        )
    )


def print_final(final_result):
    console.print(
        Panel.fit(
            str(final_result),
            title="✅ Task complete",
            border_style="bold green",
        )
    )


def main():
    global DONE, FINAL_RESULT
    DONE = False
    FINAL_RESULT = None

    if not TASK:
        console.print('Usage: nanopal "your task description"')
        sys.exit(1)

    # OpenRouter client
    client = OpenAI(
        api_key=API_KEY,
        base_url=BASE_URL,
        default_headers={
            "HTTP-Referer": "https://github.com/maxim/nanopal",
            "X-Title": "nanopal",
        },
    )

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": TASK},
    ]

    print_header()

    for step in range(MAX_STEPS):
        print_step_rule(step)

        # 1. Call LLM with retry + backoff (spinner while waiting)
        content = None
        succeeded = False
        with console.status("[cyan]Thinking...", spinner="dots") as status:
            for attempt in range(3):
                try:
                    response = client.chat.completions.create(
                        model=MODEL,
                        temperature=TEMPERATURE,
                        messages=messages,
                    )
                    content = response.choices[0].message.content
                    succeeded = True
                    break
                except Exception as e:
                    if attempt == 2:
                        console.print(
                            f"[bold red]❌ LLM call failed after 3 attempts:[/] {e}"
                        )
                        break
                    wait = 2**attempt
                    status.update(f"[yellow]Retry {attempt + 1}/3 in {wait}s: {e}")
                    time.sleep(wait)

        if not succeeded:
            break
        if not content or not content.strip():
            console.print("[yellow]⚠️  Empty response from model, retrying...[/]")
            continue

        print_model_output(content)

        # 2. Add to history
        messages.append({"role": "assistant", "content": content})

        # 3. Parse and execute
        code = extract_python(content)
        stdout_buffer = io.StringIO()
        stderr_buffer = io.StringIO()

        exec_globals = {
            "__builtins__": _SAFE_BUILTINS,
            "list_dir": list_dir,
            "read_file": read_file,
            "write_file": write_file,
            "exec_cmd": exec_cmd,
            "final_answer": final_answer,
        }

        try:
            with redirect_stdout(stdout_buffer), redirect_stderr(stderr_buffer):
                exec(code, exec_globals)

            stdout_text = stdout_buffer.getvalue().strip()
            stderr_text = stderr_buffer.getvalue().strip()

            if DONE:
                result = f"Final answer: {clip(FINAL_RESULT)}"
            else:
                observations = []
                if stdout_text:
                    observations.append(f"stdout:\n{clip(stdout_text)}")
                if stderr_text:
                    observations.append(f"stderr:\n{clip(stderr_text)}")
                result = (
                    "\n\n".join(observations) or "Executed successfully (no output)"
                )

        except FileNotFoundError:
            result = "Error: FileNotFoundError: File not found"
        except PermissionError as e:
            result = f"Error: PermissionError: {str(e)}"
        except subprocess.TimeoutExpired:
            result = "Error: TimeoutError: Command took too long"
        except Exception as e:
            result = f"Error: {type(e).__name__}: {str(e)}"

        # 4. Check if done
        if DONE:
            print_final(FINAL_RESULT)
            break

        # 5. Feed observation back
        print_observation(result)
        messages.append({"role": "user", "content": result})

        # Trim message history to prevent context overflow
        messages = trim_messages(messages)

    else:
        console.print(
            f"[yellow]⚠️  Max steps ({MAX_STEPS}) reached without final_answer()[/]"
        )


if __name__ == "__main__":
    main()
