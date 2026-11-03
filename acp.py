#!/usr/bin/env python3
"""acp.py -- hands' JSON-RPC wire, and the event vocabulary every wire shares (Turn, the one answer
rule; usage(), the one usage rule; clip, small, now). Rpc is THE stdio client: newline-delimited JSON-RPC 2.0
to one agent process (ACP agents; codex's app-server, which answers without the jsonrpc field and accepts it,
codex-cli 0.160). AcpSeat is Rpc for ACP (Agent Client Protocol v1) agents (`grok agent stdio`, `opencode acp`),
never `authenticate` (each seat runs on its own CLI's login); launch() = recipe + handshake; translate() maps
updates to the engine's events. seat.py drives codex's app-server on Rpc itself.
    seat, why = launch(provider, handle, wrap, log, stderr_path)   # why: a launch typed before any turn
    seat.prompt(text, lambda u: emit(translate(u, n, t, wire)), done)  # done(res): translate(res, n, t, wire)
"""
import datetime, json, os, select, signal, subprocess, time

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


class Rpc(object):
    """One JSON-RPC agent process over stdio, in a session of its own, driven by its owner's select loop: no threads.
    What is sent waits in `out` until the agent's stdin takes it; each line its stdout gives is dispatched at once: an
    answer to the callback its request named, a notification to note(method, params), an agent's request to _answer.
    `env` = overrides on os.environ. `log(line, direction)` gets every JSON-RPC line verbatim, "in" (agent->client) or
    "out"."""

    def __init__(self, argv, cwd, env=None, log=None, stderr_path=None, note=None):
        self.cwd, self.log, self.note = cwd, log, note
        self._nid, self._pending, self._eof, self.out, self._tail = 0, {}, False, b"", b""
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
            msg = json.loads(line)
        except ValueError:
            return                                              # a malformed line is logged raw and skipped
        try:
            isinstance(msg, dict) and self._dispatch(msg)
        except Exception:                                       # a failing callback never stops the wire
            pass

    def _gone(self):
        """Nothing more will be answered: every waiting request gets msg None (a prompt -> incomplete)."""
        self._eof = True
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
            self.note(meth, params)

    def _answer(self, mid, meth, params):   # an agent->client request this client offers nothing for
        self.send({"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": "Method not found: " + str(meth)}})

    def send(self, obj):
        """Queue one JSON-RPC line for the agent, and give it what its stdin takes now."""
        if self._in is None:
            raise AcpError("agent stdin closed")
        line = json.dumps(obj, ensure_ascii=False)
        self._hook(line, "out")
        self.out += line.encode("utf-8") + b"\n"
        self.ready([], [self._in])

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
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(self.proc.pid, sig)
            except (ProcessLookupError, PermissionError):
                break                                           # the group is empty: nothing left to reap
            self._wait(grace)
        end = time.time() + grace                               # stdout's last lines, up to its EOF
        while not self._eof and time.time() < end:
            self.pump(min(0.1, end - time.time()))
        if not self._eof:                                       # an escaped grandchild holds the pipe open
            self._gone()
        self.proc.stdout.close()
        if self._err is not subprocess.DEVNULL:
            self._err.close()
        return self.proc.poll()


class AcpSeat(Rpc):
    """One ACP agent process and its session."""

    def __init__(self, argv, cwd, env=None, log=None, stderr_path=None):
        self.session_id, self.info, self.config = None, {}, []
        self._sink, self._turn = None, None                    # the prompt in flight
        Rpc.__init__(self, argv, cwd, env, log, stderr_path, self._update)

    def _update(self, meth, params):
        u = params.get("update")                                # session/update + grok _x.ai/session_notification
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


def recipe(provider, model, effort=None, exe=None):
    """The probe-proven launch of an ACP seat -> (argv, env overrides, [(config key, value)] for
    set_config after new_session); launch() then sets the seat's model when the agent lists it (never a substitute).
    grok: -m pins the model; --reasoning-effort never reaches an ACP session, so effort is a
      session option; GROK_DISABLE_AUTOUPDATER=1 stops the managed install's update check; GROK_CLAUDE_HOOKS_ENABLED and
      GROK_CURSOR_HOOKS_ENABLED 0: grok runs the hooks it finds in ~/.claude and ~/.cursor unless told not to (seat.py:
      NO PERSONAL HOOKS; its own hooks dir has no switch).
    opencode: no -m on `acp`, and 2.x takes no model from OPENCODE_CONFIG_CONTENT on ACP (2.0.22: the agent
      ran the global default), so the model is a session option like the effort; no --cwd either
      (2.0 dropped it): like every seat, the agent runs IN the seat's cwd, and session/new carries it. It runs on the
      user's own opencode store and login, as any opencode session does: no env of its own. Every
      ACP prompt carries a model VARIANT (default: the first listed) and ACP exposes it as the effort option: set it too."""
    if provider == "grok":
        return ([exe or "grok", "agent", "--no-leader", "--always-approve"] + (["-m", model] if model else []) + ["stdio"],
                {"GROK_DISABLE_AUTOUPDATER": "1", "GROK_CLAUDE_HOOKS_ENABLED": "0", "GROK_CURSOR_HOOKS_ENABLED": "0"},
                [("thought_level", effort)] if effort else [])
    if provider == "opencode":
        if not (model or "").partition("/")[2]:
            raise ValueError("model-required", "opencode seats name provider/model; the CLI default is the driver's own pool")
        return [exe or "opencode", "acp"], {}, [("thought_level", effort)] if effort else []
    raise ValueError("unknown-provider", "no ACP wire for %r (grok|opencode)" % provider)


def forget(provider, session, exe=None):
    """The argv that deletes an ABANDONED turn's stored session (its agent gone mid-turn: a stop, a crash, a kill), or None
    when the wire keeps nothing to resume. opencode 2.x: a killed turn keeps its execution claim, and the user's managed
    background service (`opencode serve --service`) resumes it at its next boot, in the seat's folder; a deleted session
    leaves it nothing (opencode 2.0.22). --standalone: its own private server, which never sweeps claims."""
    return [exe or "opencode", "session", "delete", session, "--standalone"] if provider == "opencode" and session else None


def launch(provider, h, wrap, log=None, stderr_path=None):
    """The seat handle h -> (the agent past its handshake, None), or (the agent or None, (status, reason, why)) when
    the launch is typed before any turn. All before any prompt: initialize, the session (a recorded one is resumed,
    never replayed), the model set when the agent runs another one it lists, the model and effort checks, the effort
    set explicitly to a value the (set) model lists (none only when it lists none). Never a substitute model. wrap(argv) puts
    the agent under the caller's reaper."""
    m, e = h.get("model"), h.get("effort")
    try:
        argv, env, sets = recipe(provider, m, e, h.get("cli"))
    except ValueError as x:
        return None, ("refused",) + x.args
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
        for k, v in sets:
            seat.set_config(k, v)
    except AcpError as x:
        return seat, ("error", "handshake", str(x))
    return seat, None
