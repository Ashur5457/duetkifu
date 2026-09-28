#!/usr/bin/env python3
"""Duetkifu launcher: open a report for a data folder in the browser, with no folder picker.

    python duetkifu.py [FOLDER]            start Duetkifu for FOLDER (default: the current folder)
    python duetkifu.py check [FOLDER]      check the report in FOLDER and exit (exit code 1 on errors)
                                            (--deep: re-read every file instead of trusting size and date)
    python duetkifu.py step FOLDER --script S --in PATTERN --out PATTERN
                                            record a computation step: which script turned which files into which
    python duetkifu.py annotations FOLDER  print the open annotations with what they point at (compact JSON for agents)
    python duetkifu.py kifu check FOLDER   check kifu.json, the research record next to report.json (also part of check)
    python duetkifu.py kifu tree FOLDER    print the moves of the research record as a tree
    python duetkifu.py index FOLDER        build or update the search index of the folder (kept in ~/.duetkifu/index/)
    python duetkifu.py find FOLDER "WORDS" search the text of every file in the folder; prints short excerpts
    python duetkifu.py kifu show FOLDER MOVE
                                            one move of the record with its data chain and excerpts of its source
    python duetkifu.py extract FOLDER REPORT.html|SLIDES.pptx
                                            write the figures and tables of a report or slide deck to files, as a step
    python duetkifu.py wait FOLDER         for an agent: wait until the user clicks "Ask the agent to revise", then exit
    python duetkifu.py agent-status FOLDER working|done|failed [--note TEXT]
                                            for an agent: tell the page what it is doing
    python duetkifu.py install-skill       install the /duetkifu skill for Claude Code
    python duetkifu.py shortcut [FOLDER]   put a shortcut on the desktop that starts Duetkifu for FOLDER
    python duetkifu.py init-agent [FOLDER] add a marked section to AGENTS.md and CLAUDE.md in FOLDER that points any agent to Duetkifu

Where the report lives:
  - If FOLDER contains report.json, FOLDER is the project folder (raw data in FOLDER/data/).
  - Otherwise the report goes into FOLDER/duetkifu/ and FOLDER itself is the raw data folder.
    Raw data files are only read, never written.
  - Raw data stays outside duetkifu/ but inside FOLDER. Files computed from it (derived data, the scripts
    that compute them) go into FOLDER/duetkifu/derived_data/ and FOLDER/duetkifu/scripts/.

The launcher serves duetkifu.html on 127.0.0.1 and lets that page read and write the project
folder. Every request needs a random token that is only given to the browser window it opens.
Problems the page reports are printed here and appended to errors.log in the project folder,
so an agent running this command sees them.

Python 3.8 or later, standard library only.
"""
import argparse, base64, csv, datetime, glob, hashlib, hmac, html.parser, http.server, json, mimetypes, os, pathlib, posixpath, re, secrets, shutil, socket, sqlite3, subprocess, sys, tempfile, threading, time, urllib.parse, webbrowser, zipfile
import xml.etree.ElementTree as ET

VERSION = '0.8.0'
SCHEMA = 'duetsheet/0.7'   # the report format is Duetsheet's; Duetkifu adds kifu.json next to it
HERE = pathlib.Path(__file__).resolve().parent
PAGE = HERE / 'duetkifu.html'
SUBDIR = 'duetkifu'
WORKSPACES = (SUBDIR, 'duetsheet')   # duetsheet/: workspaces made by Duetsheet or before the rename, still opened as they are
OWN = ('report.json', 'kifu.json', 'errors.log', 'assets', 'exports', 'habits', 'lang')   # what Duetkifu may write in the project folder
SKIP_DIRS = {'.git', 'node_modules', '__pycache__', *WORKSPACES}


def home_dir(name, env, old_ok=False):
    """~/.duetkifu/<name>, or the folder in the environment variable DUETKIFU_<env> (DUETSHEET_<env> also works).
    With old_ok, ~/.duetkifu/<name> is used while only that one exists (personal habits kept before the rename)."""
    v = os.environ.get('DUETKIFU_' + env) or os.environ.get('DUETSHEET_' + env)
    if v:
        return pathlib.Path(v)
    new, old = pathlib.Path.home() / '.duetkifu' / name, pathlib.Path.home() / '.duetsheet' / name
    return old if old_ok and old.is_dir() and not new.exists() else new

for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass


def say(*parts):
    print('[duetkifu]', *parts, flush=True)


def project_of(root):
    """(project folder, raw data folder) for a folder the user opened."""
    if (root / 'report.json').is_file():
        return root, root / 'data'
    for name in WORKSPACES:
        if (root / name).is_dir():
            return root / name, root
    return root / SUBDIR, root


def atomic_write(path, data):
    """Write bytes through a temporary file and a rename, retrying while a sync client holds the file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + '.' + secrets.token_hex(4) + '.duetkifu-tmp')
    tmp.write_bytes(data)
    for attempt in range(5):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if attempt == 4:
                tmp.unlink()
                raise
            time.sleep(0.2 * (attempt + 1))


# ---------------------------------------------------------------- fingerprints
# A file is known by its path (relative to the folder of report.json) and the SHA-256 of its bytes.
# Raw data can be large (hundreds of files of tens of MB, often in a OneDrive folder, where reading a file
# may download it), so a file is only read when its size or modification time differs from what was
# recorded. Hashes computed that way are kept in cache/fingerprints.json; deleting it is harmless.

CACHE_FILE = ('cache', 'fingerprints.json')
MTIME_SLACK_MS = 2000   # copies and sync clients may move the modification time a little


def iso_ms(ms):
    return datetime.datetime.fromtimestamp(ms / 1000, datetime.timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z')


def ms_of(iso):
    try:
        return datetime.datetime.fromisoformat(str(iso).replace('Z', '+00:00')).timestamp() * 1000
    except ValueError:
        return None


def norm(rel):
    return posixpath.normpath(str(rel).replace('\\', '/'))


def inside(root, project, rel):
    """True when rel (relative to the folder of report.json) stays inside the folder the user opened."""
    if not isinstance(rel, str) or not rel or re.match(r'^([A-Za-z]:|[\\/])', rel):
        return False
    try:
        (project / norm(rel)).resolve().relative_to(root.resolve())
        return True
    except (ValueError, OSError):
        return False


class Fingerprints:
    def __init__(self, project):
        self.project, self.file = project, project.joinpath(*CACHE_FILE)
        self.lock, self.dirty, self.hashed = threading.Lock(), False, 0
        try:
            self.cache = json.loads(self.file.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            self.cache = {}
        if not isinstance(self.cache, dict):
            self.cache = {}

    def sha256(self, rel, st, force=False):
        mtime = st.st_mtime_ns // 1_000_000
        with self.lock:
            c = self.cache.get(rel)
        if not force and isinstance(c, dict) and c.get('size') == st.st_size and c.get('mtime') == mtime:
            return c.get('sha256')
        h = hashlib.sha256()
        with open(self.project / rel, 'rb') as fh:
            for b in iter(lambda: fh.read(1 << 20), b''):
                h.update(b)
        with self.lock:
            self.cache[rel] = {'size': st.st_size, 'mtime': mtime, 'sha256': h.hexdigest()}
            self.dirty = True
            self.hashed += 1
        return h.hexdigest()

    def ref(self, rel):
        """A file reference for a file that exists: {path, sha256, size, modified}."""
        rel = norm(rel)
        st = (self.project / rel).stat()
        return {'path': rel, 'sha256': self.sha256(rel, st), 'size': st.st_size, 'modified': iso_ms(st.st_mtime_ns // 1_000_000)}

    def status(self, ref, deep=False):
        """Compare a file reference {path, sha256, size?, modified?} with the file on disk.
        state: same | changed | missing | unknown (nothing recorded to compare with)."""
        rel = norm(ref.get('path'))
        out = {'path': ref.get('path'), 'state': 'missing'}
        try:
            st = (self.project / rel).stat()
        except OSError:
            return out
        if not (self.project / rel).is_file():
            return out
        out.update(size=st.st_size, modified=iso_ms(st.st_mtime_ns // 1_000_000))
        want, rec = ref.get('sha256'), ms_of(ref['modified']) if ref.get('modified') else None
        if not deep and want and ref.get('size') == st.st_size and rec is not None and abs(rec - st.st_mtime_ns / 1e6) <= MTIME_SLACK_MS:
            out.update(state='same', sha256=want)
            return out
        out['sha256'] = sha = self.sha256(rel, st, force=deep)
        out['state'] = 'unknown' if not want else 'same' if sha == want else 'changed'
        return out

    def save(self):
        with self.lock:
            if not self.dirty:
                return
            data, self.dirty = json.dumps(self.cache, indent=0, sort_keys=True), False
        try:
            atomic_write(self.file, data.encode('utf-8'))
        except OSError:
            pass


# ---------------------------------------------------------------- the data chain
# steps/{id} records one computation: a script turned input files into output files. A dataset imported
# from a step's output can be followed back, step by step, to the raw files no step produced.
# A step needs rerunning when its script or an input changed, or when an input comes from a step that
# needs rerunning; everything computed from it (outputs, datasets, charts and tables) is then out of date.

def match_patterns(root, project, patterns):
    """Files that match a step's inputPatterns now (paths relative to the folder of report.json), inside root only."""
    found = set()
    for pat in patterns if isinstance(patterns, list) else []:
        if not isinstance(pat, str) or not pat or re.match(r'^([A-Za-z]:|[\\/])', pat):
            continue
        for h in glob.glob(os.path.join(str(project), pat), recursive=True):
            rel = norm(os.path.relpath(h, project))
            if os.path.isfile(h) and inside(root, project, rel):
                found.add(rel)
    return found


def step_files(step):
    """(role, ref) for every file a step names."""
    if isinstance(step.get('script'), dict):
        yield 'script', step['script']
    for role in ('inputs', 'outputs'):
        for r in step.get(role) or []:
            yield role[:-1], r


def chart_series(spec):
    """The series of a chart: chart.series, or the one series chart.dataset / x / y of older reports."""
    ser = spec.get('series') if isinstance(spec, dict) else None
    if isinstance(ser, list) and ser:
        return [s for s in ser if isinstance(s, dict) and s.get('dataset')]
    return [{'dataset': spec.get('dataset'), 'x': spec.get('x'), 'y': spec.get('y')}] if isinstance(spec, dict) and spec.get('dataset') else []


def block_dataset_ids(b):
    if not isinstance(b, dict):
        return []
    if b.get('type') == 'chart':
        return list(dict.fromkeys(s['dataset'] for s in chart_series(b.get('chart'))))
    if b.get('type') == 'table' and isinstance(b.get('table'), dict) and b['table'].get('dataset'):
        return [b['table']['dataset']]
    return []


# ---------------------------------------------------------------- chapters


def ordered_blocks(rep):
    """The blocks in reading order, as the page shows them: report.meta.order, then the rest by createdAt."""
    blocks = rep.get('blocks') if isinstance(rep.get('blocks'), dict) else {}
    meta = (rep.get('report') or {}).get('meta') if isinstance(rep.get('report'), dict) else None
    order = meta.get('order') if isinstance(meta, dict) and isinstance(meta.get('order'), list) else []
    out, seen = [], set()
    for i in order:
        if isinstance(i, str) and i not in seen and isinstance(blocks.get(i), dict):
            seen.add(i)
            out.append(blocks[i])
    rest = [b for k, b in blocks.items() if k not in seen and isinstance(b, dict)]
    return out + sorted(rest, key=lambda b: str(b.get('createdAt') or ''))


def is_chapter(b):
    return isinstance(b, dict) and b.get('type') == 'text' and bool(str(b.get('title') or '').strip())


def chapters(rep):
    """Chapters in reading order: a chapter is a text block with a title, and the blocks after it, up to the next
    chapter, belong to it. Each chapter: {id, title, blocks}. (Duetsheet 0.7 also let a chapter branch off another,
    with parent and status; the research record, kifu.json, does that now, and those fields are ignored.)"""
    chs = []
    for b in ordered_blocks(rep):
        if is_chapter(b):
            chs.append({'id': b.get('id'), 'title': b.get('title'), 'blocks': [b]})
        elif chs:
            chs[-1]['blocks'].append(b)
    return chs


# ---------------------------------------------------------------- the research record (duetkifu/0.1)
# kifu.json, next to report.json, records every move of a research: which move it follows (parent), why it was made,
# what came of it and, for a dead end, why. The report tells a few of these lines; the record keeps all of them,
# failures included. Paths in kifu.json are relative to its folder, like the paths in report.json.

KIFU_FILE = 'kifu.json'
KIFU_SCHEMA = 'duetkifu/0.1'
MOVE_KINDS = ('question', 'attempt', 'correction', 'audit', 'conclusion')
MOVE_STATUSES = ('planned', 'active', 'paused', 'done')
OUTCOMES = ('success', 'failure', 'inconclusive')
CAUSES = ('idea', 'execution', 'measurement', 'method', 'cost', 'superseded', 'other')
TRIGGERS = ('user', 'advisor', 'supervisor', 'data', 'site', 'self', 'plan', 'unknown')
LINK_TYPES = ('corrects', 'clue', 'supports', 'compares', 'inspired', 'supersedes')
MATCHES = ('yes', 'close', 'no', 'not-reproducible')
MARKS = ('good', 'bad', 'doubtful', 'interesting')
MILESTONES = ('turning-point', 'root-cause', 'champion', 'breakthrough', 'pivot')   # others are allowed, with a warning
OUTCOME_SIGN = {'success': '✓', 'failure': '✗', 'inconclusive': '?'}
FILE_ROLES = ('report', 'table', 'figure', 'slides', 'script', 'raw', 'other')
# how far a move's numbers can be followed back (evidence): recomputed from raw files by recorded steps, sourced from
# files named with their fingerprints (a report, a table, a figure) but not recomputed, or none (noData says why)
CHAIN_LEVELS = ('recomputed', 'sourced', 'none')


def move_chain(m):
    ev = m.get('evidence') if isinstance(m, dict) and isinstance(m.get('evidence'), dict) else {}
    if ev.get('steps'):
        return 'recomputed'
    if ev.get('files'):
        return 'sourced'
    if str(ev.get('noData') or '').strip():
        return 'none'
    return None


def read_json_file(path):
    """(object, error): strict JSON in UTF-8 (a byte order mark is tolerated); NaN and Infinity are refused."""
    def no_constants(name):
        raise ValueError(f'{name} is not valid JSON; write null instead')
    try:
        return json.loads(path.read_text(encoding='utf-8-sig'), parse_constant=no_constants), None
    except json.JSONDecodeError as e:
        return None, f'{path.name} line {e.lineno}, column {e.colno}: {e.msg}'
    except (OSError, UnicodeDecodeError, ValueError) as e:
        return None, f'{path.name}: {e}'


def move_no(m):
    no = m.get('no')
    return no if isinstance(no, int) and not isinstance(no, bool) else 10 ** 9


