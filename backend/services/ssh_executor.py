"""SSH 远程执行（elastic-worker 设计 §16.3）。

paramiko 是同步库，统一 asyncio.to_thread 包装。每次 run/rsync 建独立连接，
bootstrap 场景下命令少且长耗时，连接复用收益小、状态管理成本高。
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import errno
import logging
import os
import re
import select
import shlex
import socket
import stat
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from cryptography.exceptions import UnsupportedAlgorithm

logger = logging.getLogger(__name__)


_MAX_PRIVATE_KEY_BYTES = 1024 * 1024
_OPENSSH_PUBLIC_KEY_RE = re.compile(
    r"^(?P<kind>ssh-(?:rsa|ed25519)|ecdsa-sha2-nistp(?:256|384|521)) "
    r"(?P<body>[A-Za-z0-9+/]+={0,3})$"
)
_WORKER_INSTANCE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class SSHKeyPreflightError(ValueError):
    """A private key is unsafe or cannot be used non-interactively."""

    def __init__(self, code: str, detail: str):
        super().__init__(detail)
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class SSHKeyMaterial:
    """Validated local key material safe to pass to Paramiko/OpenSSH."""

    private_key_path: str
    openssh_public_key: str


@dataclass(frozen=True)
class SSHProbeResult:
    """Structured SSH reachability result without credential contents."""

    ok: bool
    error_code: str | None = None
    detail: str | None = None


def _canonical_private_key_path(key_path: str | os.PathLike[str]) -> Path:
    if not isinstance(key_path, (str, os.PathLike)):
        raise SSHKeyPreflightError("key_path_invalid", "SSH private key path must be a path")
    raw = os.fspath(key_path)
    if not raw or not raw.strip():
        raise SSHKeyPreflightError("key_path_missing", "SSH private key path is empty")
    expanded = os.path.expandvars(os.path.expanduser(raw))
    path = Path(expanded)
    if not path.is_absolute():
        raise SSHKeyPreflightError(
            "key_path_not_absolute", "SSH private key path must resolve to an absolute path",
        )
    return path


def _read_private_key_bytes(path: Path) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except FileNotFoundError as exc:
        raise SSHKeyPreflightError("key_not_found", "SSH private key file does not exist") from exc
    except PermissionError as exc:
        raise SSHKeyPreflightError("key_unreadable", "SSH private key file is not readable") from exc
    except OSError as exc:
        code = "key_symlink" if exc.errno == errno.ELOOP else "key_unreadable"
        detail = (
            "SSH private key must not be a symlink"
            if code == "key_symlink"
            else "SSH private key file could not be opened safely"
        )
        raise SSHKeyPreflightError(code, detail) from exc

    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise SSHKeyPreflightError("key_not_regular", "SSH private key must be a regular file")
        if info.st_uid != os.geteuid():
            raise SSHKeyPreflightError(
                "key_wrong_owner", "SSH private key must be owned by the CCM service user",
            )
        if stat.S_IMODE(info.st_mode) & 0o077:
            raise SSHKeyPreflightError(
                "key_permissions", "SSH private key permissions must not grant group/other access",
            )
        if info.st_size <= 0 or info.st_size > _MAX_PRIVATE_KEY_BYTES:
            raise SSHKeyPreflightError(
                "key_size", "SSH private key has an invalid or excessive size",
            )
        chunks: list[bytes] = []
        remaining = _MAX_PRIVATE_KEY_BYTES + 1
        while remaining > 0:
            chunk = os.read(fd, min(65536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
        if len(data) != info.st_size or len(data) > _MAX_PRIVATE_KEY_BYTES:
            raise SSHKeyPreflightError(
                "key_changed", "SSH private key changed while it was being validated",
            )
        return data
    finally:
        os.close(fd)


def _public_key_from_private_bytes(data: bytes) -> str:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec, ed25519, rsa

    try:
        try:
            private_key = serialization.load_ssh_private_key(data, password=None)
        except ValueError:
            private_key = serialization.load_pem_private_key(data, password=None)
    except TypeError as exc:
        raise SSHKeyPreflightError(
            "key_encrypted", "Encrypted SSH private keys are not supported for unattended workers",
        ) from exc
    except (ValueError, UnsupportedAlgorithm) as exc:
        raise SSHKeyPreflightError(
            "key_invalid", "SSH private key format is invalid or unsupported",
        ) from exc

    if not isinstance(private_key, (rsa.RSAPrivateKey, ed25519.Ed25519PrivateKey, ec.EllipticCurvePrivateKey)):
        raise SSHKeyPreflightError("key_invalid", "SSH private key type is unsupported")
    public_key = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.OpenSSH,
        format=serialization.PublicFormat.OpenSSH,
    ).decode("ascii")
    validate_openssh_public_key(public_key)
    return public_key

def preflight_private_key(key_path: str | os.PathLike[str]) -> SSHKeyMaterial:
    """Validate a private key and derive its comment-free OpenSSH public key.

    The file is opened with ``O_NOFOLLOW`` and must be a private regular file
    owned by the CCM service user. Encrypted keys are rejected because Worker
    bootstrap has no interactive passphrase channel.
    """

    path = _canonical_private_key_path(key_path)
    data = _read_private_key_bytes(path)
    return SSHKeyMaterial(
        private_key_path=str(path),
        openssh_public_key=_public_key_from_private_bytes(data),
    )


def derive_openssh_public_key(key_path: str | os.PathLike[str]) -> str:
    """Return the validated private key's OpenSSH public key."""

    return preflight_private_key(key_path).openssh_public_key


