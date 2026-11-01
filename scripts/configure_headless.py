#!/usr/bin/python3
"""Suppress normal boot/login text while retaining known-working console devices."""
from datetime import datetime
import os
from pathlib import Path
import shutil
import subprocess


def command_line(text):
    prefixes = ('console=', 'loglevel=', 'consoleblank=', 'vt.global_cursor_default=',
                'systemd.show_status=', 'rd.systemd.show_status=', 'systemd.getty_auto=', 'fbcon=map:')
    words = [word for word in text.split() if not word.startswith(prefixes)
             and word not in ('quiet', 'logo.nologo')]
    words += ['console=serial0,115200', 'console=tty1', 'quiet', 'loglevel=3', 'logo.nologo', 'consoleblank=0',
              'vt.global_cursor_default=0', 'systemd.show_status=false',
              'rd.systemd.show_status=false', 'systemd.getty_auto=no', 'fbcon=map:1']
    return ' '.join(words) + '\n'


def main():
    if os.geteuid() != 0:
        raise SystemExit('Run this script with sudo.')
    # The qualified device has one framebuffer (fb0). Keep Linux's valid VT
    # console, but do not attach it to the LCD; the viewer uses DRM/KMS directly.
    framebuffer_file = Path('/proc/fb')
    if framebuffer_file.exists():
        indices = [line.split()[0] for line in framebuffer_file.read_text().splitlines() if line.strip()]
        if any(index != '0' for index in indices):
            raise SystemExit('Headless mapping requires the qualified single-framebuffer setup. No changes were made.')
    backup = Path('/var/backups/ascent-headless-' + datetime.now().strftime('%Y%m%d-%H%M%S-%f'))
    backup.mkdir(mode=0o700)
    boot = Path('/boot/firmware')
    for name in ('cmdline.txt', 'config.txt'):
        shutil.copy2(boot / name, backup / name)
    dropins = Path('/etc/systemd/system/getty@tty1.service.d')
    if dropins.exists():
        shutil.copytree(dropins, backup / 'getty-tty1-dropins')
    (backup / 'getty-state.txt').write_text(subprocess.run(
        ['systemctl', 'is-enabled', 'getty@tty1.service'], capture_output=True, text=True).stdout)
    (boot / 'cmdline.txt').write_text(command_line((boot / 'cmdline.txt').read_text()))
    subprocess.run(['systemctl', 'mask', '--now', 'getty@tty1.service', 'autovt@tty1.service'], check=True)
    subprocess.run(['systemctl', 'daemon-reload'], check=True)
    # Erase text already present in the console's backing framebuffer. KMS may
    # restore that framebuffer briefly while the two graphical players hand off.
    if Path('/dev/tty1').exists():
        try:
            with open('/dev/tty1', 'w') as console:
                console.write('\x1b[2J\x1b[H\x1b[?25l')
                console.flush()
        except OSError as error:
            print('Console could not be cleared immediately; reboot will apply the blank console:', error)
    print('Normal console/login text suppressed; boot console devices and SSH retained.')
    print('Boot configuration backup:', backup)
    print('Reboot to apply the kernel console settings.')


if __name__ == '__main__':
    main()
