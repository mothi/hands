#!/usr/bin/env python3
# seat stub: claude or codex by argv[0]. claude is line-driven like the real stdin wire (one message line in -> one
# turn out); codex is an app-server (codex_server(): JSON-RPC on stdin/stdout, steerable turns). Seams (P = CL for
# claude, CX for codex): P_STUB_MODE, P_ARGV_LOG (argv, one word per line),
# P_STDIN_LOG (the message lines -- codex: every client JSON-RPC line -- appended), P_CWD_LOG (the launch cwd, appended),
# P_STUB_CALLS (launch counter), P_STUB_REPLAY (a recorded stream, split into turns), P_STUB_PIDFILE, P_STUB_SLEEP (slow
# mode's seconds, 2); codex: CX_REPLAY (a recorded app-server wire, {t,dir,msg} lines: the launch count names its process,
# each opening turn/start plays its recorded turn), CX_FAIL_TEXT (the provider error text its failures carry),
# CX_ASK_DELAY (seconds each turn/start waits before its answer; a request already waiting then appends a line to
# CX_OVERLAP_LOG);
import json, os, signal, subprocess, sys, time
prov = os.path.basename(sys.argv[0])
P = {'claude': 'CL', 'codex': 'CX'}[prov]
env = lambda k, d=None: os.environ.get(P + '_' + k) or d
mode = env('STUB_MODE', 'ok')
if env('ARGV_LOG'):
    open(env('ARGV_LOG'), 'w').write(''.join(a + '\n' for a in sys.argv[1:]))
launch = 1
if env('STUB_CALLS'):
    f = env('STUB_CALLS')
    n = int(open(f).read().strip() or 0) if os.path.exists(f) else 0
    open(f, 'w').write('%d\n' % (n + 1))
    launch = n + 1
if env('CWD_LOG'):
    open(env('CWD_LOG'), 'a').write(os.getcwd() + '\n')


def out(o):
    sys.stdout.write((o if isinstance(o, str) else json.dumps(o)) + '\n')
    sys.stdout.flush()


def park():
    open(env('STUB_PIDFILE'), 'w').write(str(os.getpid()))
    time.sleep(60)


def recorded(path, n):   # CX_REPLAY: process n of a recorded app-server wire -> [the server notifications of each turn a
    # turn/start OPENED (its answer names a turn not seen before), from that request to the next such request]. The probe's
    # other client lines (a wrong-id steer, a mid-turn start, reads) and every answer are dropped: this stub answers itself
    rows, procs = [json.loads(ln) for ln in open(path)], []
    for r in rows:
        if r['dir'] == 'out' and r['msg'].get('method') == 'initialize':
            procs.append([])
        procs[-1].append(r)
    rows = procs[min(n, len(procs)) - 1]
    sent, seen, opens = {r['msg'].get('id'): r['msg'].get('method') for r in rows if r['dir'] == 'out'}, set(), set()
    th = next(r['msg']['result']['thread']['id'] for r in rows if r['dir'] == 'in' and 'thread' in (r['msg'].get('result') or {}))
    for r in rows:
        tid = ((r['msg'].get('result') or {}).get('turn') or {}).get('id') if r['dir'] == 'in' else None
        if tid and sent.get(r['msg'].get('id')) == 'turn/start' and tid not in seen:
            seen.add(tid)
            opens.add(r['msg']['id'])
    turns = []
    for r in rows:
        if r['dir'] == 'out' and r['msg'].get('id') in opens and r['msg'].get('method') == 'turn/start':
            turns.append([])
        elif r['dir'] == 'in' and 'method' in r['msg'] and 'id' not in r['msg'] and turns:
            turns[-1].append(r['msg'])
    return th, turns


