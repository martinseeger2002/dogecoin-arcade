"""The sandbox a node module's process puts itself in before it loads the module's code (arcade/modrun.py).

Linux only, and no root or user namespaces (Ubuntu 24.04 refuses those to unprivileged programs):
- **Landlock** (Linux 5.13+): the process may read only Python's own folders and the module's package, and write only
  its own data folder. Nothing else on disk, the arcade's home and keys among it, and nothing in /proc.
- **seccomp**: `socket()` fails, and so does io_uring (which makes sockets without it), so the module reaches no
  network at all, TCP, UDP or a Unix socket, and it cannot `ptrace`, read another process's memory or take another
  process's open files (`pidfd_getfd`). It talks to the arcade only over the pipes it was started with.
- **Limits:** memory, open files and the size of a file it writes.

A machine that cannot do all of this runs no module: `confine` raises, and `available` says why first."""
from __future__ import annotations

import ctypes
import os
import platform
import struct

try:
    import resource
except ImportError:      # Windows (2026-11-06): no `resource`; Linux and macOS import it exactly as before
    resource = None
import sys

#: The generic syscall numbers of Landlock, the same on x86_64 and aarch64.
_CREATE, _ADD_RULE, _RESTRICT = 444, 445, 446
_VERSION_FLAG = 1
_PATH_BENEATH = 1
_EXECUTE, _WRITE_FILE, _READ_FILE, _READ_DIR = 1 << 0, 1 << 1, 1 << 2, 1 << 3
#: Every filesystem right Landlock knows, by its ABI version: 13 rights from 1, REFER from 2, TRUNCATE from 3,
#: IOCTL_DEV from 5.
_ALL_FS = {1: (1 << 13) - 1, 2: (1 << 14) - 1, 3: (1 << 15) - 1, 4: (1 << 15) - 1, 5: (1 << 16) - 1}
_READ = _EXECUTE | _READ_FILE | _READ_DIR
#: Landlock's network rights (ABI 4: bind and connect TCP) and scopes (ABI 6: abstract Unix sockets and signals).
_NET_TCP = (1 << 0) | (1 << 1)
_SCOPED = (1 << 0) | (1 << 1)
#: Per machine: the audit arch seccomp reports, and the syscalls refused: socket, ptrace, process_vm_readv/writev.
#: And (2026-11-05 review) every other way to a socket or another process's insides: io_uring_setup/enter/register
#: (socketpair too; 425-427: an io_uring opens and connects sockets with no socket() call, and 127.0.0.1 is the operator to the
#: arcade), pidfd_open/pidfd_getfd (434, 438: a module runs as the arcade's user, so it could take a copy of the web
#: process's own sockets), process_madvise (440), kcmp, bpf and perf_event_open.
_SHARED = (425, 426, 427, 434, 438, 440)
_ARCHES = {"x86_64": (0xC000003E, (41, 53, 101, 310, 311, 312, 321, 298) + _SHARED),
           "aarch64": (0xC00000B7, (198, 199, 117, 270, 271, 272, 280, 241) + _SHARED)}
_PR_SET_NO_NEW_PRIVS, _PR_SET_SECCOMP, _SECCOMP_MODE_FILTER = 38, 22, 2
_ALLOW, _KILL, _EPERM = 0x7FFF0000, 0x80000000, 0x00050000 | 1
#: What a module's process may use: memory, open files, and the largest file it may write.
MEMORY, FILES, FILE_SIZE = 1 << 30, 64, 64 << 20


class SandboxError(RuntimeError):
    """This machine cannot confine a module, said for the operator."""


def _libc():
    return ctypes.CDLL(None, use_errno=True)


def _arch() -> str:
    return {"amd64": "x86_64", "arm64": "aarch64"}.get(platform.machine().lower(), platform.machine().lower())


def landlock_abi() -> int:
    """Landlock's ABI version on this kernel, 0 where it has none (or it is switched off)."""
    if sys.platform != "linux" or _arch() not in _ARCHES:
        return 0
    found = _libc().syscall(_CREATE, None, ctypes.c_size_t(0), ctypes.c_uint32(_VERSION_FLAG))
    return max(found, 0)


def available() -> str:
    """"" where modules can be confined here, else why not, in a sentence."""
    if sys.platform != "linux":
        return "node modules run on Linux only"
    if _arch() not in _ARCHES:
        return f"node modules run on x86_64 and aarch64 only, not {platform.machine()}"
    if landlock_abi() < 1:
        return "this Linux kernel has no Landlock (5.13 or later, with landlock among its security modules)"
    return ""


