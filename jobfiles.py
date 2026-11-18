"""Read and write the files in a Hands job directory."""
import errno
import json
import os
import stat


def file_size(path):
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def open_regular(path, flags):
    """Open a job-dir file the seat itself could have replaced: never through a symlink, and
    only a regular file (O_NONBLOCK, so a FIFO planted there cannot block the open)."""
    fd = os.open(path, flags | os.O_NOFOLLOW | os.O_NONBLOCK, 0o644)
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise OSError(errno.EINVAL, 'not a regular file', path)
    return fd


def read_tail(path, offset, limit=4000):
    """Up to the last `limit` bytes after `offset`, as text."""
    try:
        with os.fdopen(open_regular(path, os.O_RDONLY), 'rb') as f:
            f.seek(max(offset, file_size(path) - limit))
            return f.read().decode('utf-8', 'replace')
    except OSError:
        return ''


def append_json(path, value):
    """Append one JSON line in a single write, so concurrent writers never interleave. Text
    that is not valid UTF-8 (half an emoji a CLI cut) is written with '?' in its place."""
    fd = open_regular(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT)
    try:
        os.write(fd, (json.dumps(value, ensure_ascii=False) + '\n').encode('utf-8', 'replace'))
    finally:
        os.close(fd)


def write_atomic(path, text, mode=None):
    """Replace a file's whole content at once, by rename (as UTF-8, as append_json writes). The
    temporary file is made new, never through a link the seat planted; `mode` sets its
    permissions (default: as open() makes a file)."""
    temporary = '%s.tmp%d' % (path, os.getpid())
    if os.path.lexists(temporary):
        os.remove(temporary)
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                 0o666 if mode is None else mode)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8', errors='replace') as f:
            f.write(text)
        os.replace(temporary, path)
    except OSError:
        os.remove(temporary)
        raise


def read_json(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def read_lines(path, offset):
    """The complete lines after `offset` -> (their text, the offset after them)."""
    try:
        with os.fdopen(open_regular(path, os.O_RDONLY), 'rb') as f:
            f.seek(offset)
            data = f.read()
    except OSError:
        return '', offset
    data = data[:data.rfind(b'\n') + 1]
    return data.decode('utf-8', 'replace'), offset + len(data)


def line_end_and_size(path):
    """Last complete line's end and the file's size, both as byte offsets in one snapshot."""
    try:
        with os.fdopen(open_regular(path, os.O_RDONLY), 'rb') as f:
            end = size = f.seek(0, os.SEEK_END)
            while end:
                start = max(0, end - 4096)
                f.seek(start)
                newline = f.read(end - start).rfind(b'\n')
                if newline >= 0:
                    return start + newline + 1, size
                end = start
    except OSError:
        return 0, 0
    return 0, size