def codex_server():   # the app-server (codex-cli 0.160 v2): one thread, th_stub123 (a resume
    # answers the id it names); turn/start opens a turn or steers the open one (stale: the open turn ends first, so it
    # opens the next, and the command turn 1 left running completes in it, with its usage); turn/interrupt ends it interrupted and leaves its command running; stdin EOF reaps the command and exits 0;
    # a TERM kills (parkexit: exits 0, as codex 0.160's exec did). Each turn reports two model calls' usage (`last`) that
    # sum to in 10, cached 4, out 3, reasoning 1, under a THREAD total that keeps growing. subagent / latesub / nomain: a
    # collaborator reports on its own thread over this connection, before the main answer / after it / and the main
    # turn/completed never comes (the cases of codex-plugin-cc's fake app-server); badline: a malformed stdout line
    import select
    fail, st = env('FAIL_TEXT', 'stream error'), {'th': 'th_stub123', 'cur': None, 'n': 0, 'kids': [], 'total': {}, 'more': False}
    st['th'], turns = recorded(env('REPLAY'), launch) if env('REPLAY') else (st['th'], None)

    def note(m, **p):
        out({'method': m, 'params': p})

    def ans(i, r):
        out({'id': i, 'result': r})

    def err(i, code, text):
        out({'id': i, 'error': {'code': code, 'message': text}})

    def item(it, done=True):
        note('item/completed' if done else 'item/started', threadId=st['th'], turnId=st['cur']['id'], item=it)

    def end(status='completed', error=None):
        note('turn/completed', threadId=st['th'], turn={'id': st['cur']['id'], 'items': [], 'status': status, 'error': error})
        st['last'], st['cur'] = st['cur']['id'], None

    msg = lambda text, i='msg_9': {'type': 'agentMessage', 'id': i, 'text': text, 'phase': 'final_answer'}
    cmd = lambda i='exec-5', code=0: {'type': 'commandExecution', 'id': i, 'command': 'ls', 'status': 'completed' if code == 0 else 'failed',
                                      'exitCode': code, 'aggregatedOutput': '' if code == 0 else 'ls: x: No such file'}
    edit = lambda i, *paths: {'type': 'fileChange', 'id': i, 'status': 'completed',
                              'changes': [{'path': p, 'kind': {'type': 'add'}, 'diff': '+x'} for p in paths]}

    def collab(final_first):   # a collaborator codex spawned: its own thread starts a turn, thinks, runs a command,
        # answers, reports usage and completes over this connection, while the main turn waits on it
        sub, stu = 'th_sub7', 'turn-sub-%d' % st['n']
        call = lambda status: {'type': 'collabAgentToolCall', 'id': 'collab-%d' % st['n'], 'tool': 'wait', 'status': status,
                               'senderThreadId': st['th'], 'receiverThreadIds': [sub]}
        on = lambda m, **p: note(m, threadId=sub, **p)
        note('thread/started', thread={'id': sub, 'agentNickname': 'challenger'})
        item(call('inProgress'), False)
        if final_first:
            item(msg('FINAL: the main answer'))
        on('turn/started', turn={'id': stu, 'status': 'inProgress', 'items': []})
        on('item/completed', turnId=stu, item={'type': 'reasoning', 'id': 'rs-sub', 'summary': ['SUB: weighing it'], 'content': []})
        on('item/completed', turnId=stu, item=cmd('exec-sub'))
        on('item/completed', turnId=stu, item=dict(msg('SUB: the analysis', 'msg-sub'), phase='analysis'))
        on('thread/tokenUsage/updated', turnId=stu, tokenUsage={'total': {'inputTokens': 500}, 'last': {'inputTokens': 500, 'outputTokens': 50}})
        on('turn/completed', turn={'id': stu, 'items': [], 'status': 'completed', 'error': None})
        item(call('completed'))
        if not final_first:
            item(msg('FINAL: the main answer'))

    def report(last):   # one model call's usage, under the growing thread total
        st['total'] = {k: st['total'].get(k, 0) + v for k, v in last.items()}
        note('thread/tokenUsage/updated', threadId=st['th'], turnId=st['cur']['id'], tokenUsage={'total': st['total'], 'last': last})

    def complete():   # the open turn's answer, by mode, then its usage and its end
        said = ' | '.join(st['cur']['inputs'])
        lasts = [{'inputTokens': 6, 'cachedInputTokens': 2, 'outputTokens': 2, 'reasoningOutputTokens': 1, 'totalTokens': 8},
                 {'inputTokens': 4, 'cachedInputTokens': 2, 'outputTokens': 1, 'reasoningOutputTokens': 0, 'totalTokens': 5}]
        if mode in ('subagent', 'latesub', 'nomain'):   # the call that spawned the collaborator reports before it works
            report(lasts.pop(0))
            collab(mode == 'latesub')
        if mode == 'badline':
            out('{"method": "item/completed", "params": {"threadId"')
        if mode == 'noticedeath':   # the stream ends unmarked after the notice
            note('error', threadId=st['th'], turnId=st['cur']['id'], willRetry=False, error={'message': fail})
            sys.exit(1)
        if mode == 'stale':   # a command still running when the turn completes
            item(dict(cmd('exec-late-%d' % st['n']), status='inProgress', exitCode=None), False)
        if mode in ('notice', 'quota'):   # a retry notice first
            note('error', threadId=st['th'], turnId=st['cur']['id'], willRetry=True, error={'message': 'Reconnecting... 1/5'})
        body = {'quota': [], 'quoted': [msg('The log I read says: ' + fail)], 'quotedfail': [msg('The log I read says: ' + fail)],
                'nofinal': [msg('{"status":"ok","summary":"voided"}', 'msg_1'), cmd()],      # a tool after the only message voids it
                'files': [edit('fc-7', '/w/file-1.txt'), cmd('exec-8', 2), edit('fc-11', '/w/file-2.txt', '/w/file-1.txt'), msg('FINAL-3: the audit')],
                'slow': [msg('turn %d pid %d: %s' % (st['n'], os.getpid(), said))], 'stale': [msg('turn %d: %s' % (st['n'], said))],
                'subagent': [], 'latesub': [], 'nomain': [], 'unauthorized': []}.get(mode, [msg('stub ok')])
        for it in body:
            if it['type'] != 'agentMessage':
                item(dict(it, status='inProgress', exitCode=None), False)
            item(it)
        for last in lasts:
            report(last)
        if mode == 'quota':
            end('failed', {'message': fail, 'codexErrorInfo': 'usageLimitExceeded'})
        elif mode == 'quotedfail':
            end('failed', {'message': 'stream disconnected', 'codexErrorInfo': {'responseStreamDisconnected': {'httpStatusCode': None}}})
        elif mode == 'unauthorized':   # the typed field alone names it: the message says nothing of a login
            end('failed', {'message': 'request failed', 'codexErrorInfo': 'unauthorized'})
        elif mode != 'nomain':
            end()

    def handle(m):
        meth, i, p = m.get('method'), m.get('id'), m.get('params') or {}
        if meth == 'turn/start' and env('ASK_DELAY'):   # did the client send more before this answer?
            time.sleep(float(env('ASK_DELAY')))
            if select.select([0], [], [], 0)[0] or st['more']:
                open(env('OVERLAP_LOG'), 'a').write('turn/start %s answered with a request already waiting\n' % i)
        text = ' '.join(x.get('text', '') for x in p.get('input') or [] if isinstance(x, dict))
        cur = st['cur']
        if meth == 'initialize':
            ans(i, {'userAgent': 'stub/0.160.0', 'platformOs': 'macos'})
        elif meth == 'thread/start':
            ans(i, {'thread': {'id': st['th'], 'ephemeral': p.get('ephemeral')}, 'model': p.get('model'), 'sandbox': p.get('sandbox')})
            note('thread/started', thread={'id': st['th']})
        elif meth == 'thread/resume' and mode == 'badresume':
            err(i, -32600, 'no rollout found for thread id %s' % p.get('threadId'))
        elif meth == 'thread/resume':
            st['th'] = p.get('threadId')
            ans(i, {'thread': {'id': st['th']}, 'model': p.get('model')})
        elif meth == 'turn/start' and mode == 'refuse':
            err(i, -32602, fail)
        elif meth == 'turn/start' and cur and mode != 'stale':   # mid-turn: it steers the open turn
            cur['inputs'].append(text)
            item({'type': 'userMessage', 'id': 'um-s%d' % len(cur['inputs']), 'content': p.get('input')})
            ans(i, {'turn': {'id': cur['id'], 'status': 'inProgress'}})
        elif meth == 'turn/start':
            if cur:   # stale: the open turn ends before this input reaches it
                complete()
            st['n'] += 1
            seg = turns[min(st['n'], len(turns)) - 1] if turns else None
            tid = next(x['params']['turn']['id'] for x in seg if x['method'] == 'turn/started') if seg else 'turn-%d' % st['n']
            st['cur'] = {'id': tid, 'inputs': [text], 'due': time.time() + float(env('STUB_SLEEP', 2)) if mode in ('slow', 'stale') else None}
            ans(i, {'turn': {'id': tid, 'status': 'inProgress'}})
            if seg:
                for x in seg:
                    out(x)
                st['cur'] = None
                return
            note('turn/started', threadId=st['th'], turn={'id': tid, 'status': 'inProgress', 'items': []})
            item({'type': 'userMessage', 'id': 'um-%d' % st['n'], 'content': p.get('input')})
            if cur and mode == 'stale':   # the last turn's command completes inside this one, and its model call reports
                note('item/completed', threadId=st['th'], turnId=st['last'], item=cmd('exec-late-%d' % (st['n'] - 1)))
                note('thread/tokenUsage/updated', threadId=st['th'], turnId=st['last'],
                     tokenUsage={'total': st['total'], 'last': {'inputTokens': 900, 'outputTokens': 90}})
            if mode in ('park', 'parkexit'):   # a command codex runs in a session of its own; it never finishes
                kid = subprocess.Popen(['sleep', '60'], start_new_session=True)
                st['kids'].append(kid)
                item({'type': 'commandExecution', 'id': 'exec-1', 'command': 'sleep 60', 'status': 'inProgress', 'processId': str(kid.pid)}, False)
                report({'inputTokens': 1000, 'cachedInputTokens': 800, 'outputTokens': 100, 'reasoningOutputTokens': 40})   # the call that ran it
                open(env('STUB_PIDFILE'), 'w').write('%d %d' % (os.getpid(), kid.pid))
            elif st['cur']['due'] is None:
                complete()
        elif meth == 'turn/interrupt' and cur and p.get('turnId') == cur['id']:   # the turn ends; its command runs on
            ans(i, {})
            end('interrupted')
        elif meth == 'turn/interrupt':
            err(i, -32600, 'no active turn to interrupt')
        elif i is not None:
            err(i, -32601, 'stub: no method %s' % meth)

    if mode == 'parkexit':
        signal.signal(signal.SIGTERM, lambda *a: os._exit(0))
    buf = b''
    while True:
        cur = st['cur']
        due = cur['due'] if cur else None
        if not select.select([0], [], [], None if due is None else max(0, due - time.time()))[0]:
            complete()
            continue
        chunk = os.read(0, 65536)
        if not chunk:   # stdin EOF (logged as <EOF>): the app-server reaps its commands and exits
            if env('STDIN_LOG'):
                open(env('STDIN_LOG'), 'a').write('<EOF>\n')
            for k in st['kids']:
                k.kill()
            sys.exit(0)
        *lines, buf = (buf + chunk).split(b'\n')
        for k, ln in enumerate(lines):
            if env('STDIN_LOG'):
                open(env('STDIN_LOG'), 'a').write(ln.decode() + '\n')
            st['more'] = any(x.strip() for x in lines[k + 1:]) or bool(buf.strip())
            if ln.strip():
                handle(json.loads(ln))