def validate_openssh_public_key(public_key: str) -> str:
    """Return one normalized OpenSSH public key, rejecting comments/options."""

    if not isinstance(public_key, str):
        raise ValueError("SSH public key must be a string")
    normalized = " ".join(public_key.strip().split())
    match = _OPENSSH_PUBLIC_KEY_RE.fullmatch(normalized)
    if match is None:
        raise ValueError("SSH public key must contain exactly a supported key type and base64 body")
    try:
        decoded = base64.b64decode(match.group("body"), validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ValueError("SSH public key body is not valid base64") from exc
    if len(decoded) < 16 or len(decoded) > 8 * 1024:
        raise ValueError("SSH public key body has an invalid size")
    from cryptography.hazmat.primitives import serialization

    try:
        serialization.load_ssh_public_key(normalized.encode("ascii"))
    except (ValueError, UnsupportedAlgorithm) as exc:
        raise ValueError("SSH public key structure is invalid or unsupported") from exc
    return normalized


def worker_known_hosts_path(instance_id: str) -> str:
    """Return a stable per-instance trust store, avoiding private-IP reuse.

    Worker traffic uses private IPs, which AWS can recycle after termination.
    A global ``known_hosts`` entry therefore incorrectly binds a future EC2 to
    an old instance's host key.  The cloud instance id is the actual lifecycle
    identity and gives each replacement a fresh TOFU store while still
    detecting a host-key change on the same instance.
    """
    if not isinstance(instance_id, str) or _WORKER_INSTANCE_ID_RE.fullmatch(instance_id) is None:
        raise ValueError("invalid Worker cloud instance id for known_hosts")
    return str(
        Path.home() / ".ssh" / "ccm-worker-known-hosts" / instance_id
    )


def _prepare_known_hosts_file(raw_path: str) -> str:
    path = Path(os.path.expandvars(os.path.expanduser(raw_path)))
    if not path.is_absolute():
        raise ValueError("Worker known_hosts path must be absolute")
    parent = path.parent
    parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if parent.is_symlink() or path.is_symlink():
        raise ValueError("Worker known_hosts path must not be a symlink")
    os.chmod(parent, 0o700)
    flags = os.O_WRONLY | os.O_CREAT | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode):
            raise ValueError("Worker known_hosts path must be a regular file")
        os.fchmod(descriptor, 0o600)
    finally:
        os.close(descriptor)
    return str(path)


