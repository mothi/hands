#!/usr/bin/env python3
"""wires.py -- how hands talks to each provider's CLI. python3 stdlib only.

PROVIDERS is the table: each provider's family (how the wrapper talks to its CLI), delivery (what a send to a busy
seat does) and command line. launch() starts a seat's CLI and runs its handshake. The translators turn what a CLI
says into the engine's events: Wire for claude's stream-json and codex's app-server, translate() for ACP (grok,
opencode). The vocabulary every wire shares lives here too: event(), usage(), Turn (the one answer rule), clip().

Cli is one CLI process over stdio, driven by its owner's select loop (no threads). Rpc is a Cli that speaks JSON-RPC
2.0 (codex's app-server; ACP agents). AcpSeat is an Rpc for ACP v1 agents (`grok agent stdio`, `opencode acp`); it
never calls `authenticate`: each seat runs on its own CLI's login.
    seat, why = launch(handle, wrap, log, stderr_path, note)   # why: a launch typed before any turn
    seat.prompt(text, lambda u: emit(translate(u, n, t, wire)), done)  # ACP; done(res): translate(res, n, t, wire)
"""
import collections, datetime, json, os, select, signal, subprocess, time

STOP = {"end_turn": "ok", "cancelled": "cancelled", "max_tokens": "truncated",
        "max_turn_requests": "truncated", "refusal": "error"}   # a model's refusal mid-turn is an error (its event says so);
        # `refused` is the engine's own, typed before any prompt. Unknown -> error
TOOL_DONE = {"completed": "ok", "failed": "error", "cancelled": "cancelled"}
FILE_KINDS = ("edit", "delete", "move")        # a location names a touched file only for these kinds
QUIET = ("available_commands_update", "config_option_update", "session_info_update",
         "current_mode_update", "user_message_chunk")              # session state, not turn events
USAGE = (("in", "input_tokens", "inputTokens"), ("out", "output_tokens", "outputTokens"),
         ("cache_read", "cache_read_input_tokens", "cached_input_tokens", "cachedReadTokens", "cachedInputTokens"),
         ("cache_write", "cache_creation_input_tokens", "cache_write_input_tokens", "cachedWriteTokens", "cacheCreationTokens",
          "cacheWriteInputTokens"),
         ("reasoning", "reasoning_output_tokens", "thoughtTokens", "reasoningTokens", "reasoningOutputTokens",
          "output_tokens_details.thinking_tokens"))
CACHE_IN_INPUT, REASONING_APART = ("codex", "grok"), ("opencode",)   # the wires' two accounting facts


def now():
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def event(kind, turn, t=None, **kw):   # THE event line, every wire: its None fields dropped
    e = {"t": t or now(), "kind": kind, "turn": turn}
    e.update((k, v) for k, v in kw.items() if v is not None)
    return e


def num(v):
    return v if type(v) in (int, float) else None


def clip(v, n=300):
    s = v if v is None or isinstance(v, str) else json.dumps(v, ensure_ascii=False)
    return s if s is None or len(s) <= n else s[:n] + "...[%d chars]" % len(s)


def small(v, n=600):  # a tool input rides whole when short, clipped as text when long
    return v if v is None or len(json.dumps(v, ensure_ascii=False)) <= n else clip(v, n)


def jparse(line):
    try:
        return json.loads(line)
    except ValueError:
        return line


def usage(u, wire):
    """THE usage rule, every wire -> {in (uncached), out (everything billed as output), cache_read, cache_write,
    reasoning} or None. The names differ per wire; the accounting in two facts (1390 recorded objects
    agree): codex and grok count the cache inside input, opencode keeps reasoning apart from output."""
    d = {}
    for k, *names in USAGE if isinstance(u, dict) else ():
        for name in names:
            v = u
            for part in name.split("."):
                v = v.get(part) if isinstance(v, dict) else None
            if num(v) is not None:
                d[k] = v
                break
    if wire in CACHE_IN_INPUT and "in" in d:
        d["in"] -= d.get("cache_read", 0)
    if wire in REASONING_APART and "out" in d:
        d["out"] += d.get("reasoning", 0)
    return d or None


class Turn(object):
    """A turn's model responses in order -- THE answer rule, every wire: the LAST response that carries text,
    voided when a tool event follows it, failing closed when that text is whitespace; an earlier response is
    never promoted (441 recorded ACP turns agree)."""

    def __init__(t):
        t.resps, t.seq, t.last_tool, t.usage, t.t0 = [], 0, -1, None, time.time()

    def text(t, key, s):
        t.seq += 1
        if not t.resps or t.resps[-1][2] or t.resps[-1][0] != key:
            t.resps.append([key, [], False, 0])
        t.resps[-1][1].append(s)
        t.resps[-1][3] = t.seq

    def boundary(t):
        if t.resps:
            t.resps[-1][2] = True

    def tool(t):
        t.seq += 1
        t.last_tool = t.seq
        t.boundary()

    def final(t):
        for _key, chunks, _closed, seq in reversed(t.resps):
            s = "".join(chunks)
            if s:
                return s if seq > t.last_tool and s.strip() else ""
        return ""


def _text(u):
    c = u.get("content")
    return (c.get("text") or "") if isinstance(c, dict) and c.get("type") == "text" else ""


def _xtool(u):  # grok carries the tool's name/kind in _meta["x.ai/tool"]
    m = u.get("_meta") if isinstance(u.get("_meta"), dict) else {}
    return m.get("x.ai/tool") if isinstance(m.get("x.ai/tool"), dict) else {}


class AcpError(Exception):
    """A JSON-RPC error answer, a dead agent, or a refused call."""