def _landlock(read: list[str], write: list[str]) -> None:
    real = landlock_abi()
    abi = min(real, max(_ALL_FS))
    handled = _ALL_FS[abi]
    # The network and signals too, where the kernel has them (2026-11-05 review), with no rule that allows any:
    # ABI 4 refuses every TCP bind and connect (whatever made the socket, io_uring included); ABI 6 keeps signals and
    # abstract Unix sockets inside the module's own domain, so it cannot kill the arcade or a coin daemon (same user).
    if real >= 6:
        attr = struct.pack("QQQ", handled, _NET_TCP, _SCOPED)
    elif real >= 4:
        attr = struct.pack("QQ", handled, _NET_TCP)
    else:
        attr = struct.pack("Q", handled)
    libc = _libc()
    ruleset = libc.syscall(_CREATE, ctypes.c_char_p(attr), ctypes.c_size_t(len(attr)), ctypes.c_uint32(0))
    if ruleset < 0:
        raise SandboxError(f"Landlock refused a ruleset: {os.strerror(ctypes.get_errno())}")
    try:
        for paths, rights in ((read, _READ), (write, handled)):
            for path in paths:
                if not os.path.isdir(path):
                    continue
                fd = os.open(path, os.O_PATH | os.O_CLOEXEC)
                try:
                    rule = struct.pack("=Qi", rights, fd)
                    if libc.syscall(_ADD_RULE, ruleset, _PATH_BENEATH, ctypes.c_char_p(rule), ctypes.c_uint32(0)):
                        raise SandboxError(f"Landlock refused {path}: {os.strerror(ctypes.get_errno())}")
                finally:
                    os.close(fd)
        if libc.syscall(_RESTRICT, ruleset, ctypes.c_uint32(0)):
            raise SandboxError(f"Landlock would not confine: {os.strerror(ctypes.get_errno())}")
    finally:
        os.close(ruleset)


def _seccomp() -> None:
    """A BPF filter: the wrong architecture (or x32's numbers) is killed, the refused syscalls fail, the rest run."""
    audit_arch, refused = _ARCHES[_arch()]
    def op(code: int, k: int, jt: int = 0, jf: int = 0) -> bytes:
        return struct.pack("HBBI", code, jt, jf, k)
    LD, JEQ, JGE, RET = 0x20, 0x15, 0x35, 0x06            # BPF_LD|W|ABS, BPF_JMP|JEQ|K, BPF_JMP|JGE|K, BPF_RET|K
    prog = [op(LD, 4), op(JEQ, audit_arch, 1, 0), op(RET, _KILL), op(LD, 0)]
    if _arch() == "x86_64":
        prog += [op(JGE, 0x40000000, 0, 1), op(RET, _KILL)]
    for nr in refused:
        prog += [op(JEQ, nr, 0, 1), op(RET, _EPERM)]
    prog.append(op(RET, _ALLOW))
    code = b"".join(prog)
    buf = ctypes.create_string_buffer(code)
    fprog = struct.pack("HxxxxxxQ" if struct.calcsize("P") == 8 else "HxxI", len(prog), ctypes.addressof(buf))
    if _libc().prctl(_PR_SET_SECCOMP, _SECCOMP_MODE_FILTER, ctypes.c_char_p(fprog), 0, 0):
        raise SandboxError(f"seccomp refused the filter: {os.strerror(ctypes.get_errno())}")


def python_folders() -> list[str]:
    """Where this Python reads its own code from: the standard library and the installed packages."""
    found = {sys.prefix, sys.base_prefix, sys.exec_prefix, sys.base_exec_prefix}
    found |= {p for p in sys.path if os.path.isabs(p) and os.path.isdir(p)}
    return sorted(os.path.realpath(p) for p in found)


def confine(read: list[str], write: list[str]) -> None:
    """Confine this process for good: read `read` and Python's folders, write `write`, no sockets, the limits."""
    why = available()
    if why:
        raise SandboxError(why)
    for which, most in ((resource.RLIMIT_AS, MEMORY), (resource.RLIMIT_NOFILE, FILES),
                        (resource.RLIMIT_FSIZE, FILE_SIZE), (resource.RLIMIT_CORE, 0)):
        resource.setrlimit(which, (most, most))
    if _libc().prctl(_PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0):
        raise SandboxError("could not set no_new_privs")
    _landlock(python_folders() + [os.path.realpath(p) for p in read], [os.path.realpath(p) for p in write])
    _seccomp()
