"""Opening files below a folder by walking descriptors down from it, so no symlink is ever followed."""
import os
import stat
from pathlib import Path


def open_dir(name, dir_fd=None):
    """A descriptor for a directory, refused when it is a symlink. The caller closes it."""
    return os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=dir_fd)


def open_root(root):
    """A descriptor for an absolute folder, opened by walking every component from / with O_NOFOLLOW, so a symlink
    anywhere in the path, an ancestor included, raises OSError. The caller closes it."""
    root = os.path.normpath(str(Path(root)))
    if not os.path.isabs(root):
        raise OSError(f"not an absolute path: {root}")
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in root.split("/"):
            if part:
                nxt = open_dir(part, fd)
                os.close(fd)
                fd = nxt
        return fd
    except BaseException:
        os.close(fd)
        raise


def open_file(name, dir_fd):
    """A descriptor for one regular file in the directory dir_fd, refused when it is a symlink or anything but a regular
    file. O_NONBLOCK so a FIFO swapped in opens at once and is refused, instead of hanging. The caller closes it."""
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dir_fd)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError(f"not a regular file: {name}")
    except OSError:
        os.close(fd)
        raise
    return fd


def open_below(root, relative):
    """A descriptor for one regular file below root, opened by walking descriptors down from the root, so a symlink
    met anywhere, even one swapped in after a check, raises OSError instead of leading out of the folder. Every
    component must be a plain name: never empty, . or ... The caller closes the descriptor."""
    *folders, name = relative.split("/")
    if any(part in ("", ".", "..") for part in (*folders, name)):
        raise OSError(f"not a plain path below the root: {relative}")
    fds = []
    try:
        # Every component of the root is walked with O_NOFOLLOW, the last and its ancestors alike.
        fds.append(open_root(root))
        for folder in folders:
            fds.append(open_dir(folder, fds[-1]))
        return open_file(name, fds[-1])
    finally:
        for d in fds:
            os.close(d)


def read_fd(fd, limit=None):
    """Every byte of an open file, or its first limit bytes, then closes it."""
    try:
        chunks, left = [], float("inf") if limit is None else limit
        while left > 0 and (chunk := os.read(fd, int(min(1 << 20, left)))):
            chunks.append(chunk)
            left -= len(chunk)
        return b"".join(chunks)
    finally:
        os.close(fd)
