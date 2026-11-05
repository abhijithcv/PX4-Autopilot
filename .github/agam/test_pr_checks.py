"""Tests for pr_checks.py against a throwaway git repository.

Run from .github/agam: python3 -m unittest test_pr_checks
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import pr_checks  # noqa: E402

# Stand-in for PX4's conventional_commits.parse_header (same pattern).
HEADER = pr_checks.re.compile(
    r'^(feat|fix|docs|style|refactor|perf|test|build|ci|chore|revert)'
    r'\(([a-zA-Z0-9_/\-\.]+)\)(!)?: (.{5,})$')


def parse_header(text):
    return HEADER.match(text.strip())


class Repo:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.git('init', '-q', '-b', 'main')
        self.git('config', 'user.name', 'Test Person')
        self.git('config', 'user.email', 'test@example.com')

    def git(self, *args, env=None):
        return subprocess.run(['git', '-C', str(self.path), *args], check=True,
                              capture_output=True, text=True,
                              env={**os.environ, **(env or {})}).stdout

    def write(self, rel, text, mode=None):
        p = self.path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
        if mode:
            p.chmod(mode)
        self.git('add', rel)

    def commit(self, msg, env=None):
        self.git('commit', '-q', '-m', msg, env=env)
        return self.git('rev-parse', 'HEAD').strip()


def board(repo, name, board_id, can=None, board_type=None):
    repo.write(f'boards/agam/{name}/firmware.prototype',
               json.dumps({'board_id': board_id}))
    repo.write(f'boards/agam/{name}/default.px4board', 'CONFIG_X=y\n')
    if can:
        repo.write(f'boards/agam/{name}/uavcan_board_identity',
                   f'set(uavcanblid_hw_version_major {can[0]})\n'
                   f'set(uavcanblid_hw_version_minor {can[1]})\n')
    if board_type is not None:
        repo.write(f'boards/agam/{name}/src/hw_config.h',
                   f'#define BOARD_TYPE {board_type}\n')


class PrChecksTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Repo(Path(self.tmp.name))
        self.repo.write('src/a.c', 'int a;\n')
        board(self.repo, 'florange', 1939, can=(7, 147))
        self.base = self.repo.commit('feat(src): base')
        # origin/main as the checks expect it
        self.repo.git('update-ref', 'refs/remotes/origin/main', self.base)
        self.repo.git('checkout', '-q', '-b', 'pr-test')
        self.git = pr_checks.Git(str(self.repo.path))

    def tearDown(self):
        self.tmp.cleanup()

    def titles(self, findings, level=None):
        return [f.title for f in findings if level in (None, f.level)]

    def test_clean_branch_passes(self):
        self.repo.write('src/a.c', 'int a = 1;\n')
        self.repo.commit('fix(src): initialise a')
        found = pr_checks.check_commits(self.git, self.base, 'HEAD', 'main',
                                        parse_header)
        found += pr_checks.check_paths(
            self.git, pr_checks.changes(self.git, self.base, 'HEAD'), 'main')
        self.assertEqual(found, [])

    def test_commit_problems(self):
        self.repo.write('src/b.c', 'int b;\n')
        self.repo.commit('fixed stuff')
        self.repo.write('src/c.c', 'int c;\n')
        self.repo.commit('fix(src): add c\n\nCo-Authored-By: Claude <noreply@anthropic.com>')
        self.repo.write('src/d.c', 'int d;\n')
        self.repo.commit('WIP more', env={'GIT_AUTHOR_EMAIL': 'someone@MacBook.local'})
        self.repo.git('checkout', '-q', '-b', 'side', self.base)
        self.repo.write('src/e.c', 'int e;\n')
        self.repo.commit('fix(src): add e')
        self.repo.git('checkout', '-q', 'pr-test')
        self.repo.git('merge', '-q', '--no-edit', 'side')
        found = pr_checks.check_commits(self.git, self.base, 'HEAD', 'main',
                                        parse_header)
        titles = self.titles(found)
        self.assertIn('Merge commits on the branch', titles)
        self.assertTrue(any('PX4 format' in t for t in titles))
        self.assertIn('AI attribution in commit messages', titles)
        self.assertIn('Work-in-progress commits', titles)
        self.assertTrue(any('placeholder author' in t for t in titles))

    def test_path_problems(self):
        sub_sha = 'a' * 40
        self.repo.git('update-index', '--add', '--cacheinfo',
                      f'160000,{sub_sha},platforms/nuttx/NuttX/nuttx')
        self.repo.write('docs/zh/index.md', 'x\n')
        self.repo.write('BuildReleases/fw.px4', 'x\n')
        self.repo.write('src/.DS_Store', 'x\n')
        self.repo.write('boards/agam/florange/extras/bl.bin', 'x\n')
        self.repo.write('tools.txt', 'x\n', mode=0o755)
        self.repo.commit('fix(src): many problems')
        found = pr_checks.check_paths(
            self.git, pr_checks.changes(self.git, self.base, 'HEAD'), 'main')
        fails = self.titles(found, 'fail')
        warns = self.titles(found, 'warn')
        self.assertIn('Submodule pointers changed', fails)
        self.assertTrue(any('translated docs' in t for t in fails))
        junk = next(f for f in found if f.title.startswith('Files that should'))
        self.assertEqual(len(junk.items), 2)  # BuildReleases and .DS_Store
        self.assertIn('Binaries PX4 keeps in the tree were changed', warns)
        self.assertIn('Files made executable', warns)

    def test_conflict_markers_and_vendor(self):
        self.repo.write('src/a.c', '<<<<<<< HEAD\nint a;\n=======\n>>>>>>> side\n')
        self.repo.write('boards/agam/florange/nsh/defconfig', 'CONFIG_ARK_FLOW=y\n')
        self.repo.write('docs/en/flow.md', 'ARK Flow is great\n')  # not an Agam page
        self.repo.commit('fix(src): oops')
        diff = self.git('diff', '-U0', self.base, 'HEAD')
        os.environ['AGAM_VENDOR_DENYLIST'] = r'\bark\b|ark[-_ ]?flow'
        try:
            found = pr_checks.check_diff(diff)
        finally:
            del os.environ['AGAM_VENDOR_DENYLIST']
        conflicts = next(f for f in found if f.title == 'Merge conflict markers')
        self.assertEqual(conflicts.items, ['`src/a.c:1`', '`src/a.c:4`'])
        vendor = next(f for f in found if 'manufacturer' in f.title)
        self.assertEqual(vendor.items, ['`boards/agam/florange/nsh/defconfig:1`'])

    def test_board_identity(self):
        board(self.repo, 'florange', 1939, can=(1, 0))      # CAN says 256
        board(self.repo, 'megh7', 1939, board_type=35)      # dup id, bad BOARD_TYPE
        board(self.repo, 'gnss', 81)                        # outside the block
        self.repo.commit('fix(boards/agam): ids')
        found, report = pr_checks.check_boards(self.git, self.base, 'HEAD')
        problems = ' | '.join(found[0].items)
        self.assertIn('board 256 on CAN', problems)
        self.assertIn('needs 7.147', problems)
        self.assertIn('board_id 1939 is used by', problems)
        self.assertIn('BOARD_TYPE 35 != board_id 1939', problems)
        self.assertIn('board_id 81 is outside', problems)
        self.assertEqual(len(report), 3)

    def test_board_identity_ok(self):
        board(self.repo, 'florange', 1939, can=(7, 147))
        self.repo.write('boards/agam/florange/init/rc.board', 'x\n')
        self.repo.commit('fix(boards/agam): rc')
        found, report = pr_checks.check_boards(self.git, self.base, 'HEAD')
        self.assertEqual(found, [])
        self.assertEqual(report, ['- `boards/agam/florange`: prototype 1939, '
                                  'uavcan hw 7.147 = 1939'])

    def test_targets(self):
        self.repo.write('boards/agam/florange/init/rc.board', 'x\n')
        self.repo.write('src/a.c', 'int a = 2;\n')
        self.repo.write('docs/en/x.md', 'x\n')
        self.repo.commit('fix(src): x')
        self.assertEqual(pr_checks.board_target(self.git, 'HEAD', 'boards/agam/florange'),
                         'agam_florange_default')

    def test_code_escapes_mentions(self):
        self.assertEqual(pr_checks.code('@kapil `x`'), "`@kapil 'x'`")


if __name__ == '__main__':
    unittest.main()