class Cli(object):
    """One CLI process over stdio, in a session of its own, driven by its owner's select loop: no threads. What is
    written waits in `out` until the CLI's stdin takes it; each whole line its stdout gives goes to line(), which hands
    it to note(): a JSON object as a dict, any other line as its text. `env` = overrides on os.environ. `log(line,
    direction)` gets every line verbatim, "in" (CLI->client) or "out"."""

    session_id = None                                           # the conversation, once a handshake names it

    def __init__(self, argv, cwd, env=None, log=None, stderr_path=None, note=None):
        self.cwd, self.log, self.note = cwd, log, note
        self._eof, self.out, self._tail = False, b"", b""
        self._err = open(stderr_path, "ab") if stderr_path else subprocess.DEVNULL
        self.proc = subprocess.Popen(argv, cwd=cwd, env=dict(os.environ, **(env or {})), stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=self._err, start_new_session=True)
        self._in, self._out = self.proc.stdin.fileno(), self.proc.stdout.fileno()
        os.set_blocking(self._in, False)
        os.set_blocking(self._out, False)

    def _hook(self, line, direction):
        try:
            self.log and self.log(line, direction)
        except Exception:                                       # a failing hook never stops the wire
            pass

    def fds(self):
        """What a select waits on for this agent -> (readable, writable)."""
        return ([] if self._eof else [self._out]), ([self._in] if self.out and self._in is not None else [])

    def ready(self, readable, writable):
        """Give the agent's stdin what it takes; read its stdout and dispatch each whole line."""
        if self.out and self._in is not None and self._in in writable:
            try:
                self.out = self.out[os.write(self._in, self.out):]
            except BlockingIOError:
                pass
            except OSError:                                     # the agent closed its stdin: the rest is lost with it
                self.out = b""
        if self._eof or self._out not in readable:
            return
        try:
            chunk = os.read(self._out, 65536)
        except BlockingIOError:
            return
        if chunk:
            *lines, self._tail = (self._tail + chunk).split(b"\n")
        else:                                                   # EOF: a last line with no newline still counts
            lines, self._tail = [self._tail], b""
        for raw in lines:
            self._line(raw)
        if not chunk:
            self._gone()

    def pump(self, timeout):
        """One select on this agent alone (the handshake, a call): wait up to `timeout` s, then ready()."""
        r, w = self.fds()
        if not r and not w:
            time.sleep(min(timeout, 0.05))
            return
        r, w, _ = select.select(r, w, [], timeout)
        self.ready(r, w)

    def _line(self, raw):
        line = raw.decode("utf-8", "replace").rstrip("\r")
        if not line.strip():
            return
        self._hook(line, "in")
        try:
            self.line(line)
        except Exception:                                       # a failing callback never stops the wire
            pass

    def line(self, line):
        msg = jparse(line)
        if self.note is not None:
            self.note(msg if isinstance(msg, dict) else line)

    def _gone(self):
        self._eof = True

    def write(self, data):
        """Queue bytes for the CLI's stdin, and give it what it takes now."""
        if self._in is None:
            raise AcpError("agent stdin closed")
        self.out += data
        self.ready([], [self._in])

    def _wait(self, grace):   # up to `grace` s for the agent to exit, reading its stdout all the while
        end = time.time() + grace
        while self.proc.poll() is None and time.time() < end:
            self.pump(min(0.1, end - time.time()))

    def close(self, grace=4.0):
        """stdin EOF (every agent exits on it: grok ~3.1 s, opencode ~0.05 s, codex app-server ~0.1 s), `grace`, then
        TERM -- at last KILL -- the whole process group, so a tool's children die with the seat. What the agent wrote
        is read and dispatched first; what it left unanswered gets None. Returns the exit code."""
        if self._in is not None:
            self.out, self._in = b"", None
            try:
                self.proc.stdin.close()
            except (OSError, ValueError):
                pass
        self._wait(grace)
        # A seat's process is its reaper (seat.py reap), which answers a TERM by ending its CLI's group -- KILL within
        # 2 s -- and exiting. It gets that long: a reaper KILLed first would leave behind a CLI that ignores TERM
        for sig, wait in ((signal.SIGTERM, grace + 3), (signal.SIGKILL, grace)):
            try:
                os.killpg(self.proc.pid, sig)
            except (ProcessLookupError, PermissionError):
                break                                           # the group is empty: nothing left to reap
            self._wait(wait)
        end = time.time() + grace                               # stdout's last lines, up to its EOF
        while not self._eof and time.time() < end:
            self.pump(min(0.1, end - time.time()))
        if not self._eof:                                       # an escaped grandchild holds the pipe open
            self._gone()
        self.proc.stdout.close()
        if self._err is not subprocess.DEVNULL:
            self._err.close()
        return self.proc.poll()