def _classify_probe_exception(exc: BaseException) -> SSHProbeResult:
    if isinstance(exc, SSHKeyPreflightError):
        return SSHProbeResult(False, exc.code, exc.detail)

    import paramiko

    if isinstance(exc, paramiko.BadHostKeyException):
        return SSHProbeResult(False, "host_key_mismatch", "SSH host key does not match the trusted key")
    if isinstance(exc, paramiko.AuthenticationException):
        return SSHProbeResult(False, "authentication_failed", "SSH rejected the configured user/private key")
    if isinstance(exc, paramiko.NoValidConnectionsError):
        nested = tuple(exc.errors.values())
        if nested and all(isinstance(item, ConnectionRefusedError) for item in nested):
            return SSHProbeResult(False, "connection_refused", "TCP port 22 refused the SSH connection")
        if any(isinstance(item, (TimeoutError, socket.timeout)) for item in nested):
            return SSHProbeResult(False, "connection_timeout", "TCP connection to SSH timed out")
        if any(
            isinstance(item, OSError) and item.errno in {errno.ENETUNREACH, errno.EHOSTUNREACH}
            for item in nested
        ):
            return SSHProbeResult(False, "network_unreachable", "Worker private network is unreachable")
        return SSHProbeResult(False, "connection_failed", "Could not establish a TCP connection to SSH")
    if isinstance(exc, socket.gaierror):
        return SSHProbeResult(False, "name_resolution_failed", "SSH host name could not be resolved")
    if isinstance(exc, (TimeoutError, socket.timeout)):
        return SSHProbeResult(False, "connection_timeout", "SSH connection or command timed out")
    if isinstance(exc, paramiko.SSHException):
        return SSHProbeResult(False, "ssh_protocol_error", "SSH negotiation failed")
    if isinstance(exc, OSError):
        if exc.errno == errno.ECONNREFUSED:
            return SSHProbeResult(False, "connection_refused", "TCP port 22 refused the SSH connection")
        if exc.errno in {errno.ENETUNREACH, errno.EHOSTUNREACH}:
            return SSHProbeResult(False, "network_unreachable", "Worker private network is unreachable")
        return SSHProbeResult(False, "connection_failed", "SSH connection failed")
    return SSHProbeResult(False, "unexpected_error", f"SSH probe failed ({type(exc).__name__})")


