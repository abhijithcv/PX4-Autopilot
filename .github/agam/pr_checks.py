#!/usr/bin/env python3
"""Checks for pull requests in Agam's PX4 fork (agam-robotics/PX4-Autopilot).

Run by .github/workflows/agam-pr-checks.yml on PRs from pr-* branches. The
PR is only read through git; nothing from it is executed here.

  lint      hygiene, commit messages and Agam board identity. Writes a
            markdown report and exits 1 when something must be fixed.
  plan      build targets for the changed files, plus the px4-dev images
            PX4's own CI uses on main, as GITHUB_OUTPUT lines.
  artifact  after a build, the firmware's board_id must match the board's
            firmware.prototype.

Commit subjects are checked with PX4's Tools/ci/conventional_commits.py
from main, so the rule is the one upstream enforces.

The fix commands in the report assume the README's clone: `upstream` is
agam-robotics/PX4-Autopilot and `origin` is the contributor's own fork.
"""

import argparse
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Tuple

AGAM_BOARD_DIRS = ('boards/agam/', 'boards/agam-robotics/')
RESERVED_IDS = range(1930, 1950)  # Agam's block in board_types.txt
MAX_TARGETS = 4
BEHIND_WARN = 500
LARGE_FILE = 1 << 20
BIG_PR_LINES = 2000
BIG_PR_COMMITS = 30
MAX_ITEMS = 15

TRANSLATED_DOCS = re.compile(r'^docs/(ko|zh|uk)/')
JUNK = [
    (re.compile(r'(^|/)\.DS_Store$'), 'macOS Finder file'),
    (re.compile(r'^(BuildReleases|build|logs)/'), 'local build output'),
    (re.compile(r'\.(orig|rej|swp)$|~$'), 'merge or editor leftover'),
    (re.compile(r'\.(ulg|ulog|log)$'), 'log file'),
    (re.compile(r'\.(bin|hex|px4|elf|apj)$'), 'firmware binary'),
    (re.compile(r'\.(zip|tar|tgz|gz|7z|rar)$'), 'archive'),
]
# Binaries PX4 keeps in the tree (bootloaders, doc assets): warn only.
TRACKED_BINARIES = re.compile(r'^boards/[^/]+/[^/]+/(extras|boot)/|^docs/assets/')
EDITOR_DIRS = re.compile(r'^(\.vscode|\.devcontainer|\.idea)/')
AI_ATTRIBUTION = re.compile(
    r'^co-authored-by:.*\b(claude|anthropic|copilot|chatgpt|openai|gemini|cursor)\b'
    r'|^.*generated (with|by)\b.*\b(claude|copilot|chatgpt|gemini)\b', re.I | re.M)
WIP = re.compile(r'^(fixup|squash|amend)! |^wip\b', re.I)
BAD_IDENTITY = re.compile(r'\.local$|localhost|\(none\)|\.lan$', re.I)
SOURCE = re.compile(r'^(src|platforms|msg|srv|cmake|ROMFS)/|^(CMakeLists\.txt|Makefile)$')
DEFAULT_TARGETS = ('px4_sitl_default', 'px4_fmu-v6x_default')
CONFLICT = re.compile(r'^(<{7}|>{7})( |$)')
SAFE_TARGET = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]*$')


class Git:
    def __init__(self, repo: str) -> None:
        self.repo = repo

    def __call__(self, *args: str, check: bool = True, stdin: str = '') -> str:
        r = subprocess.run(['git', '-C', self.repo, *args], input=stdin,
                           capture_output=True, text=True, errors='replace')
        if r.returncode != 0:
            if check:
                raise RuntimeError(f'git {" ".join(args)}: {r.stderr.strip()}')
            return ''
        return r.stdout


@dataclass
class Finding:
    level: str  # 'fail' or 'warn'
    title: str
    items: List[str] = field(default_factory=list)
    fix: str = ''


@dataclass
class Change:
    status: str
    old_mode: str
    new_mode: str
    old_sha: str
    new_sha: str
    path: str


def code(text: str) -> str:
    """Inline code; also stops @mentions and markdown in PR-controlled text."""
    return '`' + text.replace('`', "'").replace('\n', ' ').strip()[:200] + '`'