class Rpc(Cli):
    """A Cli speaking newline-delimited JSON-RPC 2.0 (ACP agents; codex's app-server, which answers without the jsonrpc
    field and accepts it, codex-cli 0.160). Each line is dispatched at once: an answer to the callback its request
    named, a notification to note({"method", "params"}), an agent's request to _answer."""

    def __init__(self, argv, cwd, env=None, log=None, stderr_path=None, note=None):
        self._nid, self._pending = 0, {}
        Cli.__init__(self, argv, cwd, env, log, stderr_path, note)

    def line(self, line):
        msg = jparse(line)
        if isinstance(msg, dict):                               # a malformed line is logged raw and skipped
            self._dispatch(msg)

    def _gone(self):
        """Nothing more will be answered: every waiting request gets msg None (a prompt -> incomplete)."""
        Cli._gone(self)
        waiting, self._pending = list(self._pending.values()), {}
        for done in waiting:
            try:
                done(None)
            except Exception:
                pass

    def _dispatch(self, msg):
        meth, mid = msg.get("method"), msg.get("id")
        if meth is None:                                        # the answer to one of our requests
            done = self._pending.pop(mid, None)
            return done and done(msg)
        params = msg.get("params") if isinstance(msg.get("params"), dict) else {}
        if mid is not None:
            return self._answer(mid, meth, params)
        if self.note is not None:
            self.note({"method": meth, "params": params})

    def _answer(self, mid, meth, params):   # an agent->client request this client offers nothing for
        self.send({"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": "Method not found: " + str(meth)}})

    def send(self, obj):
        """Queue one JSON-RPC line for the agent, and give it what its stdin takes now."""
        if self._in is None:
            raise AcpError("agent stdin closed")
        line = json.dumps(obj, ensure_ascii=False)
        self._hook(line, "out")
        self.write(line.encode("utf-8") + b"\n")

    def notify(self, method, params=None):
        self.send(dict({"jsonrpc": "2.0", "method": method}, **({"params": params} if params is not None else {})))

    def request(self, method, params, done):
        """One request; done(msg) gets its answer, or None when the agent is gone first. -> its id (None: gone)"""
        if self._eof or self._in is None:
            done(None)
            return None
        self._nid += 1
        mid = self._nid
        self._pending[mid] = done
        self.send({"jsonrpc": "2.0", "id": mid, "method": method, "params": params})
        return mid

    def call(self, method, params, timeout):
        """A request waited for, reading the agent all the while -> its answer message; None = the agent is gone."""
        box = []
        mid, end = self.request(method, params, box.append), time.time() + timeout
        while not box:
            if time.time() >= end:
                self._pending.pop(mid, None)
                raise AcpError("no answer to %s within %ss" % (method, timeout))
            self.pump(min(0.5, end - time.time()))
        return box[0]

    def ask(self, method, params, timeout=45):   # a handshake call -> its result; AcpError on an error or no answer
        msg = self.call(method, params, timeout)
        if msg is None:
            raise AcpError("agent exited before answering %s (rc=%s)" % (method, self.proc.poll()))
        if "error" in msg:
            e = msg["error"] if isinstance(msg["error"], dict) else {"message": str(msg["error"])}
            raise AcpError("%s failed: %s %s" % (method, e.get("code"), e.get("message")))
        return msg.get("result") if isinstance(msg.get("result"), dict) else {}