class SSHExecutor:
    def __init__(
        self,
        host: str,
        user: str,
        key_path: str,
        *,
        known_hosts_path: str | None = None,
    ):
        self.host = host
        self.user = user
        self.key_path = os.path.expandvars(os.path.expanduser(key_path))
        self.known_hosts_path = known_hosts_path
        self.last_probe_result: SSHProbeResult | None = None

    def _execute_sync(
        self,
        command: str,
        timeout: int,
        input_data: bytes | None,
    ) -> tuple[int, str]:
        import paramiko

        if isinstance(timeout, bool) or timeout <= 0:
            raise ValueError("SSH timeout must be positive")
        deadline = time.monotonic() + float(timeout)
        key = preflight_private_key(self.key_path)
        client = paramiko.SSHClient()
        if self.known_hosts_path:
            client.load_host_keys(_prepare_known_hosts_file(self.known_hosts_path))
        else:
            client.load_system_host_keys()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        try:
            connect_timeout = max(1.0, min(float(timeout), 15.0))
            client.connect(
                self.host,
                username=self.user,
                key_filename=key.private_key_path,
                timeout=connect_timeout,
                banner_timeout=connect_timeout,
                auth_timeout=connect_timeout,
                allow_agent=False,
                look_for_keys=False,
            )
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"SSH connection timed out after {timeout}s")
            _stdin, stdout, _stderr = client.exec_command(
                command, timeout=remaining,
            )
            channel = stdout.channel
            if input_data:
                channel.sendall(input_data)
            channel.shutdown_write()

            out_chunks: list[bytes] = []
            err_chunks: list[bytes] = []
            while True:
                made_progress = False
                while channel.recv_ready():
                    out_chunks.append(channel.recv(65536))
                    made_progress = True
                while channel.recv_stderr_ready():
                    err_chunks.append(channel.recv_stderr(65536))
                    made_progress = True
                if (
                    channel.exit_status_ready()
                    and not channel.recv_ready()
                    and not channel.recv_stderr_ready()
                ):
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    channel.close()
                    raise TimeoutError(f"SSH command timed out after {timeout}s")
                if not made_progress:
                    select.select([channel], [], [], min(0.1, remaining))

            exit_code = channel.recv_exit_status()
            out = b"".join(out_chunks).decode(errors="replace")
            err = b"".join(err_chunks).decode(errors="replace")
            return exit_code, out + (("\n" + err) if err.strip() else "")
        finally:
            client.close()

    def _run_sync(self, command: str, timeout: int) -> tuple[int, str]:
        return self._execute_sync(command, timeout, None)

    def _run_with_input_sync(
        self, command: str, input_data: bytes, timeout: int,
    ) -> tuple[int, str]:
        return self._execute_sync(command, timeout, input_data)

    async def run(
        self, command: str, timeout: int = 300, *, sensitive: bool = False,
    ) -> tuple[int, str]:
        """执行远程命令，返回 (exit_code, output)。"""
        logger.debug(
            "ssh %s: %s", self.host,
            "[sensitive command redacted]" if sensitive else command[:200],
        )
        return await asyncio.to_thread(self._run_sync, command, timeout)

    async def run_with_input(
        self,
        command: str,
        input_data: str | bytes,
        timeout: int = 300,
        *,
        sensitive: bool = True,
    ) -> tuple[int, str]:
        """Execute ``command`` and deliver data through the SSH channel stdin.

        The input is never placed in argv or logs. stdout and stderr are
        drained concurrently before the exit status is collected, so a remote
        command producing large output on either stream cannot deadlock.
        """

        if not isinstance(input_data, (str, bytes)):
            raise TypeError("input_data must be str or bytes")
        payload = input_data.encode("utf-8") if isinstance(input_data, str) else input_data
        logger.debug(
            "ssh %s: %s", self.host,
            "[sensitive command redacted]" if sensitive else command[:200],
        )
        return await asyncio.to_thread(
            self._run_with_input_sync, command, payload, timeout,
        )

    async def probe(self, timeout: int = 10) -> SSHProbeResult:
        """Check SSH authentication and return a stable, sanitized failure code."""

        try:
            code, _ = await self.run("true", timeout=timeout, sensitive=True)
            result = (
                SSHProbeResult(True)
                if code == 0
                else SSHProbeResult(False, "remote_command_failed", "SSH probe command returned non-zero")
            )
        except Exception as exc:
            result = _classify_probe_exception(exc)
        self.last_probe_result = result
        return result

    async def check_alive(self, timeout: int = 10) -> bool:
        return (await self.probe(timeout=timeout)).ok

    def _rsync_ssh_command(self) -> str:
        key = preflight_private_key(self.key_path)
        host_key_options = "-o StrictHostKeyChecking=accept-new"
        if self.known_hosts_path:
            known_hosts = _prepare_known_hosts_file(self.known_hosts_path)
            host_key_options += f" -o UserKnownHostsFile={shlex.quote(known_hosts)}"
        return (
            f"ssh -i {shlex.quote(key.private_key_path)} "
            "-o IdentitiesOnly=yes -o BatchMode=yes "
            f"{host_key_options} -o ConnectTimeout=15"
        )

    async def copy_file(self, local_path: str, remote_path: str, timeout: int = 120) -> None:
        """复制单个文件到远端同路径（先 mkdir -p 再 rsync，无 filter）。"""
        import os as _os
        remote_dir = _os.path.dirname(remote_path)
        code, out = await self.run(f"mkdir -p {shlex.quote(remote_dir)}", timeout=30)
        if code != 0:
            raise RuntimeError(f"mkdir failed: {out[-500:]}")
        ssh_opt = self._rsync_ssh_command()
        cmd = ["rsync", "-az", "-e", ssh_opt, local_path,
               f"{self.user}@{self.host}:{remote_path}"]

        def _sync() -> None:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
            if r.returncode != 0:
                raise RuntimeError(f"rsync file failed ({r.returncode}): {r.stderr[-1000:]}")

        await asyncio.to_thread(_sync)

    async def rsync_from(
        self,
        remote_path: str,
        local_path: str,
        timeout: int = 600,
        delete: bool = True,
    ) -> None:
        """rsync 远端目录/文件到本地（迁移用：全量含 .git 与未提交改动，无过滤）。"""
        import os as _os
        _os.makedirs(_os.path.dirname(local_path.rstrip("/")) or "/", exist_ok=True)
        ssh_opt = self._rsync_ssh_command()
        cmd = ["rsync", "-az"] + (["--delete"] if delete else []) + [
            "-e", ssh_opt, f"{self.user}@{self.host}:{remote_path}", local_path]

        def _sync() -> None:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
            if r.returncode != 0:
                raise RuntimeError(f"rsync from failed ({r.returncode}): {r.stderr[-2000:]}")

        await asyncio.to_thread(_sync)

    async def rsync_to(
        self,
        local_path: str,
        remote_path: str,
        excludes: list[str] | None = None,
        timeout: int = 600,
    ) -> None:
        """rsync 本地目录到远端（用系统 rsync over ssh，增量+保权限）。"""
        # .gitignore 的忽略规则自动生效（.venv/node_modules/*.db 等），
        # 与仓库保持同步，避免手工 exclude 列表漂移；excludes 只补充
        # git 跟踪之外必须排除的内容
        cmd = ["rsync", "-az", "--delete", "--filter", ":- .gitignore"]
        for ex in excludes or []:
            cmd += ["--exclude", ex]
        ssh_opt = self._rsync_ssh_command()
        cmd += ["-e", ssh_opt, local_path, f"{self.user}@{self.host}:{remote_path}"]

        def _sync() -> None:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
            if r.returncode != 0:
                raise RuntimeError(f"rsync failed ({r.returncode}): {r.stderr[-2000:]}")

        await asyncio.to_thread(_sync)
