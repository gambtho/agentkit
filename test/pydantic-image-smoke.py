"""Exercise the Pydantic runtime's OS, ABI and real stdio MCP tool contract.

Run against an adapter or converter-built agent; no model credentials required.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import shutil
import ssl
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path


def check_runtime() -> None:
    os_release = Path("/etc/os-release").read_text()
    assert "ID=wolfi" in os_release or 'ID="wolfi"' in os_release, "expected Wolfi OS"
    assert sys.version_info[:2] == (3, 12), sys.version
    assert (os.getuid(), os.getgid()) == (1000, 1000), "expected UID/GID 1000"
    assert os.access("/agent", os.W_OK), "/agent must be writable"
    assert os.access("/opt/agentkit/bin/agentkit-serve", os.X_OK), (
        "missing ABI entrypoint"
    )
    assert os.path.samefile("/usr/local/bin/python", sys.executable), (
        "Python alias changed"
    )
    home = Path.home()
    assert home != Path("/") and os.access(home, os.W_OK), "tool HOME must be writable"
    assert ssl.create_default_context().cert_store_stats()["x509_ca"] > 0, (
        "missing CA bundle"
    )
    for command in ("sh", "node", "npm", "npx", "uv", "uvx"):
        assert shutil.which(command), f"missing runtime tool: {command}"
    assert not Path("/src").exists(), "installer sources leaked into runtime"
    for command in ("go", "gcc", "pytest"):
        assert shutil.which(command) is None, (
            f"test/build tool leaked into runtime: {command}"
        )

    # Import native extensions and the absolute CLI before starting tool children.
    import agentkit_serve.agent_factory  # noqa: F401
    import agentkit_serve_common.acp  # noqa: F401
    import grpc  # noqa: F401
    import pydantic_core  # noqa: F401
    import yaml  # noqa: F401

    subprocess.run(
        ["/opt/agentkit/bin/agentkit-serve", "--help"],
        check=True,
        stdout=subprocess.DEVNULL,
    )
    print(f"runtime contract passed: Python {sys.version.split()[0]}", flush=True)


async def check_mcp() -> None:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    with tempfile.TemporaryDirectory(prefix="agentkit-mcp-") as directory:
        Path(directory, "hello.txt").write_text("hello")
        servers = [
            ("npx", ["-y", "@modelcontextprotocol/server-filesystem@1.0.0", directory]),
            ("uvx", ["--from", "mcp-server-fetch==2026.10.10", "mcp-server-fetch"]),
        ]
        for command, args in servers:
            # Match an empty declared tool env: the SDK keeps only safe defaults,
            # never the full agent environment or its model credentials.
            parameters = StdioServerParameters(command=command, args=args, env={})
            async with asyncio.timeout(180):
                async with stdio_client(parameters) as (read, write):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        tools = await session.list_tools()
                        if command == "npx":
                            result = await session.call_tool(
                                "list_directory", {"path": directory}
                            )
                            assert not result.isError, "filesystem MCP tool failed"
                            assert "hello.txt" in str(result.content), (
                                "fixture file not listed"
                            )
                        else:
                            assert "fetch" in {tool.name for tool in tools.tools}, (
                                "missing fetch tool"
                            )
            print(f"{command} MCP smoke passed", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("image", nargs="?")
    parser.add_argument(
        "--platform", choices=("linux/amd64", "linux/arm64"), default="linux/amd64"
    )
    parser.add_argument("--inside", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.inside:
        check_runtime()
        asyncio.run(check_mcp())
        return
    if not args.image:
        parser.error("an adapter or generated agent image is required")
    name = f"agentkit-image-smoke-{uuid.uuid4().hex}"
    try:
        # Stdin carries only this test's source; no host environment is forwarded.
        subprocess.run(
            [
                "docker",
                "run",
                "--name",
                name,
                "-i",
                "--platform",
                args.platform,
                "--entrypoint",
                "/opt/agentkit/bin/python",
                args.image,
                "-",
                "--inside",
            ],
            input=Path(__file__).read_text(),
            text=True,
            check=True,
            timeout=420,
        )
    finally:
        # A host timeout kills the Docker client, not its daemon-managed child.
        failure = sys.exception()
        try:
            subprocess.run(
                ["docker", "rm", "-f", name],
                check=True,
                capture_output=True,
                text=True,
                timeout=30,
            )
        except Exception as cleanup_error:
            if failure is None:
                raise
            print(
                f"Container cleanup failed for {name}: {cleanup_error}", file=sys.stderr
            )


if __name__ == "__main__":
    main()
