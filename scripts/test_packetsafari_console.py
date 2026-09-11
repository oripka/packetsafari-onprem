"""Offline smoke checks for the vendored console and operator argument parsing."""
import io
import unittest
from packetsafari_onprem.console import Console
from packetsafari_onprem.cli import build_parser

class ConsoleTests(unittest.TestCase):
    def test_quiet_retains_errors(self):
        out = io.StringIO()
        ui = Console(out, quiet=True)
        ui.log('info', 'hidden')
        ui.log('error', 'failed')
        self.assertNotIn('hidden', out.getvalue())
        self.assertIn('failed', out.getvalue())

    def test_quiet_json_status(self):
        args = build_parser().parse_args(['--quiet', 'status', '--json'])
        self.assertTrue(args.quiet)
        self.assertTrue(args.json)

if __name__ == '__main__':
    unittest.main()
