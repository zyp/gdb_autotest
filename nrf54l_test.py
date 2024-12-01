#!/usr/bin/env python3

from pygdbmi.gdbcontroller import GdbController
from contextlib import contextmanager
from dataclasses import dataclass
import subprocess
import time
import sys
import os
import logging
import coloredlogs

blackmagic_exec = os.environ.get('BLACKMAGIC', 'blackmagic')
orbtrace_exec = os.environ.get('ORBTRACE', 'orbtrace')
gdb_exec = os.environ.get('GDB', 'arm-none-eabi-gdb')

logger = logging.getLogger()
coloredlogs.install(
    level = 'DEBUG',
    fmt = '%(asctime)s %(levelname)s %(message)s'
)

if not '--debug' in sys.argv[1:]: # TODO: Use click or something.
    logging.getLogger('pygdbmi').setLevel('ERROR')

@contextmanager
def start_bmda():
    try:
        with open('bmda_log.txt', 'w') as bmda_log:
            proc = subprocess.Popen(
                args = f'"{blackmagic_exec}" -v 1',
                shell = True,
                stdin = subprocess.DEVNULL,
                stdout = bmda_log,
                stderr = bmda_log,
            )

        time.sleep(0.1)
        if not proc.poll() is None:
            logger.error('BMDA quit unexpectedly:')
            for line in open('bmda_log.txt'):
                logger.info(line.strip())

            raise RuntimeError('BMDA quit unexpectedly')

        yield

    finally:
        proc.terminate()
        proc.wait()

def set_power(enabled):
    subprocess.check_call(f'"{orbtrace_exec}" --voltage vtpwr,5 --power vtpwr,{'on' if enabled else 'off'}', shell = True)
    time.sleep(0.1)

def filter_types(messages, *types):
    for msg in messages:
        if msg['type'] in types:
            yield msg

def filter_result(messages):
    return filter_types(messages, 'result')

@dataclass
class MemoryRegion:
    addr: int
    size: int
    access: str

class Gdb:
    def __init__(self):
        self.gdb = GdbController([gdb_exec, '--interpreter=mi4'])

        # Skip the initial wall of text.
        self.gdb.get_gdb_response()

        self.gdb.write('-gdb-set mem inaccessible-by-default 0')
        #self.gdb.write('-gdb-set debug remote 1')

    def version(self):
        return self.gdb.write('-gdb-version')[0]['payload'].strip()

    def monitor(self, command):
        return [msg['payload'].strip() for msg in self.gdb.write(f'interpreter console "monitor {command}"') if msg['type'] == 'target']

    def memory_map(self):
        lines = [msg['payload'].strip() for msg in self.gdb.write(f'interpreter console "info mem"') if msg['type'] == 'console']
        if lines[0] == 'Using memory regions provided by the target.':
            lines.pop(0)
        regions = {}
        for line in lines[1:]:
            num, enb, low, high, access, attrs = line.split(maxsplit = 5)
            low = int(low, 16)
            high = int(high, 16)
            regions[low] = MemoryRegion(
                addr = low,
                size = high - low,
                access = access,
            )
        return regions

    def compare_sections(self):
        lines = [msg['payload'].strip() for msg in self.gdb.write(f'interpreter console "compare-sections"') if msg['type'] == 'console']
        return all(line.endswith(': matched.') for line in lines)

    def peek(self, addr, type = 'unsigned'):
        res, = filter_result(self.gdb.write(f'-data-evaluate-expression {{{type}}}{addr:#x}'))
        assert res['message'] in ['done', 'error']
        if res['message'] == 'error':
            return None
        return int(res['payload']['value'])

    def poke(self, addr, value, type = 'unsigned'):
        res, = filter_result(self.gdb.write(f'-data-evaluate-expression {{{type}}}{addr:#x}={value:#x}'))
        assert res['message'] in ['done', 'error']
        return res['message'] == 'done'

    def attach(self, pid):
        res, = filter_result(self.gdb.write(f'-target-attach {pid}'))
        assert res['message'] in ['done', 'error']
        return res['message'] == 'done'

    def detach(self):
        res, = filter_result(self.gdb.write(f'-target-detach'))
        assert res['message'] in ['done', 'error']
        return res['message'] == 'done'
    
    def file(self, filename):
        res, = filter_result(self.gdb.write(f'-file-exec-and-symbols {filename}'))
        assert res['message'] in ['done', 'error']
        return res['message'] == 'done'

    def load(self):
        res, = filter_result(self.gdb.write(f'-target-download'))
        assert res['message'] in ['done', 'error']
        return res['message'] == 'done'
    
    def breakpoint(self, location):
        res, = filter_result(self.gdb.write(f'-break-insert {location}'))
        assert res['message'] in ['done', 'error']
        if res['message'] == 'error':
            return None
        return int(res['payload']['number'])

    def start(self):
        msgs = filter_types(self.gdb.write('-exec-run --start'), 'result', 'notify')
        for res in msgs:
            if res['message'] == 'error':
                return False
            if res['message'] == 'stopped':
                return True

        return None

