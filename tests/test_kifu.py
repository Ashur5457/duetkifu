"""Tests for the research record (kifu.json, duetkifu/0.1) in duetkifu.py: kifu check, kifu tree, check.
Usage: python tests/test_kifu.py      (from the repository root)"""
import base64, copy, json, os, pathlib, shutil, subprocess, sys, tempfile, time, zipfile

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.dont_write_bytecode = True
import duetkifu as D  # noqa: E402

PY = sys.executable
fails = []


def ok(cond, what):
    print(('  ok   ' if cond else '  FAIL ') + what)
    if not cond:
        fails.append(what)


def run(*args):
    r = subprocess.run([PY, str(REPO / 'duetkifu.py'), *args], capture_output=True, text=True, encoding='utf-8')
    return r.returncode, r.stdout + r.stderr


def move(i, no, parent, **kw):
    m = {'id': i, 'no': no, 'parent': parent, 'title': f'move {i}', 'kind': 'attempt', 'status': 'done', 'outcome': 'success',
         'trigger': {'kind': 'self'}, 'when': '9/01', 'at': '2026-09-01', 'result': {'text': 'r'}}
    m.update(kw)
    return m


GOOD = {'schema': 'duetkifu/0.1',
        'kifu': {'meta': {'question': 'Why?', 'metric': {'name': 'rho', 'better': 'higher'}}},
        'moves': {'R': move('R', 1, None, kind='question'),
                  'A': move('A', 2, 'R', evidence={'steps': ['s-sum'], 'checks': [
                      {'claim': 'n', 'recorded': 10, 'recomputed': 10, 'match': 'yes', 'step': 's-sum',
                       'source': {'path': 'derived_data/sum.csv', 'keys': ['n']}},
                      {'claim': 'mean', 'recorded': 0.5, 'recomputed': 0.7, 'match': 'no', 'step': 's-sum'}]},
                      source={'title': 'report', 'file': '../notes.html'}),
                  'B': move('B', 3, 'R', outcome='failure', cause='measurement', causeConfirmed=False, reason='noisy'),
                  'C': move('C', 4, 'A', kind='correction', links=[{'type': 'corrects', 'to': 'A'}],
                            milestone={'kind': 'root-cause', 'reason': 'x'}, mark={'kind': 'interesting', 'note': 'y'}),
                  'P': move('P', 5, 'C', status='planned', outcome=None, at=None, trigger={'kind': 'plan'})}}
for m in GOOD['moves'].values():
    for k in [k for k, v in m.items() if v is None and k != 'parent']:
        del m[k]


def make_project(tmp):
    root = tmp / 'exp'
    proj = root / 'duetkifu'
    (proj / 'derived_data').mkdir(parents=True)
    (root / 'raw.csv').write_text('x\n1\n', encoding='utf-8')
    (root / 'notes.html').write_text('<p>notes</p>', encoding='utf-8')
    (proj / 'derived_data' / 'sum.csv').write_text('n\n10\n', encoding='utf-8')
    fp = D.Fingerprints(proj)
    rep = {'schema': D.SCHEMA, 'report': {'meta': {'title': 't', 'order': []}}, 'blocks': {},
           'steps': {'s-sum': {'id': 's-sum', 'script': None, 'inputs': [fp.ref('../raw.csv')], 'outputs': [fp.ref('derived_data/sum.csv')],
                               'at': D.iso_ms(time.time() * 1000), 'by': 'claude'}}}
    (proj / 'report.json').write_text(json.dumps(rep, ensure_ascii=False), encoding='utf-8')
    return root, proj


def write(proj, kifu):
    (proj / D.KIFU_FILE).write_text(json.dumps(kifu, ensure_ascii=False, indent=1), encoding='utf-8')


def check(root, proj, kifu, stale=None):
    write(proj, kifu)
    rep = json.loads((proj / 'report.json').read_text(encoding='utf-8'))
    return D.check_kifu(proj, root, rep, stale)