def kifu_moves(kifu):
    """The moves in order of their number, as a tree, and a list of problems. Each node: {id, move, parent, kids, depth}.
    A node's kids are ordered by number; the first one continues its parent's line (same depth), the others branch off."""
    raw = kifu.get('moves') if isinstance(kifu, dict) else None
    if not isinstance(raw, dict):
        return [], ['"moves" must be an object keyed by move id']
    nodes, problems = {k: {'id': k, 'move': m, 'parent': None, 'kids': [], 'depth': 0} for k, m in raw.items() if isinstance(m, dict)}, []
    for n in nodes.values():
        p = n['move'].get('parent')
        if p in (None, ''):
            continue
        if p == n['id'] or p not in nodes:
            problems.append(f'moves.{n["id"]}.parent "{p}" is not another move')
        else:
            n['parent'] = p
    for n in nodes.values():
        seen, x = {n['id']}, n['parent']
        while x:
            if x in seen:
                problems.append(f'moves.{n["id"]}.parent: the parents form a loop')
                n['parent'] = None
                break
            seen.add(x)
            x = nodes[x]['parent']
    ordered = sorted(nodes.values(), key=lambda n: (move_no(n['move']), n['id']))
    for n in ordered:
        if n['parent']:
            nodes[n['parent']]['kids'].append(n)
    stack = [(n, 0) for n in reversed(ordered) if not n['parent']]
    while stack:
        n, d = stack.pop()
        n['depth'] = d
        stack += [(k, d + (1 if i else 0)) for i, k in reversed(list(enumerate(n['kids'])))]
    return ordered, problems


def check_kifu(project, root, rep=None, stale=None):
    """Return (errors, warnings, notes) for project/kifu.json. rep is the report, whose steps the evidence names;
    stale is {step id: reasons} for the steps that need rerunning (see check_chain)."""
    errors, warnings, notes = [], [], []
    kifu, err = read_json_file(project / KIFU_FILE)
    if err:
        return [err], [], []
    if not isinstance(kifu, dict):
        return [f'{KIFU_FILE} must be a JSON object'], [], []
    if kifu.get('schema') != KIFU_SCHEMA:
        errors.append(f'{KIFU_FILE}: "schema" must be "{KIFU_SCHEMA}"')
    meta = (kifu.get('kifu') or {}).get('meta') if isinstance(kifu.get('kifu'), dict) else None
    if not isinstance(meta, dict) or not str(meta.get('question') or '').strip():
        warnings.append('kifu.meta.question is missing: say in one sentence what the research asks')
    groups = meta.get('groups') if isinstance(meta, dict) else None
    if groups is not None and (not isinstance(groups, dict) or not all(isinstance(v, str) for v in groups.values())):
        errors.append('kifu.meta.groups must be {"<group>": "name shown for it"}')
    nodes, problems = kifu_moves(kifu)
    errors += problems
    moves = kifu.get('moves') if isinstance(kifu.get('moves'), dict) else {}
    steps = rep.get('steps') if isinstance(rep, dict) and isinstance(rep.get('steps'), dict) else {}
    stale = stale or {}
    nos, missing_src, unconfirmed, mismatches, stale_use = {}, [], [], [], {}
    fp, file_states = Fingerprints(project), {'changed': [], 'missing': []}
    counts = dict.fromkeys(MATCHES, 0)

    def one_of(where, v, allowed):
        if v not in allowed:
            errors.append(f'{where} must be {", ".join(allowed)}' + (f' (not "{v}")' if v is not None else ''))
            return False
        return True

    def path_ok(where, p):
        """True when p names an existing file; outside the opened folder is an error."""
        if not isinstance(p, str) or not p:
            errors.append(f'{where} must be a path')
            return False
        if not inside(root, project, p):
            errors.append(f'{where} "{p}" is outside the folder "{root.name}" (paths are relative to the folder of {KIFU_FILE})')
            return False
        return (project / norm(p)).is_file()

    def text(v, n=60):
        s = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)
        return s if len(s) <= n else s[:n - 1] + '…'

    for key, m in moves.items():
        w = f'moves.{key}'
        if not isinstance(m, dict):
            errors.append(f'{w} must be an object')
            continue
        if m.get('id') != key:
            errors.append(f'{w}.id must be "{key}"')
        no = move_no(m)
        if no == 10 ** 9 or no < 1:
            errors.append(f'{w}.no must be a whole number from 1 (the order in which the moves were made)')
        elif no in nos:
            warnings.append(f'{w}.no {no} is also the number of move "{nos[no]}"')
        else:
            nos[no] = key
        if not isinstance(m.get('title'), str) or not m['title'].strip():
            errors.append(f'{w}.title is missing')
        one_of(f'{w}.kind', m.get('kind'), MOVE_KINDS)
        if m.get('parent') in (None, '') and m.get('kind') != 'question':
            warnings.append(f'{w} has no parent; only the research question starts without one')
        st, oc = m.get('status'), m.get('outcome')
        one_of(f'{w}.status', st, MOVE_STATUSES)
        if oc is not None:
            one_of(f'{w}.outcome', oc, OUTCOMES)
            if st != 'done':
                warnings.append(f'{w}.outcome is set but the move is {st}, not done')
        elif st == 'done':
            errors.append(f'{w}: a done move needs an outcome ({", ".join(OUTCOMES)})')
        ends_badly = oc in ('failure', 'inconclusive') or st == 'paused'
        if ends_badly and not str(m.get('reason') or '').strip():
            errors.append(f'{w}: say why in "reason" (needed when a move fails, is inconclusive or is paused)')
        cause = m.get('cause')
        if cause is not None:
            if one_of(f'{w}.cause', cause, CAUSES) and not ends_badly:
                warnings.append(f'{w}.cause is only used when a move fails, is inconclusive or is paused')
            if m.get('causeConfirmed') is False:
                unconfirmed.append(f'{key} ({cause})')
        elif oc in ('failure', 'inconclusive'):
            warnings.append(f'{w}: no "cause". A wrong idea and data that cannot be trusted are different dead ends')
        if 'causeConfirmed' in m and not isinstance(m['causeConfirmed'], bool):
            errors.append(f'{w}.causeConfirmed must be true or false')
        trig = m.get('trigger')
        if trig is None:
            warnings.append(f'{w}.trigger is missing: who or what started this move')
        elif not isinstance(trig, dict):
            errors.append(f'{w}.trigger must be {{"kind", "who"}}')
        else:
            one_of(f'{w}.trigger.kind', trig.get('kind'), TRIGGERS)
        at = m.get('at')
        if at is not None and not (isinstance(at, str) and re.match(r'^\d{4}-\d{2}-\d{2}(T|$)', at)):
            errors.append(f'{w}.at must be an ISO date (YYYY-MM-DD); put a loose date such as "late August" in "when"')
        res = m.get('result')
        if res is not None:
            if not isinstance(res, dict):
                errors.append(f'{w}.result must be {{"text", "value", "n"}}')
            else:
                if res.get('value') is not None and (not isinstance(res['value'], (int, float)) or isinstance(res['value'], bool)):
                    errors.append(f'{w}.result.value must be a number (the metric); put words in result.text')
                if res.get('n') is not None and (not isinstance(res['n'], int) or isinstance(res['n'], bool)):
                    errors.append(f'{w}.result.n must be a whole number (samples or repeats)')
        for f, allowed, why in (('mark', MARKS, 'note'), ('milestone', None, 'reason')):
            v = m.get(f)
            if v is None:
                continue
            if not isinstance(v, dict) or not isinstance(v.get('kind'), str) or not v['kind']:
                errors.append(f'{w}.{f} must be {{"kind", "{why}"}}')
            elif allowed:
                one_of(f'{w}.{f}.kind', v['kind'], allowed)
            elif v['kind'] not in MILESTONES:
                warnings.append(f'{w}.milestone.kind "{v["kind"]}" is not one of {", ".join(MILESTONES)}; it is shown as it is')
        links = m.get('links', [])
        if not isinstance(links, list):
            errors.append(f'{w}.links must be a list of {{"type", "to"}}')
            links = []
        for i, l in enumerate(links):
            if not isinstance(l, dict):
                errors.append(f'{w}.links[{i}] must be {{"type", "to"}}')
            elif one_of(f'{w}.links[{i}].type', l.get('type'), LINK_TYPES) and (l.get('to') not in moves or l.get('to') == key):
                errors.append(f'{w}.links[{i}].to "{l.get("to")}" is not another move')
        src = m.get('source')
        if src is not None:
            if not isinstance(src, dict):
                errors.append(f'{w}.source must be {{"title", "file", "url", "section"}}')
            elif src.get('file') is not None and not path_ok(f'{w}.source.file', src['file']) and inside(root, project, src['file']):
                missing_src.append(src['file'])
        if 'dataOutside' in m and not isinstance(m['dataOutside'], bool):
            errors.append(f'{w}.dataOutside must be true or false')
        ev = m.get('evidence')
        if ev is None:
            continue
        if not isinstance(ev, dict):
            errors.append(f'{w}.evidence must be {{"steps", "checks"}}')
            continue
        esteps = ev.get('steps', [])
        if not isinstance(esteps, list) or not all(isinstance(s, str) for s in esteps):
            errors.append(f'{w}.evidence.steps must be a list of step ids')
            esteps = []
        for s in esteps:
            if s not in steps:
                errors.append(f'{w}.evidence.steps: "{s}" is not a step in report.json')
            elif s in stale:
                stale_use.setdefault(s, []).append(key)
        efiles = ev.get('files', [])
        if not isinstance(efiles, list):
            errors.append(f'{w}.evidence.files must be a list of {{"path", "sha256", "role", "note"}}')
            efiles = []
        for i, r in enumerate(efiles):
            fw = f'{w}.evidence.files[{i}]'
            if not isinstance(r, dict):
                errors.append(f'{fw} must be {{"path", "sha256", "role", "note"}}')
                continue
            if r.get('role') is not None:
                one_of(f'{fw}.role', r['role'], FILE_ROLES)
            if not path_ok(f'{fw}.path', r.get('path')):
                if inside(root, project, r.get('path') or ''):
                    file_states['missing'].append(f'{key}: {r["path"]}')
                continue
            if not re.fullmatch(r'[0-9a-f]{64}', str(r.get('sha256', ''))):
                warnings.append(f'{fw}: "{r["path"]}" has no sha256, so changes to it cannot be seen')
            elif fp.status(r).get('state') == 'changed':
                file_states['changed'].append(f'{key}: {r["path"]}')
        if 'noData' in ev and not isinstance(ev['noData'], str):
            errors.append(f'{w}.evidence.noData must say, in words, why this move has no data')
        checks = ev.get('checks', [])
        if not isinstance(checks, list):
            errors.append(f'{w}.evidence.checks must be a list')
            checks = []
        for i, c in enumerate(checks):
            cw = f'{w}.evidence.checks[{i}]'
            if not isinstance(c, dict):
                errors.append(f'{cw} must be an object')
                continue
            if not isinstance(c.get('claim'), str) or not c['claim'].strip():
                errors.append(f'{cw}.claim is missing: what the record says')
            if not one_of(f'{cw}.match', c.get('match'), MATCHES):
                continue
            counts[c['match']] += 1
            if c.get('step') is not None and c['step'] not in esteps:
                warnings.append(f'{cw}.step "{c["step"]}" is not in {w}.evidence.steps')
            cs = c.get('source')
            if cs is not None:
                if not isinstance(cs, dict):
                    errors.append(f'{cw}.source must be {{"path", "keys"}}')
                elif not path_ok(f'{cw}.source.path', cs.get('path')) and inside(root, project, cs.get('path') or ''):
                    warnings.append(f'{cw}.source.path "{cs["path"]}" does not exist')
            if c['match'] in ('no', 'not-reproducible'):
                mismatches.append(f'{key}: {text(c.get("claim"))}: recorded {text(c.get("recorded"))}, recomputed {text(c.get("recomputed"))} ({c["match"]})')

    for s, keys in stale_use.items():
        warnings.append(f'step "{s}" needs rerunning, so the evidence of move(s) {", ".join(keys)} may be out of date')
    changes = kifu.get('changes', {})
    if not isinstance(changes, dict):
        errors.append(f'{KIFU_FILE}: "changes" must be an object keyed by change id')
        changes = {}
    for cid, c in changes.items():   # the change log of the record: one document per changed field
        if not isinstance(c, dict) or c.get('by') not in ('user', 'claude') or not isinstance(c.get('move'), str) or not isinstance(c.get('field'), str):
            errors.append(f'changes.{cid} must be {{"by": "user" | "claude", "at", "move", "field", "before", "after"}}')
    if changes:
        notes.append(f'{len(changes)} recorded change(s) to the record ({sum(1 for c in changes.values() if isinstance(c, dict) and c.get("by") == "user")} by the user)')
    blocks = rep.get('blocks') if isinstance(rep, dict) and isinstance(rep.get('blocks'), dict) else {}
    for bid, b in blocks.items():   # a chapter of the report says which move it tells
        if isinstance(b, dict) and b.get('move') not in (None, '') and b['move'] not in moves:
            warnings.append(f'blocks.{bid}.move "{b["move"]}" is not a move in {KIFU_FILE}')
    if nodes:
        tally = lambda f, allowed: ', '.join(f'{n} {v}' for v in allowed for n in [sum(1 for x in nodes if x['move'].get(f) == v)] if n)
        notes.append(f'{KIFU_FILE}: {len(nodes)} moves ({tally("status", MOVE_STATUSES)}; {tally("outcome", OUTCOMES) or "no outcomes yet"})')
    if any(counts.values()):
        notes.append('Checks against recomputed numbers: ' + ', '.join(f'{n} {k}' for k, n in counts.items()))
    if mismatches:
        notes.append(f'{len(mismatches)} check(s) do not match; the user decides what to do with them:')
        notes += ['  ' + x for x in mismatches]
    if unconfirmed:
        notes.append(f'Causes the user has not confirmed yet: {", ".join(unconfirmed)}')
    for st, what in (('changed', 'changed since they were recorded'), ('missing', 'are missing')):
        if file_states[st]:
            warnings.append(f'{len(file_states[st])} evidence file(s) {what}: {", ".join(file_states[st][:5])}{" ..." if len(file_states[st]) > 5 else ""}')
    fp.save()
    if nodes:
        levels = {n['id']: move_chain(n['move']) for n in nodes}
        notes.append('Data chain of the moves: ' + ', '.join(f'{sum(1 for v in levels.values() if v == lv)} {lv}' for lv in CHAIN_LEVELS)
                     + f', {sum(1 for v in levels.values() if v is None)} not recorded')
        todo = [k for k, v in levels.items() if v is None]
        if todo:
            notes.append(f'Moves without a data chain yet (evidence.steps, evidence.files, or evidence.noData saying why): '
                         f'{", ".join(todo[:12])}{" ..." if len(todo) > 12 else ""}')
    if missing_src:
        uniq = list(dict.fromkeys(missing_src))
        notes.append(f'{len(uniq)} source file(s) named by moves are not in the folder: {", ".join(uniq[:3])}{" ..." if len(uniq) > 3 else ""}')
    return errors, warnings, notes


