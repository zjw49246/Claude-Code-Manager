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
import secrets
import shlex
import shutil
import signal
import tempfile
from dataclasses import dataclass

from backend.services.process_safety import require_safe_process_group_id

logger = logging.getLogger(__name__)

SANDBOX_IMAGE = "ccm-sandbox:latest"
CONTAINER_PREFIX = "ccm-project-"
_EXEC_TOKEN_ENV = "CCM_CONTAINER_EXEC_TOKEN"
_EXEC_ROLE_ENV = "CCM_CONTAINER_EXEC_ROLE"


class ContainerExecSpawnCleanupError(RuntimeError):
    """A cancelled docker-exec spawn whose exact cleanup was not proven."""

    def __init__(
        self,
        process: asyncio.subprocess.Process,
        spec: "ContainerExecSpec",
    ):
        super().__init__(
            f"Cancelled container exec {spec.token} could not be reaped"
        )
        self.process = process
        self.spec = spec


@dataclass(frozen=True)
class ContainerExecSpec:
    """Stable token and paths for one exact in-container generation."""

    container_name: str
    token: str
    pid_file: str
    wrapper_path: str | None = None


@dataclass(frozen=True)
class _ContainerExec:
    """Host process identity paired with its in-container generation."""

    process: asyncio.subprocess.Process
    spec: ContainerExecSpec


# ``docker exec`` is only a client connection.  Killing that host process does
# not guarantee that the command in the container stopped.  This supervisor
# gives the inner command its own session, publishes its group identity, and
# reaps/kills any group members which outlive the command leader.  Arguments
# are passed positionally, never interpolated into shell source.
_EXEC_SUPERVISOR = r"""
import os
import signal
import sys
import time

pid_file = sys.argv[1]
command = sys.argv[2:]
if not command:
    raise SystemExit(127)

child = os.fork()
if child == 0:
    os.setsid()
    os.environ["CCM_CONTAINER_EXEC_ROLE"] = "agent"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    fd = os.open(pid_file, flags, 0o600)
    try:
        os.write(fd, str(os.getpid()).encode("ascii"))
        os.fsync(fd)
    finally:
        os.close(fd)
    os.execvpe(command[0], command, os.environ)

if child <= 1:
    # ``killpg(1, sig)`` becomes the special broadcast ``kill(-1, sig)``.
    # fork() must never return such an identity to this parent.
    raise SystemExit(125)

def forward(signum, _frame):
    try:
        os.killpg(child, signum)
    except ProcessLookupError:
        pass

for forwarded in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
    signal.signal(forwarded, forward)

while True:
    try:
        _, status = os.waitpid(child, 0)
        break
    except InterruptedError:
        continue

def group_alive():
    try:
        os.killpg(child, 0)
        return True
    except ProcessLookupError:
        return False

# Tool processes can retain inherited descriptors after the agent leader exits.
# They must not survive into the next task which reuses this project container.
for cleanup_signal, deadline_seconds in (
    (signal.SIGTERM, 1.0),
    (signal.SIGKILL, 1.0),
):
    if not group_alive():
        break
    try:
        os.killpg(child, cleanup_signal)
    except ProcessLookupError:
        break
    deadline = time.monotonic() + deadline_seconds
    while group_alive() and time.monotonic() < deadline:
        time.sleep(0.02)

try:
    os.unlink(pid_file)
except FileNotFoundError:
    pass

if os.WIFEXITED(status):
    raise SystemExit(os.WEXITSTATUS(status))
if os.WIFSIGNALED(status):
    raise SystemExit(128 + os.WTERMSIG(status))
raise SystemExit(1)
"""


