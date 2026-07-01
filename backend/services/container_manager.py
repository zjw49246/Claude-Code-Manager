"""Docker container manager for shared Project isolation.

Each shared Project gets its own Docker container with:
- Project directory mounted as /workspace
- Claude Code CLI available
- Restricted capabilities (cap-drop ALL, read-only root, no-new-privileges)
- Resource limits (memory, CPU, pids)
- No access to host filesystem, SSH keys, or other projects
"""

import asyncio
import logging
import os
import shutil

logger = logging.getLogger(__name__)

SANDBOX_IMAGE = "ccm-sandbox:latest"
CONTAINER_PREFIX = "ccm-project-"


class ContainerManager:
    """Manages Docker containers for shared project isolation."""

    def __init__(self):
        self._containers: dict[int, str] = {}  # project_id -> container_name
        self._locks: dict[int, asyncio.Lock] = {}

    def _lock(self, project_id: int) -> asyncio.Lock:
        if project_id not in self._locks:
            self._locks[project_id] = asyncio.Lock()
        return self._locks[project_id]

    @staticmethod
    async def _run(cmd: list[str], timeout: int = 60) -> tuple[int, str]:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            return 1, "timeout"
        return proc.returncode or 0, (out or b"").decode("utf-8", errors="replace")

    @staticmethod
    def is_docker_available() -> bool:
        return shutil.which("docker") is not None

    async def ensure_container(self, project_id: int, project_path: str,
                                config_dir: str | None = None) -> str:
        """Ensure a running container exists for this project. Returns container name."""
        async with self._lock(project_id):
            name = f"{CONTAINER_PREFIX}{project_id}"

            # Check if already running
            code, out = await self._run(["docker", "inspect", "-f", "{{.State.Running}}", name])
            if code == 0 and "true" in out.lower():
                return name

            # Remove stale container
            await self._run(["docker", "rm", "-f", name])

            os.makedirs(project_path, exist_ok=True)

            cmd = [
                "docker", "run", "-d",
                "--name", name,
                "--security-opt", "no-new-privileges",
                "--cap-drop", "ALL",
                "--read-only",
                "--tmpfs", "/tmp:size=2g",
                "--tmpfs", "/home/sandbox:size=1g",
                "--memory", "4g",
                "--cpus", "2",
                "--pids-limit", "200",
                "-v", f"{project_path}:/workspace",
            ]

            if config_dir:
                cmd.extend(["-v", f"{config_dir}:/home/sandbox/.claude:ro"])

            cmd.extend(["--entrypoint", "tail", SANDBOX_IMAGE, "-f", "/dev/null"])

            code, out = await self._run(cmd, timeout=120)
            if code != 0:
                logger.error("Failed to start container %s: %s", name, out)
                raise RuntimeError(f"Docker container start failed: {out[:500]}")

            self._containers[project_id] = name
            logger.info("Container %s started for project %d", name, project_id)
            return name

    async def exec_command(self, project_id: int, cmd: list[str],
                           env: dict[str, str] | None = None,
                           cwd: str = "/workspace") -> asyncio.subprocess.Process:
        """Execute a command inside the project's container. Returns Process with pipes."""
        name = self._containers.get(project_id, f"{CONTAINER_PREFIX}{project_id}")

        docker_cmd = ["docker", "exec", "-i", "-w", cwd]
        if env:
            for k, v in env.items():
                docker_cmd.extend(["-e", f"{k}={v}"])

        docker_cmd.append(name)
        docker_cmd.extend(cmd)

        return await asyncio.create_subprocess_exec(
            *docker_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=10 * 1024 * 1024,
        )

    async def stop_container(self, project_id: int):
        name = self._containers.pop(project_id, f"{CONTAINER_PREFIX}{project_id}")
        await self._run(["docker", "stop", "-t", "10", name])
        await self._run(["docker", "rm", "-f", name])
        logger.info("Container %s stopped", name)

    async def cleanup_all(self):
        for pid in list(self._containers.keys()):
            try:
                await self.stop_container(pid)
            except Exception:
                pass


async def is_shared_project(project_id: int | None, db_factory) -> bool:
    """Check if a project has been shared to any user."""
    if not project_id:
        return False
    from sqlalchemy import select
    from backend.models.team_share import TeamProjectShare
    async with db_factory() as db:
        result = await db.execute(
            select(TeamProjectShare.id).where(
                TeamProjectShare.project_id == project_id
            ).limit(1)
        )
        return result.scalar_one_or_none() is not None


async def build_sandbox_image():
    """Build the ccm-sandbox Docker image if it doesn't exist."""
    proc = await asyncio.create_subprocess_exec(
        "docker", "images", "-q", SANDBOX_IMAGE,
        stdout=asyncio.subprocess.PIPE,
    )
    out, _ = await proc.communicate()
    if out.strip():
        return

    logger.info("Building sandbox image %s ...", SANDBOX_IMAGE)
    dockerfile = """\
FROM ubuntu:22.04
RUN apt-get update && apt-get install -y --no-install-recommends \\
    curl git ssh-client ca-certificates python3 nodejs npm \\
    && rm -rf /var/lib/apt/lists/*
RUN npm install -g @anthropic-ai/claude-code
RUN groupadd -g 1000 sandbox && useradd -m -u 1000 -g sandbox sandbox
USER sandbox
WORKDIR /workspace
"""
    dockerfile_path = "/tmp/ccm-sandbox-Dockerfile"
    with open(dockerfile_path, "w") as f:
        f.write(dockerfile)

    proc = await asyncio.create_subprocess_exec(
        "docker", "build", "-t", SANDBOX_IMAGE, "-f", dockerfile_path, "/tmp",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    out, _ = await proc.communicate()
    if proc.returncode != 0:
        logger.error("Failed to build sandbox image: %s", out.decode()[:1000])
        raise RuntimeError("Failed to build ccm-sandbox image")
    logger.info("Sandbox image built successfully")
