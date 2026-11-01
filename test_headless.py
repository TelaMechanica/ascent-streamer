import unittest
from scripts.configure_headless import command_line


class HeadlessTests(unittest.TestCase):
    def test_retains_working_console_devices_and_preserves_boot_arguments(self):
        result = command_line('console=serial0,115200 console=tty1 root=PARTUUID=abc rootwait fbcon=rotate:1 loglevel=7')
        self.assertEqual([w for w in result.split() if w.startswith('console=')], ['console=serial0,115200', 'console=tty1'])
        self.assertIn('root=PARTUUID=abc', result)
        self.assertIn('rootwait', result)
        self.assertIn('fbcon=rotate:1', result)
        self.assertIn('systemd.getty_auto=no', result)
        self.assertIn('fbcon=map:1', result)
        self.assertNotIn('loglevel=7', result)
        self.assertEqual(len(result.splitlines()), 1)

    def test_idempotent_console_configuration(self):
        once = command_line('root=PARTUUID=abc quiet console=tty0 fbcon=map:0')
        self.assertEqual(command_line(once), once)

    def test_repairs_the_withdrawn_null_console_preset(self):
        result = command_line('root=PARTUUID=abc console=null systemd.getty_auto=no')
        self.assertNotIn('console=null', result)
        self.assertIn('console=tty1', result)
        self.assertIn('root=PARTUUID=abc', result)


if __name__ == '__main__':
    unittest.main()
