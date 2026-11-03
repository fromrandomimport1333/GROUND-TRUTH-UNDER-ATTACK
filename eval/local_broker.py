"""Owned temporary loopback broker for evaluation; never manages a system service."""
import os
from pathlib import Path
import secrets
import socket
import subprocess
import time
from core.local_config import load_manifest
from core.secure_files import protect
from scripts.setup_local import setup_local


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1',0))
        return sock.getsockname()[1]


class LocalBroker:
    def __init__(self,root: Path,executable: Path, *, plaintext_control=False):
        self.executable=Path(executable).absolute()
        if not self.executable.is_file():
            raise FileNotFoundError('Mosquitto executable not found')
        self.password=secrets.token_urlsafe(32).encode()
        self.root=setup_local(root,self.password,port=free_port())
        self.manifest=load_manifest(self.root)
        self.plain_port=None
        if plaintext_control:
            self.plain_port=free_port()
            while self.plain_port==self.manifest['port']: self.plain_port=free_port()
            config=self.root/'mosquitto.conf'
            config.write_text('per_listener_settings true\n'+config.read_text()+
                f'\nlistener {self.plain_port} 127.0.0.1\nallow_anonymous true\n',encoding='utf-8')
        self.process=None;self.log=None

    def start(self):
        if self.process is not None:
            raise RuntimeError('owned broker already started')
        self.log=(self.root/'broker.log').open('ab')
        try:
            self.process=subprocess.Popen([str(self.executable),'-c',str(self.root/'mosquitto.conf')],stdout=self.log,stderr=subprocess.STDOUT)
            deadline=time.monotonic()+10
            while True:
                if self.process.poll() is not None:
                    raise RuntimeError('owned broker exited during startup; inspect temporary broker log')
                try:
                    with socket.create_connection(('127.0.0.1',self.manifest['port']),timeout=.2): return
                except OSError:
                    if time.monotonic()>=deadline: raise TimeoutError('owned broker startup timed out')
                    time.sleep(.05)
        except BaseException:
            self.stop();raise

    def stop(self):
        process,self.process=self.process,None
        try:
            if process is not None and process.poll() is None:
                process.terminate()
                try: process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill();process.wait(timeout=5)
        finally:
            if self.log is not None:
                self.log.close();self.log=None

    def __enter__(self):
        self.start();return self

    def __exit__(self,*args):
        self.stop()