def squash_fix(base: str) -> str:
    return ('Squash the branch into one commit with a clean message:\n'
            '```sh\n'
            'git fetch upstream\n'
            f'git reset --soft $(git merge-base upstream/{base} HEAD)\n'
            'git commit -m "type(scope): what changed"\n'
            'git push --force-with-lease\n'
            '```')


# ---------------------------------------------------------------- git reads

def changes(git: Git, base: str, head: str) -> List[Change]:
    out = git('diff', '--raw', '-z', '--no-abbrev', '--no-renames', base, head)
    parts = out.split('\0')
    result = []
    i = 0
    while i + 1 < len(parts) and parts[i].startswith(':'):
        old_mode, new_mode, old_sha, new_sha, status = parts[i][1:].split(' ')
        result.append(Change(status[0], old_mode, new_mode, old_sha, new_sha,
                             parts[i + 1]))
        i += 2
    return result


def added_lines(diff: str) -> Iterator[Tuple[str, int, str]]:
    """(path, new line number, text) for every added line of a -U0 diff."""
    path: Optional[str] = None
    line_no = 0
    prev = ''
    for line in diff.splitlines():
        if line.startswith('+++ ') and prev.startswith('--- '):
            target = line[4:]
            path = target[2:] if target.startswith('b/') else None
        elif line.startswith('@@'):
            m = re.match(r'@@ -\S+ \+(\d+)', line)
            line_no = int(m.group(1)) if m else 0
        elif path and line.startswith('+'):
            yield path, line_no, line[1:]
            line_no += 1
        prev = line


def blob_sizes(git: Git, shas: List[str]) -> Dict[str, int]:
    if not shas:
        return {}
    out = git('cat-file', '--batch-check=%(objectname) %(objectsize)',
              stdin='\n'.join(shas) + '\n')
    sizes = {}
    for line in out.splitlines():
        sha, _, size = line.partition(' ')
        if size.isdigit():
            sizes[sha] = int(size)
    return sizes


def board_id(prototype: str) -> Optional[int]:
    try:
        value = json.loads(prototype).get('board_id')
        return int(value) if value is not None else None
    except (ValueError, TypeError, AttributeError):
        return None


# ------------------------------------------------------------------- checks

def check_commits(git: Git, base: str, head: str, base_ref: str,
                  parse_header) -> List[Finding]:
    merges, subjects, wip, ai, identity = [], [], [], [], []
    for sha in git('rev-list', '--reverse', f'{base}..{head}').split():
        meta = git('show', '-s', '--format=%P%x00%an%x00%ae%x00%s%x00%B', sha)
        parents, name, email, subject, body = meta.split('\0', 4)
        ref = f'{code(sha[:10])} {code(subject)}'
        if len(parents.split()) > 1:
            merges.append(ref)
            continue
        if WIP.search(subject):
            wip.append(ref)
        elif not parse_header(subject):
            subjects.append(ref)
        if AI_ATTRIBUTION.search(body):
            ai.append(ref)
        if not name.strip() or '@' not in email or BAD_IDENTITY.search(email):
            identity.append(f'{code(sha[:10])} {code(f"{name} <{email}>")}')

    found = []
    if merges:
        found.append(Finding('fail', 'Merge commits on the branch', merges,
                             'Rebase on the base branch instead of merging it in:\n'
                             '```sh\n'
                             'git fetch upstream\n'
                             f'git rebase upstream/{base_ref}\n'
                             'git push --force-with-lease\n'
                             '```'))
    if subjects:
        found.append(Finding(
            'fail', 'Commit subjects not in PX4 format `type(scope): description`',
            subjects,
            'Example: `fix(drivers/ina228): correct the shunt scaling`. '
            'Types: feat, fix, docs, style, refactor, perf, test, build, ci, '
            'chore, revert.\n\n' + squash_fix(base_ref)))
    if ai:
        found.append(Finding('fail', 'AI attribution in commit messages', ai,
                             'PX4 accepts AI disclosure only as an `Assisted-by:` '
                             'trailer.\n\n' + squash_fix(base_ref)))
    if wip:
        found.append(Finding('warn', 'Work-in-progress commits', wip,
                             squash_fix(base_ref)))
    if identity:
        found.append(Finding(
            'warn', 'Commits with a placeholder author name or email', identity,
            'Use the name and email on your GitHub account:\n'
            '```sh\n'
            'git config --global user.name "Your Name"\n'
            'git config --global user.email "you@example.com"\n'
            '```\n'
            'then squash (the new commit takes the new identity).\n\n'
            + squash_fix(base_ref)))
    return found


