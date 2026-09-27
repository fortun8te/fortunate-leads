"""Many Tor circuits across several tor processes, one Egress per circuit.

Each process gets its own DataDirectory and one SocksPort with IsolateSOCKSAuth, so every SOCKS
username is its own circuit; a circuit swap is a new username (Egress.rotate()). Several
processes spread CPU (one tor process is single-threaded for crypto) and guard selection.

    pool = TorPool(processes=4, circuits_per_process=40, base_port=9260, root='data/tor')
    pool.start(); egresses = pool.egresses(); ...; pool.stop()

Measured 2026-09-24 (igscraper, one process): 36 circuits -> ~152 profiles/min, near-linear from
24 circuits. Circuits beyond ~1-2 hundred increasingly land on the same exit relays (about 1,400
exits exist and selection is bandwidth-weighted), so scale-out past that is an estimate to verify
with metrics, not a promise.
"""
import os
import shutil
import subprocess
import tempfile
import time

from scale.egress import Egress

TORRC = """SocksPort 127.0.0.1:{port} IsolateSOCKSAuth
DataDirectory {data}
Log notice file {log}
AvoidDiskWrites 1
CircuitBuildTimeout 30
LearnCircuitBuildTimeout 0
MaxClientCircuitsPending 64
NewCircuitPeriod 600
MaxCircuitDirtiness 1800
"""


class TorPool:
    def __init__(self, processes=2, circuits_per_process=16, base_port=9260, root=None, tor_bin='tor'):
        self.processes, self.cpp, self.base_port = int(processes), int(circuits_per_process), int(base_port)
        self.root = root or tempfile.mkdtemp(prefix='fl-tor-')
        self.tor_bin = shutil.which(tor_bin) or tor_bin
        self.procs = []

    def ports(self):
        return [self.base_port + i for i in range(self.processes)]

    def start(self, timeout=120):
        os.makedirs(self.root, exist_ok=True)
        for i, port in enumerate(self.ports()):
            d = os.path.join(self.root, 'p%d' % i)
            os.makedirs(d, mode=0o700, exist_ok=True)
            log = os.path.join(d, 'tor.log')
            rc = os.path.join(d, 'torrc')
            with open(rc, 'w') as f:
                f.write(TORRC.format(port=port, data=d, log=log))
            self.procs.append((subprocess.Popen([self.tor_bin, '-f', rc], stdout=subprocess.DEVNULL,
                                                stderr=subprocess.DEVNULL), log))
        deadline = time.time() + timeout
        pending = set(range(len(self.procs)))
        while pending and time.time() < deadline:
            for i in list(pending):
                p, log = self.procs[i]
                if p.poll() is not None:
                    raise RuntimeError('tor process %d exited (see %s)' % (i, log))
                try:
                    with open(log) as f:
                        if 'Bootstrapped 100%' in f.read():
                            pending.discard(i)
                except FileNotFoundError:
                    pass
            time.sleep(1)
        if pending:
            raise RuntimeError('tor did not bootstrap in %ss: processes %s' % (timeout, sorted(pending)))
        return self

    def egresses(self):
        out = []
        for i, port in enumerate(self.ports()):
            for c in range(self.cpp):
                out.append(Egress('tor%d-%d' % (i, c), 'tor', host='127.0.0.1', port=port, group='tor%d' % i))
        return out

    def stop(self):
        for p, _ in self.procs:
            if p.poll() is None:
                p.terminate()
        for p, _ in self.procs:
            try:
                p.wait(10)
            except subprocess.TimeoutExpired:
                p.kill()
        self.procs = []