def print_kifu_tree(project):
    """The moves as an indented tree (depth first, children in the order they were made): number, status and outcome,
    title, cause, checks, corrections."""
    kifu, err = read_json_file(project / KIFU_FILE)
    if err:
        say('ERROR', err)
        return 1
    nodes, problems = kifu_moves(kifu)
    meta = ((kifu.get('kifu') or {}).get('meta') or {}) if isinstance(kifu, dict) else {}
    print(f'{meta.get("question") or KIFU_FILE}  ({len(nodes)} moves)')

    def line(n, depth):
        m = n['move']
        head = f'{m.get("status") or "?"}' + (f' {OUTCOME_SIGN.get(m.get("outcome"), "")}' if m.get('outcome') else '')
        extra = []
        if m.get('cause'):
            extra.append(f'cause: {m["cause"]}' + ('' if m.get('causeConfirmed', True) else ', not confirmed'))
        checks = ((m.get('evidence') or {}).get('checks') or []) if isinstance(m.get('evidence'), dict) else []
        if checks:
            extra.append('checks: ' + ' '.join(f'{k} {c}' for k in MATCHES for c in [sum(1 for x in checks if isinstance(x, dict) and x.get('match') == k)] if c))
        fixes = [l.get('to') for l in m.get('links') or [] if isinstance(l, dict) and l.get('type') == 'corrects']
        if fixes:
            extra.append('corrects ' + ', '.join(map(str, fixes)))
        if move_chain(m):
            extra.append(f'data: {move_chain(m)}')
        no = move_no(m)
        print(f'{no if no < 10 ** 9 else "?":>3}  {"  " * depth}{n["id"]} [{head}] {m.get("title") or ""}' + (f'  ({"; ".join(extra)})' if extra else ''))

    stack = [(n, 0) for n in reversed(nodes) if not n['parent']]   # depth first; children in the order they were made
    while stack:
        n, d = stack.pop()
        line(n, d)
        stack += [(k, d + 1) for k in reversed(n['kids'])]
    for p in problems:
        say('ERROR', p)
    return 1 if problems else 0


def run_kifu(root, sub, deep=False):
    project, _ = project_of(root)
    if not (project / KIFU_FILE).is_file():
        say('ERROR', f'no {KIFU_FILE} in {project}')
        return 1
    if sub == 'tree':
        return print_kifu_tree(project)
    rep, stale = None, None
    if (project / 'report.json').is_file():
        rep, _ = read_json_file(project / 'report.json')
        if isinstance(rep, dict):
            info = {}
            check_chain(project, root, rep, deep, info)
            stale = info.get('stale')
    errors, warnings, notes = check_kifu(project, root, rep, stale)
    print_problems(project / KIFU_FILE, errors, warnings, notes)
    return 1 if errors else 0


def check_chain(project, root, rep, deep, out=None):
    """Return (errors, warnings, notes) about steps and dataset sources.
    out, when given, receives 'stale': {step id: [reasons]} for the steps that need rerunning."""
    errors, warnings, notes = [], [], []
    steps = rep.get('steps') or {}
    datasets = rep.get('datasets') if isinstance(rep.get('datasets'), dict) else {}
    blocks = rep.get('blocks') if isinstance(rep.get('blocks'), dict) else {}
    if not isinstance(steps, dict):
        return ['"steps" must be an object keyed by step id'], [], []
    fp, seen = Fingerprints(project), {}

    def state(ref):
        key = (norm(ref.get('path')), ref.get('sha256'), ref.get('size'), ref.get('modified'))
        if key not in seen:
            try:
                seen[key] = fp.status(ref, deep)
            except OSError as e:
                seen[key] = {'state': 'missing', 'error': str(e)}
        return seen[key]['state']

    good = {}
    for key, s in steps.items():
        where = f'steps.{key}'
        if not isinstance(s, dict):
            errors.append(f'{where} must be an object')
            continue
        if s.get('id') != key:
            errors.append(f'{where}.id must be "{key}"')
        if not isinstance(s.get('outputs'), list) or not s['outputs']:
            errors.append(f'{where}.outputs must list the files the step wrote')
        if not isinstance(s.get('inputs', []), list):
            errors.append(f'{where}.inputs must be a list')
            continue
        ok = True
        for role, r in step_files(s):
            if not isinstance(r, dict) or not isinstance(r.get('path'), str) or not r['path']:
                errors.append(f'{where}: every {role} needs a "path"')
                ok = False
            elif not inside(root, project, r['path']):
                errors.append(f'{where}: {role} "{r["path"]}" is outside the folder "{root.name}". Raw data must be inside "{root.name}" '
                              f'(outside duetkifu/), and paths are relative to the folder of report.json')
                ok = False
            elif not re.fullmatch(r'[0-9a-f]{64}', str(r.get('sha256', ''))):
                warnings.append(f'{where}: {role} "{r["path"]}" has no sha256, so changes to it cannot be seen')
        pats = s.get('inputPatterns', [])
        if not isinstance(pats, list) or not all(isinstance(p, str) and p for p in pats):
            errors.append(f'{where}.inputPatterns must be a list of file patterns')
            ok = False
        else:
            for p in pats:   # the fixed part of a pattern must stay inside the folder
                fixed = re.split(r'[*?\[]', p)[0].rsplit('/', 1)[0] or '.'
                if re.match(r'^([A-Za-z]:|[\\/])', p) or not inside(root, project, fixed):
                    errors.append(f'{where}: input pattern "{p}" is outside the folder "{root.name}"')
                    ok = False
        if ok and isinstance(s.get('outputs'), list):
            good[key] = s

    # which step made each file: the latest one that lists it as an output
    made_by = {}
    for key, s in sorted(good.items(), key=lambda kv: str(kv[1].get('at', ''))):
        for r in s.get('outputs') or []:
            made_by[norm(r['path'])] = key

    # files that match a step's input patterns but are not among its recorded inputs: new data to compute with
    new_inputs = {key: sorted(match_patterns(root, project, s.get('inputPatterns')) - {norm(r['path']) for r in s.get('inputs') or []})
                  for key, s in good.items() if s.get('inputPatterns')}
    reasons, stale = {}, {}

    def is_stale(key, trail=()):
        if key in stale:
            return stale[key]
        if key in trail:   # a loop; reported once below
            return False
        s, why = good[key], []
        new = new_inputs.get(key) or []
        if new:
            why.append(f'{len(new)} new file(s) match its input patterns ({", ".join(new[:3])}{" ..." if len(new) > 3 else ""})')
        for role, r in step_files(s):
            st = state(r)
            if st == 'missing':
                why.append(f'{role} {r["path"]} is missing')
            elif st == 'changed':
                why.append(f'{role} {r["path"]} changed' + (' after the step was recorded' if role == 'output' else ''))
        for r in s.get('inputs') or []:
            up = made_by.get(norm(r['path']))
            if up and up != key and is_stale(up, trail + (key,)):
                why.append(f'input {r["path"]} comes from step "{up}", which needs rerunning')
        reasons[key], stale[key] = why, bool(why)
        return stale[key]

    for key in good:
        is_stale(key)

    uses = {}   # dataset id -> block ids
    for bid, b in blocks.items():
        for dsid in block_dataset_ids(b):
            uses.setdefault(dsid, []).append(bid)

    stale_ds = {}
    for key, ds in datasets.items():
        src = ds.get('source') if isinstance(ds, dict) else None
        if not isinstance(src, dict) or not src.get('path'):
            continue
        where = f'datasets.{key}'
        if not inside(root, project, src['path']):
            errors.append(f'{where}.source.path "{src["path"]}" is outside the folder "{root.name}"; '
                          f'put the file inside "{root.name}" (paths are relative to the folder of report.json)')
            continue
        st = state(src)
        up = made_by.get(norm(src['path']))
        if st == 'missing':
            warnings.append(f'{where}.source.path "{src["path"]}" does not exist (paths are relative to the folder of report.json)')
        elif st == 'changed':
            stale_ds[key] = f'{src["path"]} changed since it was imported (sha256 differs); import it again'
        elif up and stale.get(up):
            stale_ds[key] = f'{src["path"]} comes from step "{up}", which needs rerunning'

    def short(items, n=3):
        items = list(items)
        return ', '.join(items[:n]) + (f' and {len(items) - n} more' if len(items) > n else '')

    for key in good:
        if stale[key]:
            outs = [r['path'] for r in good[key].get('outputs') or []]
            ds = [d for d, x in datasets.items() if isinstance(x, dict) and isinstance(x.get('source'), dict)
                  and norm(x['source'].get('path', '')) in {norm(o) for o in outs}]
            warnings.append(f'step "{key}" needs rerunning: {short(reasons[key])}. It wrote {short(outs)}'
                            + (f', imported as dataset {short(ds)}' if ds else ''))
    for key, why in stale_ds.items():
        warnings.append(f'datasets.{key}: {why}' + (f'. Used by {short(uses[key])}' if uses.get(key) else ''))

    if good:
        made = set(made_by)
        raw = {norm(r['path']) for s in good.values() for r in s.get('inputs') or []} - made
        scripts = {norm(s['script']['path']) for s in good.values() if isinstance(s.get('script'), dict)}
        n_stale = sum(stale.values())
        n_new = len({p for v in new_inputs.values() for p in v})
        notes.append(f'Data chain: {len(good)} step(s), {len(made)} derived file(s), {len(raw)} raw file(s), {len(scripts)} script(s); '
                     + (f'{n_stale} step(s) need rerunning' if n_stale else 'all up to date')
                     + (f'; {n_new} new raw file(s) not used yet' if n_new else ''))
    if fp.hashed:
        notes.append(f'Read {fp.hashed} file(s) to compute fingerprints (kept in {"/".join(CACHE_FILE)} for next time)')
    fp.save()
    if out is not None:
        out['stale'] = {k: reasons[k] for k in good if stale[k]}
    return errors, warnings, notes


# ---------------------------------------------------------------- checking a report

STYLE_FIELDS = {'font.family', 'font.size', 'font.label', 'marker', 'line', 'ticks', 'frame', 'grid', 'palette',
                'figure.preset', 'chartDefaults.kind', 'chartDefaults.logY'}


def check_report(project, root=None, deep=False, out=None):
    """Return (errors, warnings, notes) for project/report.json, as lists of strings.
    root is the folder the user opens (the parent of duetkifu/, or project itself).
    out, when given, receives 'report' (the parsed report) and 'stale' (see check_chain)."""
    errors, warnings, notes = [], [], []
    root = root or project
    path = project / 'report.json'
    if not path.is_file():
        return [f'{path} does not exist'], [], []
    raw = path.read_bytes()
    if raw[:2] in (b'\xff\xfe', b'\xfe\xff'):
        return ['report.json is UTF-16; save it as UTF-8'], [], []
    if raw[:3] == b'\xef\xbb\xbf':
        warnings.append('report.json starts with a byte order mark (BOM); write plain UTF-8')
        raw = raw[3:]
    try:
        text = raw.decode('utf-8')
    except UnicodeDecodeError as e:
        return [f'report.json is not UTF-8: {e}'], warnings, notes

    def no_constants(name):
        raise ValueError(f'{name} is not valid JSON; write null instead (Python: json.dump(..., allow_nan=False))')
    try:
        rep = json.loads(text, parse_constant=no_constants)
    except json.JSONDecodeError as e:
        return [f'report.json line {e.lineno}, column {e.colno}: {e.msg}'], warnings, notes
    except ValueError as e:
        # find where the constant is, for the message
        for i, line in enumerate(text.splitlines(), 1):
            for tok in ('-Infinity', 'Infinity', 'NaN'):
                col = line.find(tok)
                if col >= 0 and line[:col].count('"') % 2 == 0:
                    return [f'report.json line {i}, column {col + 1}: {e}'], warnings, notes
        return [f'report.json: {e}'], warnings, notes

    if not isinstance(rep, dict):
        return ['report.json must be a JSON object'], warnings, notes
    meta = (rep.get('report') or {}).get('meta') if isinstance(rep.get('report'), dict) else None
    if not isinstance(meta, dict):
        errors.append('report.meta is missing: report.json needs {"report": {"meta": {"title": ..., "order": [...]}}}')
        meta = {}
    blocks = rep.get('blocks') or {}
    datasets = rep.get('datasets') or {}
    for name in ('blocks', 'datasets', 'annotations', 'changes', 'rounds', 'examples'):
        if name in rep and not isinstance(rep[name], dict):
            errors.append(f'"{name}" must be an object keyed by document id')
    if not isinstance(blocks, dict):
        blocks = {}
    if not isinstance(datasets, dict):
        datasets = {}

    order = meta.get('order', [])
    if not isinstance(order, list):
        errors.append('report.meta.order must be a list of block ids')
        order = []
    for bid in order:
        if bid not in blocks:
            warnings.append(f'report.meta.order lists "{bid}", which is not in blocks')

    for key, ds in datasets.items():
        where = f'datasets.{key}'
        if not isinstance(ds, dict):
            errors.append(f'{where} must be an object')
            continue
        cols = ds.get('columns')
        rows = ds.get('rows')
        if not isinstance(cols, list) or not all(isinstance(c, dict) and 'key' in c for c in cols):
            errors.append(f'{where}.columns must be a list of {{"key", "label"}}')
            cols = []
        if not isinstance(rows, list):
            errors.append(f'{where}.rows must be a list')
            rows = []
        ids = [r.get('id') if isinstance(r, dict) else None for r in rows]
        if any(not isinstance(i, (int, float)) or isinstance(i, bool) for i in ids):
            errors.append(f'{where}: every row needs a numeric "id"')
        elif len(set(ids)) != len(ids):
            errors.append(f'{where}: row ids must be unique')

    colkeys = {k: {c.get('key') for c in (d.get('columns') or []) if isinstance(c, dict)} for k, d in datasets.items() if isinstance(d, dict)}
    for key, b in blocks.items():
        where = f'blocks.{key}'
        if not isinstance(b, dict):
            errors.append(f'{where} must be an object')
            continue
        if b.get('id') != key:
            errors.append(f'{where}.id must be "{key}"')
        t = b.get('type')
        if t not in ('text', 'chart', 'table', 'image', 'outline'):
            errors.append(f'{where}.type must be text, chart, table, image or outline')
            continue
        if b.get('breakBefore', 'auto') not in ('auto', 'page', 'avoid', 'beside'):
            errors.append(f'{where}.breakBefore must be auto, page, avoid or beside')
        if t in ('chart', 'table'):
            spec = b.get(t)
            if not isinstance(spec, dict):
                errors.append(f'{where}.{t} is missing')
                continue
            ds = spec.get('dataset')
            if ds not in datasets:
                errors.append(f'{where}.{t}.dataset "{ds}" is not in datasets')
                continue
            for f in (('x', 'y', 'color') if t == 'chart' else ('sortBy',)):
                v = spec.get(f)
                if v is not None and v not in colkeys.get(ds, set()):
                    errors.append(f'{where}.{t}.{f} "{v}" is not a column of dataset "{ds}"')
            if t == 'chart' and 'series' in spec:
                ser = spec['series']
                if not isinstance(ser, list):
                    errors.append(f'{where}.chart.series must be a list of {{"dataset", "x", "y", "label"}}')
                    continue
                for i, s in enumerate(ser):
                    sd = s.get('dataset') if isinstance(s, dict) else None
                    if sd not in datasets:
                        errors.append(f'{where}.chart.series[{i}].dataset "{sd}" is not in datasets')
                        continue
                    for f in ('x', 'y'):
                        if s.get(f) not in colkeys.get(sd, set()):
                            errors.append(f'{where}.chart.series[{i}].{f} "{s.get(f)}" is not a column of dataset "{sd}"')
                if ser and isinstance(ser[0], dict) and (ser[0].get('dataset'), ser[0].get('x'), ser[0].get('y')) != (ds, spec.get('x'), spec.get('y')):
                    warnings.append(f'{where}.chart.series[0] should repeat chart.dataset, x and y (older pages draw only those)')
        if t == 'image':
            im = b.get('image')
            if not isinstance(im, dict):
                errors.append(f'{where}.image is missing')
            elif im.get('asset'):
                if not any((project / 'assets').glob(im['asset'] + '.*')):
                    warnings.append(f'{where}: no file assets/{im["asset"]}.* in the project folder')
            elif not str(im.get('src', '')).startswith('data:image/'):
                errors.append(f'{where}.image needs "asset" or a data:image/ "src"')


    for key, a in (rep.get('annotations') or {}).items():
        if not isinstance(a, dict):
            continue
        if a.get('status') not in ('open', 'done'):
            errors.append(f'annotations.{key}.status must be open or done')
        fl = a.get('files', [])
        if not isinstance(fl, list) or not all(isinstance(p, str) for p in fl):
            errors.append(f'annotations.{key}.files must be a list of paths')
        else:
            out_f = [p for p in fl if not inside(root, project, p)]
            if out_f:
                errors.append(f'annotations.{key}.files: "{out_f[0]}" is outside the folder "{root.name}"')
        th = a.get('thread', [])
        if not isinstance(th, list) or not all(isinstance(m, dict) and m.get('by') in ('user', 'claude') and isinstance(m.get('text'), str) for m in th):
            errors.append(f'annotations.{key}.thread must be a list of {{"by": "user" | "claude", "text", "at"}}')
        bid = (a.get('target') or {}).get('blockId')
        if bid and bid not in blocks:
            warnings.append(f'annotations.{key} points at block "{bid}", which no longer exists')
    for key, c in (rep.get('changes') or {}).items():
        if isinstance(c, dict) and c.get('by') not in ('user', 'claude'):
            errors.append(f'changes.{key}.by must be user or claude')
    prop = (rep.get('style') or {}).get('proposal') if isinstance(rep.get('style'), dict) else None
    if isinstance(prop, dict):
        for i, r in enumerate(prop.get('rows') or []):
            if not isinstance(r, dict) or r.get('field') not in STYLE_FIELDS:
                warnings.append(f'style.proposal.rows[{i}] has an unknown field and will be ignored')
    for doc in ('writing', 'writingProposal'):
        w = (rep.get('style') or {}).get(doc) if isinstance(rep.get('style'), dict) else None
        if w is not None and (not isinstance(w, dict) or not isinstance(w.get('rules'), list)):
            errors.append(f'style.{doc} must be {{"rules": [{{"text": ...}}]}}')
        elif w:
            for i, r in enumerate(w['rules']):
                if not isinstance(r, dict) or not isinstance(r.get('text'), str) or not r['text'].strip():
                    warnings.append(f'style.{doc}.rules[{i}] needs a "text" and will be ignored')
    if out is not None:
        out['report'] = rep
    e, w, n = check_chain(project, root, rep, deep, out)
    return errors + e, warnings + w, notes + n