# Locate only processes carrying the unguessable per-exec token.  The pid file
# is an optimization, not the trust boundary: an agent can remove files in
# /tmp, so /proc is also scanned before signalling a process group.
_EXEC_CONTROL = r"""
import os
import signal
import sys
import time

token, pid_file, action = sys.argv[1:4]
requested_signal = int(sys.argv[4]) if action == "signal" else 0
wait_seconds = float(sys.argv[5])
token_entry = ("CCM_CONTAINER_EXEC_TOKEN=" + token).encode()
agent_role = b"CCM_CONTAINER_EXEC_ROLE=agent"
supervisor_role = b"CCM_CONTAINER_EXEC_ROLE=supervisor"

def tagged_processes():
    agents = []
    supervisors = []
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        pid = int(entry)
        try:
            with open("/proc/%d/environ" % pid, "rb") as stream:
                values = stream.read().split(b"\0")
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            continue
        if token_entry not in values:
            continue
        if agent_role in values:
            agents.append(pid)
        elif supervisor_role in values:
            supervisors.append(pid)
    return agents, supervisors

deadline = time.monotonic() + wait_seconds
unsafe_group = False
while True:
    agents, supervisors = tagged_processes()
    groups = set()
    for pid in agents:
        try:
            group = os.getpgid(pid)
            if group <= 1:
                unsafe_group = True
            else:
                groups.add(group)
        except ProcessLookupError:
            pass
    if unsafe_group:
        # Never turn a targeted container cleanup into kill(-1, sig).
        raise SystemExit(4)
    if groups or time.monotonic() >= deadline:
        break
    # A check can return immediately when no tagged process exists.  A signal
    # intentionally waits out the short docker-exec startup window: killing
    # the host client does not prove the daemon will not start an already
    # accepted tokenized command a moment later.
    if action == "check" and not supervisors:
        break
    time.sleep(0.02)

alive_groups = []
for group in groups:
    try:
        os.killpg(group, 0)
        alive_groups.append(group)
    except ProcessLookupError:
        pass

if action == "check":
    if alive_groups:
        raise SystemExit(0)
    # A live supervisor with no visible child is a short startup/cleanup
    # transition.  Treat it as alive so callers fail closed.
    raise SystemExit(2 if supervisors else 3)

signalled = False
if alive_groups:
    for group in alive_groups:
        try:
            os.killpg(group, requested_signal)
            signalled = True
        except ProcessLookupError:
            pass
# With no published agent group, include the exact tokenized supervisor (and a
# child in the tiny pre-agent role transition) so cancellation cannot leave a
# command which has not completed setsid yet.  Once an agent group is visible,
# leave its supervisor alive long enough to reap the leader and report the
# command's conventional exit status.
if not alive_groups:
    for pid in supervisors:
        try:
            os.kill(pid, requested_signal)
            signalled = True
        except ProcessLookupError:
            pass
if signalled:
    raise SystemExit(0)
raise SystemExit(3)
"""


