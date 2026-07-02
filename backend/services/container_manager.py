"""Docker container manager for shared Project isolation.

Each shared Project gets its own Docker container with:
- Project directory mounted as /workspace
- Project-specific git credentials (Deploy Key or HTTPS token)
- Restricted capabilities (cap-drop ALL, read-only root, no-new-privileges)
- Resource limits (memory, CPU, pids)
- No access to host filesystem, SSH keys, or other projects
"""

import asyncio
import logging
import os
import shutil
import tempfile

logger = logging.getLogger(__name__)

SANDBOX_IMAGE = "ccm-sandbox:latest"
CONTAINER_PREFIX = "ccm-project-"


class ContainerManager:
    """Manages Docker containers for shared project isolation."""

    def __init__(self):
        self._containers: dict[int, str] = {}
        self._locks: dict[int, asyncio.Lock] = {}
        self._git_dirs: dict[int, str] = {}  # project_id -> temp dir with git credentials

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

    def _prepare_git_credentials(self, project_id: int, git_credential_type: str | None,
                                  git_ssh_key_path: str | None,
                                  git_https_username: str | None,
                                  git_https_token: str | None) -> str | None:
        """Create a temp directory with project-specific git credentials.

        Returns the temp dir path to mount into the container, or None.
        """
        if not git_credential_type:
            return None

        git_dir = tempfile.mkdtemp(prefix=f"ccm-git-{project_id}-")
        self._git_dirs[project_id] = git_dir

        if git_credential_type == "ssh" and git_ssh_key_path:
            # Copy the Deploy Key into the temp dir
            ssh_dir = os.path.join(git_dir, ".ssh")
            os.makedirs(ssh_dir, mode=0o700)
            key_dest = os.path.join(ssh_dir, "id_rsa")
            try:
                shutil.copy2(git_ssh_key_path, key_dest)
                os.chmod(key_dest, 0o600)
            except Exception:
                logger.warning("Failed to copy SSH key %s for project %d", git_ssh_key_path, project_id)
                return None

            # SSH config: skip host key checking for git
            with open(os.path.join(ssh_dir, "config"), "w") as f:
                f.write("Host *\n  StrictHostKeyChecking no\n  UserKnownHostsFile /dev/null\n  IdentityFile ~/.ssh/id_rsa\n")
            os.chmod(os.path.join(ssh_dir, "config"), 0o600)

        elif git_credential_type == "https" and git_https_token:
            # Git credential helper that returns the token
            cred_script = os.path.join(git_dir, "git-credential-helper.sh")
            username = git_https_username or "oauth2"
            with open(cred_script, "w") as f:
                f.write(f"#!/bin/sh\necho username={username}\necho password={git_https_token}\n")
            os.chmod(cred_script, 0o755)

            # .gitconfig that uses the credential helper
            with open(os.path.join(git_dir, ".gitconfig"), "w") as f:
                f.write(f"[credential]\n  helper = {cred_script}\n")

        return git_dir

    async def ensure_container(self, project_id: int, project_path: str,
                                config_dir: str | None = None,
                                git_credential_type: str | None = None,
                                git_ssh_key_path: str | None = None,
                                git_https_username: str | None = None,
                                git_https_token: str | None = None) -> str:
        """Ensure a running container for this project with isolated git credentials."""
        async with self._lock(project_id):
            name = f"{CONTAINER_PREFIX}{project_id}"

            code, out = await self._run(["docker", "inspect", "-f", "{{.State.Running}}", name])
            if code == 0 and "true" in out.lower():
                return name

            await self._run(["docker", "rm", "-f", name])
            os.makedirs(project_path, exist_ok=True)

            # Prepare project-specific git credentials
            git_dir = self._prepare_git_credentials(
                project_id, git_credential_type,
                git_ssh_key_path, git_https_username, git_https_token
            )

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

            # Mount Claude config (read-only, for auth)
            if config_dir:
                cmd.extend(["-v", f"{config_dir}:/home/sandbox/.claude:ro"])

            # Mount project-specific git credentials
            if git_dir:
                ssh_dir = os.path.join(git_dir, ".ssh")
                if os.path.isdir(ssh_dir):
                    cmd.extend(["-v", f"{ssh_dir}:/home/sandbox/.ssh:ro"])
                gitconfig = os.path.join(git_dir, ".gitconfig")
                if os.path.isfile(gitconfig):
                    cmd.extend(["-v", f"{gitconfig}:/home/sandbox/.gitconfig:ro"])
                cred_script = os.path.join(git_dir, "git-credential-helper.sh")
                if os.path.isfile(cred_script):
                    cmd.extend(["-v", f"{cred_script}:/home/sandbox/git-credential-helper.sh:ro"])

            cmd.extend(["--entrypoint", "tail", SANDBOX_IMAGE, "-f", "/dev/null"])

            code, out = await self._run(cmd, timeout=120)
            if code != 0:
                logger.error("Failed to start container %s: %s", name, out)
                raise RuntimeError(f"Docker container start failed: {out[:500]}")

            self._containers[project_id] = name
            logger.info("Container %s started for project %d (git: %s)",
                        name, project_id, git_credential_type or "none")
            return name

    async def exec_command(self, project_id: int, cmd: list[str],
                           env: dict[str, str] | None = None,
                           cwd: str = "/workspace") -> asyncio.subprocess.Process:
        """Execute a command inside the project's container."""
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
        # Clean up git credentials temp dir
        git_dir = self._git_dirs.pop(project_id, None)
        if git_dir and os.path.isdir(git_dir):
            shutil.rmtree(git_dir, ignore_errors=True)
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
FROM node:22-slim
RUN apt-get update && apt-get install -y --no-install-recommends \\
    curl git ssh-client ca-certificates python3 \\
    && rm -rf /var/lib/apt/lists/*
RUN npm install -g @anthropic-ai/claude-code
RUN groupadd -g 1000 sandbox 2>/dev/null; useradd -m -u 1000 -g 1000 sandbox 2>/dev/null; exit 0
USER 1000
WORKDIR /workspace
"""
    build_dir = "/tmp/ccm-docker-build"
    os.makedirs(build_dir, exist_ok=True)
    dockerfile_path = os.path.join(build_dir, "Dockerfile")
    with open(dockerfile_path, "w") as f:
        f.write(dockerfile)

    proc = await asyncio.create_subprocess_exec(
        "docker", "build", "-t", SANDBOX_IMAGE, build_dir,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    out, _ = await proc.communicate()
    if proc.returncode != 0:
        logger.error("Failed to build sandbox image: %s", out.decode()[:1000])
        raise RuntimeError("Failed to build ccm-sandbox image")
    logger.info("Sandbox image built successfully")