def check_paths(git: Git, all_changes: List[Change], base_ref: str) -> List[Finding]:
    restore = (f'git fetch upstream\n'
               f'git restore --source=upstream/{base_ref} --staged --worktree -- {{paths}}\n'
               f'git commit -m "type(scope): what changed"\n'
               'git push --force-with-lease')
    submodules, translated, junk, binaries, editor = [], [], [], [], []
    large, large_assets, modes = [], [], []
    live = [c for c in all_changes
            if c.status != 'D' and '160000' not in (c.old_mode, c.new_mode)]
    sizes = blob_sizes(git, [c.new_sha for c in live])

    for c in all_changes:
        if '160000' in (c.old_mode, c.new_mode):
            submodules.append(f'{code(c.path)} {code(c.old_sha[:10])} → '
                              f'{code(c.new_sha[:10])}')
    for c in live:
        if TRANSLATED_DOCS.match(c.path):
            translated.append(code(c.path))
        for pattern, what in JUNK:
            if pattern.search(c.path):
                (binaries if TRACKED_BINARIES.match(c.path) else junk).append(
                    f'{code(c.path)} ({what})')
                break
        if EDITOR_DIRS.match(c.path):
            editor.append(code(c.path))
        size = sizes.get(c.new_sha, 0)
        if size > LARGE_FILE:
            item = f'{code(c.path)} ({size / LARGE_FILE:.1f} MiB)'
            (large_assets if c.path.startswith('docs/assets/') else large).append(item)
        if (c.new_mode == '100755' and c.old_mode != '100755'
                and not c.path.endswith(('.sh', '.py'))):
            modes.append(code(c.path))

    found = []
    if submodules:
        paths = ' '.join(c.path for c in all_changes
                         if '160000' in (c.old_mode, c.new_mode))
        found.append(Finding(
            'fail', 'Submodule pointers changed', submodules,
            'Almost always accidental. If the PR really needs a newer submodule, '
            'say so in the description and a maintainer will check it. '
            'Otherwise put them back:\n'
            '```sh\n'
            'git fetch upstream\n'
            f'git restore --source=upstream/{base_ref} --staged -- {paths}\n'
            'git submodule update --init --recursive\n'
            'git commit -m "type(scope): what changed"\n'
            'git push --force-with-lease\n'
            '```'))
    if translated:
        found.append(Finding(
            'fail', 'Edits to translated docs (docs/ko, docs/zh, docs/uk)', translated,
            'Only edit `docs/en`; the translations are regenerated from it.\n'
            '```sh\n' + restore.format(paths='docs/ko docs/zh docs/uk') + '\n```'))
    if junk:
        found.append(Finding(
            'fail', 'Files that should not be committed', junk,
            'Remove them from the branch (your local copies stay):\n'
            '```sh\n'
            'git rm -r --cached -- <paths>\n'
            'git commit -m "type(scope): what changed"\n'
            'git push --force-with-lease\n'
            '```'))
    if large:
        found.append(Finding('fail', 'Files over 1 MiB', large,
                             'Large files bloat every clone of PX4. Remove them '
                             'the same way as above.'))
    if binaries:
        found.append(Finding('warn', 'Binaries PX4 keeps in the tree were changed',
                             binaries, 'Confirm a bootloader was rebuilt from '
                             'source for this exact change, not copied in.'))
    if large_assets:
        found.append(Finding('warn', 'Large doc assets', large_assets,
                             'Compress images before committing.'))
    if editor:
        found.append(Finding('warn', 'Editor or dev-container settings changed',
                             editor, 'Usually unintended; revert unless the PR '
                             'is about them.'))
    if modes:
        found.append(Finding('warn', 'Files made executable', modes,
                             '`git update-index --chmod=-x <path>` if unintended.'))
    return found