class AcpSeat(Rpc):
    """One ACP agent process and its session."""

    def __init__(self, argv, cwd, env=None, log=None, stderr_path=None):
        self.session_id, self.info, self.config = None, {}, []
        self._sink, self._turn = None, None                    # the prompt in flight
        Rpc.__init__(self, argv, cwd, env, log, stderr_path, self._update)

    def _update(self, msg):
        u = msg["params"].get("update")                         # session/update + grok _x.ai/session_notification
        if not isinstance(u, dict) or "sessionUpdate" not in u:
            return                                              # x.ai queue/models/sessions chatter: raw only
        if u["sessionUpdate"] == "config_option_update" and isinstance(u.get("configOptions"), list):
            self.config = u["configOptions"]
        turn, sink, k = self._turn, self._sink, u["sessionUpdate"]
        if turn is not None:                                    # the answer rule's input + the calls left open
            turn.bounded = turn.bounded or k == "response_completed" or bool(
                k in ("agent_message_chunk", "agent_thought_chunk") and u.get("messageId"))
            if k == "agent_message_chunk":
                turn.text(u.get("messageId"), _text(u))
            elif k == "response_completed":
                turn.boundary()
            elif k in ("tool_call", "tool_call_update"):
                turn.tool()
                if u.get("status") in TOOL_DONE:
                    turn.calls.pop(u.get("toolCallId"), None)
                elif u.get("toolCallId"):
                    turn.calls.setdefault(u["toolCallId"], True)
        if sink is not None:
            sink(u)

    def _answer(self, mid, meth, params):
        """Agent->client requests. Permission: allow_once first (an always pick may persist a rule in the
        CLI's config), else the first allow option. fs/* and terminal/* were not offered at initialize."""
        if meth != "session/request_permission":
            return Rpc._answer(self, mid, meth, params)
        opts = [o for o in params.get("options") or [] if isinstance(o, dict)]
        pick = next((o for o in opts if o.get("kind") == "allow_once"), None) or \
            next((o for o in opts if str(o.get("kind", "")).startswith("allow")), None)
        out = {"outcome": "selected", "optionId": pick.get("optionId")} if pick else {"outcome": "cancelled"}
        self.send({"jsonrpc": "2.0", "id": mid, "result": {"outcome": out}})
        sink, call = self._sink, params.get("toolCall") if isinstance(params.get("toolCall"), dict) else {}
        if sink is not None:
            sink({"_client": "permission", "toolCallId": call.get("toolCallId"), "outcome": out})

    def ask(self, method, params, timeout=45):
        r = Rpc.ask(self, method, params, timeout)
        if isinstance(r.get("configOptions"), list):
            self.config = r["configOptions"]
        return r

    def initialize(self):
        """ACP handshake (protocolVersion 1; no fs, no terminal) -> the agent's capabilities."""
        self.info = self.ask("initialize", {"protocolVersion": 1, "clientCapabilities": {
            "fs": {"readTextFile": False, "writeTextFile": False}, "terminal": False}})
        return self.info.get("agentCapabilities") or {}

    def new_session(self):
        r = self.ask("session/new", {"cwd": self.cwd, "mcpServers": []})
        if not r.get("sessionId"):
            raise AcpError("session/new answered without a sessionId")
        self.session_id = r["sessionId"]
        return self.session_id

    def resume(self, session_id):
        """Reattach a stored session in this (new) process: session/resume, which replays no history."""
        if "resume" not in ((self.info.get("agentCapabilities") or {}).get("sessionCapabilities") or {}):
            raise AcpError("agent does not advertise sessionCapabilities.resume")
        self.ask("session/resume", {"sessionId": session_id, "cwd": self.cwd, "mcpServers": []})
        self.session_id = session_id

    def _option(self, key):
        return next((o for o in self.config if isinstance(o, dict) and key in (o.get("id"), o.get("category"))), None)

    def current(self, key):
        """currentValue of the config option whose id or category is `key` (model, thought_level, mode)."""
        return (self._option(key) or {}).get("currentValue")

    def values(self, key):
        """The values option `key` lists (id or category); None = the agent has no such option."""
        opt = self._option(key)
        return None if opt is None else [o.get("value") for o in opt.get("options") or [] if isinstance(o, dict)]

    def set_config(self, key, value):
        """session/set_config_option on the option whose id OR category is `key` (effort: grok
        reasoning_effort, opencode effort -- both category thought_level) -> the new currentValue.
        None = no such option (no-op); AcpError = refused, or the value did not take (nor in a config update within 2 s)."""
        opt = self._option(key)
        if opt is None:
            return None
        r = self.ask("session/set_config_option", {"sessionId": self.session_id, "configId": opt["id"], "value": value})
        if not isinstance(r.get("configOptions"), list):
            opt["currentValue"] = value                         # an ack without state: take it at its word
        late = time.time() + 2   # opencode 2.x can reply with the OLD value, then confirm in a config update ~20 ms later
        while self.current(opt["id"]) != value and time.time() < late and not self._eof:
            self.pump(min(0.05, late - time.time()))
        if self.current(opt["id"]) != value:
            raise AcpError("%s=%s did not take (currentValue %r)" % (opt["id"], value, self.current(opt["id"])))
        return value

    def busy(self):
        return self._turn is not None

    def prompt(self, text, on_update, done):
        """session/prompt with one text block; it returns at once, the owner's loop runs the turn. on_update(u) gets, in
        order: a turn_start record, every session update of the turn, then a tool_call_update {status: cancelled} per
        tool call left open (grok sends no closing update after a cancel). done(res) gets the PromptResponse result +
        _client, sessionId, answer (Turn's rule), final; + error (a JSON-RPC error) or incomplete (the agent went first).
        The wall clock is the caller's: close() answers a prompt still open."""
        if not self.session_id:
            raise AcpError("no session: call new_session() or resume() first")
        if self._turn is not None:
            raise AcpError("a prompt is already running on this seat (ACP v1 has no queue)")
        turn, sink = Turn(), on_update or (lambda u: None)
        turn.calls, turn.bounded = {}, False
        sink({"_client": "turn_start", "sessionId": self.session_id})
        self._turn, self._sink = turn, sink

        def answered(msg):   # the turn is over: later updates join no turn
            self._turn = self._sink = None
            for cid in list(turn.calls):
                sink({"_client": "tool_closed", "sessionUpdate": "tool_call_update", "toolCallId": cid, "status": "cancelled"})
            res = dict(msg["result"]) if msg and isinstance(msg.get("result"), dict) else {}
            if msg is None:
                res["incomplete"] = "agent exited before answering session/prompt (rc=%s)" % self.proc.poll()
            elif "error" in msg:
                res["error"] = msg["error"]
            res.update(_client="turn_end", sessionId=self.session_id, answer=turn.final(),
                       final="boundary" if turn.bounded else "after-last-tool")
            done(res)
        self.request("session/prompt", {"sessionId": self.session_id, "prompt": [{"type": "text", "text": text}]}, answered)


def _tool(u):
    return u.get("name") or u.get("title") or _xtool(u).get("name")


def _paths(u):
    """Files a tool touched: diff content paths, plus locations when the tool kind edits, deletes or
    moves (a shell tool's location is its cwd). May repeat across updates: consumers dedupe by path."""
    content = u.get("content") if isinstance(u.get("content"), list) else []
    ps = [c.get("path") for c in content if isinstance(c, dict) and c.get("type") == "diff"]
    if (u.get("kind") or _xtool(u).get("kind")) in FILE_KINDS:
        ps += [x.get("path") for x in u.get("locations") or [] if isinstance(x, dict)]
    return list(dict.fromkeys(p for p in ps if p))


def translate(u, turn, t=None, wire=None):
    """Pure map of one dict from AcpSeat -- an ACP session update (grok's _x.ai kinds included), a
    client record, or prompt()'s result -- to events {"t", "kind", "turn", ...}, kind in turn_start
    text thought tool_call tool_result file usage turn_end error progress. Session state maps to [].
    `wire` (grok|opencode) names the usage rule's facts."""
    t = t or now()

    def ev(kind, **kw):
        return event(kind, turn, t, **kw)

    rec, k = u.get("_client"), u.get("sessionUpdate")
    if rec == "turn_start":
        return [ev("turn_start", session=u.get("sessionId"))]
    if rec == "turn_end":
        return _turn_end(u, ev, wire)
    if rec == "permission":
        return [ev("progress", call=u.get("toolCallId"), text="permission %s" % ((u.get("outcome") or {}).get("optionId") or "cancelled"))]
    if k in ("agent_message_chunk", "agent_thought_chunk"):
        return [ev("text" if k == "agent_message_chunk" else "thought", text=_text(u))]
    if k in ("tool_call", "tool_call_update"):
        call, st, out = u.get("toolCallId"), u.get("status"), []
        if k == "tool_call":
            out.append(ev("tool_call", call=call, tool=_tool(u), input=small(u.get("rawInput"))))
        if st in TOOL_DONE:
            out.append(ev("tool_result", call=call, status=TOOL_DONE[st], output=clip(u.get("rawOutput") or u.get("content")),
                          synthetic=True if rec else None))
        elif k == "tool_call_update":
            out.append(ev("progress", call=call, text="tool %s" % (st or "update"), input=small(u.get("rawInput") or None)))
        return out + [ev("file", call=call, path=p, tool=_tool(u)) for p in _paths(u)]
    if k == "plan":
        ents = [e for e in u.get("entries") or [] if isinstance(e, dict)]
        return [ev("progress", text=clip("plan: " + "; ".join("[%s] %s" % (e.get("status"), e.get("content")) for e in ents)))]
    if k == "usage_update":            # session state: context fill + CUMULATIVE cost (RFD session-usage)
        return [ev("progress", text="context", context_used=u.get("used"), context_size=u.get("size"), cost=u.get("cost"))]
    return [] if k in QUIET else [ev("progress", text=str(k))]   # grok x.ai: hooks, response/turn_completed


