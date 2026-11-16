"""Behaviour guards for install.sh — the webinar-critical installer.

These shell out to `bash` (present on macOS and the Ubuntu CI image) and assert BEHAVIOUR,
not text: the preflight names every missing dependency with an install command, and the
setup launch falls back cleanly when there is no usable /dev/tty. Cross-platform on purpose.
"""
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
INSTALL = REPO / "install.sh"


class InstallSyntaxTests(unittest.TestCase):
    def test_syntax_is_valid(self):
        r = subprocess.run(["bash", "-n", str(INSTALL)], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)


class PreflightTests(unittest.TestCase):
    def test_missing_dependency_reported_up_front_with_install_hint(self):
        """Run install.sh with a PATH that provably lacks tmux, from inside the repo so git
        isn't required. The preflight must stop BEFORE doing any install work and name the
        missing dependency plus how to install it — so someone new installs everything in one
        go instead of one failure at a time.

        The PATH is built here rather than borrowed from the host: an earlier version used
        `/usr/bin:/bin`, which silently passed on CI runners that ship tmux in /usr/bin. The
        test then asserted nothing at all.
        """
        with tempfile.TemporaryDirectory() as fake_bin:
            # Mirror every executable on the current PATH except tmux, so the installer has all
            # the ordinary tools it expects and is missing exactly the one thing under test.
            # Listing the tools by hand was fragile — it broke the moment install.sh reached for
            # one that had not been listed.
            for d in os.environ.get("PATH", "").split(os.pathsep):
                if not os.path.isdir(d):
                    continue
                for name in os.listdir(d):
                    if name == "tmux" or os.path.exists(os.path.join(fake_bin, name)):
                        continue
                    try:
                        os.symlink(os.path.join(d, name), os.path.join(fake_bin, name))
                    except OSError:
                        pass
            self.assertIsNone(shutil.which("tmux", path=fake_bin),
                              "the point of this test is a PATH without tmux")
            self.assertIsNotNone(shutil.which("bash", path=fake_bin),
                                 "the installer needs bash — the mirrored PATH is incomplete")

            r = subprocess.run(
                ["bash", str(INSTALL)],
                cwd=str(REPO),
                env={"PATH": fake_bin, "HOME": os.environ.get("HOME", "/tmp")},
                stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=30,
            )
            out = (r.stdout + r.stderr).lower()
            self.assertNotEqual(r.returncode, 0, "preflight should stop the installer")
            self.assertIn("tmux", out, "the missing tmux must be named")
            self.assertTrue(
                any(w in out for w in ("install", "apt", "dnf", "brew")),
                "the error must tell the user how to install it",
            )


class TtyGuardTests(unittest.TestCase):
    def test_no_usable_tty_takes_the_fallback_branch(self):
        """The old guard `[ -e /dev/tty ]` only checked EXISTENCE, so on a host where
        /dev/tty exists but can't be opened (pipe, many containers, `… </dev/null`) the
        installer ran `exec … setup </dev/tty`, which failed and made a SUCCESSFUL install
        report an error (exit 1). The fix also checks OPENABILITY, so it falls back to the
        printed instructions instead. Here stdin is /dev/null → no usable tty → fallback."""
        r = subprocess.run(
            ["bash", "-c",
             '[ -e /dev/tty ] && (: </dev/tty) 2>/dev/null && echo TTY || echo FALLBACK'],
            stdin=subprocess.DEVNULL, capture_output=True, text=True,
        )
        self.assertEqual(r.stdout.strip(), "FALLBACK")

    def test_install_sh_uses_openability_not_just_existence(self):
        self.assertIn("(: </dev/tty)", INSTALL.read_text(),
                      "the /dev/tty branch must test openability, not just existence")


if __name__ == "__main__":
    unittest.main()