def print_problems(path, errors, warnings, notes):
    for n in notes:
        say(n)
    for w in warnings:
        say('WARNING', w)
    for e in errors:
        say('ERROR', e)
    if not errors:
        say('OK', path, f'({len(warnings)} warning(s))' if warnings else '')


def run_check(root, deep=False):
    """Check report.json and, when there is one, kifu.json next to it."""
    project, _ = project_of(root)
    info = {}
    errors, warnings, notes = check_report(project, root, deep, info)
    print_problems(project / 'report.json', errors, warnings, notes)
    bad = bool(errors)
    if (project / KIFU_FILE).is_file():
        e, w, n = check_kifu(project, root, info.get('report'), info.get('stale'))
        print_problems(project / KIFU_FILE, e, w, n)
        bad = bad or bool(e)
    return 1 if bad else 0


# ---------------------------------------------------------------- the page asks the agent to act
# The page's "Ask the agent to revise" button reaches an agent (Claude Code, Codex, ...) through small files in
# ~/.duetkifu/run/<folder name>-<hash of the project path>/ (outside the project, so a sync client such as OneDrive
# does not upload a file every few seconds), so any agent that can run a command in the background can answer:
#   launcher.json  written every few seconds by the running launcher (so waiting agents notice when it stops)
#   request.json   the latest request from the page: {id, kind, open, at}. It carries no text, only a kind from KINDS.
#   listening.json written every few seconds while an agent runs "duetkifu.py wait"
#   status.json    what the agent reported with "duetkifu.py agent-status": {request, state, at, note}
# A request is only a notice; the agent reads the annotations from report.json and treats them as feedback.

RUN_DIR = home_dir('run', 'RUN_DIR')
PERSONAL_DIR = home_dir('habits', 'HABITS_DIR', old_ok=True)   # habits for all projects
KINDS = ('revise',)
STATES = ('working', 'done', 'failed')
FRESH_S = 15          # an agent is listening when listening.json is younger than this
LAUNCHER_STALE_S = 30


def agent_file(project, name):
    p = project.resolve()
    key = hashlib.sha1(str(p).lower().encode('utf-8')).hexdigest()[:12]
    label = re.sub(r'[^\w.-]+', '_', (p.parent.name if p.name in WORKSPACES else p.name))[:40]
    return RUN_DIR / f'{label}-{key}' / name


def read_json(path):
    try:
        v = json.loads(path.read_text(encoding='utf-8'))
        return v if isinstance(v, dict) else None
    except (OSError, ValueError):
        return None


def write_json(path, obj):
    atomic_write(path, (json.dumps(obj, ensure_ascii=False) + '\n').encode('utf-8'))


def now_iso():
    return iso_ms(time.time() * 1000)


def age_s(obj):
    t = ms_of(obj.get('at')) if obj else None
    return (time.time() * 1000 - t) / 1000 if t is not None else float('inf')


def agent_state(project):
    """What the page shows next to the button."""
    req, st = read_json(agent_file(project, 'request.json')), read_json(agent_file(project, 'status.json'))
    return {'listening': age_s(read_json(agent_file(project, 'listening.json'))) < FRESH_S, 'request': req,
            'status': st if st and req and st.get('request') == req.get('id') else None}


def pending_request(project):
    """The latest request, if no agent has started on it yet."""
    req, st = read_json(agent_file(project, 'request.json')), read_json(agent_file(project, 'status.json'))
    if req and not (st and st.get('request') == req.get('id') and st.get('state') in STATES):
        return req
    return None


def launcher_stale(project, tries=3):
    """True when launcher.json is old or missing on `tries` reads a second apart. A single read can fail while the
    launcher replaces the file (Windows locks it for that moment), which is not a stopped launcher."""
    for i in range(tries):
        if age_s(read_json(agent_file(project, 'launcher.json'))) <= LAUNCHER_STALE_S:
            return False
        if i < tries - 1:
            time.sleep(1)
    return True


def wait_for_request(root):
    """For an agent: return when the user asks for something (exit 0), or when the launcher stops (exit 3).
    Meant to run in the background; each line it prints is something the agent should act on."""
    project, _ = project_of(root)
    if launcher_stale(project):
        say('LAUNCHER NOT RUNNING: start it first:', f'python "{HERE / "duetkifu.py"}" "{root}"')
        return 3
    beat = 0
    try:
        while True:
            if time.time() - beat >= 5:
                write_json(agent_file(project, 'listening.json'), {'at': now_iso(), 'pid': os.getpid()})
                beat = time.time()
            req = pending_request(project)
            if req:
                say(f'{req.get("kind", "revise").upper()} REQUESTED: {req.get("open", 0)} open annotation(s) (request {req.get("id")}).',
                    'Report progress with agent-status, then run wait again.')
                return 0
            if launcher_stale(project):
                say('LAUNCHER STOPPED: Duetkifu is no longer running for', root)
                return 3
            time.sleep(1)
    except KeyboardInterrupt:
        return 130
    finally:
        try:
            agent_file(project, 'listening.json').unlink()
        except OSError:
            pass


def set_agent_status(root, state, note):
    project, _ = project_of(root)
    req = read_json(agent_file(project, 'request.json'))
    write_json(agent_file(project, 'status.json'), {'request': req.get('id') if req else None, 'state': state, 'at': now_iso(), 'note': note})
    say('Status:', state, f'({note})' if note else '')
    return 0


def open_annotations(root):
    """The open annotations with the block, settings and data rows they point at, as compact JSON.
    A report can be megabytes of data rows; this is what an agent needs to read to revise it."""
    project, _ = project_of(root)
    try:
        rep = json.loads((project / 'report.json').read_text(encoding='utf-8-sig'))
    except (OSError, ValueError) as err:
        say('ERROR', 'cannot read report.json:', err)
        return 1
    blocks, datasets = rep.get('blocks') or {}, rep.get('datasets') or {}
    rounds, changes = (rep.get('rounds') or {}).values(), (rep.get('changes') or {}).values()
    last = max((r.get('at', '') for r in rounds if isinstance(r, dict)), default='')
    chap_of = {b.get('id'): c for c in chapters(rep) for b in c['blocks']}
    kifu, _ = read_json_file(project / KIFU_FILE) if (project / KIFU_FILE).is_file() else (None, None)
    moves = kifu.get('moves') if isinstance(kifu, dict) and isinstance(kifu.get('moves'), dict) else {}
    out = []
    for a in sorted((a for a in (rep.get('annotations') or {}).values() if isinstance(a, dict) and a.get('status') != 'done'),
                    key=lambda a: a.get('no', 0)):
        t = a.get('target') or {}
        if t.get('kind') == 'move':   # a comment on one move of the research record (kifu.json), not a report block
            m = moves.get(t.get('moveId'))
            out.append({'annotation': a, 'move': m if isinstance(m, dict) else None,
                       'moveNote': None if isinstance(m, dict) else f'move "{t.get("moveId")}" is not in {KIFU_FILE}'})
            continue
        b = blocks.get(t.get('blockId')) or {}
        blk = {k: v for k, v in b.items() if k not in ('createdAt', 'updatedAt')}
        if isinstance(blk.get('image'), dict):   # no image data in the output
            blk['image'] = {k: v for k, v in blk['image'].items() if k != 'src'}
        item = {'annotation': a, 'block': blk}
        ch = chap_of.get(t.get('blockId'))
        if ch:   # the chapter the block belongs to
            item['chapter'] = {'id': ch['id'], 'title': ch['title']}
        info = lambda ds: {'id': ds.get('id'), 'title': ds.get('title'), 'columns': ds.get('columns'),
                           'rows': len(ds.get('rows') or []), 'source': (ds.get('source') or {}).get('path')}
        dss = [datasets[i] for i in block_dataset_ids(b) if isinstance(datasets.get(i), dict)]
        if len(dss) == 1:
            item['dataset'] = info(dss[0])
        elif dss:
            item['datasets'] = [info(d) for d in dss]   # a chart with several series (see block.chart.series)
        # the rows a point, box or lasso points at: {dataset id: [row ids]}
        if 'rowId' in t:
            want = {t.get('datasetId') or (dss[0].get('id') if dss else None): [t['rowId']]}
        elif isinstance(t.get('enclosedBy'), dict):
            want = t['enclosedBy']
        else:
            want = {dss[0].get('id'): t.get('enclosed') or []} if dss else {}
        rows = []
        for dsid, ids in want.items():
            ids = set(ids or [])
            rows += [{'dataset': dsid, **r} for r in (datasets.get(dsid) or {}).get('rows') or [] if isinstance(r, dict) and r.get('id') in ids]
        if rows:
            item['targetRows'] = rows[:30]
            if len(rows) > 30:
                item['targetRowsNote'] = f'{len(rows) - 30} more rows not shown'
        out.append(item)
    style = rep.get('style') if isinstance(rep.get('style'), dict) else {}
    rules = [r.get('text') for r in ((style.get('writing') or {}).get('rules') or []) if isinstance(r, dict) and r.get('text')]
    print(json.dumps({'report': (rep.get('report') or {}).get('meta', {}).get('title'), 'file': str(project / 'report.json'),
                      'writingRules': rules, 'open': len(out), 'lastRoundAt': last or None,
                      'userChangesAfterLastRound': sum(1 for c in changes if isinstance(c, dict) and c.get('by') == 'user' and c.get('at', '') > last),
                      'annotations': out}, ensure_ascii=False, indent=1))
    return 0


# ---------------------------------------------------------------- the server