def _turn_end(u, ev, wire):
    """prompt()'s result -> the turn's one usage event, an error event when it failed, then turn_end."""
    stop = u.get("stopReason")
    status = "incomplete" if "incomplete" in u else "error" if "error" in u else STOP.get(stop, "error")
    meta = u.get("_meta") if isinstance(u.get("_meta"), dict) else {}
    raw, out = u.get("usage") if isinstance(u.get("usage"), dict) else meta.get("usage"), []
    if isinstance(raw, dict):
        n, ticks = usage(raw, wire) or {}, num(raw.get("costUsdTicks"))     # grok: 1 tick = 1e-10 USD
        out.append(ev("usage", usage=n, cost_usd=ticks / 1e10 if ticks is not None else None) if any(n.values())
                   else ev("usage", usage_unknown=True))       # opencode's cancelled turn reads 0: unknown, not free
    err = u.get("error") if isinstance(u.get("error"), dict) else {}
    if status in ("error", "incomplete"):
        why = u.get("incomplete") or (err and "%s %s" % (err.get("code"), err.get("message")))
        out.append(ev("error", text=clip(why or "stopReason %r" % stop)))
    out.append(ev("turn_end", status=status, text=u.get("answer", "") if status == "ok" else "",
                  session=u.get("sessionId"), final=u.get("final"), stop=stop))
    return out


# NO PERSONAL HOOKS: every seat runs its CLI with the user's hooks off -- hooks are the user's code, written for the
# user's own sessions. Each command names its CLI's own switch: claude disableAllHooks, codex --disable hooks, grok its
# claude- and cursor-hook scanners. opencode runs no claude hooks; its plugins have no switch short of its whole config
# dir (SKILL.md)
def claude_command(h):   # claude: one process for the whole seat -> (argv, env)
    m, e, sid = h.get('model'), h.get('effort'), h.get('session')
    # Every seat runs bypassPermissions: full access, as the caller. Background tasks OFF: a turn left waiting on one
    # parks. ONE settings source, --settings: the seat refuses other sessions' cross-session messages (its own inbox
    # socket stays bound and drops them).
    # LEAN (measured on paired haiku buys, prefix 36693 -> 33998 tokens): --strict-mcp-config, no host
    # MCP servers (their tool schemas cost every call); --exclude-dynamic-system-prompt-sections, cwd, env and git
    # status move into the first message, so the system prompt is shared across folders; auto-memory OFF, no
    # memory section and no memory folder (a seat whose cwd is the driver's repo would get the driver's own).
    # This is the ONE claude launch: start and every relaunch send the same flags, so a resume never re-creates
    # the cached prefix. NO PERSONAL HOOKS (above): disableAllHooks in the one settings source
    st = {'crossSessionInbound': 'refuse', 'disableAllHooks': True}
    a = ['claude', '-p', '--input-format', 'stream-json', '--output-format', 'stream-json', '--verbose', '--model', m or 'sonnet',
         '--settings', json.dumps(st, separators=(',', ':')), '--strict-mcp-config', '--exclude-dynamic-system-prompt-sections']
    a += (['--effort', e] if e else []) + ['--permission-mode', 'bypassPermissions']
    return a + (['--resume', sid] if sid else []), \
        {'CLAUDE_CODE_DISABLE_BACKGROUND_TASKS': '1', 'CLAUDE_CODE_DISABLE_AUTO_MEMORY': '1'}


def codex_command(h):   # codex: ONE app-server process per launch, over stdio (codex-cli 0.160), all root
    # options. --no-daemon: the seat never runs through the user's shared app-server daemon; --disable memories: no user
    # memory is read or written (the twin of claude's auto-memory off); --disable hooks: NO PERSONAL HOOKS
    # (above); web_search live: the live search exec's --search gave (app-server reads no --search: its config/read shows
    # web_search null either way). The cwd, sandbox, model and approvals ride the thread (codex_open), the effort each
    # turn/start (seat.py's Seat.rpc_turn)
    return [h.get('cli') or 'codex', '--no-daemon', '--disable', 'memories', '--disable', 'hooks', '-c', 'web_search="live"',
            'app-server'], {}


