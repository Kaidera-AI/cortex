"""The signing source suite uses a pinned, job-local tool before network isolation."""
import os
from pathlib import Path
import re
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[4]
PROFILE = ROOT / '.github/workflows/next-offline.yml'
PIN = '9a599b48ba6eb7b1e80f12f36b94ceca7c00b7a5173c95c3efc88d9822957e73'
URL = 'https://github.com/jedisct1/minisign/releases/download/0.12/minisign-0.12-linux.tar.gz'


def preparation():
    text = PROFILE.read_text()
    match = re.search(r'^      - name: pinned source-signing tool\n        run: \|\n((?:          .*\n|\n)+)', text, re.M)
    if match is None:
        return text, ''
    return text, '\n'.join(line[10:] for line in match[1].splitlines()) + '\n'


class SigningHome(unittest.TestCase):
    def test_pinned_tool_is_prepared_before_the_network_isolated_worker(self):
        text, script = preparation()
        self.assertTrue(script, 'BODY: pinned signing-tool preparation absent')
        self.assertIn(URL, script)
        self.assertIn(PIN, script)
        self.assertLess(script.index('shasum -a 256 --check --strict'), script.index('tar -xzf'))
        self.assertIn('minisign-linux/x86_64/minisign', script)
        self.assertIn('minisign 0.12', script)
        self.assertIn('next-offline-tools/bin', script)
        self.assertNotIn('sudo', script)
        self.assertLess(text.index('name: pinned source-signing tool'), text.index('name: frozen inventory'))
        worker = text.split('name: every admitted next suite', 1)[1]
        self.assertIn('PATH="${{ runner.temp }}/next-offline-tools/bin:${{ runner.temp }}/next-offline-venv/bin:/usr/bin:/bin"', worker)
        self.assertIn('PrivateNetwork=yes', worker)
        self.assertIn('RestrictAddressFamilies=AF_UNIX', worker)
        syntax = subprocess.run(['bash', '-n'], input=script, text=True, capture_output=True)
        self.assertEqual(syntax.returncode, 0, syntax.stderr)

    def test_changed_download_is_refused_before_tar_can_execute(self):
        _, script = preparation()
        self.assertTrue(script, 'BODY: pinned signing-tool preparation absent')
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            tools = root / 'stubs'
            tools.mkdir()
            (tools / 'curl').write_text('#!/bin/sh\nwhile [ "$1" != "--output" ]; do shift; done\nprintf changed > "$2"\n')
            (tools / 'tar').write_text('#!/bin/sh\ntouch "$TAR_CALLED"\nexit 91\n')
            for file in tools.iterdir():
                file.chmod(0o755)
            script = script.replace('${{ runner.temp }}', str(root))
            result = subprocess.run(['bash', '-e', '-o', 'pipefail', '-c', script],
                env=dict(os.environ, PATH=str(tools) + ':/usr/bin:/bin', TAR_CALLED=str(root / 'tar-called')),
                text=True, capture_output=True, timeout=10)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('FAILED', result.stdout)
            self.assertFalse((root / 'tar-called').exists())


if __name__ == '__main__':
    unittest.main()