def has(msgs, text):
    return any(text in m for m in msgs)


def main():
    tmp = pathlib.Path(tempfile.mkdtemp(prefix='kifu-test-'))
    try:
        root, proj = make_project(tmp)

        print('a good record')
        e, w, n = check(root, proj, GOOD)
        ok(e == [] and w == [], f'no errors or warnings: {e} {w}')
        ok(has(n, '5 moves (1 planned, 4 done; 3 success, 1 failure)'), 'counts statuses and outcomes')
        ok(has(n, '1 yes, 0 close, 1 no, 0 not-reproducible'), 'counts checks')
        ok(has(n, 'A: mean: recorded 0.5, recomputed 0.7 (no)'), 'lists the check that does not match')
        ok(has(n, 'not confirmed yet: B (measurement)'), 'lists unconfirmed causes')

        def bad(label, change, expect, where='e'):
            k = copy.deepcopy(GOOD)
            change(k)
            e, w, n = check(root, proj, k)
            ok(has({'e': e, 'w': w, 'n': n}[where], expect), f'{label} -> {expect!r}')

        print('errors')
        bad('wrong schema', lambda k: k.update(schema='duetkifu/9'), '"schema" must be "duetkifu/0.1"')
        bad('unknown parent', lambda k: k['moves']['A'].update(parent='Z'), 'moves.A.parent "Z" is not another move')
        bad('parent loop', lambda k: (k['moves']['R'].update(parent='C')), 'the parents form a loop')
        bad('id differs from key', lambda k: k['moves']['A'].update(id='AA'), 'moves.A.id must be "A"')
        bad('no number', lambda k: k['moves']['A'].pop('no'), 'moves.A.no must be a whole number')
        bad('unknown kind', lambda k: k['moves']['A'].update(kind='guess'), 'moves.A.kind must be question')
        bad('unknown status', lambda k: k['moves']['A'].update(status='stopped'), 'moves.A.status must be planned')
        bad('done without outcome', lambda k: k['moves']['A'].pop('outcome'), 'moves.A: a done move needs an outcome')
        bad('failure without reason', lambda k: k['moves']['B'].pop('reason'), 'moves.B: say why in "reason"')
        bad('paused without reason', lambda k: k['moves']['P'].update(status='paused'), 'moves.P: say why in "reason"')
        bad('unknown cause', lambda k: k['moves']['B'].update(cause='bad luck'), 'moves.B.cause must be idea')
        bad('unknown trigger', lambda k: k['moves']['A'].update(trigger={'kind': 'boss'}), 'moves.A.trigger.kind must be user')
        bad('loose date in at', lambda k: k['moves']['A'].update(at='late August'), 'moves.A.at must be an ISO date')
        bad('words in result.value', lambda k: k['moves']['A'].update(result={'text': 't', 'value': 'high'}), 'result.value must be a number')
        bad('mark as a string', lambda k: k['moves']['C'].update(mark='good'), 'moves.C.mark must be {"kind", "note"}')
        bad('unknown mark', lambda k: k['moves']['C'].update(mark={'kind': 'brilliant'}), 'moves.C.mark.kind must be good')
        bad('unknown link type', lambda k: k['moves']['C'].update(links=[{'type': 'fixes', 'to': 'A'}]), 'links[0].type must be corrects')
        bad('link to a missing move', lambda k: k['moves']['C'].update(links=[{'type': 'corrects', 'to': 'Q'}]), 'links[0].to "Q" is not another move')
        bad('link to itself', lambda k: k['moves']['C'].update(links=[{'type': 'clue', 'to': 'C'}]), 'links[0].to "C" is not another move')
        bad('evidence step not in report', lambda k: k['moves']['A']['evidence'].update(steps=['s-none']), 'evidence.steps: "s-none" is not a step')
        bad('unknown match', lambda k: k['moves']['A']['evidence']['checks'][0].update(match='maybe'), 'checks[0].match must be yes')
        bad('check without claim', lambda k: k['moves']['A']['evidence']['checks'][0].pop('claim'), 'checks[0].claim is missing')
        bad('source outside the folder', lambda k: k['moves']['A']['source'].update(file='../../elsewhere.html'), 'is outside the folder "exp"')
        bad('check source outside', lambda k: k['moves']['A']['evidence']['checks'][0]['source'].update(path='C:/x.csv'), 'is outside the folder')

        print('warnings and notes')
        bad('duplicate number', lambda k: k['moves']['B'].update(no=2), 'is also the number of move', 'w')
        bad('outcome before done', lambda k: k['moves']['P'].update(outcome='success'), 'moves.P.outcome is set but the move is planned', 'w')
        bad('failure without cause', lambda k: k['moves']['B'].pop('cause'), 'moves.B: no "cause"', 'w')
        bad('cause on a success', lambda k: k['moves']['A'].update(cause='idea'), 'moves.A.cause is only used when', 'w')
        bad('no trigger', lambda k: k['moves']['A'].pop('trigger'), 'moves.A.trigger is missing', 'w')
        bad('second root', lambda k: k['moves']['B'].update(parent=None), 'moves.B has no parent', 'w')
        bad('new milestone kind', lambda k: k['moves']['C'].update(milestone={'kind': 'eureka'}), 'milestone.kind "eureka" is not one of', 'w')
        bad('check step not in evidence', lambda k: k['moves']['A']['evidence']['checks'][0].update(step='s-other'), 'checks[0].step "s-other" is not in', 'w')
        bad('check source missing', lambda k: k['moves']['A']['evidence']['checks'][0]['source'].update(path='derived_data/none.csv'), 'does not exist', 'w')
        bad('no question', lambda k: k['kifu']['meta'].pop('question'), 'kifu.meta.question is missing', 'w')
        bad('source file missing', lambda k: k['moves']['A']['source'].update(file='../gone.html'), '1 source file(s) named by moves are not in the folder: ../gone.html', 'n')
        write(proj, GOOD)
        e, w, n = D.check_kifu(proj, root, json.loads((proj / 'report.json').read_text(encoding='utf-8')), {'s-sum': ['input changed']})
        ok(has(w, 'step "s-sum" needs rerunning, so the evidence of move(s) A may be out of date'), 'evidence from a stale step is flagged')

        rep = json.loads((proj / 'report.json').read_text(encoding='utf-8'))
        rep['blocks'] = {'b1': {'id': 'b1', 'type': 'text', 'title': 'Chapter', 'move': 'A'}, 'b2': {'id': 'b2', 'type': 'text', 'title': 'Other', 'move': 'Q'}}
        e, w, n = D.check_kifu(proj, root, rep)
        ok(has(w, 'blocks.b2.move "Q" is not a move') and not has(w, 'blocks.b1'), 'a chapter pointing at a missing move is flagged')

        print('data chain of the moves')
        fp = D.Fingerprints(proj)
        k = copy.deepcopy(GOOD)
        k['kifu']['meta']['groups'] = {'A': 'Branch A'}
        k['moves']['B']['evidence'] = {'files': [{**fp.ref('../notes.html'), 'role': 'report', 'note': 'section 2'}]}
        k['moves']['P']['evidence'] = {'noData': 'not done yet'}
        e, w, n = check(root, proj, k)
        ok(e == [] and w == [], f'files, noData and groups are accepted: {e} {w}')
        ok(has(n, 'Data chain of the moves: 1 recomputed, 1 sourced, 1 none, 2 not recorded'), 'counts the chain level of each move')
        ok(has(n, 'Moves without a data chain yet') and has(n, 'R, C'), 'lists the moves without a chain')
        (root / 'notes.html').write_text('<p>changed</p>', encoding='utf-8')
        e, w, n = check(root, proj, k)
        ok(has(w, '1 evidence file(s) changed since they were recorded: B: ../notes.html'), 'a changed evidence file is flagged')
        (root / 'notes.html').unlink()
        e, w, n = check(root, proj, k)
        ok(has(w, '1 evidence file(s) are missing: B: ../notes.html'), 'a missing evidence file is flagged')
        (root / 'notes.html').write_text('<p>notes</p>', encoding='utf-8')
        bad('unknown file role', lambda k: k['moves']['A']['evidence'].update(files=[{'path': '../notes.html', 'role': 'paper'}]), 'files[0].role must be report')
        bad('file outside', lambda k: k['moves']['A']['evidence'].update(files=[{'path': '../../x.csv'}]), 'is outside the folder')
        bad('file without sha256', lambda k: k['moves']['A']['evidence'].update(files=[{'path': '../notes.html'}]), 'has no sha256', 'w')
        bad('noData not text', lambda k: k['moves']['A']['evidence'].update(noData=True), 'noData must say, in words')
        bad('groups not text', lambda k: k['kifu']['meta'].update(groups={'A': 1}), 'kifu.meta.groups must be')
        bad('change without who', lambda k: k.update(changes={'k1': {'move': 'A', 'field': 'title'}}), 'changes.k1 must be')
        bad('change log counted', lambda k: k.update(changes={'k1': {'by': 'user', 'move': 'A', 'field': 'title', 'before': 'a', 'after': 'b'}}), '1 recorded change(s) to the record (1 by the user)', 'n')

        print('tree order')
        nodes, problems = D.kifu_moves(GOOD)
        by = {x['id']: x for x in nodes}
        ok([k['id'] for k in by['R']['kids']] == ['A', 'B'], 'children ordered by move number')
        ok(by['A']['depth'] == 0 and by['B']['depth'] == 1, 'the first child continues the line, the others branch off')

        print('command line')
        write(proj, GOOD)
        code, out = run('kifu', 'check', str(root))
        ok(code == 0 and 'OK' in out, 'kifu check passes a good record')
        code, out = run('kifu', 'tree', str(root))
        lines = [l for l in out.splitlines() if l.strip()]
        ok(code == 0 and lines[0].startswith('Why?') and 'R [done ✓]' in lines[1] and '  A [done ✓]' in lines[2], 'kifu tree prints the question and the moves')
        ok(any('B [done ✗]' in l and 'cause: measurement, not confirmed' in l for l in lines), 'kifu tree shows failures with their cause')
        ok(any('C [done ✓]' in l and 'corrects A' in l for l in lines), 'kifu tree shows corrections')
        order = [l.split()[1] for l in lines[1:]]
        ok(order == ['R', 'A', 'C', 'P', 'B'], f'kifu tree is depth first: {order}')
        ok(any('A [done ✓]' in l and 'data: recomputed' in l for l in lines), 'kifu tree shows the chain level')
        code, out = run('check', str(root))
        ok(code == 0 and 'kifu.json: 5 moves' in out, 'check also checks kifu.json')
        k = copy.deepcopy(GOOD)
        k['moves']['A']['parent'] = 'Z'
        write(proj, k)
        code, out = run('check', str(root))
        ok(code == 1 and 'moves.A.parent "Z"' in out, 'check fails on errors in kifu.json')
        code, out = run('kifu', 'check', str(root))
        ok(code == 1, 'kifu check fails on errors')
        (proj / D.KIFU_FILE).write_text('{"schema": "duetkifu/0.1", "moves": {NaN}}', encoding='utf-8')
        code, out = run('kifu', 'check', str(root))
        ok(code == 1 and 'kifu.json line 1' in out, 'broken JSON is reported with its position')
        (proj / D.KIFU_FILE).unlink()
        code, out = run('kifu', 'check', str(root))
        ok(code == 1 and 'no kifu.json' in out, 'kifu check without kifu.json')
        code, out = run('check', str(root))
        ok(code == 0 and 'kifu.json' not in out, 'check without kifu.json is unchanged')
        code, out = run('kifu', str(root))
        ok(code == 1 and 'usage' in out, 'kifu without check or tree prints the usage')

        print('extract figures and tables')
        png = base64.b64encode(b'\x89PNG\r\n\x1a\n' + b'0' * 2000).decode()
        icon = base64.b64encode(b'\x89PNG\r\n\x1a\n' + b'0' * 100).decode()
        (root / 'report.html').write_text(f'''<html><body><script>var x = "<table><tr><td>not a table</td></tr></table>";</script>
<section id="values"><h2>02.1 Overview</h2>
<figure><img src="data:image/png;base64,{png}" alt="alt text"><figcaption>Figure 1. Predicted vs measured</figcaption></figure>
<img src="data:image/png;base64,{icon}">
<h3>02.2 Table</h3><table><caption>Round summary</caption><tr><th>round</th><th colspan="2">MAE</th></tr>
<tr><td>R1</td><td>0.19</td><td>0.2</td></tr></table>
<table id="filled-by-script"></table></section>
<h2 id="sec-next">Next</h2><img src="data:image/png;base64,{png}"><p>Text right after the image serves as its caption.</p>
</body></html>''', encoding='utf-8')
        code, out = run('extract', str(root), str(root / 'report.html'))
        base = proj / 'derived_data' / 'report_figures' / 'report'
        idx = json.loads((base / 'index.json').read_text(encoding='utf-8')) if (base / 'index.json').is_file() else {'items': []}
        items = {i['file']: i for i in idx['items']}
        ok(code == 0 and sorted(items) == ['figure-01.png', 'figure-02.png', 'table-01.csv'], f'two figures and one table, the icon and the empty table left out: {sorted(items)}')
        f1 = items.get('figure-01.png', {})
        ok(f1.get('section') == 'values' and f1.get('heading') == '02.1 Overview' and f1.get('caption') == 'Figure 1. Predicted vs measured', f'section, heading and figcaption: {f1}')
        f2 = items.get('figure-02.png', {})
        ok(f2.get('section') == 'sec-next' and f2.get('caption', '').startswith('Text right after the image'), f'text after an image is its caption: {f2}')
        t1 = items.get('table-01.csv', {})
        rows = (base / 'table-01.csv').read_text(encoding='utf-8').splitlines() if (base / 'table-01.csv').is_file() else []
        ok(t1.get('caption') == 'Round summary' and rows == ['round,MAE,', 'R1,0.19,0.2'], f'table with caption and colspan: {t1} {rows}')
        ok((base / 'figure-01.png').read_bytes()[:4] == b'\x89PNG', 'the figure is written as a file')
        rep = json.loads((proj / 'report.json').read_text(encoding='utf-8'))
        st = rep['steps'].get('s-extract-report', {})
        ok(st.get('inputs', [{}])[0].get('path') == '../report.html' and len(st.get('outputs', [])) == 4, 'the extraction is recorded as a step')
        (root / 'report.html').write_text('<html><body><p>nothing here</p></body></html>', encoding='utf-8')
        code, out = run('extract', str(root), str(root / 'report.html'))
        rep = json.loads((proj / 'report.json').read_text(encoding='utf-8'))
        ok(code == 0 and not base.exists() and 's-extract-report' not in rep['steps'], 'a report with nothing to extract leaves no folder and drops the old step')
        with zipfile.ZipFile(root / 'deck.pptx', 'w') as z:
            A, R = 'http://schemas.openxmlformats.org/drawingml/2006/main', 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'
            z.writestr('ppt/slides/slide1.xml', f'<p:sld xmlns:p="x" xmlns:a="{A}" xmlns:r="{R}"><a:p><a:r><a:t>Root cause</a:t></a:r></a:p>'
                       f'<a:p><a:r><a:t>Same cell</a:t></a:r></a:p><a:blip r:embed="rId2"/>'
                       f'<a:tbl><a:tr><a:tc><a:p><a:r><a:t>round</a:t></a:r></a:p></a:tc></a:tr><a:tr><a:tc><a:p><a:r><a:t>R1</a:t></a:r></a:p></a:tc></a:tr></a:tbl></p:sld>')
            z.writestr('ppt/slides/_rels/slide1.xml.rels', '<Relationships xmlns="x"><Relationship Id="rId2" Target="../media/image1.png"/></Relationships>')
            z.writestr('ppt/media/image1.png', b'\x89PNG\r\n\x1a\n' + b'0' * 2000)
        code, out = run('extract', str(root), str(root / 'deck.pptx'))
        idx = json.loads((proj / 'derived_data' / 'report_figures' / 'deck' / 'index.json').read_text(encoding='utf-8'))
        ok(code == 0 and [(i['file'], i['section'], i['heading'], i['caption']) for i in idx['items']] ==
           [('figure-01.png', 'slide-1', 'Root cause', 'Root cause Same cell round R1'), ('table-01.csv', 'slide-1', 'Root cause', 'Root cause Same cell round R1')],
           f'slides: picture and table with the slide heading: {idx["items"]}')

        print('search index')
        env = {**os.environ, 'DUETKIFU_INDEX_DIR': str(tmp / 'index')}
        irun = lambda *a: (lambda r: (r.returncode, r.stdout + r.stderr))(subprocess.run([PY, str(REPO / 'duetkifu.py'), *a], capture_output=True, text=True, encoding='utf-8', env=env))
        (root / 'notes.md').write_text('# \u7bc4\u4f8b\u6a19\u984c\n\u7bc4\u4f8b\u7684 pH \u70ba 7\u3002\n# Other\nnothing\n', encoding='utf-8')
        (root / 'report.html').write_text('<html><body><section id="corr"><h2>\u7bc4\u4f8b\u7ae0\u7bc0</h2><p>\u7bc4\u4f8b\u6bb5\u843d\u7684\u7b2c\u4e00\u53e5\u8a71</p>'
                                          '<script>var hidden = "\u4e0d\u8a72\u88ab\u7d22\u5f15";</script></section></body></html>', encoding='utf-8')
        W = 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'
        with zipfile.ZipFile(root / 'memo.docx', 'w') as z:
            z.writestr('word/document.xml', f'<w:document xmlns:w="{W}"><w:body><w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:t>\u7d50\u8ad6</w:t></w:r></w:p>'
                       f'<w:p><w:r><w:t>\u7bc4\u4f8b\u7d50\u8ad6\u7684\u5167\u6587</w:t></w:r></w:p></w:body></w:document>')
        S_ = 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'
        with zipfile.ZipFile(root / 'cells.xlsx', 'w') as z:
            z.writestr('xl/workbook.xml', f'<workbook xmlns="{S_}" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
                       '<sheets><sheet name="Ch001_Raw" sheetId="1" r:id="rId1"/></sheets></workbook>')
            z.writestr('xl/_rels/workbook.xml.rels', '<Relationships xmlns="x"><Relationship Id="rId1" Target="worksheets/sheet1.xml"/></Relationships>')
            z.writestr('xl/worksheets/sheet1.xml', f'<worksheet xmlns="{S_}"><dimension ref="A1:C5000"/><sheetData><row r="1">'
                       '<c r="A1" t="s"><v>0</v></c><c r="B1" t="s"><v>1</v></c><c r="C1" t="inlineStr"><is><t>\u7dcf\uff7b\uff72\uff78\uff99</t></is></c></row>'
                       '<row r="2"><c r="A2"><v>1</v></c></row></sheetData></worksheet>')
            z.writestr('xl/sharedStrings.xml', f'<sst xmlns="{S_}"><si><t>\u96fb\u5727[V]</t></si><si><t>\u96fb\u6d41[mA]</t></si></sst>')
        code, out = irun('index', str(root))
        ok(code == 0 and 'Index:' in out and 'read' in out, f'index builds: {out.strip()[:200]}')
        dbs = list((tmp / 'index').glob('*.sqlite'))
        ok(len(dbs) == 1, 'the index lives outside the project folder')
        code, out = irun('find', str(root), '\u7bc4\u4f8b\u6a19\u984c')
        ok('../notes.md' in out and '\u7bc4\u4f8b\u6a19\u984c' in out, f'find: markdown by heading: {out[:200]}')
        code, out = irun('find', str(root), '\u7bc4\u4f8b\u6bb5\u843d')
        ok('../report.html' in out and '#corr' in out, f'find: html with its section id: {out[:200]}')
        code, out = irun('find', str(root), '\u4e0d\u8a72\u88ab\u7d22\u5f15')
        ok('Nothing found' in out, 'script text in html is not indexed')
        code, out = irun('find', str(root), '\u7bc4\u4f8b\u7d50\u8ad6')
        ok('../memo.docx' in out and '[\u7d50\u8ad6]' in out, f'find: docx under its heading: {out[:300]}')
        code, out = irun('find', str(root), '\u96fb\u6d41[mA]')
        ok('../cells.xlsx' in out and 'Ch001_Raw' in out, f'find: xlsx sheet and headers: {out[:300]}')
        code, out = irun('find', str(root), 'Same cell')
        ok('../deck.pptx' in out and 'slide 1' in out, f'find: slides: {out[:300]}')
        code, out = irun('find', str(root), 'pH')
        ok('../notes.md' in out, 'find: words under 3 characters still work')
        code, out = irun('find', str(root), 'cells.xlsx')
        ok('Files named like it' in out and '1 sheet(s)' in out, f'find: file names: {out[:200]}')
        code, out = irun('index', str(root))
        ok('0 read, 0 removed' in out, f'an unchanged folder is not read again: {out.strip()}')
        (root / 'notes.md').write_text('# \u7bc4\u4f8b\u6a19\u984c\n\u6539\u904e\u4e86\n', encoding='utf-8')
        (root / 'memo.docx').unlink()
        code, out = irun('index', str(root))
        ok('1 read, 1 removed' in out, f'only the changed file is read, a removed file leaves the index: {out.strip()}')
        write(proj, GOOD)
        k = copy.deepcopy(GOOD)
        k['moves']['A']['source'] = {'title': 'report', 'file': '../report.html', 'section': 'corr'}
        write(proj, k)
        code, out = irun('kifu', 'show', str(root), 'A')
        ok(code == 0 and '# A move A' in out and 'follows: R' in out and 'data chain: recomputed' in out and '\u7bc4\u4f8b\u6bb5\u843d\u7684\u7b2c\u4e00\u53e5' in out,
           f'kifu show: the move, its chain and its source section: {out[:400]}')
        code, out = irun('kifu', 'show', str(root), 'nope')
        ok(code == 1 and 'no move "nope"' in out, 'kifu show of an unknown move')

        print('workspace folder: duetkifu/, and duetsheet/ from before the rename')
        w = tmp / 'ws'
        w.mkdir()
        ok(D.project_of(w)[0] == w / 'duetkifu', 'a new raw data folder gets duetkifu/')
        (w / 'duetsheet').mkdir()
        ok(D.project_of(w)[0] == w / 'duetsheet', 'an existing duetsheet/ is opened as it is')
        (w / 'duetkifu').mkdir()
        ok(D.project_of(w)[0] == w / 'duetkifu', 'with both, duetkifu/ comes first')
        ok({'duetkifu', 'duetsheet'} <= D.SKIP_DIRS, 'neither workspace is listed as raw data')
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print(f'\n{len(fails)} failure(s)' if fails else '\nall passed')
    return 1 if fails else 0


if __name__ == '__main__':
    sys.exit(main())