def check_diff(diff: str) -> List[Finding]:
    conflicts, vendor = [], []
    deny = os.environ.get('AGAM_VENDOR_DENYLIST', '').strip()
    deny_re = re.compile(deny, re.I) if deny else None
    for path, line_no, text in added_lines(diff):
        if CONFLICT.match(text):
            conflicts.append(code(f'{path}:{line_no}'))
        agam_page = path.startswith(AGAM_BOARD_DIRS) or (
            path.startswith('docs/en/') and 'agam' in path.lower())
        if deny_re and agam_page and deny_re.search(text):
            vendor.append(code(f'{path}:{line_no}'))
    found = []
    if conflicts:
        found.append(Finding('fail', 'Merge conflict markers', conflicts,
                             'Open each file, resolve the conflict and delete the '
                             'marker lines.'))
    if vendor:
        found.append(Finding('fail', 'Agam files name another manufacturer', vendor,
                             'Agam boards and pages describe Agam hardware only; '
                             'reword these lines.'))
    return found


def check_boards(git: Git, base: str, head: str) -> Tuple[List[Finding], List[str]]:
    changed = []
    for path in git('diff', '--name-only', base, head, '--',
                    *AGAM_BOARD_DIRS).split('\n'):
        parts = path.split('/')
        if len(parts) >= 4 and '/'.join(parts[:3]) not in changed:
            changed.append('/'.join(parts[:3]))
    if not changed:
        return [], []

    owners: Dict[int, List[str]] = {}
    for root in AGAM_BOARD_DIRS:
        for board in git('ls-tree', '-d', '--name-only', head, root,
                         check=False).split():
            bid = board_id(git('show', f'{head}:{board}/firmware.prototype',
                               check=False))
            if bid is not None:
                owners.setdefault(bid, []).append(board)

    problems, report = [], []
    for bid, boards in sorted(owners.items()):
        if len(boards) > 1:
            problems.append(f'board_id {bid} is used by ' +
                            ', '.join(code(b) for b in boards))
    for board in changed:
        if not git('ls-tree', '--name-only', head, board + '/', check=False):
            continue  # board removed
        proto = git('show', f'{head}:{board}/firmware.prototype', check=False)
        bid = board_id(proto)
        if bid is None:
            problems.append(f'{code(board)}: no readable `firmware.prototype` board_id')
            continue
        line = [f'prototype {bid}']
        if bid not in RESERVED_IDS:
            problems.append(f'{code(board)}: board_id {bid} is outside Agam\'s '
                            'block 1930-1949')
        ident = git('show', f'{head}:{board}/uavcan_board_identity', check=False)
        if ident:
            major = re.search(r'uavcanblid_hw_version_major\s+(\d+)', ident)
            minor = re.search(r'uavcanblid_hw_version_minor\s+(\d+)', ident)
            if not (major and minor):
                problems.append(f'{code(board)}: `uavcan_board_identity` has no '
                                'hw_version major/minor')
            else:
                can_id = (int(major.group(1)) << 8) + int(minor.group(1))
                line.append(f'uavcan hw {major.group(1)}.{minor.group(1)} = {can_id}')
                if can_id != bid:
                    problems.append(
                        f'{code(board)}: uavcan hw version {major.group(1)}.'
                        f'{minor.group(1)} is board {can_id} on CAN, but the '
                        f'prototype says {bid} (needs {bid >> 8}.{bid & 0xff})')
        hw_config = git('show', f'{head}:{board}/src/hw_config.h', check=False)
        m = re.search(r'#define\s+BOARD_TYPE\s+(\d+)', hw_config)
        if m:
            line.append(f'bootloader BOARD_TYPE {m.group(1)}')
            if int(m.group(1)) != bid:
                problems.append(f'{code(board)}: bootloader BOARD_TYPE '
                                f'{m.group(1)} != board_id {bid}')
        report.append(f'- {code(board)}: ' + ', '.join(line))

    found = []
    if problems:
        found.append(Finding('fail', 'Agam board IDs disagree', problems,
                             'The board ID is how ground stations and CAN '
                             'firmware updates pick an image; a wrong one can '
                             'flash the wrong firmware. All fields must name the '
                             'board\'s reserved ID.'))
    return found, report