def grok_command(h):
    """grok: -m pins the model; --reasoning-effort never reaches an ACP session, so the effort is a session option (the
    handshake sets it). GROK_DISABLE_AUTOUPDATER=1 stops the managed install's update check; GROK_CLAUDE_HOOKS_ENABLED
    and GROK_CURSOR_HOOKS_ENABLED 0: grok runs the hooks it finds in ~/.claude and ~/.cursor unless told not to (NO
    PERSONAL HOOKS, above; its own hooks dir has no switch)."""
    model = h.get("model")
    return ([h.get("cli") or "grok", "agent", "--no-leader", "--always-approve"] + (["-m", model] if model else []) + ["stdio"],
            {"GROK_DISABLE_AUTOUPDATER": "1", "GROK_CLAUDE_HOOKS_ENABLED": "0", "GROK_CURSOR_HOOKS_ENABLED": "0"})


def opencode_command(h):
    """opencode: no -m on `acp`, and 2.x takes no model from OPENCODE_CONFIG_CONTENT on ACP (2.0.22: the agent ran the
    global default), so the model is a session option like the effort; no --cwd either (2.0 dropped it): like every
    seat, the agent runs IN the seat's cwd, and session/new carries it. It runs on the user's own opencode store and
    login, as any opencode session does: no env of its own. Every ACP prompt carries a model VARIANT (default: the first
    listed) and ACP exposes it as the effort option: the handshake sets it too."""
    if not (h.get("model") or "").partition("/")[2]:
        raise ValueError("model-required", "opencode seats name provider/model; the CLI default is the driver's own pool")
    return [h.get("cli") or "opencode", "acp"], {}


# THE PROVIDER TABLE. family: how the wrapper talks to the CLI -- stdin (claude's own stream-json on its stdin and
# stdout), rpc (codex's app-server) or acp (Agent Client Protocol). delivery: what a send to a busy seat does -- steer
# (it joins the running turn) or queue (it waits: ACP v1 has no mid-turn input). command(h) -> (argv, env overrides)
Provider = collections.namedtuple("Provider", "family delivery command")
PROVIDERS = {
    "claude": Provider("stdin", "steer", claude_command),
    "codex": Provider("rpc", "steer", codex_command),
    "grok": Provider("acp", "queue", grok_command),
    "opencode": Provider("acp", "queue", opencode_command),
}


def forget(provider, session, exe=None):
    """The argv that deletes an ABANDONED turn's stored session (its agent gone mid-turn: a stop, a crash, a kill), or None
    when the wire keeps nothing to resume. opencode 2.x: a killed turn keeps its execution claim, and the user's managed
    background service (`opencode serve --service`) resumes it at its next boot, in the seat's folder; a deleted session
    leaves it nothing (opencode 2.0.22). --standalone: its own private server, which never sweeps claims."""
    return [exe or "opencode", "session", "delete", session, "--standalone"] if provider == "opencode" and session else None


def codex_open(h, rpc):   # the app-server handshake -> the seat's thread: initialize, initialized, then thread/start -- or,
    # for a seat that has one, thread/resume (a thread has no rollout to resume before its first turn).
    # Every seat runs danger-full-access, approvals never (no one answers them).
    # Never ephemeral: a thread with no session files cannot resume. No fallback model: without one, config.toml's runs
    rpc.ask('initialize', {'clientInfo': {'name': 'hands', 'version': '2'}})
    rpc.notify('initialized')
    p = {'cwd': h['cwd'], 'sandbox': 'danger-full-access', 'approvalPolicy': 'never'}
    p.update({'model': h['model']} if h.get('model') else {})
    r = rpc.ask('thread/resume', dict(p, threadId=h['session'])) if h.get('session') else rpc.ask('thread/start', dict(p, ephemeral=False))
    tid = (r.get('thread') or {}).get('id')
    if not tid:
        raise AcpError('the app-server answered the thread without its id')
    return tid


def launch(h, wrap, log=None, stderr_path=None, note=None):
    """The seat handle h -> (its CLI past the handshake, None), or (the CLI or None, (status, reason, why)) when the
    launch is typed before any turn. wrap(argv) puts the CLI under the caller's reaper; note gets what a claude or
    codex CLI says (an ACP agent's updates go to each prompt's own sink). claude has no handshake. codex: the thread
    (codex_open). ACP, all before any prompt: initialize, the session (a recorded one is resumed, never replayed), the
    model set when the agent runs another one it lists, the model and effort checks, the effort set explicitly to a
    value the (set) model lists (none only when it lists none). Never a substitute model."""
    family, _, command = PROVIDERS[h["provider"]]
    m, e = h.get("model"), h.get("effort")
    try:
        argv, env = command(h)
    except ValueError as x:
        return None, ("refused",) + x.args
    if family == "stdin":
        return Cli(wrap(argv), h["cwd"], env, log, stderr_path, note), None
    if family == "rpc":
        rpc = Rpc(wrap(argv), h["cwd"], env, log, stderr_path, note)
        try:
            rpc.session_id = codex_open(h, rpc)
        except AcpError as x:
            return rpc, ("error", "handshake", str(x))
        return rpc, None
    seat = AcpSeat(wrap(argv), h["cwd"], env, log, stderr_path)
    try:
        seat.initialize()
        seat.resume(h["session"]) if h.get("session") else seat.new_session()
        have = seat.current("model")
        if m and have != m and m in (seat.values("model") or []):   # its default is not the seat's: set the listed one
            have = seat.set_config("model", m)
        vals = seat.values("thought_level") or []                   # read after the model: each lists its own variants
        if m and have != m:
            return seat, ("refused", "model-mismatch", "the agent runs %s and lists %s; the seat names %s: never substituted"
                          % (have, ", ".join(seat.values("model") or []) or "no models", m))
        if e not in (vals or [None]):
            return seat, ("refused", "effort-unsupported", "%s lists effort %s; the seat names %s" % (have, "|".join(vals) or "none", e or "none"))
        if e:
            seat.set_config("thought_level", e)
    except AcpError as x:
        return seat, ("error", "handshake", str(x))
    return seat, None