class ContainerManager:
    """Manages Docker containers for shared project isolation."""

    def __init__(self):
        self._containers: dict[int, str] = {}
        self._locks: dict[int, asyncio.Lock] = {}
        self._git_dirs: dict[int, str] = {}  # project_id -> temp dir with git credentials
        self._execs: dict[int, _ContainerExec] = {}

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
            await proc.wait()
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
        """Execute a command in an exact, externally controllable inner group."""
        name = self._containers.get(project_id, f"{CONTAINER_PREFIX}{project_id}")
        token = secrets.token_hex(24)
        pid_file = f"/tmp/ccm-exec-{token}.pid"
        spec = ContainerExecSpec(name, token, pid_file)

        docker_cmd = ["docker", "exec", "-i", "-w", cwd]
        if env:
            for k, v in env.items():
                docker_cmd.extend(["-e", f"{k}={v}"])
        docker_cmd.extend([
            "-e", f"{_EXEC_TOKEN_ENV}={token}",
            "-e", f"{_EXEC_ROLE_ENV}=supervisor",
            name,
            "python3",
            "-c",
            _EXEC_SUPERVISOR,
            pid_file,
            *cmd,
        ])

        spawn_kwargs = {
            "stdout": asyncio.subprocess.PIPE,
            "stderr": asyncio.subprocess.PIPE,
            "limit": 10 * 1024 * 1024,
        }
        if os.name == "posix":
            spawn_kwargs["start_new_session"] = True
        spawn = asyncio.create_task(
            asyncio.create_subprocess_exec(*docker_cmd, **spawn_kwargs)
        )
        cancellation: asyncio.CancelledError | None = None
        first_wait = True
        while first_wait or not spawn.done():
            first_wait = False
            try:
                await asyncio.shield(spawn)
            except asyncio.CancelledError as exc:
                if cancellation is None:
                    cancellation = exc

        process = spawn.result()
        self.register_exec(process, spec)
        if cancellation is not None:
            cleanup = asyncio.create_task(
                self._cleanup_cancelled_exec_spawn(process, spec)
            )
            while not cleanup.done():
                try:
                    await asyncio.shield(cleanup)
                except asyncio.CancelledError:
                    continue
                except BaseException:
                    if cleanup.done():
                        break
                    raise
            # On failure keep the exact record in ``_execs`` as fail-closed
            # evidence.  The cleanup exception is logged, but cancellation
            # remains the public outcome expected by the caller.
            cleanup_error: BaseException | None = None
            try:
                cleanup.result()
            except BaseException as exc:
                cleanup_error = exc
                logger.exception(
                    "Could not prove cancelled container exec %s terminal",
                    spec.token,
                    exc_info=(type(exc), exc, exc.__traceback__),
                )
            if cleanup_error is not None:
                raise ContainerExecSpawnCleanupError(
                    process, spec
                ) from cleanup_error
            raise cancellation
        return process

    async def _cleanup_cancelled_exec_spawn(
        self,
        process: asyncio.subprocess.Process,
        spec: ContainerExecSpec,
    ) -> None:
        """Stop both sides of a docker-exec spawn cancelled before return."""

        host_group_alive = False
        host_process_group_id: int | None = None
        if os.name == "posix":
            host_process_group_id = require_safe_process_group_id(
                getattr(process, "pid", None),
                context=f"container exec {spec.token}",
            )
            try:
                os.killpg(host_process_group_id, 0)
                host_group_alive = True
            except ProcessLookupError:
                pass
            except PermissionError:
                host_group_alive = True

        if process.returncode is None or host_group_alive:
            try:
                if host_process_group_id is not None:
                    os.killpg(host_process_group_id, signal.SIGKILL)
                else:
                    process.kill()
            except ProcessLookupError:
                pass
        if process.returncode is None:
            await asyncio.wait_for(
                asyncio.shield(process.wait()), timeout=5.0
            )

        # Killing docker(1) only closes a client connection.  Scan by the
        # unguessable token after host termination, kill startup transitions
        # and agent groups, then require a subsequent empty scan.
        code = await self._control_spec(
            spec,
            action="signal",
            sig=signal.SIGKILL,
            wait_seconds=2.0,
        )
        if code not in (0, 3):
            raise RuntimeError(
                f"Could not signal cancelled container exec {spec.token}"
            )
        deadline = asyncio.get_running_loop().time() + 5.0
        while True:
            code = await self._control_spec(spec, action="check")
            if code == 3:
                break
            if asyncio.get_running_loop().time() >= deadline:
                raise RuntimeError(
                    f"Cancelled container exec {spec.token} survived SIGKILL"
                )
            await asyncio.sleep(0.05)
        self.forget_exec(process)

    def create_pty_wrapper(
        self,
        project_id: int,
        instance_id: int,
    ) -> tuple[str, ContainerExecSpec]:
        """Create a PTY binary wrapper using the same supervised exec protocol."""

        name = self._containers.get(project_id, f"{CONTAINER_PREFIX}{project_id}")
        token = secrets.token_hex(24)
        pid_file = f"/tmp/ccm-exec-{token}.pid"
        # Respect the host's configured temporary directory.  Some deployments
        # put /tmp on a small per-user quota even when the application volume
        # still has ample space; hard-coding /tmp can then make PTY launch fail
        # before Docker is involved.
        wrapper_path = os.path.join(
            tempfile.gettempdir(),
            f"ccm-docker-claude-{instance_id}-{token}.sh",
        )
        spec = ContainerExecSpec(name, token, pid_file, wrapper_path)
        command = [
            "docker",
            "exec",
            "-i",
            "-e",
            f"{_EXEC_TOKEN_ENV}={token}",
            "-e",
            f"{_EXEC_ROLE_ENV}=supervisor",
            name,
            "python3",
            "-c",
            _EXEC_SUPERVISOR,
            pid_file,
            "claude",
        ]
        quoted = " ".join(shlex.quote(part) for part in command)
        with open(wrapper_path, "w", encoding="utf-8") as wrapper:
            wrapper.write(
                "#!/bin/sh\n"
                f"exec {quoted} \"$@\"\n"
            )
        os.chmod(wrapper_path, 0o700)
        return wrapper_path, spec

    def register_exec(
        self,
        process: asyncio.subprocess.Process,
        spec: ContainerExecSpec,
    ) -> None:
        self._execs[id(process)] = _ContainerExec(process=process, spec=spec)

    def owns_exec(self, process: asyncio.subprocess.Process) -> bool:
        record = self._execs.get(id(process))
        return record is not None and record.process is process

    async def _control_exec(
        self,
        process: asyncio.subprocess.Process,
        *,
        action: str,
        sig: signal.Signals | None = None,
        wait_seconds: float = 0.0,
    ) -> int | None:
        record = self._execs.get(id(process))
        if record is None or record.process is not process:
            return None
        return await self._control_spec(
            record.spec,
            action=action,
            sig=sig,
            wait_seconds=wait_seconds,
        )

    async def _control_spec(
        self,
        spec: ContainerExecSpec,
        *,
        action: str,
        sig: signal.Signals | None = None,
        wait_seconds: float = 0.0,
    ) -> int:
        code, output = await self._run(
            [
                "docker",
                "exec",
                spec.container_name,
                "python3",
                "-c",
                _EXEC_CONTROL,
                spec.token,
                spec.pid_file,
                action,
                str(int(sig or 0)),
                str(wait_seconds),
            ],
            timeout=max(5, int(wait_seconds) + 3),
        )
        if code not in (0, 2, 3):
            raise RuntimeError(
                f"Could not {action} container exec in "
                f"{spec.container_name}: {output[:500]}"
            )
        return code

    async def signal_exec(
        self,
        process: asyncio.subprocess.Process,
        sig: signal.Signals,
    ) -> bool:
        """Signal the exact in-container process group for ``process``."""

        code = await self._control_exec(
            process,
            action="signal",
            sig=sig,
            wait_seconds=1.0,
        )
        if code is None:
            return False
        if code == 2 and process.returncode is None:
            raise RuntimeError(
                "Container exec supervisor is live but its agent group "
                "could not be identified"
            )
        return code == 0

    async def exec_is_alive(
        self,
        process: asyncio.subprocess.Process,
    ) -> bool:
        """Return whether the exact tokenized inner group/supervisor survives."""

        code = await self._control_exec(process, action="check")
        if code is None:
            return False
        return code in (0, 2)

    def forget_exec(self, process: asyncio.subprocess.Process) -> None:
        record = self._execs.get(id(process))
        if record is not None and record.process is process:
            self._execs.pop(id(process), None)
            self.discard_spec(record.spec)

    @staticmethod
    def discard_spec(spec: ContainerExecSpec) -> None:
        if spec.wrapper_path:
            try:
                os.unlink(spec.wrapper_path)
            except FileNotFoundError:
                pass

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
