"""Fail closed if another local API process owns this runtime's database."""
import os
from pathlib import Path

class ProcessLease:
    def __init__(self,path):
        self.path=Path(path)
        self.file=None

    def acquire(self):
        self.path.parent.mkdir(parents=True,exist_ok=True)
        self.file=self.path.open('a+b')
        try:
            if self.path.stat().st_size==0:
                self.file.write(b'1');self.file.flush()
            self.file.seek(0)
            if os.name=='nt':
                import msvcrt
                msvcrt.locking(self.file.fileno(),msvcrt.LK_NBLCK,1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
        except OSError:
            self.file.close();self.file=None
            raise RuntimeError('Another API process owns this runtime. Use one worker; shared scheduling is required for multiple instances.') from None
        return self

    def release(self):
        if self.file:
            self.file.close();self.file=None