class Handler(http.server.BaseHTTPRequestHandler):
    server_version = 'Duetkifu/' + VERSION
    root = None       # the folder the user opened
    token = ''
    port = 0
    prints = None     # Fingerprints of the project folder, shared by all requests

    def log_message(self, *a):
        pass

    # -- helpers
    def send(self, code, body=b'', ctype='text/plain; charset=utf-8', headers=None):
        if isinstance(body, str):
            body = body.encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != 'HEAD':
            self.wfile.write(body)

    def allowed(self):
        host = self.headers.get('Host', '')
        if host not in (f'127.0.0.1:{self.port}', f'localhost:{self.port}'):
            self.send(403, 'bad host')
            return False
        return True

    def authorised(self):
        if not hmac.compare_digest(self.headers.get('X-Duetkifu-Token', ''), self.token):
            self.send(403, 'missing or wrong token')
            return False
        return True

    def target(self):
        """Resolve /fs/<path> inside the opened folder. Returns (path, is_dir) or None."""
        rel = urllib.parse.unquote(urllib.parse.urlsplit(self.path).path[len('/fs/'):])
        is_dir = rel.endswith('/') or rel == ''
        parts = [p for p in rel.split('/') if p]
        if any(p in ('.', '..') or '\\' in p or ':' in p for p in parts):
            self.send(400, 'bad path')
            return None
        p = self.root.joinpath(*parts) if parts else self.root
        try:
            p.resolve().relative_to(self.root.resolve())
        except ValueError:
            self.send(400, 'outside the folder')
            return None
        return p, is_dir

    def writable(self, p):
        project, _ = project_of(self.root)
        try:
            rel = p.resolve().relative_to(project.resolve())
        except ValueError:
            return p.resolve() == project.resolve()
        return not rel.parts or rel.parts[0] in OWN

    @staticmethod
    def mtime(p):
        return str(p.stat().st_mtime_ns // 1_000_000)

    # -- methods
    def do_GET(self):
        if not self.allowed():
            return
        path = urllib.parse.urlsplit(self.path).path
        if path in ('/', '/index.html', '/duetkifu.html'):
            return self.send(200, PAGE.read_bytes(), 'text/html; charset=utf-8')
        if not self.authorised():
            return
        if path == '/api/info':
            project, data = project_of(self.root)
            return self.send(200, json.dumps({'name': self.root.name, 'version': VERSION, 'layout': 'data-folder' if project != self.root else 'project-folder',
                                              'root': str(self.root), 'app': str(stable_app(False))}), 'application/json')
        if path == '/api/agent':
            project, _ = project_of(self.root)
            return self.send(200, json.dumps(agent_state(project)), 'application/json')
        if path == '/api/personal':   # the user's habits shared by all projects
            return self.send(200, json.dumps({k: read_json(PERSONAL_DIR / f'{k}.json') for k in ('profile', 'writing')}), 'application/json')
        if path.startswith('/fs/'):
            t = self.target()
            if not t:
                return
            p, is_dir = t
            if is_dir:
                if not p.is_dir():
                    return self.send(404, 'not found')
                items = []
                for c in sorted(p.iterdir(), key=lambda c: c.name):
                    if c.name.startswith('.') or c.name.endswith(('.duetkifu-tmp', '.duetkifu-tmp')):
                        continue
                    st = c.stat()
                    items.append({'name': c.name, 'kind': 'directory' if c.is_dir() else 'file', 'size': st.st_size, 'mtime': st.st_mtime_ns // 1_000_000})
                return self.send(200, json.dumps(items), 'application/json')
            if not p.is_file():
                return self.send(404, 'not found')
            return self.send(200, p.read_bytes(), mimetypes.guess_type(p.name)[0] or 'application/octet-stream', {'X-Mtime': self.mtime(p)})
        self.send(404, 'not found')

    def do_HEAD(self):
        if not self.allowed() or not self.authorised():
            return
        if not urllib.parse.urlsplit(self.path).path.startswith('/fs/'):
            return self.send(404)
        t = self.target()
        if not t:
            return
        p, is_dir = t
        ok = p.is_dir() if is_dir else p.is_file()
        self.send(200 if ok else 404, b'', headers={'X-Mtime': self.mtime(p)} if ok else None)

    def do_POST(self):
        if not self.allowed() or not self.authorised():
            return
        path = urllib.parse.urlsplit(self.path).path
        body = self.rfile.read(int(self.headers.get('Content-Length', 0) or 0))
        if path == '/api/log':
            try:
                e = json.loads(body.decode('utf-8'))
            except Exception:
                return self.send(400, 'bad log entry')
            level = str(e.get('level', 'error')).upper()
            msg, detail = str(e.get('msg', '')), str(e.get('detail', ''))
            say(level, msg + (' | ' + detail.replace('\n', ' | ') if detail else ''))
            project, _ = project_of(self.root)
            try:
                project.mkdir(parents=True, exist_ok=True)
                with open(project / 'errors.log', 'a', encoding='utf-8') as f:
                    f.write(f'{e.get("t") or time.strftime("%Y-%m-%dT%H:%M:%S")} {level} {msg}\n' + ''.join('    ' + l + '\n' for l in detail.splitlines()))
            except OSError:
                pass
            return self.send(204)
        if path == '/api/fingerprints':   # {files: [file reference]} -> the state of each file now (see Fingerprints.status)
            try:
                files = json.loads(body.decode('utf-8'))['files']
                assert isinstance(files, list)
            except Exception:
                return self.send(400, 'expected {"files": [{"path": ...}]}')
            project, _ = project_of(self.root)
            if Handler.prints is None or Handler.prints.project != project:
                Handler.prints = Fingerprints(project)
            out = []
            for ref in files[:20000]:
                if not isinstance(ref, dict) or not isinstance(ref.get('path'), str):
                    out.append({'state': 'missing'})
                elif not inside(self.root, project, ref['path']):
                    out.append({'path': ref['path'], 'state': 'outside'})
                else:
                    try:
                        out.append(Handler.prints.status(ref))
                    except OSError as err:
                        out.append({'path': ref['path'], 'state': 'missing', 'error': str(err)})
            Handler.prints.save()
            return self.send(200, json.dumps(out), 'application/json')
        if path == '/api/reveal':   # open the folder of a file in Explorer / Finder, with the file selected
            try:
                rel = json.loads(body.decode('utf-8'))['path']
                assert isinstance(rel, str) and rel
            except Exception:
                return self.send(400, 'expected {"path": ...}')
            project, _ = project_of(self.root)
            if not inside(self.root, project, rel):
                return self.send(403, 'outside the folder')
            p = (project / norm(rel)).resolve()
            if not p.exists():
                return self.send(404, 'not found')
            try:
                if os.name == 'nt':
                    subprocess.Popen(f'explorer /select,"{p}"')   # a Windows path cannot contain a double quote
                elif sys.platform == 'darwin':
                    subprocess.Popen(['open', '-R', str(p)])
                else:
                    subprocess.Popen(['xdg-open', str(p.parent if p.is_file() else p)])
            except OSError as err:
                return self.send(500, f'could not open the folder: {err}')
            return self.send(204)
        if path == '/api/revise':   # the page's "Ask the agent to revise": only a kind and a count are taken from the page
            try:
                req = json.loads(body.decode('utf-8'))
                kind, n = req.get('kind', 'revise'), int(req.get('open', 0))
                assert kind in KINDS and 0 <= n < 100000
            except Exception:
                return self.send(400, 'expected {"kind": "revise", "open": <number>}')
            project, _ = project_of(self.root)
            write_json(agent_file(project, 'request.json'), {'id': 'q' + secrets.token_hex(6), 'kind': kind, 'open': n, 'at': now_iso()})
            say(f'{kind.upper()} REQUESTED: {n} open annotation(s)')
            return self.send(200, json.dumps(agent_state(project)), 'application/json')
        if path.startswith('/fs/'):   # create a folder
            t = self.target()
            if not t:
                return
            p, _ = t
            if not self.writable(p):
                return self.send(403, 'Duetkifu only writes in its own project folder')
            p.mkdir(parents=True, exist_ok=True)
            return self.send(200)
        self.send(404, 'not found')

    def do_PUT(self):
        if not self.allowed() or not self.authorised():
            return
        if urllib.parse.urlsplit(self.path).path == '/api/personal':   # {profile?, writing?} -> ~/.duetkifu/habits/
            try:
                new = json.loads(self.rfile.read(min(int(self.headers.get('Content-Length', 0) or 0), 2_000_000)).decode('utf-8'))
                assert isinstance(new, dict)
            except Exception:
                return self.send(400, 'expected {"profile": {...}, "writing": {"rules": [...]}}')
            for k in ('profile', 'writing'):
                if isinstance(new.get(k), dict):
                    write_json(PERSONAL_DIR / f'{k}.json', {**new[k], 'savedAt': now_iso()})
            return self.send(204)
        if not urllib.parse.urlsplit(self.path).path.startswith('/fs/'):
            return self.send(404)
        t = self.target()
        if not t:
            return
        p, is_dir = t
        if is_dir or not self.writable(p):
            return self.send(403, 'Duetkifu only writes in its own project folder')
        body = self.rfile.read(int(self.headers.get('Content-Length', 0) or 0))
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(p.name + '.' + secrets.token_hex(4) + '.duetkifu-tmp')
        tmp.write_bytes(body)
        for attempt in range(5):   # a sync client (OneDrive, Dropbox) may hold the file for a moment
            try:
                os.replace(tmp, p)
                return self.send(200, b'', headers={'X-Mtime': self.mtime(p)})
            except PermissionError as e:
                err = e
                time.sleep(0.2 * (attempt + 1))
        try:
            tmp.unlink()
        except OSError:
            pass
        self.send(503, f'the file is locked by another program: {err}')


class Server(http.server.ThreadingHTTPServer):
    # On Windows, SO_REUSEADDR lets a second launcher bind a port another one already uses, and requests then go to
    # either of them. Take the port exclusively there, so a busy port is skipped and the next one is used.
    allow_reuse_address = os.name != 'nt'

    def server_bind(self):
        if os.name == 'nt' and hasattr(socket, 'SO_EXCLUSIVEADDRUSE'):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def serve(root, port, open_browser):
    project, data = project_of(root)
    if not PAGE.is_file():
        say('ERROR', f'{PAGE} not found; keep duetkifu.py next to duetkifu.html')
        return 1
    try:
        stable_app(False)  # keep ~/.duetkifu/app (used by shortcuts) as new as this plugin version
    except OSError as err:
        say('WARNING', 'could not update', STABLE, err)
    if (project / 'report.json').is_file():
        run_check(root)
    else:
        say('No report yet. The page will create', project / 'report.json')
    token = secrets.token_hex(16)
    httpd = None
    for p in ([port] if port else range(8765, 8790)):
        try:
            httpd = Server(('127.0.0.1', p), Handler)
            break
        except OSError:
            continue
    if httpd is None:
        say('ERROR', 'no free port between 8765 and 8789; use --port')
        return 1
    Handler.root, Handler.token, Handler.port = root, token, httpd.server_address[1]
    url = f'http://127.0.0.1:{Handler.port}/#token={token}'
    say('Duetkifu', VERSION, 'for', root)
    say('Report:', project / 'report.json')
    say('Raw data (read only):', data)
    say('Open:', url)
    say('Problems reported by the page are printed here and saved in', project / 'errors.log')
    say('Stop with Ctrl+C.')
    if open_browser:
        threading.Timer(0.5, webbrowser.open, [url]).start()

    stop = threading.Event()

    def heartbeat():   # lets "duetkifu.py wait" notice when the launcher stops
        while not stop.is_set():
            try:
                write_json(agent_file(project, 'launcher.json'), {'at': now_iso(), 'pid': os.getpid(), 'port': Handler.port})
            except OSError:
                pass
            stop.wait(5)
    threading.Thread(target=heartbeat, daemon=True).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        say('stopped')
    finally:
        stop.set()
        try:
            agent_file(project, 'launcher.json').unlink()
        except OSError:
            pass
    return 0


# ---------------------------------------------------------------- recording a computation step

def record_step(root, a):
    """Add (or replace) steps/<id> in report.json with fingerprints of the script, inputs and outputs.
    Patterns are relative to the folder of report.json, for example ../Data/*.xlsx or derived_data/cells.csv."""
    project, _ = project_of(root)
    path = project / 'report.json'
    if not path.is_file():
        say('ERROR', f'{path} does not exist; write the report (or start Duetkifu once) before recording steps')
        return 1
    fp = Fingerprints(project)

    def expand(patterns, what):
        out = []
        for pat in patterns:
            hits = sorted(h for h in glob.glob(os.path.join(str(project), pat), recursive=True) if os.path.isfile(h))
            if not hits:
                raise ValueError(f'{what} "{pat}" matches no file (patterns are relative to {project})')
            for h in hits:
                rel = norm(os.path.relpath(h, project))
                if not inside(root, project, rel):
                    raise ValueError(f'{what} "{rel}" is outside the folder "{root.name}"; put it inside "{root.name}" first')
                if rel not in [r['path'] for r in out]:
                    out.append(fp.ref(rel))
        return out

    try:
        script = expand([a.script], '--script')[0] if a.script else None
        inputs, outputs = expand(a.inputs, '--in'), expand(a.outputs, '--out')
    except (ValueError, OSError) as err:
        say('ERROR', err)
        return 1
    if not outputs:
        say('ERROR', 'name the files the step wrote with --out')
        return 1
    params = {}
    for p in a.param:
        k, _, v = p.partition('=')
        try:
            params[k] = json.loads(v)
        except ValueError:
            params[k] = v
    stem = pathlib.PurePosixPath((script or outputs[0])['path']).stem
    sid = a.id or 's-' + (re.sub(r'[^a-z0-9]+', '-', stem.lower()).strip('-') or 'step')
    step = {'id': sid, 'script': script, 'command': a.command, 'params': params, 'inputs': inputs, 'outputs': outputs,
            'inputPatterns': [norm(p) if not re.search(r'[*?\[]', p) else p.replace('\\', '/') for p in a.inputs],
            'at': iso_ms(time.time() * 1000), 'by': a.by, 'note': a.note}
    try:
        rep = json.loads(path.read_text(encoding='utf-8-sig'))   # read fresh: the page may have saved since
        steps = rep.setdefault('steps', {})
        replaced = sid in steps
        steps[sid] = step
        rep['schema'] = SCHEMA
        atomic_write(path, (json.dumps(rep, ensure_ascii=False, indent=1, allow_nan=False) + '\n').encode('utf-8'))
    except (OSError, ValueError) as err:
        say('ERROR', 'could not update report.json:', err)
        return 1
    fp.save()
    total = len(inputs) + len(outputs) + bool(script)
    say('Replaced' if replaced else 'Recorded', f'step "{sid}":', f'{len(inputs)} input(s) -> {len(outputs)} output(s)',
        '' if fp.hashed == total else f'({fp.hashed} of {total} file(s) read; the other fingerprints were already known)')
    return 0


# ---------------------------------------------------------------- figures and tables of an HTML report
# A report made earlier (by a person or an agent) often holds the figures and tables a move of the research record is
# about, embedded in one large HTML file. `extract` writes them out as files next to the report's other derived data,
# with the section, heading and caption each one sits under, so a move can point at them (kifu.json evidence.files)
# and the page can show them. Figures drawn by scripts in the page (canvas, SVG built at load time) are not included.

EXTRACT_DIR = 'derived_data/report_figures'
IMAGE_EXT = {'png': 'png', 'jpeg': 'jpg', 'jpg': 'jpg', 'gif': 'gif', 'webp': 'webp', 'svg+xml': 'svg'}


class ReportReader(html.parser.HTMLParser):
    """Collects embedded images (data: URLs) and tables in document order, each with the id of the section it is in,
    the latest heading and a caption (figcaption, table caption, alt text, or the text right after an image)."""
    HEADINGS = ('h1', 'h2', 'h3', 'h4')

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.items, self.section, self.heading = [], '', ''
        self.skip = 0              # inside script or style
        self.text = None           # the heading being read
        self.figure = []           # items of the open <figure>
        self.figcap = None         # the figcaption being read
        self.after = None          # an image waiting for the text after it
        self.table = None          # the table being read: {rows, row, cell, caption}
        self.tables = []           # open tables (a table inside a table is read as text of its cell)

    def item(self, kind, **kw):
        it = {'kind': kind, 'section': self.section, 'heading': self.heading, 'caption': '', **kw}
        self.items.append(it)
        return it

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag in ('script', 'style'):
            self.skip += 1
            return
        if a.get('id') and (tag in ('section', 'article') + self.HEADINGS):
            self.section = a['id']
        if tag in self.HEADINGS:
            self.text = []
            self.after = None
        elif tag == 'figure':
            self.figure = []
        elif tag == 'figcaption':
            self.figcap = []
        elif tag == 'img':
            m = re.match(r'data:image/([\w.+-]+);base64,(.*)', a.get('src') or '', re.S)
            if m and m.group(1).lower() in IMAGE_EXT:
                it = self.item('figure', ext=IMAGE_EXT[m.group(1).lower()], data=m.group(2), caption=' '.join((a.get('alt') or '').split()))
                self.figure.append(it)
                self.after = it if not it['caption'] else None
                self.after_text = []
        elif tag == 'table':
            if self.table:
                self.tables.append(self.table)
            self.table = {'rows': [], 'row': None, 'cell': None, 'caption': None, 'section': self.section, 'heading': self.heading}
            self.after = None
        elif self.table and tag == 'tr':
            self.table['row'] = []
        elif self.table and tag in ('td', 'th'):
            self.table['cell'] = []
            try:
                self.table['span'] = max(1, min(50, int(a.get('colspan') or 1)))
            except ValueError:
                self.table['span'] = 1
        elif self.table and tag == 'caption':
            self.table['caption'] = []
        elif tag == 'br' and self.table and self.table['cell'] is not None:
            self.table['cell'].append(' ')

    def handle_endtag(self, tag):
        if tag in ('script', 'style'):
            self.skip = max(0, self.skip - 1)
            return
        if tag in self.HEADINGS and self.text is not None:
            self.heading = ' '.join(''.join(self.text).split())[:200]
            self.text = None
        elif tag == 'figcaption' and self.figcap is not None:
            cap = ' '.join(''.join(self.figcap).split())[:400]
            for it in self.figure:
                it['caption'] = cap
            self.figcap = None
            self.after = None
        elif tag == 'figure':
            self.figure = []
        elif self.table and tag in ('td', 'th') and self.table['cell'] is not None:
            if self.table['row'] is None:
                self.table['row'] = []
            text = ' '.join(''.join(self.table['cell']).split())
            self.table['row'] += [text] + [''] * (self.table.get('span', 1) - 1)
            self.table['cell'] = None
        elif self.table and tag == 'tr' and self.table['row'] is not None:
            if any(c for c in self.table['row']):
                self.table['rows'].append(self.table['row'])
            self.table['row'] = None
        elif self.table and tag == 'caption' and self.table['caption'] is not None:
            self.table['caption'] = ' '.join(''.join(self.table['caption']).split())[:400]
        elif tag == 'table' and self.table:
            t = self.table
            if len(t['rows']) >= 2:   # a table with a header and at least one row
                it = {'kind': 'table', 'section': t['section'], 'heading': t['heading'], 'caption': t['caption'] if isinstance(t['caption'], str) else '',
                      'rows': t['rows']}
                self.items.append(it)
            self.table = self.tables.pop() if self.tables else None

    def handle_data(self, data):
        if self.skip:
            return
        if self.text is not None:
            self.text.append(data)
        if self.figcap is not None:
            self.figcap.append(data)
        if self.table:
            if isinstance(self.table.get('caption'), list):
                self.table['caption'].append(data)
            elif self.table['cell'] is not None:
                self.table['cell'].append(data)
        elif self.after is not None and data.strip():
            self.after_text.append(data)
            text = ' '.join(''.join(self.after_text).split())
            self.after['caption'] = text[:300]
            if len(text) >= 300:
                self.after = None


def read_pptx(path):
    """Items of a PowerPoint file, slide by slide: its pictures and tables, with the slide's first text as heading
    and its text as caption; the section is slide-<n>."""
    items = []
    A = '{http://schemas.openxmlformats.org/drawingml/2006/main}'
    with zipfile.ZipFile(path) as z:
        names = set(z.namelist())
        slides = sorted((int(m.group(1)), n) for n in names for m in [re.fullmatch(r'ppt/slides/slide(\d+)\.xml', n)] if m)
        for no, name in slides:
            root_el = ET.fromstring(z.read(name))
            texts = [''.join(t.text or '' for t in p.iter(A + 't')) for p in root_el.iter(A + 'p')]   # one text per paragraph
            words = ' '.join(' '.join(texts).split())
            heading = next((' '.join(t.split()) for t in texts if t.strip()), '')[:200]
            rels = {}
            rel_name = f'ppt/slides/_rels/slide{no}.xml.rels'
            if rel_name in names:
                for r in ET.fromstring(z.read(rel_name)):
                    rels[r.get('Id')] = posixpath.normpath(posixpath.join('ppt/slides', r.get('Target') or ''))
            for blip in root_el.iter(A + 'blip'):
                target = rels.get(blip.get('{http://schemas.openxmlformats.org/officeDocument/2006/relationships}embed'))
                ext = (target or '').rsplit('.', 1)[-1].lower()
                if target in names and ext in ('png', 'jpg', 'jpeg', 'gif', 'webp', 'svg'):
                    items.append({'kind': 'figure', 'section': f'slide-{no}', 'heading': heading, 'caption': words[:300],
                                  'ext': 'jpg' if ext == 'jpeg' else ext, 'bytes': z.read(target)})
            for tbl in root_el.iter(A + 'tbl'):
                rows = [[' '.join(''.join(t.text or '' for t in tc.iter(A + 't')).split()) for tc in tr.iter(A + 'tc')] for tr in tbl.iter(A + 'tr')]
                if len(rows) >= 2:
                    items.append({'kind': 'table', 'section': f'slide-{no}', 'heading': heading, 'caption': words[:300], 'rows': rows})
    return items


def extract_report(root, report, note=''):
    """Write the embedded images and the tables of an HTML report under duetkifu/derived_data/report_figures/<name>/,
    with index.json (section, heading, caption of each), and record it as a step."""
    project, _ = project_of(root)
    if not (project / 'report.json').is_file():
        say('ERROR', f'{project / "report.json"} does not exist; start Duetkifu once before extracting')
        return 1
    src = pathlib.Path(report).expanduser()
    src = (src if src.is_absolute() else pathlib.Path.cwd() / src).resolve()
    rel = norm(os.path.relpath(src, project))
    if not src.is_file() or not inside(root, project, rel):
        say('ERROR', f'{report} is not a file inside the folder "{root.name}"')
        return 1
    try:
        if src.suffix.lower() == '.pptx':
            items = read_pptx(src)
        else:
            reader = ReportReader()
            reader.feed(src.read_text(encoding='utf-8-sig', errors='replace'))
            items = reader.items
    except (OSError, ValueError, zipfile.BadZipFile, ET.ParseError) as err:
        say('ERROR', f'cannot read {rel}:', err)
        return 1
    name = re.sub(r'[\\/:*?"<>|\s]+', '_', src.stem).strip('_') or 'report'
    out = project / EXTRACT_DIR / name
    if out.is_dir():   # a fresh extraction replaces the files of the previous one
        for old in out.iterdir():
            if old.is_file():
                old.unlink()
    out.mkdir(parents=True, exist_ok=True)
    index, nf, nt = [], 0, 0
    for it in items:
        if it['kind'] == 'figure':
            try:
                data = it['bytes'] if 'bytes' in it else base64.b64decode(re.sub(r'\s+', '', it['data']), validate=False)
            except (ValueError, TypeError):
                continue
            if len(data) < 1024:   # an icon, not a figure
                continue
            nf += 1
            fname = f'figure-{nf:02d}.{it["ext"]}'
            (out / fname).write_bytes(data)
        else:
            nt += 1
            fname = f'table-{nt:02d}.csv'
            width = max(len(r) for r in it['rows'])
            with open(out / fname, 'w', encoding='utf-8', newline='') as f:
                csv.writer(f, lineterminator='\n').writerows(r + [''] * (width - len(r)) for r in it['rows'])
        index.append({'file': fname, 'kind': it['kind'], 'section': it['section'], 'heading': it['heading'], 'caption': it['caption']})
    base = f'{EXTRACT_DIR}/{name}'
    sid = f's-extract-{re.sub(r"[^\w]+|_", "-", name.lower()).strip("-") or "report"}'   # keeps CJK letters, so names stay apart
    if not index:   # nothing to point at: leave no empty folder, and drop the step of an earlier extraction
        out.rmdir()
        say(f'{rel}: no embedded figures or tables (figures and tables drawn by the page\'s own scripts are not included)')
        rep, err = read_json_file(project / 'report.json')
        if isinstance(rep, dict) and sid in (rep.get('steps') or {}):
            del rep['steps'][sid]
            atomic_write(project / 'report.json', (json.dumps(rep, ensure_ascii=False, indent=1, allow_nan=False) + '\n').encode('utf-8'))
            say(f'Removed step "{sid}"')
        return 0
    (out / 'index.json').write_text(json.dumps({'report': rel, 'items': index}, ensure_ascii=False, indent=1) + '\n', encoding='utf-8', newline='\n')
    step = argparse.Namespace(script='', inputs=[rel], outputs=[f'{base}/*'], command=f'python duetkifu.py extract "{root.name}" "{rel}"',
                              param=[], id=sid, by='claude', note=note or f'Figures and tables pulled out of {src.name} (duetkifu.py extract)')
    say(f'{rel}: {nf} figure(s) and {nt} table(s) -> {base}/')
    return record_step(root, step)


# ---------------------------------------------------------------- the search index
# A project folder can hold thousands of files: instrument exports, workbooks of 100 MB, HTML reports of several MB
# (mostly embedded images), slides, notes. Reading them to find one sentence costs an agent a lot. `index` keeps the
# text of each file, cut into pieces with where each piece is (section, slide, sheet, move), in an SQLite database with
# full-text search; `find` searches it and prints short excerpts, and `kifu show` prints one move with the excerpts of
# its source. The database lives outside the project (~/.duetkifu/index/), because a sync client such as OneDrive
# can lock or duplicate a database file; it can be deleted at any time and is rebuilt. A file is read again only when
# its size or modification time changed. Nothing in the folder is changed.

INDEX_DIR = home_dir('index', 'INDEX_DIR')
INDEX_VERSION = '1'
PIECE = 1500          # characters per piece of text
TEXT_EXT = {'.md', '.txt', '.py', '.r', '.m', '.jl', '.js', '.sh', '.ps1', '.bat', '.yaml', '.yml', '.toml', '.ini', '.cfg', '.log', '.tex', '.bib', '.rst'}
IMAGE_EXTS = {'.png', '.jpg', '.jpeg', '.gif', '.webp', '.svg', '.bmp', '.tif', '.tiff'}
SKIP_INDEX_DIRS = {'.git', 'node_modules', '__pycache__', '.ipynb_checkpoints'}


def index_file(project):
    p = project.resolve()
    key = hashlib.sha1(str(p).lower().encode('utf-8')).hexdigest()[:12]
    label = re.sub(r'[^\w.-]+', '_', (p.parent.name if p.name in WORKSPACES else p.name))[:40]
    return INDEX_DIR / f'{label}-{key}.sqlite'


def pieces(text, loc=''):
    """Cut text into pieces of about PIECE characters, at line ends where possible."""
    text = re.sub(r'[ \t\r\f\v]+', ' ', text or '').strip()
    text = re.sub(r'\n\s*\n+', '\n', text)
    out, start = [], 0
    while start < len(text):
        end = min(len(text), start + PIECE)
        if end < len(text):
            cut = text.rfind('\n', start + PIECE // 2, end)
            end = cut if cut > 0 else end
        chunk = text[start:end].strip()
        if chunk:
            out.append((loc, chunk))
        start = end
    return out


class HtmlText(html.parser.HTMLParser):
    """The visible text of an HTML page, in pieces that start at each heading (loc: #id heading)."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out, self.buf, self.loc, self.skip, self.head, self.sec = [], [], '', 0, None, ''

    def flush(self):
        self.out += pieces(''.join(self.buf), self.loc)
        self.buf = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag in ('script', 'style', 'svg', 'canvas'):
            self.skip += 1
        if a.get('id') and tag in ('section', 'article', 'h1', 'h2', 'h3', 'h4'):
            self.sec = a['id']
        if tag in ('h1', 'h2', 'h3', 'h4'):
            self.flush()
            self.head = []
        elif tag in ('p', 'div', 'li', 'tr', 'br', 'table', 'section', 'figure', 'figcaption'):
            self.buf.append('\n')
        elif tag in ('td', 'th'):
            self.buf.append(' | ')

    def handle_endtag(self, tag):
        if tag in ('script', 'style', 'svg', 'canvas'):
            self.skip = max(0, self.skip - 1)
        elif tag in ('h1', 'h2', 'h3', 'h4') and self.head is not None:
            h = ' '.join(''.join(self.head).split())[:120]
            self.loc = (f'#{self.sec} ' if self.sec else '') + h
            self.buf.append(h + '\n')
            self.head = None

    def handle_data(self, data):
        if self.skip:
            return
        if self.head is not None:
            self.head.append(data)
        else:
            self.buf.append(data)


def xlsx_outline(path):
    """Sheet names, their size (from <dimension>) and first row, without reading whole workbooks (they can be 100 MB)."""
    S = '{http://schemas.openxmlformats.org/spreadsheetml/2006/main}'
    R = '{http://schemas.openxmlformats.org/officeDocument/2006/relationships}'
    out = []
    with zipfile.ZipFile(path) as z:
        names = set(z.namelist())
        wb = ET.fromstring(z.read('xl/workbook.xml'))
        rels = {r.get('Id'): r.get('Target') for r in ET.fromstring(z.read('xl/_rels/workbook.xml.rels'))} if 'xl/_rels/workbook.xml.rels' in names else {}
        firsts = []
        for sh in wb.iter(S + 'sheet'):
            target = rels.get(sh.get(R + 'id'), '')
            target = posixpath.normpath(target.lstrip('/') if target.startswith('/') else posixpath.join('xl', target))
            head, dim = b'', ''
            if target in names:
                with z.open(target) as f:
                    head = f.read(262144)
                m = re.search(rb'<dimension ref="([^"]+)"', head)
                dim = m.group(1).decode() if m else ''
            row = re.search(rb'<row[^>]*>(.*?)</row>', head, re.S)
            cells = re.findall(rb'<c ([^>]*?)(?:/>|>(.*?)</c>)', row.group(1), re.S) if row else []
            firsts.append((sh.get('name'), dim, cells))
        need = {int(v) for _, _, cells in firsts for a, body in cells if b't="s"' in a
                for v in re.findall(rb'<v>(\d+)</v>', body or b'')}
        shared = {}
        if need and 'xl/sharedStrings.xml' in names:
            top = max(need)
            with z.open('xl/sharedStrings.xml') as f:
                i = 0
                for ev, el in ET.iterparse(f):
                    if el.tag == S + 'si':
                        if i in need:
                            shared[i] = ''.join(t.text or '' for t in el.iter(S + 't'))
                        i += 1
                        el.clear()
                        if i > top:
                            break
        for name, dim, cells in firsts:
            vals = []
            for a, body in cells:
                body = body or b''
                v = re.search(rb'<v>(.*?)</v>', body, re.S)
                if b't="s"' in a and v:
                    vals.append(shared.get(int(v.group(1)), ''))
                elif b't="inlineStr"' in a:
                    vals.append(''.join(x.decode('utf-8', 'replace') for x in re.findall(rb'<t[^>]*>(.*?)</t>', body, re.S)))
                elif v:
                    vals.append(v.group(1).decode('utf-8', 'replace'))
            out.append((name, dim, [html.unescape(x) for x in vals]))
    return out


def figure_caption(path):
    """The heading and caption `extract` recorded for a figure or table file, if it came from a report."""
    idx = path.parent / 'index.json'
    if not idx.is_file():
        return None
    try:
        for it in json.loads(idx.read_text(encoding='utf-8')).get('items', []):
            if it.get('file') == path.name:
                return ' | '.join(x for x in (it.get('heading'), it.get('caption')) if x)
    except (OSError, ValueError):
        pass
    return None


def read_for_index(path, rel):
    """(kind, note, [(loc, text)]) for one file."""
    ext = path.suffix.lower()
    name = path.name.lower()
    if ext in ('.html', '.htm'):
        p = HtmlText()
        p.feed(path.read_text(encoding='utf-8-sig', errors='replace'))
        p.flush()
        return 'report', '', p.out
    if ext == '.md':
        out, loc, buf = [], '', []
        for line in path.read_text(encoding='utf-8-sig', errors='replace').splitlines():
            if re.match(r'#{1,6} ', line):
                out += pieces('\n'.join(buf), loc)
                loc, buf = line.lstrip('#').strip()[:120], []
            buf.append(line)
        return 'text', '', out + pieces('\n'.join(buf), loc)
    if ext in TEXT_EXT:
        return ('script' if ext in ('.py', '.r', '.m', '.jl', '.js', '.sh', '.ps1', '.bat') else 'text'), '', pieces(path.read_text(encoding='utf-8-sig', errors='replace'))
    if ext in ('.csv', '.tsv'):
        size = path.stat().st_size
        with open(path, encoding='utf-8-sig', errors='replace', newline='') as f:
            lines = [ln for _, ln in zip(range(7), f)]
        rows = list(csv.reader(lines, delimiter='\t' if ext == '.tsv' else ','))
        n = None
        if size < 50_000_000:
            with open(path, 'rb') as f:
                n = sum(1 for _ in f) - 1
        note = f'{len(rows[0]) if rows else 0} columns' + (f', {n} rows' if n is not None else ', large file')
        body = '\n'.join(' | '.join(r) for r in rows[:6])
        cap = figure_caption(path)
        return 'table', note, ([('caption', cap)] if cap else []) + [('columns', body[:PIECE])]
    if ext == '.json':
        if name == KIFU_FILE:
            k = json.loads(path.read_text(encoding='utf-8-sig'))
            out = []
            meta = (k.get('kifu') or {}).get('meta') or {}
            if meta.get('question'):
                out.append(('question', f'{meta.get("question")}\n{meta.get("context") or ""}'))
            for mid, m in (k.get('moves') or {}).items():
                if isinstance(m, dict):
                    res = m.get('result') if isinstance(m.get('result'), dict) else {}
                    body = '\n'.join(str(x) for x in (m.get('title'), res.get('text'), m.get('why'), m.get('note'), m.get('reason'), m.get('population')) if x)
                    out.append((f'move {mid}', body[:PIECE]))
            return 'record', f'{len(k.get("moves") or {})} moves', out
        if name == 'report.json':
            r = json.loads(path.read_text(encoding='utf-8-sig'))
            out = []
            for bid, b in (r.get('blocks') or {}).items():
                if isinstance(b, dict):
                    body = '\n'.join(str(b.get(f) or '') for f in ('title', 'text', 'caption')).strip()
                    if body:
                        out += pieces(body, f'block {bid}')
            for did, d in (r.get('datasets') or {}).items():
                if isinstance(d, dict):
                    cols = ', '.join(str(c.get('label') or c.get('key')) for c in d.get('columns') or [] if isinstance(c, dict))
                    out.append((f'dataset {did}', f'{d.get("title") or did}: {cols}'[:PIECE]))
            return 'report', f'{len(r.get("blocks") or {})} blocks', out
        if name == 'index.json' and path.parent.parent.name == 'report_figures':
            return 'data', '', []   # the captions are indexed with each figure and table
        if path.stat().st_size < 1_000_000:
            return 'data', '', pieces(path.read_text(encoding='utf-8-sig', errors='replace'))
        return 'data', 'large JSON, not read', []
    if ext == '.pptx':
        A = '{http://schemas.openxmlformats.org/drawingml/2006/main}'
        out = []
        with zipfile.ZipFile(path) as z:
            slides = sorted((int(m.group(1)), n) for n in z.namelist() for m in [re.fullmatch(r'ppt/slides/slide(\d+)\.xml', n)] if m)
            for no, n in slides:
                el = ET.fromstring(z.read(n))
                out += pieces('\n'.join(''.join(t.text or '' for t in p.iter(A + 't')) for p in el.iter(A + 'p')), f'slide {no}')
        return 'slides', f'{len(slides)} slides', out
    if ext == '.docx':
        W = '{http://schemas.openxmlformats.org/wordprocessingml/2006/main}'
        out, loc, buf = [], '', []
        with zipfile.ZipFile(path) as z:
            for p in ET.fromstring(z.read('word/document.xml')).iter(W + 'p'):
                text = ''.join(t.text or '' for t in p.iter(W + 't'))
                style = p.find(f'{W}pPr/{W}pStyle')
                if style is not None and re.match(r'(Heading|Title|\d)', style.get(W + 'val') or '') and text.strip():
                    out += pieces('\n'.join(buf), loc)
                    loc, buf = text.strip()[:120], []
                buf.append(text)
        return 'document', '', out + pieces('\n'.join(buf), loc)
    if ext in ('.xlsx', '.xlsm'):
        sheets = xlsx_outline(path)
        return 'workbook', f'{len(sheets)} sheet(s)', [(f'sheet {n}', f'{n} ({d}): ' + ' | '.join(v)[:PIECE]) for n, d, v in sheets]
    if ext in IMAGE_EXTS:
        cap = figure_caption(path)
        return 'figure', '', [('caption', cap)] if cap else []
    if ext == '.pdf':
        return 'pdf', 'text not indexed', []
    return 'file', '', []


def open_index(project):
    """The index database of a project; (connection, has full-text search)."""
    db = index_file(project)
    db.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(db))
    con.execute('CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT)')
    ver = con.execute("SELECT value FROM meta WHERE key='version'").fetchone()
    if not ver or ver[0] != INDEX_VERSION:   # built by another version: start again
        con.executescript('DROP TABLE IF EXISTS files; DROP TABLE IF EXISTS chunks;')
        con.execute("INSERT OR REPLACE INTO meta VALUES('version', ?)", (INDEX_VERSION,))
    con.execute('CREATE TABLE IF NOT EXISTS files(path TEXT PRIMARY KEY, ext TEXT, size INTEGER, mtime INTEGER, kind TEXT, note TEXT)')
    fts = True
    try:
        con.execute("CREATE VIRTUAL TABLE IF NOT EXISTS chunks USING fts5(path UNINDEXED, loc UNINDEXED, body, tokenize='trigram')")
    except sqlite3.OperationalError:   # SQLite older than 3.34: plain table, searched with LIKE
        fts = False
        con.execute('CREATE TABLE IF NOT EXISTS chunks(path TEXT, loc TEXT, body TEXT)')
    con.commit()
    return con, fts


def update_index(root, quiet=False):
    """Bring the index of the folder up to date; returns (connection, full-text search available)."""
    project, _ = project_of(root)
    con, fts = open_index(project)
    known = {p: (s, m) for p, s, m in con.execute('SELECT path, size, mtime FROM files')}
    seen, read, failed, t0 = set(), 0, [], time.time()
    for dirpath, dirs, files in os.walk(root):
        d = pathlib.Path(dirpath)
        dirs[:] = [x for x in dirs if not x.startswith('.') and x not in SKIP_INDEX_DIRS and not (d == project and x == CACHE_FILE[0])]
        for fn in files:
            if fn.startswith('.') or fn.endswith(('.duetkifu-tmp', '.duetkifu-tmp')) or fn.startswith('~$'):
                continue
            p = d / fn
            try:
                st = p.stat()
            except OSError:
                continue
            rel = norm(os.path.relpath(p, project))
            seen.add(rel)
            mtime = st.st_mtime_ns // 1_000_000
            if known.get(rel) == (st.st_size, mtime):
                continue
            try:
                kind, note, parts = read_for_index(p, rel)
            except Exception as err:   # an unreadable file is listed without text, and reported
                kind, note, parts = 'file', f'not read: {type(err).__name__}', []
                failed.append(rel)
            con.execute('DELETE FROM chunks WHERE path=?', (rel,))
            con.executemany('INSERT INTO chunks(path, loc, body) VALUES(?,?,?)', [(rel, loc, body) for loc, body in parts if body])
            con.execute('INSERT OR REPLACE INTO files VALUES(?,?,?,?,?,?)', (rel, p.suffix.lower(), st.st_size, mtime, kind, note))
            read += 1
            if read % 200 == 0:
                con.commit()
    gone = [p for p in known if p not in seen]
    for p in gone:
        con.execute('DELETE FROM files WHERE path=?', (p,))
        con.execute('DELETE FROM chunks WHERE path=?', (p,))
    con.commit()
    if not quiet or read or gone:
        n = con.execute('SELECT COUNT(*) FROM files').fetchone()[0]
        say(f'Index: {n} files, {read} read, {len(gone)} removed ({time.time() - t0:.1f} s) -> {index_file(project)}')
    if failed:
        say('WARNING', f'{len(failed)} file(s) could not be read: {", ".join(failed[:5])}{" ..." if len(failed) > 5 else ""}')
    return con, fts


def search_index(con, fts, query, limit=20):
    """[(path, loc, excerpt)] for the pieces that contain every word of the query."""
    words = [w for w in query.split() if w]
    if not words:
        return []
    if fts and all(len(w) >= 3 for w in words):   # trigram search needs at least 3 characters per word
        q = ' '.join('"' + w.replace('"', '""') + '"' for w in words)
        rows = con.execute("SELECT path, loc, snippet(chunks, 2, '[', ']', '...', 24) FROM chunks WHERE chunks MATCH ? ORDER BY bm25(chunks) LIMIT ?",
                           (q, limit)).fetchall()
        return [(p, l, ' '.join(s.split())) for p, l, s in rows]
    # "+body": LIKE on a full-text table would otherwise be handed to the trigram index, which cannot match words under 3 characters
    sql = 'SELECT path, loc, body FROM chunks WHERE ' + ' AND '.join(['+body LIKE ?'] * len(words)) + ' LIMIT ?'
    out = []
    for p, l, body in con.execute(sql, [f'%{w}%' for w in words] + [limit]).fetchall():
        i = body.lower().find(words[0].lower())
        s = body[max(0, i - 60):i + 100]
        out.append((p, l, ('...' if i > 60 else '') + ' '.join(s.split()).replace(words[0], f'[{words[0]}]') + '...'))
    return out


def run_find(root, query, limit, refresh=True):
    project, _ = project_of(root)
    con, fts = update_index(root, quiet=True) if refresh else open_index(project)
    names = con.execute("SELECT path, kind, note FROM files WHERE path LIKE ? ORDER BY path LIMIT 10", (f'%{query.strip()}%',)).fetchall()
    hits = search_index(con, fts, query, limit)
    if names:
        print('Files named like it:')
        for p, kind, note in names:
            print(f'  {p}  ({kind}{", " + note if note else ""})')
    if hits:
        print(f'Text ({len(hits)}{"+" if len(hits) == limit else ""}):')
        for p, loc, excerpt in hits:
            print(f'  {p}' + (f'  [{loc}]' if loc else ''))
            print(f'      {excerpt[:300]}')
    if not names and not hits:
        print(f'Nothing found for "{query}". (Paths are relative to the folder of report.json.)')
    return 0


def kifu_show(root, mid, refresh=True):
    """One move, its place in the record, its data chain, and excerpts of its source from the index: what an agent
    needs to work on it, in a few thousand characters instead of whole reports."""
    project, _ = project_of(root)
    kifu, err = read_json_file(project / KIFU_FILE)
    if err or not isinstance(kifu, dict):
        say('ERROR', err or f'{KIFU_FILE} is not an object')
        return 1
    moves = kifu.get('moves') if isinstance(kifu.get('moves'), dict) else {}
    m = moves.get(mid)
    if not isinstance(m, dict):
        say('ERROR', f'no move "{mid}" in {KIFU_FILE}')
        return 1
    title = lambda i: f'{i} {(moves.get(i) or {}).get("title", "")}'.strip()
    res = m.get('result') if isinstance(m.get('result'), dict) else {}
    print(f'# {mid} {m.get("title", "")}   (#{m.get("no")}, {m.get("kind")}, {m.get("status")}' + (f', {m.get("outcome")}' if m.get('outcome') else '') + ')')
    trig = m.get('trigger') if isinstance(m.get('trigger'), dict) else {}
    print(f'when: {m.get("when") or m.get("at") or "?"}; started by: {trig.get("kind", "?")}' + (f' ({trig["who"]})' if trig.get('who') else '')
          + (f'; group: {m["group"]}' if m.get('group') else ''))
    if m.get('parent'):
        print(f'follows: {title(m["parent"])}')
    kids = sorted((i for i, o in moves.items() if isinstance(o, dict) and o.get('parent') == mid), key=lambda i: move_no(moves[i]))
    if kids:
        print('next moves: ' + '; '.join(title(i) for i in kids))
    for l in m.get('links') or []:
        if isinstance(l, dict):
            print(f'{l.get("type")} -> {title(l.get("to"))}' + (f' ({l["note"]})' if l.get('note') else ''))
    for i, o in moves.items():
        for l in (o.get('links') or []) if isinstance(o, dict) else []:
            if isinstance(l, dict) and l.get('to') == mid:
                print(f'{l.get("type")} <- {title(i)}')
    for label, v in (('result', res.get('text')), ('why', m.get('why')), ('note', m.get('note')), ('reason', m.get('reason')),
                     ('cause', m.get('cause') and f'{m["cause"]}' + ('' if m.get('causeConfirmed', True) else ' (not confirmed)')),
                     ('population', m.get('population'))):
        if v:
            print(f'{label}: {v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)}')
    ev = m.get('evidence') if isinstance(m.get('evidence'), dict) else {}
    print(f'data chain: {move_chain(m) or "not recorded"}')
    rep, _ = read_json_file(project / 'report.json') if (project / 'report.json').is_file() else (None, None)
    steps = (rep or {}).get('steps') or {}
    for sid in ev.get('steps') or []:
        s = steps.get(sid) or {}
        print(f'  step {sid}: {((s.get("script") or {}).get("path")) or "(no script)"} -> {", ".join(o.get("path", "") for o in s.get("outputs") or [])[:200]}')
    for c in ev.get('checks') or []:
        if isinstance(c, dict):
            print(f'  check: {c.get("claim")}: recorded {c.get("recorded")}, recomputed {c.get("recomputed")} ({c.get("match")})')
    for f in ev.get('files') or []:
        if isinstance(f, dict):
            print(f'  {f.get("role", "file")}: {f.get("path")}' + (f'  - {f["note"][:160]}' if f.get('note') else ''))
    if ev.get('noData'):
        print(f'  no data: {ev["noData"]}')
    src = m.get('source') if isinstance(m.get('source'), dict) else {}
    if src:
        print(f'source: {src.get("title", "")}' + (f' | {src["file"]}' if src.get('file') else '') + (f' #{src["section"]}' if src.get('section') else '')
              + (f' | {src["url"]}' if src.get('url') else ''))
    # excerpts from the index: the source section, then the tables the move points at
    con, fts = update_index(root, quiet=True) if refresh else open_index(project)
    shown = 0
    if src.get('file'):
        path = norm(src['file'])
        rows = con.execute('SELECT loc, body FROM chunks WHERE path=?', (path,)).fetchall()
        sec = str(src.get('section') or '')
        pick = [r for r in rows if sec and re.match(rf'#(sec-)?{re.escape(sec)}\b', r[0] or '')] if sec else []
        if pick:
            print(f'\n## From {path} [{pick[0][0]}]')
            for loc, body in pick[:3]:
                print(body[:900] + ('...' if len(body) > 900 else ''))
                shown += 1
        elif rows:
            print(f'\n({len(rows)} indexed piece(s) of {path}; search them with: duetkifu.py find FOLDER "<words>")')
    tabs = [f for f in ev.get('files') or [] if isinstance(f, dict) and f.get('role') == 'table']
    for f in tabs[:4]:
        r = con.execute("SELECT body FROM chunks WHERE path=? AND loc='columns'", (norm(f.get('path')),)).fetchone()
        if r:
            print(f'\n## Table {f.get("path")}\n{r[0][:600]}')
    return 0


# ---------------------------------------------------------------- the Claude Code skill

def install_skill(target):
    src = HERE / 'skills' / 'duetkifu' / 'SKILL.md'
    if not src.is_file():
        say('ERROR', f'{src} not found')
        return 1
    dest = pathlib.Path(target).expanduser() / 'duetkifu'
    dest.mkdir(parents=True, exist_ok=True)
    text = src.read_text(encoding='utf-8').replace('${CLAUDE_PLUGIN_ROOT}', str(HERE))
    (dest / 'SKILL.md').write_text(text, encoding='utf-8')
    say('Installed', dest / 'SKILL.md')
    say('In Claude Code, type /duetkifu (in a new session) to start.')
    return 0


# ---------------------------------------------------------------- a stable place for shortcuts and pointers

APP_FILES = ('duetkifu.py', 'duetkifu.html', 'AGENTS.md', 'schema/report.schema.json')
STABLE = pathlib.Path.home() / '.duetkifu' / 'app'


def from_plugin_cache():
    parts = [p.lower() for p in HERE.parts]
    return 'plugins' in parts and 'cache' in parts


def stable_app(create):
    """The folder that shortcuts and AGENTS.md pointers should name. A Claude Code plugin lives in a cache folder
    that changes with every update, so from there Duetkifu copies itself to ~/.duetkifu/app and names that."""
    if not from_plugin_cache():
        return HERE
    if create or STABLE.is_dir():
        for f in APP_FILES:
            (STABLE / f).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(HERE / f, STABLE / f)
    return STABLE


# ---------------------------------------------------------------- a pointer for other agents

MARK_START, MARK_END = '<!-- duetkifu:start -->', '<!-- duetkifu:end -->'


def init_agent(root):
    app, (project, _) = stable_app(True), project_of(root)
    py = f'"{sys.executable}"' if ' ' in sys.executable else sys.executable
    block = '\n'.join([
        MARK_START,
        '## Duetkifu: the report and the research record',
        '',
        'This folder is kept with Duetkifu: the report (report.json) and the record of every move of the research, dead ends included'
        ' (kifu.json). The user reads, annotates and edits both in the browser while you write and revise them.',
        '',
        f'- Read the full rules before you change anything: `{app / "AGENTS.md"}`',
        f'- The report: `{project / "report.json"}`. Raw data files are only read, never changed.',
        f'- Raw data stays inside "{root.name}", outside `duetkifu/`. Files computed from it go into `duetkifu/derived_data/`,'
        ' the scripts into `duetkifu/scripts/`; record every script run with `duetkifu.py step`.',
        f'- Check after every write: `{py} "{app / "duetkifu.py"}" check "{root}"`',
        f'- Start it for the user (keep it running in the background): `{py} "{app / "duetkifu.py"}" "{root}"`',
        f'- Then listen for the page\'s "Ask the agent to revise" button (also in the background): `{py} "{app / "duetkifu.py"}" wait "{root}"`;'
        ' see "Answer the Ask the agent to revise button" in AGENTS.md.',
        MARK_END, ''])
    # The same section in AGENTS.md (Codex, Copilot, Cursor and others) and CLAUDE.md (Claude Code). Only the part between
    # the markers is Duetkifu's: running this again replaces it, and everything else in the files is kept as it is.
    # A marker counts only on a line of its own, so text that merely mentions it is never replaced.
    section = re.compile('^' + re.escape(MARK_START) + r'[ \t]*$.*?^' + re.escape(MARK_END) + r'[ \t]*$', re.M | re.S)
    for name in ('AGENTS.md', 'CLAUDE.md'):
        f = root / name
        old = f.read_text(encoding='utf-8-sig') if f.is_file() else ''
        m = section.search(old)
        if m:
            new = old[:m.start()] + block.rstrip('\n') + old[m.end():]
        else:
            new = (old.rstrip('\n') + '\n\n' if old.strip() else '') + block
        f.write_text(new, encoding='utf-8')
        say('Updated' if old else 'Created', f)
    say('Agents that read AGENTS.md or CLAUDE.md (Claude Code, Codex, Copilot, Cursor and others) now know how to use Duetkifu here.')
    return 0


# ---------------------------------------------------------------- a desktop shortcut

def desktop_dir():
    if os.name == 'nt':
        try:  # the real desktop, also when OneDrive has moved it
            import ctypes
            buf = ctypes.create_unicode_buffer(1024)
            if ctypes.windll.shell32.SHGetFolderPathW(None, 0x10, None, 0, buf) == 0 and buf.value:
                return pathlib.Path(buf.value)
        except Exception:
            pass
    d = pathlib.Path.home() / 'Desktop'
    return d if d.is_dir() else pathlib.Path.home()


def shortcut_name(root):
    project, _ = project_of(root)
    title = ''
    try:
        title = json.loads((project / 'report.json').read_text(encoding='utf-8-sig'))['report']['meta']['title']
    except Exception:
        pass
    name = str(title or (root.parent.name if root.name in WORKSPACES else root.name)).strip()
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', ' ', name).strip()[:60] or 'report'
    return 'Duetkifu - ' + name


def make_shortcut(root, target):
    dest = pathlib.Path(target).expanduser() if target else desktop_dir()
    dest.mkdir(parents=True, exist_ok=True)
    name, py, script = shortcut_name(root), sys.executable, stable_app(True) / 'duetkifu.py'
    if os.name == 'nt':
        # The .lnk is made in a temporary folder and then moved: Windows may keep PowerShell from writing to the
        # desktop (controlled folder access) while still letting Python do it. WScript.Shell only creates the file:
        # it stores paths in the ANSI code page, so the paths are set through Shell.Application, which keeps Unicode.
        link = dest / (name + '.lnk')
        tmp = pathlib.Path(tempfile.mkdtemp(prefix='duetkifu-')) / 'shortcut.lnk'
        q = lambda v: "'" + str(v).replace("'", "''") + "'"
        args = f'"{script}" "{root}"'
        ps = (f'$w=(New-Object -ComObject WScript.Shell).CreateShortcut({q(tmp)});$w.TargetPath={q(os.environ.get("COMSPEC", "cmd.exe"))};$w.Save();'
              f'$s=(New-Object -ComObject Shell.Application).NameSpace({q(tmp.parent)}).ParseName({q(tmp.name)}).GetLink;'
              f'$s.Path={q(py)};$s.Arguments={q(args)};$s.WorkingDirectory={q(root)};'
              f'$s.Description={q("Start Duetkifu for " + root.name)};$s.Save()')
        enc = base64.b64encode(ps.encode('utf-16-le')).decode('ascii')
        r = subprocess.run(['powershell', '-NoProfile', '-NonInteractive', '-EncodedCommand', enc], capture_output=True, text=True, errors='replace')
        try:
            if r.returncode or not tmp.is_file():
                raise OSError(re.sub(r'<[^>]+>', ' ', r.stderr or r.stdout).strip()[-400:])
            shutil.move(str(tmp), str(link))
        except OSError as err:
            say('ERROR', 'could not create the shortcut:', err)
            return 1
        finally:
            shutil.rmtree(tmp.parent, ignore_errors=True)
    else:
        link = dest / (name + ('.command' if sys.platform == 'darwin' else '.sh'))
        sh = lambda v: "'" + str(v).replace("'", "'\\''") + "'"
        link.write_text(f'#!/bin/sh\nexec {sh(py)} {sh(script)} {sh(root)}\n', encoding='utf-8')
        link.chmod(0o755)
    say('Created', link)
    say('Double-click it to open the report; keep the window that opens while you use it.')
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description='Duetkifu launcher', formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    ap.add_argument('args', nargs='*', help='[check | step | annotations | kifu check|tree|show | extract | index | find | wait | agent-status | install-skill | shortcut | init-agent] [FOLDER]')
    ap.add_argument('--note', default='', help='step, agent-status: a short note')
    ap.add_argument('--port', type=int, default=0, help='port (default: first free port from 8765)')
    ap.add_argument('--no-browser', action='store_true', help='do not open a browser window')
    ap.add_argument('--limit', type=int, default=20, help='find: how many excerpts to print')
    ap.add_argument('--no-refresh', action='store_true', help='find, kifu show: use the index as it is, without looking for changed files')
    ap.add_argument('--deep', action='store_true', help='check: re-read every file instead of trusting size and date')
    ap.add_argument('--skills-dir', default='~/.claude/skills', help='where install-skill puts the skill')
    ap.add_argument('--to', default='', help='where shortcut puts the shortcut (default: the desktop)')
    st = ap.add_argument_group('step', 'record a computation step (paths and patterns relative to the folder of report.json)')
    st.add_argument('--script', default='', help='the script that was run, for example scripts/make_cells.py')
    st.add_argument('--in', dest='inputs', action='append', default=[], metavar='PATTERN', help='files it read (repeatable; * and ** allowed)')
    st.add_argument('--out', dest='outputs', action='append', default=[], metavar='PATTERN', help='files it wrote (repeatable)')
    st.add_argument('--command', default='', help='the command line that was run')
    st.add_argument('--param', action='append', default=[], metavar='KEY=VALUE', help='a parameter of the run (repeatable)')
    st.add_argument('--id', default='', help='step id (default: s-<script name>; an existing step with this id is replaced)')
    st.add_argument('--by', default='claude', choices=('claude', 'user'), help='who ran it (claude stands for any agent)')
    ap.add_argument('--version', action='version', version=VERSION)
    a = ap.parse_args(argv)
    cmds = ('check', 'step', 'annotations', 'kifu', 'extract', 'index', 'find', 'wait', 'agent-status', 'install-skill', 'shortcut', 'init-agent', 'serve')
    cmd = a.args[0] if a.args and a.args[0] in cmds else 'serve'
    rest = a.args[1:] if a.args and a.args[0] == cmd else a.args
    if cmd == 'install-skill':
        return install_skill(a.skills_dir)
    state = None
    if cmd == 'agent-status':
        if not rest or rest[-1] not in STATES:
            say('ERROR', 'usage: duetkifu.py agent-status FOLDER working|done|failed [--note TEXT]')
            return 1
        rest, state = rest[:-1], rest[-1]
    sub = None
    if cmd == 'kifu':
        if not rest or rest[0] not in ('check', 'tree', 'show') or (rest[0] == 'show' and len(rest) < 3):
            say('ERROR', 'usage: duetkifu.py kifu check|tree FOLDER, or kifu show FOLDER MOVE')
            return 1
        sub, rest = rest[0], rest[1:]
        if sub == 'show':
            rest, move = rest[:1], rest[1]
    if cmd == 'find':
        if len(rest) < 2:
            say('ERROR', 'usage: duetkifu.py find FOLDER "WORDS" [--limit N]')
            return 1
        rest, query = rest[:1], ' '.join(rest[1:])
    if cmd == 'extract':
        if len(rest) < 2:
            say('ERROR', 'usage: duetkifu.py extract FOLDER REPORT.html [REPORT.html ...]')
            return 1
        rest, reports = rest[:1], rest[1:]
    root = pathlib.Path(rest[0] if rest else os.getcwd()).expanduser().resolve()
    if not root.is_dir():
        say('ERROR', f'{root} is not a folder')
        return 1
    if cmd == 'check':
        return run_check(root, a.deep)
    if cmd == 'step':
        return record_step(root, a)
    if cmd == 'annotations':
        return open_annotations(root)
    if cmd == 'kifu':
        return kifu_show(root, move, not a.no_refresh) if sub == 'show' else run_kifu(root, sub, a.deep)
    if cmd == 'index':
        update_index(root)
        return 0
    if cmd == 'find':
        return run_find(root, query, a.limit, not a.no_refresh)
    if cmd == 'extract':
        return max(extract_report(root, r, a.note) for r in reports)
    if cmd == 'wait':
        return wait_for_request(root)
    if cmd == 'agent-status':
        return set_agent_status(root, state, a.note)
    if cmd == 'shortcut':
        return make_shortcut(root, a.to)
    if cmd == 'init-agent':
        return init_agent(root)
    return serve(root, a.port, not a.no_browser)


if __name__ == '__main__':
    sys.exit(main())
