"""One writer/signer service per local project data directory, Windows + Linux."""
import os


class Instance:
    def __init__(self, directory):
        directory.mkdir(parents=True, exist_ok=True)
        self.file = open(directory/'service.lock', 'a+b')
        try:
            self.file.seek(0)
            if self.file.read(1) == b'':
                self.file.write(b'0')
                self.file.flush()
            self.file.seek(0)
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.file.close()
            raise RuntimeError('another-sol-follow-service-is-running') from None

    def close(self):
        self.file.close()  # Do not unlink a live lock inode.