# ---------- the in.fifo record, and the claude and codex translators
def user_line(prov, text):   # the in.fifo line: claude's own stdin shape, else the one the JSON-RPC wrappers read (said())
    if prov == 'claude':
        return {'type': 'user', 'message': {'role': 'user', 'content': [{'type': 'text', 'text': text}]}}
    return {'message': {'content': text}}


def said(ln):   # a JSON-RPC wrapper's in.fifo record -> the text it delivers
    o = jparse(ln.decode('utf-8', 'replace'))
    return o['message']['content'] if isinstance(o, dict) and isinstance(o.get('message'), dict) else str(o)


CODEX_TOOLS = {'commandExecution': ('shell', 'command'), 'fileChange': ('apply_patch', 'changes'),
               'webSearch': ('web_search', 'query'), 'mcpToolCall': (None, 'arguments')}
FILE_TOOLS = {'Edit': 'file_path', 'Write': 'file_path', 'MultiEdit': 'file_path', 'NotebookEdit': 'notebook_path'}


def rec(turn, status, text='', use=None, final='none', errtext='', session=None, t0=None, extra=None):
    """A closed turn, as every wire hands it to finish(); extra = usage-event fields (usage_unknown, cost_usd)."""
    return {'turn': turn, 'status': status, 'text': text or '', 'usage': use, 'final': final, 'errtext': errtext,
            'session': session, 't0': t0, 'extra': extra or {}}


def plus(a, b):   # two raw usage reports summed field by field, over the fields of either (a call may omit one)
    a, b = a or {}, b or {}
    return {f: a.get(f, 0) + b.get(f, 0) for f in set(a) | set(b)
            if type(a.get(f, 0)) in (int, float) and type(b.get(f, 0)) in (int, float)}