def check_size(git: Git, base: str, head: str, base_ref: str) -> List[Finding]:
    found = []
    behind = int(git('rev-list', '--count', f'{head}..origin/{base_ref}',
                     check=False).strip() or 0)
    if behind > BEHIND_WARN:
        found.append(Finding('warn', f'Branch is {behind} commits behind `{base_ref}`',
                             [], 'Rebase so the checks test current code:\n'
                             '```sh\n'
                             'git fetch upstream\n'
                             f'git rebase upstream/{base_ref}\n'
                             'git push --force-with-lease\n'
                             '```'))
    commits = int(git('rev-list', '--count', f'{base}..{head}').strip() or 0)
    stat = git('diff', '--shortstat', base, head)
    lines = sum(int(n) for n in re.findall(r'(\d+) (?:insertion|deletion)', stat))
    if commits > BIG_PR_COMMITS or lines > BIG_PR_LINES:
        found.append(Finding('warn', f'Large PR ({commits} commits, {lines} changed lines)',
                             [], 'Smaller PRs get reviewed and merged faster '
                             'upstream; split it if the parts stand alone.'))
    return found


# ------------------------------------------------------------------- report

def render(findings: List[Finding], boards: List[str], summary: str) -> str:
    fails = [f for f in findings if f.level == 'fail']
    warns = [f for f in findings if f.level == 'warn']
    if fails:
        head = f'## ❌ Agam PR checks: {len(fails)} to fix, {len(warns)} to check'
    elif warns:
        head = f'## ⚠️ Agam PR checks: {len(warns)} to check'
    else:
        head = '## ✅ Agam PR checks passed'
    out = [head, '', summary]
    for title, group in (('Must fix', fails), ('Worth checking', warns)):
        if not group:
            continue
        out += ['', f'### {title}']
        for f in group:
            out += ['', f'**{f.title}**']
            out += [f'- {item}' for item in f.items[:MAX_ITEMS]]
            if len(f.items) > MAX_ITEMS:
                out.append(f'- … and {len(f.items) - MAX_ITEMS} more')
            if f.fix:
                out += ['', f.fix]
    if boards:
        out += ['', '### Agam board identity', ''] + boards
    out += ['', '---',
            '*Updated on every push. The AI review and the PX4 gate checks '
            'report separately below.*']
    return '\n'.join(out) + '\n'


def lint(args: argparse.Namespace) -> int:
    sys.path.insert(0, args.px4_ci)
    from conventional_commits import parse_header  # PX4's own rule, from main

    git = Git(args.repo)
    base = git('merge-base', f'origin/{args.base_ref}', args.head).strip()
    all_changes = changes(git, base, args.head)
    diff = git('diff', '-U0', '--no-color', '--no-ext-diff', base, args.head)

    findings: List[Finding] = []
    findings += check_commits(git, base, args.head, args.base_ref, parse_header)
    findings += check_paths(git, all_changes, args.base_ref)
    findings += check_diff(diff)
    board_findings, boards = check_boards(git, base, args.head)
    findings += board_findings
    findings += check_size(git, base, args.head, args.base_ref)

    commits = git('rev-list', '--count', f'{base}..{args.head}').strip()
    summary = (f'{commits} commit(s), {len(all_changes)} file(s) changed against '
               f'`{args.base_ref}` (merge base {code(base[:10])}).')
    Path(args.report).write_text(render(findings, boards, summary), encoding='utf-8')
    status = ('fail' if any(f.level == 'fail' for f in findings)
              else 'warn' if findings else 'pass')
    if args.status_file:
        Path(args.status_file).write_text(status + '\n', encoding='utf-8')
    print(f'status={status}')
    return 1 if status == 'fail' else 0


# --------------------------------------------------------------------- plan

def board_target(git: Git, head: str, board_dir: str) -> Optional[str]:
    vendor, board = board_dir.split('/')[1:3]
    files = git('ls-tree', '--name-only', head, board_dir + '/', check=False).split()
    variants = sorted(Path(f).stem for f in files if f.endswith('.px4board'))
    if not variants:
        return None
    variant = 'default' if 'default' in variants else variants[0]
    target = f'{vendor}_{board}_{variant}'
    # names come from the PR's directories and end up on a make command line
    return target if SAFE_TARGET.match(target) else None