if prov == 'codex':
    codex_server()
chunks, seen = [[]], False
if mode == 'replay':   # one recorded turn per message line: split before each turn's opening event
    for ln in open(env('STUB_REPLAY')):
        o = json.loads(ln)
        opens = (o.get('type'), o.get('subtype')) == ('system', 'init')
        if opens and seen:
            chunks.append([])
        seen = seen or opens
        chunks[-1].append(ln.rstrip('\n'))
t = 0
while True:
    line = sys.stdin.readline()
    if not line:   # stdin EOF, logged as <EOF> (as the codex stub logs it): the CLI ends
        if env('STDIN_LOG'):
            open(env('STDIN_LOG'), 'a').write('<EOF>\n')
        break
    t += 1
    if env('STDIN_LOG'):
        open(env('STDIN_LOG'), 'a').write(line)
    if mode == 'replay':
        for ln in chunks[t - 1] if t <= len(chunks) else []:
            out(ln)
        continue
    sid = 'sess-new' if mode == 'newsid' else 'sess-stub'   # newsid: a resume that silently started fresh
    res = lambda text: out({'type': 'result', 'subtype': 'success', 'is_error': False, 'result': text,
                            'session_id': sid, 'usage': {'input_tokens': 2, 'output_tokens': 3}})
    if mode == 'authfail':
        sys.stderr.write('Invalid API key · Please run /login\n')
        sys.exit(1)
    out({'type': 'system', 'subtype': 'init', 'session_id': sid})
    if mode == 'initonly':
        sys.exit(1)
    elif mode == 'park':
        park()
    elif mode == 'deaf':   # ignores TERM: only its wrapper's KILL ends it
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        park()
    elif mode == 'count':
        res('turn %d pid %d' % (t, os.getpid()))
    elif mode == 'slow':   # a turn still running when the next line arrives
        time.sleep(float(env('STUB_SLEEP', 2)))
        res('turn %d pid %d' % (t, os.getpid()))
    elif mode == 'echo':   # the answer is the message itself: the row chooses its bytes
        res(json.loads(line)['message']['content'][0]['text'])
    elif mode == 'quoted':
        res('The log I read says: Claude AI usage limit reached, resets 5pm')
    elif mode == 'quotedfail':   # a FAILING turn whose prose quotes a quota line: typed error, never quota
        out({'type': 'assistant', 'message': {'content': [{'type': 'text', 'text': 'The log says: Claude AI usage limit reached, resets 5pm'}]}})
        out({'type': 'result', 'subtype': 'error_during_execution', 'is_error': True, 'errors': ['API Error: 500 Internal server error'], 'session_id': sid})
    elif mode == 'quota':
        out({'type': 'result', 'subtype': 'success', 'is_error': True, 'result': 'Claude AI usage limit reached, resets 6:20am', 'session_id': sid})
    else:   # the answer names the two switches a claude seat's env must carry
        if mode == 'straggler':   # a process the seat backgrounded and forgot
            open(env('STUB_PIDFILE'), 'w').write(str(subprocess.Popen(['sleep', '300']).pid))
        res('bg=%s mem=%s turn=%d' % (os.environ.get('CLAUDE_CODE_DISABLE_BACKGROUND_TASKS', 'unset'),
                                      os.environ.get('CLAUDE_CODE_DISABLE_AUTO_MEMORY', 'unset'), t))
        if mode == 'straggler':   # ...and the CLI then ends ON ITS OWN: no stop finds the forgotten child, the
            sys.exit(0)           # reaper's natural-exit pass is all that kills it