class Wire:
    """One translator per stdin/rpc wire, the method named after its provider: feed(obj) emits events through
    put() and returns the turn records the wire closed (usually none, sometimes one). ACP translates in translate()."""
    def __init__(w, prov, base, session, put, pending):
        # notes + calls + tid: codex's error notices (they type a turn that ends unmarked), the items it opened, and the
        # open turn's own id. spent: what claude's total_cost_usd held before this turn (cumulative over its process: a
        # recorded 3-turn seat), so a turn's cost is the step
        w.prov, w.turn, w.base, w.session, w.put, w.pending, w.cur = prov, base, base, session, put, pending, None
        w.notes, w.calls, w.spent, w.tid = [], {}, 0, None

    def ev(w, kind, **kw):
        w.put(event(kind, w.turn, **kw))

    def start(w):
        cut = [w.cut()] if w.cur else []   # a new turn began with no end marker for the open one
        w.turn, w.cur = w.turn + 1, Turn()
        w.ev('turn_start', session=w.session)
        return cut

    def ensure(w):   # a line that belongs to a turn: after a turn_end only a delivered message opens one
        return [] if w.cur else w.start() if w.turn == w.base or w.pending() else None

    def end(w, status, text, use, final, errtext='', turn=None, extra=None):
        t0, w.cur, w.tid = (w.cur.t0 if w.cur else None), None, None
        return rec(turn or w.turn, status, text, use, final, errtext, w.session, t0, extra)

    def cut(w, turn=None):   # the stream stopped inside (or before) a turn: no end marker, no answer; the usage it
        # reported so far, in the envelope's fields as a turn_end's is
        return w.end('incomplete', '', usage(w.cur.usage, w.prov) if w.cur else None, 'none', '\n'.join(w.notes), turn=turn)

    def stray(w, what):
        w.ev('progress', text='%s belongs to no open turn (the last was turn %d): not a turn' % (what, w.turn))
        return []

    def tool_call(w, name, call, inp):
        w.ev('tool_call', tool=name, call=call, input=small(inp))
        key = FILE_TOOLS.get(name)
        if key and isinstance(inp, dict) and inp.get(key):
            w.ev('file', path=inp[key], tool=name, call=call)
        w.cur.tool()

    def feed(w, o):
        return getattr(w, w.prov)(o)

    def claude(w, o):
        ty, st = o.get('type'), o.get('subtype')
        if ty == 'system' and st == 'init':            # stdin mode re-sends init at every turn
            w.session = o.get('session_id') or w.session
            return w.start()
        if ty not in ('assistant', 'user', 'result', 'tool_progress'):
            w.ev('progress', text='%s%s' % (ty, '/' + st if st else ''))
            return []
        recs = w.ensure()
        if recs is None:
            return w.stray(ty)
        if ty == 'result':                              # .result is the answer only when is_error is false
            w.session = o.get('session_id') or w.session
            bad = bool(o.get('is_error')) or st not in (None, 'success')
            etxt = ' '.join(str(o[k]) for k in ('result', 'errors', 'api_error_status', 'terminal_reason') if o.get(k)) if bad else ''
            if bad:
                w.ev('error', text=clip(etxt or st))
            status = 'error' if bad else ('truncated' if o.get('stop_reason') == 'max_tokens' else 'ok')
            text = o.get('result') if status == 'ok' and isinstance(o.get('result'), str) else ''
            tot, cost = o.get('total_cost_usd'), None
            if type(tot) in (int, float):
                cost, w.spent = round(tot - w.spent, 10), tot
            return recs + [w.end(status, text, usage(o.get('usage'), 'claude'), 'field', etxt,
                                 extra=None if cost is None else {'cost_usd': cost})]
        if ty == 'tool_progress':
            w.ev('progress', tool=o.get('tool_name'), call=o.get('parent_tool_use_id'), text='heartbeat %ss' % o.get('elapsed_time_seconds'))
            return recs
        cont = (o.get('message') or {}).get('content')
        if isinstance(cont, str):
            w.ev('progress', text='user message: ' + clip(cont, 120))
        for c in cont if isinstance(cont, list) else []:
            ct = c.get('type') if isinstance(c, dict) else None
            if ct == 'text':
                w.ev('text', text=c.get('text', ''))
            elif ct == 'thinking':
                w.ev('thought', text=c.get('thinking', ''))
            elif ct == 'tool_use':
                w.tool_call(c.get('name'), c.get('id'), c.get('input'))
            elif ct == 'tool_result':
                w.ev('tool_result', call=c.get('tool_use_id'), output=clip(c.get('content')), status='error' if c.get('is_error') else 'ok')
            else:
                w.ev('progress', text='%s block %s' % (ty, ct))
        return recs

    def codex(w, o):   # app-server notifications: discrete items, the answer = the LAST agentMessage. Every item, usage and
        # end names its thread and turn: another thread's (a collaborator codex spawned reports over this connection,
        # codex-plugin-cc) is never the seat's text, calls or turns -- while a seat turn is open its calls are that turn's
        # spend and each of its items and turns a progress line (the seat is alive; no payload) -- and what arrives for a
        # turn that is not the open one (a command codex finishes after its turn completed) takes the
        # stray path, never a later turn's. Deltas, statuses, startup and account chatter stay raw (stream.jsonl): each
        # item lands whole at item/completed
        m, p = o.get('method'), o.get('params') if isinstance(o.get('params'), dict) else {}
        it, tid = p.get('item') if isinstance(p.get('item'), dict) else {}, p.get('turnId') or (p.get('turn') or {}).get('id')
        if p.get('threadId') not in (None, w.session):
            if w.cur is not None and m == 'thread/tokenUsage/updated':
                w.cur.usage = plus(w.cur.usage, (p.get('tokenUsage') or {}).get('last'))
            elif w.cur is not None and m in ('item/completed', 'turn/started', 'turn/completed'):
                w.ev('progress', text=' '.join(x for x in ('collaborator', m, it.get('type')) if x))
            return []
        if m == 'turn/started':
            recs = w.start()
            w.tid, w.notes, w.calls = tid, [], {}
            return recs
        if m == 'error':   # a notice (a retry among them), never a failure by itself: turn/completed failed is the failure
            e = p.get('error') if isinstance(p.get('error'), dict) else {}
            w.notes.append(' '.join(str(x) for x in (e.get('message'), e.get('codexErrorInfo')) if x))
            w.ev('progress', text='error notice: ' + clip(w.notes[-1], 200))
            return []
        if m not in ('item/started', 'item/completed', 'thread/tokenUsage/updated', 'turn/completed', 'turn/plan/updated'):
            return []
        if w.cur is None or tid != w.tid:
            return w.stray(' '.join(x for x in (m, it.get('type')) if x))
        k, iid = it.get('type'), it.get('id')
        if m == 'thread/tokenUsage/updated':   # one per model call (probe: turn 1 sent two, 17274 + 17325 = its total
            # 34599): `last` is that call's, so the turn's usage is their sum; `total` is the thread's
            w.cur.usage = plus(w.cur.usage, (p.get('tokenUsage') or {}).get('last'))
        elif m == 'turn/completed':
            t = p.get('turn') or {}
            st, e = t.get('status'), t.get('error') if isinstance(t.get('error'), dict) else {}
            text, use = w.cur.final(), usage(w.cur.usage, 'codex')
            if st == 'completed':
                return [w.end('ok', text, use, 'boundary')]
            why = ' '.join(str(x) for x in (e.get('message'), e.get('codexErrorInfo')) if x) or 'turn %s' % st
            if st != 'interrupted':
                w.ev('error', text=clip(why))
            return [w.end('cancelled' if st == 'interrupted' else 'error', '', use, 'boundary', '\n'.join(w.notes + [why]))]
        elif m == 'turn/plan/updated':
            w.ev('progress', text=clip('plan: ' + '; '.join('[%s] %s' % (x.get('status'), x.get('step'))
                                                            for x in p.get('plan') or [] if isinstance(x, dict)), 300))
        elif k == 'agentMessage' and m == 'item/completed':
            w.ev('text', text=it.get('text', ''))
            w.cur.text(iid, it.get('text', ''))
            w.cur.boundary()
        elif k == 'reasoning' and m == 'item/completed':
            said = ' '.join(it.get('summary') or it.get('content') or [])
            if said:
                w.ev('thought', text=said)
        elif k in CODEX_TOOLS:
            name, field = CODEX_TOOLS[k]
            name, inp = name or '%s/%s' % (it.get('server'), it.get('tool')), it.get(field)
            if iid not in w.calls:   # an item first seen at its completion still opens its call
                w.calls[iid] = inp
                w.tool_call(name, iid, inp)
            if m == 'item/completed':   # webSearch reports no status; its query may arrive only here
                bad = it.get('status') in ('failed', 'declined') or it.get('exitCode') not in (None, 0) or it.get('error')
                w.ev('tool_result', tool=name, call=iid, status='error' if bad else 'ok', input=None if w.calls[iid] else small(inp),
                     output=clip(it.get('aggregatedOutput') or it.get('result') or it.get('error')))
                for ch in it['changes'] if k == 'fileChange' and isinstance(it.get('changes'), list) else []:
                    if isinstance(ch, dict) and ch.get('path'):
                        w.ev('file', path=ch['path'], tool=name, call=iid)
                w.cur.tool()
        elif k not in ('userMessage', None) and m == 'item/completed':   # the input itself is its delivery marker
            w.ev('progress', text=clip('item %s' % k, 200))
        return []
