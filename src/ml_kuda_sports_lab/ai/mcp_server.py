"""Single MCP server entrypoint for this repository.

Keep Continue pointed to this file only:
  /home/ares/Documents/gitrepos/ml_kuda_sports_lab/mcp_server.py
"""

from __future__ import annotations

import asyncio
import sys
import time
from typing import Any, Literal

from mcp.server.fastmcp import FastMCP

Target = Literal["dev", "prod"]

MCP = FastMCP("ml-kuda-sports-lab")

PIPELINES = {
    "run_etl_train_models": "ml_kuda_sports_lab.etl.gold.mma_gold_train_models",
    "run_catboost": "ml_kuda_sports_lab.etl.gold.mma_gold_catboost",
    "run_ranking": "ml_kuda_sports_lab.etl.gold.mma_gold_ranking",
}


def _truncate(text: str, max_chars: int = 16_000) -> tuple[str, bool]:
    if len(text) <= max_chars:
        return text, False
    return text[:max_chars] + f"\n...[truncated {len(text) - max_chars} chars]", True


async def _run_pipeline(
    module: str,
    *,
    target: Target,
    rebuild: bool,
    timeout_seconds: int,
) -> dict[str, Any]:
    cmd = [sys.executable, "-m", module, "--target", target]
    if rebuild:
        cmd.append("--rebuild")

    started = time.monotonic()
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    timed_out = False
    try:
        out_b, err_b = await asyncio.wait_for(proc.communicate(), timeout=timeout_seconds)
    except asyncio.TimeoutError:
        timed_out = True
        proc.kill()
        out_b, err_b = await proc.communicate()

    stdout, out_trunc = _truncate(out_b.decode("utf-8", errors="replace"))
    stderr, err_trunc = _truncate(err_b.decode("utf-8", errors="replace"))

    return {
        "ok": proc.returncode == 0 and not timed_out,
        "module": module,
        "target": target,
        "command": cmd,
        "exit_code": proc.returncode,
        "timed_out": timed_out,
        "timeout_seconds": timeout_seconds,
        "duration_seconds": round(time.monotonic() - started, 3),
        "stdout": stdout,
        "stderr": stderr,
        "stdout_truncated": out_trunc,
        "stderr_truncated": err_trunc,
    }


@MCP.tool()
def health_check() -> dict[str, Any]:
    """Quick server health check for Continue."""
    return {"ok": True, "server": "ml-kuda-sports-lab", "python": sys.executable}


@MCP.tool()
async def run_etl_train_models(
    target: Target = "dev",
    rebuild: bool = True,
    timeout_seconds: int = 1800,
) -> dict[str, Any]:
    """Run MMA ETL and baseline model training with structured output."""
    return await _run_pipeline(
        PIPELINES["run_etl_train_models"],
        target=target,
        rebuild=rebuild,
        timeout_seconds=timeout_seconds,
    )


@MCP.tool()
async def run_catboost(
    target: Target = "dev",
    rebuild: bool = False,
    timeout_seconds: int = 1800,
) -> dict[str, Any]:
    """Run CatBoost pipeline with structured output."""
    return await _run_pipeline(
        PIPELINES["run_catboost"],
        target=target,
        rebuild=rebuild,
        timeout_seconds=timeout_seconds,
    )


@MCP.tool()
async def run_ranking(
    target: Target = "dev",
    rebuild: bool = False,
    timeout_seconds: int = 1800,
) -> dict[str, Any]:
    """Run MMA rankings pipeline with structured output."""
    return await _run_pipeline(
        PIPELINES["run_ranking"],
        target=target,
        rebuild=rebuild,
        timeout_seconds=timeout_seconds,
    )


def main() -> None:
    MCP.run(transport="stdio")


if __name__ == "__main__":
    main()