class BlackmagicGdb(Gdb):
    def __init__(self):
        super().__init__()

        self.gdb.write('-target-select extended-remote localhost:2000')

    def bmd_version(self):
        return self.monitor('version')[:2]
    
    def swd_scan(self):
        targets = [(int(pid), name) for pid, name in (line.split(maxsplit = 1) for line in self.monitor('swd_scan')[3:])]
        for i, (pid, _) in enumerate(targets, 1):
            assert i == pid, 'PIDs are not contiguous'
        return [name for _, name in targets]

    def erase_mass(self):
        res = self.monitor('erase_mass')

        return res == ['Erasing device Flash:', 'done']

def main():
    with start_bmda():
        gdb = BlackmagicGdb()
        logger.info(gdb.version())
        for line in gdb.bmd_version():
            logger.info(line)
    
        # Find the device and get it into a known initial state (erased):

        logger.info('Turning on target')
        set_power(False)
        set_power(True)

        logger.info('Performing SWD scan')
        match gdb.swd_scan():
            case []:
                return logger.error('No targets')
            case ['Nordic nRF54L Access Port (protected)']:
                logger.info('Found locked nRF54L')
                ctrl_ap = 1
            case ['Nordic nRF54L M33', 'Nordic nRF54L Access Port']:
                logger.info('Found unlocked nRF54L')
                ctrl_ap = 2
            case _ as res:
                return logger.error(f'Unexpected scan result: {res}')

        logger.info('Performing mass erase via CTRL-AP')
        if not gdb.attach(ctrl_ap):
            return logger.error('Could not attach to CTRL-AP')

        if not gdb.erase_mass():
            return logger.error('Could not erase')

        if not gdb.detach():
            return logger.error('Could not detach from CTRL-AP')

        logger.info('Rescanning')
        match gdb.swd_scan():
            case []:
                return logger.error('No targets')
            case ['Nordic nRF54L Access Port (protected)']:
                return logger.error('Found locked nRF54L, expected unlocked')
            case ['Nordic nRF54L M33', 'Nordic nRF54L Access Port']:
                logger.info('Found unlocked nRF54L')
            case _ as res:
                return logger.error(f'Unexpected scan result: {res}')

        # Powercycle the device, confirm that it auto-locks, then unlock it and attach:

        logger.info('Cycling target power')
        set_power(False)
        set_power(True)

        logger.info('Performing SWD scan')
        match gdb.swd_scan():
            case []:
                return logger.error('No targets')
            case ['Nordic nRF54L Access Port (protected)']:
                logger.info('Found locked nRF54L')
            case ['Nordic nRF54L M33', 'Nordic nRF54L Access Port']:
                return logger.error('Found unlocked nRF54L, expected locked')
            case _ as res:
                return logger.error(f'Unexpected scan result: {res}')

        logger.info('Unlocking target through mass erase')
        if not gdb.attach(1):
            return logger.error('Could not attach to CTRL-AP')

        if not gdb.erase_mass():
            return logger.error('Could not erase')

        if not gdb.detach():
            return logger.error('Could not detach from CTRL-AP')

        logger.info('Rescanning')
        match gdb.swd_scan():
            case []:
                return logger.error('No targets')
            case ['Nordic nRF54L Access Port (protected)']:
                return logger.error('Found locked nRF54L, expected unlocked')
            case ['Nordic nRF54L M33', 'Nordic nRF54L Access Port']:
                logger.info('Found unlocked nRF54L')
            case _ as res:
                return logger.error(f'Unexpected scan result: {res}')

        if not gdb.attach(1):
            return logger.error('Could not attach')

        memory_map = gdb.memory_map()

        if region := memory_map.pop(0x0000_0000, None):
            if region.size != 1524 * 1024:
                return logger.error(f'Unexpected rram size: {region.size}')
        else:
            return logger.error('Rram not in memory map')

        if region := memory_map.pop(0x00ff_d000, None):
            if region.size != 0x1000:
                return logger.error(f'Unexpected UICR size: {region.size}')
        else:
            return logger.error('UICR not in memory map')

        if region := memory_map.pop(0x2000_0000, None):
            if region.size != 256 * 1024:
                return logger.error(f'Unexpected ram size: {region.size}')
        else:
            return logger.error('Ram not in memory map')

        if memory_map:
            return logger.error(f'Unexpected regions in memory_map: {memory_map}')

        # Load firmware:

        logger.info('Loading firmware')

        if not gdb.file('nrf54l_firmware.elf'):
            return logger.error('GDB could not open firmware file')

        if not gdb.load():
            return logger.error('Writing firmware failed')

        if not gdb.compare_sections():
            return logger.error('Verifying rram contents failed')

        if not gdb.detach():
            return logger.error('Could not detach')

        # Powercycle the device, confirm that it now stays unlocked and attach:

        logger.info('Cycling target power')
        set_power(False)
        set_power(True)

        logger.info('Performing SWD scan')
        match gdb.swd_scan():
            case []:
                return logger.error('No targets')
            case ['Nordic nRF54L Access Port (protected)']:
                return logger.error('Found locked nRF54L, expected unlocked')
            case ['Nordic nRF54L M33', 'Nordic nRF54L Access Port']:
                logger.info('Found unlocked nRF54L')
            case _ as res:
                return logger.error(f'Unexpected scan result: {res}')

        if not gdb.attach(1):
            return logger.error('Could not attach')

        logger.info('Running to main')
        if not gdb.start():
            return logger.error('Starting firmware failed')

        # Test mass erase

        logger.info('Testing mass erase')
        if not gdb.erase_mass():
            return logger.error('Could not erase')

        if gdb.compare_sections():
            return logger.error('Verifying rram contents succeeded unexpectedly after erase')

        logger.info('Loading firmware again')

        if not gdb.load():
            return logger.error('Writing firmware failed')

        if not gdb.compare_sections():
            return logger.error('Verifying rram contents failed')

        if not gdb.detach():
            return logger.error('Could not detach')

        # Powercycle the device again, confirm that it still stays unlocked and attach:

        logger.info('Cycling target power')
        set_power(False)
        set_power(True)

        logger.info('Performing SWD scan')
        match gdb.swd_scan():
            case []:
                return logger.error('No targets')
            case ['Nordic nRF54L Access Port (protected)']:
                return logger.error('Found locked nRF54L, expected unlocked')
            case ['Nordic nRF54L M33', 'Nordic nRF54L Access Port']:
                logger.info('Found unlocked nRF54L')
            case _ as res:
                return logger.error(f'Unexpected scan result: {res}')

        if not gdb.attach(1):
            return logger.error('Could not attach')

        logger.info('Locking device through UICR')
        if not gdb.file('nrf54l_uicr_approtect.hex'):
            return logger.error('GDB could not open UICR hex file')

        if not gdb.load():
            return logger.error('Writing UICR failed')

        if not gdb.detach():
            return logger.error('Could not detach')
        
        logger.info('Rescanning')
        match gdb.swd_scan():
            case []:
                return logger.error('No targets')
            case ['Nordic nRF54L Access Port (protected)']:
                logger.info('Found locked nRF54L')
            case ['Nordic nRF54L M33', 'Nordic nRF54L Access Port']:
                return logger.error('Found unlocked nRF54L, expected locked')
            case _ as res:
                return logger.error(f'Unexpected scan result: {res}')

if __name__ == '__main__':
    main()