def image_from(path: Path, pattern: str) -> str:
    m = re.search(pattern, path.read_text(encoding='utf-8'))
    if not m:
        raise SystemExit(f'error: no px4-dev image found in {path}')
    return m.group(1)


def plan(args: argparse.Namespace) -> int:
    git = Git(args.repo)
    base = git('merge-base', f'origin/{args.base_ref}', args.head).strip()
    files = git('diff', '--name-only', base, args.head).split('\n')
    builds: List[Dict[str, str]] = []
    needs_default = False
    for f in files:
        m = re.match(r'^(boards/[^/]+/[^/]+)/', f)
        if m:
            target = board_target(git, args.head, m.group(1))
            if target and all(b['target'] != target for b in builds):
                agam = (m.group(1) + '/').startswith(AGAM_BOARD_DIRS)
                builds.append({'target': target, 'board': m.group(1) if agam else ''})
        elif SOURCE.match(f):
            needs_default = True
    if needs_default:
        builds += [{'target': t, 'board': ''} for t in DEFAULT_TARGETS
                   if all(b['target'] != t for b in builds)]
    skipped = builds[MAX_TARGETS:]
    builds = builds[:MAX_TARGETS]

    root = Path(args.px4_root)
    build_image = image_from(root / 'Tools/ci/build_all_config.yml',
                             r'default:\s*"?(ghcr\.io/px4/px4-dev:[^"\s]+)')
    checks_image = image_from(root / '.github/workflows/checks.yml',
                              r'image:\s*(ghcr\.io/px4/px4-dev:\S+)')
    print(f'matrix={json.dumps({"include": builds})}')
    print(f'has_builds={"true" if builds else "false"}')
    print(f'build_image={build_image}')
    print(f'checks_image={checks_image}')
    if skipped:
        print('not built (limit reached): ' + ', '.join(b['target'] for b in skipped),
              file=sys.stderr)
    return 0


# ----------------------------------------------------------------- artifact

def artifact(args: argparse.Namespace) -> int:
    repo = Path(args.repo)
    expected = board_id((repo / args.board / 'firmware.prototype').read_text())
    out = repo / 'build' / args.target
    found = []
    for px4 in sorted(out.glob('*.px4')):
        found.append((px4.name, board_id(px4.read_text(errors='replace'))))
    for binary in sorted((out / 'deploy').glob('*.bin')):
        if binary.stem.isdigit():
            found.append((f'deploy/{binary.name}', int(binary.stem)))
    if not found:
        print(f'error: no .px4 or deploy/<id>.bin in {out}')
        return 1
    bad = [(name, bid) for name, bid in found if bid != expected]
    for name, bid in found:
        print(f'{name}: board_id {bid} (prototype {expected})')
    if bad:
        print('error: built firmware board_id does not match firmware.prototype')
        return 1
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    sub = parser.add_subparsers(dest='cmd', required=True)
    for name in ('lint', 'plan'):
        sp = sub.add_parser(name)
        sp.add_argument('--repo', required=True, help='git checkout of the PR')
        sp.add_argument('--head', default='HEAD', help='PR head ref in --repo')
        sp.add_argument('--base-ref', required=True,
                        help='PR base branch; must exist as origin/<name>')
    lint_p = sub.choices['lint']
    lint_p.add_argument('--px4-ci', required=True,
                        help="Tools/ci from PX4's main (for conventional_commits)")
    lint_p.add_argument('--report', required=True)
    lint_p.add_argument('--status-file', default='')
    sub.choices['plan'].add_argument('--px4-root', required=True,
                                     help="sparse checkout of PX4's main")
    art = sub.add_parser('artifact')
    art.add_argument('--repo', required=True)
    art.add_argument('--target', required=True)
    art.add_argument('--board', required=True)
    args = parser.parse_args()
    return {'lint': lint, 'plan': plan, 'artifact': artifact}[args.cmd](args)


if __name__ == '__main__':
    sys.exit(main())
