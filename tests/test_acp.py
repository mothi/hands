#!/usr/bin/env python3
"""Hermetic tests for wires.py (its ACP and JSON-RPC client, the translators), zero model calls. A fake ACP agent (this file re-run with
--fake; hands.sh's selftest runs it as its grok and opencode stubs) replays recorded grok and
opencode transcripts (fixtures/*-acp-*.jsonl) request by request, mapping the recorded answer ids to
the live client's. Run: python3 tests/test_acp.py"""
import itertools, json, os, signal, subprocess, sys, tempfile, time, unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))   # the repo root: wires.py
import wires  # noqa: E402

# HERMETIC: a run inside a Claude Code session inherits the driver's inbox socket — dropped for the whole module, so no
# agent or child this suite starts can reach it
for _k in ("CLAUDE_CODE_MESSAGING_SOCKET", "CLAUDE_CODE_MESSAGING_TOKEN"):
    os.environ.pop(_k, None)

FX = os.environ.get("ACP_TRANSCRIPTS", os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures"))
# Expected event kinds per turn, derived by hand from each transcript's raw update order + the map.
GROK = ["turn_start progress*3 thought*16 text*2 progress*2 usage turn_end",
        "turn_start progress*2 thought*39 text*2 progress*2 usage turn_end",
        "turn_start progress*3 thought*42 text*15 progress*3 tool_call progress*5 tool_result usage turn_end",
        "turn_start progress*2 thought*42 text progress*2 usage turn_end"]
OPENCODE = ["turn_start text progress usage turn_end"] * 2 + [
    "turn_start thought*24 tool_call progress*2 tool_result progress usage turn_end",
    "turn_start thought*8 text progress usage turn_end"]


def upd(**u):
    return {"jsonrpc": "2.0", "method": "session/update", "params": {"sessionId": "s1", "update": u}}


def chunk(text):
    return {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": text}}


SYN = [  # synthetic: a malformed line, agent->client requests, file events, death mid-turn
    ("out", {"id": 1, "method": "initialize"}),
    ("in", {"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": 1, "agentCapabilities": {"loadSession": True}}}),
    ("out", {"id": 2, "method": "session/new"}),
    ("in", {"jsonrpc": "2.0", "id": 2, "result": {"sessionId": "s1"}}),
    ("out", {"id": 3, "method": "session/prompt"}),
    ("in", "this line is not json {"),
    ("in", {"jsonrpc": "2.0", "id": "p1", "method": "session/request_permission", "params": {
        "sessionId": "s1", "toolCall": {"toolCallId": "c1"}, "options": [
            {"optionId": "always", "kind": "allow_always"}, {"optionId": "once", "kind": "allow_once"},
            {"optionId": "no", "kind": "reject_once"}]}}),
    ("out", {"id": "p1"}),
    ("in", {"jsonrpc": "2.0", "id": "f1", "method": "fs/read_text_file", "params": {"sessionId": "s1", "path": "/etc/hosts"}}),
    ("out", {"id": "f1"}),
    ("in", upd(sessionUpdate="tool_call", toolCallId="c1", title="write", kind="edit", status="pending",
               locations=[{"path": "/w/a.txt"}])),
    ("in", upd(sessionUpdate="tool_call_update", toolCallId="c1", status="completed",
               content=[{"type": "diff", "path": "/w/a.txt", "oldText": None, "newText": "x"}])),
    ("in", upd(**chunk("Done"))),
    ("in", {"jsonrpc": "2.0", "id": 3, "result": {"stopReason": "end_turn"}}),
    ("out", {"id": 5, "method": "session/prompt"}),
    ("in", upd(sessionUpdate="tool_call", toolCallId="c2", title="bash", kind="execute", status="in_progress")),
]


def efforts(v):
    return [{"id": "effort", "category": "thought_level", "currentValue": v,
             "options": [{"value": x} for x in ("default", "low", "high")]}]


LATE = [  # opencode 2.0.22: the set's reply carries the OLD value, a config update ~20 ms later the new one
    ("out", {"id": 1, "method": "initialize"}),
    ("in", {"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": 1, "agentCapabilities": {}}}),
    ("out", {"id": 2, "method": "session/new"}),
    ("in", {"jsonrpc": "2.0", "id": 2, "result": {"sessionId": "s1", "configOptions": efforts("default")}}),
    ("out", {"id": 3, "method": "session/set_config_option"}),
    ("in", upd(sessionUpdate="config_option_update", configOptions=efforts("default"))),
    ("in", {"jsonrpc": "2.0", "id": 3, "result": {"configOptions": efforts("default")}}),
    ("sleep", 0.3),
    ("in", upd(sessionUpdate="config_option_update", configOptions=efforts("low"))),
    ("out", {"id": 4, "method": "session/set_config_option"}),   # a value that never takes: no update follows
    ("in", {"jsonrpc": "2.0", "id": 4, "result": {"configOptions": efforts("low")}}),
]


def fake(path, mode, rec):
    """The fake agent: split the transcript at each client ("out") message; on the client's k-th line
    (same method, else exit 3) replay segment k. mode: eof (exit 0 on stdin EOF), die (exit 1 after
    the last segment), stubborn (ignore EOF; a child in our group; a marker file on TERM), escape (as
    die, but a detached child holds our stdout open, so the client never sees EOF), slow (each prompt
    answers 2 s late), termcancel (on TERM or stdin EOF, answer the open prompt stopReason cancelled, then exit:
    opencode 2.x on a TERM), termerror (on TERM, answer the open prompt with the JSON-RPC error opencode 2.0.22 sends
    when its server died first, then exit). Every mode logs how it was ended, in order, to rec.ends:
    eof (its stdin closed), term."""
    segs, ids, log, last = [[None, []]], {}, open(rec, "a"), {}
    for r in (json.loads(line) for line in open(path)):   # a "sleep" row pauses the replay for msg seconds
        segs.append([r["msg"], []]) if r["dir"] == "out" else segs[-1][1].append(r)

    def mark(how):
        if rec != os.devnull:
            with open(rec + ".ends", "a") as f:
                f.write(how + "\n")

    def cancel():
        sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": last.get("id"), "result": {"stopReason": "cancelled"}}) + "\n")
        sys.stdout.flush()

    def term(*a):   # stubborn: a marker, then exit; termcancel: the cancelled answer, then exit; else TERM's own death
        mark("term")
        if mode == "termerror":
            sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": last.get("id"), "error": {
                "code": -32603, "message": "Internal error: OpenCode server is unavailable",
                "data": {"errorName": "ServerUnavailable"}}}) + "\n")
            sys.stdout.flush()
            os._exit(1)
        if mode in ("stubborn", "termcancel"):
            open(rec + ".term", "w").write("TERM") if mode == "stubborn" else cancel()
            os._exit(0)
        signal.signal(signal.SIGTERM, signal.SIG_DFL)
        os.kill(os.getpid(), signal.SIGTERM)
    signal.signal(signal.SIGTERM, term)
    if mode == "stubborn":
        open(rec + ".child", "w").write(str(subprocess.Popen(["sleep", "60"]).pid))
    if mode == "escape":
        open(rec + ".child", "w").write(str(subprocess.Popen(["sleep", "60"], start_new_session=True).pid))

    def emit(rows):
        for r in rows:
            m = r["msg"]
            if r["dir"] == "sleep":
                time.sleep(m)
                continue
            if isinstance(m, dict) and "method" not in m and m.get("id") in ids:
                m = dict(m, id=ids[m["id"]])
            sys.stdout.write((m if isinstance(m, str) else json.dumps(m)) + "\n")
            sys.stdout.flush()

    emit(segs[0][1])
    for want, rows in segs[1:]:
        line = sys.stdin.readline()
        if not line:
            mark("eof")
            mode == "termcancel" and cancel()
            return 0
        log.write(line)
        log.flush()
        got = json.loads(line)
        if got.get("method") != want.get("method"):
            return 3
        if "method" in want and "id" in want:
            ids[want["id"]] = got["id"]
        if got.get("method") == "session/prompt":
            last["id"] = got["id"]
        if mode == "slow" and want.get("method") == "session/prompt":
            time.sleep(2)
        emit(rows)
    if mode in ("die", "escape"):
        return 1
    for line in iter(sys.stdin.readline, ""):   # past the transcript: record, refuse any request
        log.write(line)
        m = json.loads(line)
        if "id" in m and "method" in m:
            emit([{"msg": {"jsonrpc": "2.0", "id": m["id"], "error": {"code": -32603, "message": "fake: past the transcript"}}}])
    mark("eof")
    while mode == "stubborn":
        time.sleep(1)
    return 0


def rl(events):  # run-length kinds: "turn_start thought*16 text*2 ..."
    runs = ((k, len(list(g))) for k, g in itertools.groupby(e["kind"] for e in events))
    return " ".join(k if n == 1 else "%s*%d" % (k, n) for k, n in runs)


class AcpTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="acp-test-")
        self.addCleanup(self.tmp.cleanup)
        self.rec = os.path.join(self.tmp.name, "received.jsonl")

    def seat(self, transcript, mode="eof"):
        self.assertTrue(os.path.isfile(transcript), "missing transcript %s (set ACP_TRANSCRIPTS)" % transcript)
        with open(transcript) as f:   # the recorded client cancelled turn 3; this client never cancels (stop ends
            rows = [json.loads(line) for line in f]   # a seat), so the fake plays that turn to its own end
        transcript = os.path.join(self.tmp.name, "nocancel.jsonl")
        with open(transcript, "w") as f:
            f.writelines(json.dumps(r) + "\n" for r in rows
                         if not (r["dir"] == "out" and isinstance(r["msg"], dict) and r["msg"].get("method") == "session/cancel"))
        s = wires.AcpSeat([sys.executable, os.path.abspath(__file__), "--fake", transcript, mode, self.rec], self.tmp.name)
        self.addCleanup(s.close, 0.5)
        return s

    def turn(self, seat, n, text, wire=None):
        evs, box = [], []
        seat.prompt(text, lambda u: evs.extend(wires.translate(u, n, None, wire)), box.append)
        end = time.time() + 30
        while not box and time.time() < end:   # the seat's owner runs the loop: here, this test
            seat.pump(0.5)
        res = box[0]
        return res, evs + wires.translate(res, n, None, wire)

    def test_grok_transcript_round_trips_and_translates(self):
        seat = self.seat(FX + "/grok-acp-sample.jsonl")
        self.assertTrue(seat.initialize()["loadSession"])
        self.assertEqual(seat.new_session(), "01a0d35c-6d25-77c1-b7bd-1a71c642327e")
        self.assertEqual((seat.current("model"), seat.current("thought_level")), ("grok-4.6", "xhigh"))
        self.assertEqual((seat.values("thought_level"), seat.values("nope")), (["xhigh", "high", "medium", "low"], None))
        self.assertEqual(seat.set_config("thought_level", "low"), "low")   # grok's CLI effort flag never reaches ACP
        runs = [self.turn(seat, n, t, "grok") for n, t in ((1, "PONG"), (2, "said?"), (3, "sleep"), (4, "AFTER"))]
        self.assertEqual([rl(evs) for _, evs in runs], GROK)
        self.assertEqual([(e[-1]["status"], e[-1]["text"], e[-1]["final"], e[-1]["session"]) for _, e in runs],
                         [(s, t, "boundary", "01a0d35c-6d25-77c1-b7bd-1a71c642327e")
                          for s, t in (("ok", "PONG"), ("ok", "PONG"), ("cancelled", ""), ("ok", "AFTER"))])
        closed = [(e["call"], e["status"], e.get("synthetic")) for e in runs[2][1] if e["kind"] == "tool_result"]
        self.assertEqual(closed, [("call-44db5404-56c6-4801-b4cc-522d9916cad1-0", "cancelled", True)])
        use = [e for e in runs[2][1] if e["kind"] == "usage"][0]           # grok's usage stays real on cancel
        self.assertEqual((use["usage"]["in"], use["usage"]["cache_read"], use["cost_usd"]), (17746, 2816, 0.01272144))
        self.assertEqual(seat.close(), 0)

    def test_opencode_transcript_round_trips_and_translates(self):
        seat = self.seat(FX + "/opencode-acp-sample.jsonl")
        self.assertTrue(seat.initialize()["loadSession"])
        self.assertEqual((seat.new_session(), seat.current("model"), seat.current("thought_level")),
                         ("ses_f2ca45c73ffe55AqfFsvtJPq2O", "opencode-go/deepseek-v4.1-flash", "low"))
        runs = [self.turn(seat, n, t, "opencode") for n, t in ((1, "PONG"), (2, "said?"), (3, "sleep"), (4, "AFTER"))]
        self.assertEqual([rl(evs) for _, evs in runs], OPENCODE)
        self.assertEqual([(e[-1]["status"], e[-1]["text"], e[-1]["final"]) for _, e in runs],
                         [("ok", "PONG", "boundary"), ("ok", "PONG", "boundary"), ("cancelled", "", "boundary"), ("ok", "AFTER", "boundary")])
        uses = [[e for e in evs if e["kind"] == "usage"][0] for _, evs in runs]
        self.assertEqual(uses[0]["usage"], {"in": 10740, "out": 3, "cache_read": 1792})   # input excludes the cache
        self.assertEqual((uses[2].get("usage_unknown"), "usage" in uses[2]), (True, False))  # zeros on cancel: unknown
        closed = [(e["status"], e.get("synthetic")) for e in runs[2][1] if e["kind"] == "tool_result"]
        self.assertEqual(closed, [("ok", None)])                            # opencode closes its own aborted tool
        late = [e["input"].get("command") for e in runs[2][1] if e["kind"] == "progress" and isinstance(e.get("input"), dict)]
        self.assertEqual(late[:1], ["sleep 30"])                            # an input sent late (opencode) rides the update
        self.assertEqual(seat.close(), 0)

    def test_agent_requests_malformed_line_and_death(self):
        path = os.path.join(self.tmp.name, "syn.jsonl")
        with open(path, "w") as f:
            f.writelines(json.dumps({"dir": d, "msg": m}) + "\n" for d, m in SYN)
        seat = self.seat(path, "die")
        seat.initialize()
        seat.new_session()
        res, evs = self.turn(seat, 1, "write a.txt")                        # past a malformed line: the reader lived on
        self.assertEqual(rl(evs), "turn_start progress tool_call file tool_result file text turn_end")
        self.assertEqual((evs[-1]["status"], evs[-1]["text"], evs[-1]["final"]), ("ok", "Done", "after-last-tool"))
        self.assertEqual({e["path"] for e in evs if e["kind"] == "file"}, {"/w/a.txt"})
        with open(self.rec) as f:
            sent = [json.loads(line) for line in f]
        self.assertEqual(sent[3]["result"], {"outcome": {"outcome": "selected", "optionId": "once"}})  # allow_once first
        self.assertEqual(sent[4]["error"]["code"], -32601)                   # fs/* was never offered
        res, evs = self.turn(seat, 2, "the agent dies mid-turn")
        self.assertEqual(rl(evs), "turn_start tool_call tool_result error turn_end")
        self.assertEqual((evs[-1]["status"], evs[2]["status"], evs[2]["call"]), ("incomplete", "cancelled", "c2"))
        self.assertEqual(seat.close(), 1)

    def test_close_frees_a_prompt_whose_pipe_an_escaped_child_holds(self):
        path = os.path.join(self.tmp.name, "esc.jsonl")
        with open(path, "w") as f:
            f.writelines(json.dumps({"dir": d, "msg": m}) + "\n" for d, m in SYN[:4] + SYN[-2:])
        seat = self.seat(path, "escape")
        seat.initialize()
        seat.new_session()
        box, end = [], time.time() + 10
        seat.prompt("x", None, box.append)
        while seat.proc.poll() is None and time.time() < end:
            seat.pump(0.2)
        self.assertIsNotNone(seat.proc.poll())                # the agent is gone, its detached child is not
        for _ in range(5):
            seat.pump(0.2)
        self.assertEqual(box, [], "EOF arrived: the escape fixture did not hold the pipe")
        seat.close(grace=0.5)
        with open(self.rec + ".child") as f:
            os.kill(int(f.read()), signal.SIGKILL)
        self.assertIn("incomplete", box[0] if box else {})

    def test_a_big_prompt_while_the_agent_floods_its_stdout(self):   # falsification: a blocking stdin write deadlocks
        rows = SYN[:4] + [("in", upd(**chunk("y" * 1000)))] * 2000 + [   # 2 MB the agent writes before it reads on
            ("out", {"id": 3, "method": "session/prompt"}),
            ("in", {"jsonrpc": "2.0", "id": 3, "result": {"stopReason": "end_turn"}})]
        path = os.path.join(self.tmp.name, "flood.jsonl")
        with open(path, "w") as f:
            f.writelines(json.dumps({"dir": d, "msg": m}) + "\n" for d, m in rows)
        seat = self.seat(path)
        seat.initialize()
        seat.new_session()

        def stuck(*a):
            raise AssertionError("deadlocked: the client blocked writing while the agent blocked writing")
        signal.signal(signal.SIGALRM, stuck)
        signal.alarm(20)
        try:
            res, evs = self.turn(seat, 1, "x" * (1 << 20))   # a 1 MB prompt: more than the pipe holds
        finally:
            signal.alarm(0)
        self.assertEqual(evs[-1]["status"], "ok")

    def test_close_reads_to_eof_before_it_gives_up_on_an_answer(self):   # falsification: skip close's last read
        # the agent exits at stdin EOF; a child it left (a session of its own: no TERM reaches it) answers 0.3 s later
        child = 'import time; time.sleep(0.3); print(%r, flush=True)' % json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"ok": 1}})
        agent = ('import subprocess, sys; sys.stdin.read(); subprocess.Popen([sys.executable, "-c", %r], '
                 'start_new_session=True)' % child)
        rpc, box = wires.Rpc([sys.executable, "-c", agent], self.tmp.name), []
        rpc.request("ping", {}, box.append)
        rpc.close(2)
        self.assertEqual(box, [{"jsonrpc": "2.0", "id": 1, "result": {"ok": 1}}])

    def test_close_terms_the_process_group(self):
        path = os.path.join(self.tmp.name, "init.jsonl")
        with open(path, "w") as f:
            f.write(json.dumps({"dir": "out", "msg": {"id": 1, "method": "initialize"}}) + "\n")
            f.write(json.dumps({"dir": "in", "msg": {"jsonrpc": "2.0", "id": 1, "result": {"agentCapabilities": {}}}}) + "\n")
        seat = self.seat(path, "stubborn")
        seat.initialize()
        with open(self.rec + ".child") as f:
            child, t0 = int(f.read()), time.time()
        seat.close(grace=0.5)                                  # this agent ignores stdin EOF: only TERM ends it
        self.assertTrue(os.path.exists(self.rec + ".term"), "the agent never saw SIGTERM")
        for _ in range(50):
            try:
                os.kill(child, 0)
            except ProcessLookupError:
                break
            time.sleep(0.1)
        else:
            self.fail("the agent's child %d outlived close()" % child)
        self.assertLess(time.time() - t0, 5)

    def test_translate_rules(self):
        def tr(u):
            return wires.translate(u, 7, t="T")
        self.assertEqual(wires.usage({"inputTokens": 20429, "outputTokens": 18, "totalTokens": 20447, "cachedReadTokens": 128,
                                    "cacheCreationTokens": 0, "reasoningTokens": 16}, "grok"),       # cache inside input
                         {"in": 20301, "out": 18, "cache_read": 128, "cache_write": 0, "reasoning": 16})
        self.assertEqual(wires.usage({"inputTokens": 197, "outputTokens": 3, "totalTokens": 12764, "thoughtTokens": 20,
                                    "cachedReadTokens": 12544}, "opencode"),                        # reasoning apart from output
                         {"in": 197, "out": 23, "cache_read": 12544, "reasoning": 20})
        self.assertEqual(wires.usage({"input_tokens": 10, "cache_read_input_tokens": 21806, "cache_creation_input_tokens": 8449,
                                    "output_tokens": 43, "output_tokens_details": {"thinking_tokens": 36}}, "claude"),
                         {"in": 10, "out": 43, "cache_read": 21806, "cache_write": 8449, "reasoning": 36})
        self.assertEqual(wires.usage({"input_tokens": 10, "cached_input_tokens": 4, "output_tokens": 3, "reasoning_output_tokens": 1}, "codex"),
                         {"in": 6, "out": 3, "cache_read": 4, "reasoning": 1})
        self.assertIsNone(wires.usage({"totalTokens": 3}, "opencode"))                       # no field it maps: unknown
        for stop, want in (("end_turn", "ok"), ("refusal", "error"), ("max_tokens", "truncated"), (None, "error"),
                           ("max_turn_requests", "truncated"), ("cancelled", "cancelled"), ("bogus", "error")):
            end = tr({"_client": "turn_end", "stopReason": stop, "answer": "A"})[-1]
            self.assertEqual((end["kind"], end["status"], end["text"]), ("turn_end", want, "A" if want == "ok" else ""))
        self.assertEqual([(e["kind"], e.get("status")) for e in tr({"_client": "turn_end", "error": {"code": -32000, "message": "auth"}})],
                         [("error", None), ("turn_end", "error")])
        self.assertEqual([e["kind"] for e in tr({"sessionUpdate": "tool_call", "toolCallId": "x", "kind": "execute",
                                                 "locations": [{"path": "/w"}]})], ["tool_call"])   # a shell's cwd is no file
        self.assertEqual(tr({"sessionUpdate": "available_commands_update"}), [])
        t = wires.Turn()
        t.text(None, "I'll run it.")
        t.tool()
        self.assertEqual(t.final(), "")                                     # narration before a tool is never the answer
        t = wires.Turn()
        t.text("m1", "the answer")
        t.text("m2", " \n")
        self.assertEqual(t.final(), "")                                     # a blank last response fails closed: no promotion
        self.assertEqual(wires.opencode_command({"model": "opencode-go/deepseek-v4.1-flash", "effort": "low"}),
                         (["opencode", "acp"], {}))   # 2.0 has no --cwd, 2.x takes no model from OPENCODE_CONFIG_CONTENT
        # on ACP (the model and the effort are session options: launch sets them), and the seat runs on the user's own
        # store: no env of its own
        self.assertEqual(wires.forget("opencode", "ses_1", "/x/opencode"), ["/x/opencode", "session", "delete", "ses_1", "--standalone"])
        self.assertEqual((wires.forget("grok", "s1"), wires.forget("opencode", None)), (None, None))   # grok keeps no claim
        self.assertEqual(wires.grok_command({"model": "grok-4.6"})[0][-3:], ["-m", "grok-4.6", "stdio"])
        self.assertEqual(wires.grok_command({})[0][-2:], ["--always-approve", "stdio"])   # model-less: the CLI default
        self.assertRaises(ValueError, wires.opencode_command, {"model": "deepseek"})

    def test_set_config_waits_for_a_late_config_update(self):   # falsification: drop set_config's wait
        path = os.path.join(self.tmp.name, "late.jsonl")
        with open(path, "w") as f:
            f.writelines(json.dumps({"dir": d, "msg": m}) + "\n" for d, m in LATE)
        seat = self.seat(path)
        seat.initialize()
        seat.new_session()
        self.assertEqual(seat.set_config("thought_level", "low"), "low")   # the reply said default; the update took it
        t0 = time.time()
        self.assertRaises(wires.AcpError, seat.set_config, "thought_level", "high")   # never confirmed: still refused
        self.assertLess(time.time() - t0, 5)                                # and the wait is bounded

    def test_suite_env_carries_no_driver_inbox(self):   # falsification: drop the pop, run with a fake socket exported
        self.assertFalse({"CLAUDE_CODE_MESSAGING_SOCKET", "CLAUDE_CODE_MESSAGING_TOKEN"} & set(os.environ))


if __name__ == "__main__":
    if sys.argv[1:2] == ["--fake"]:
        sys.exit(fake(*sys.argv[2:5]))
    result = unittest.main(exit=False, verbosity=2).result
    bad = len(result.failures) + len(result.errors)
    print("test_acp: %d passed, %d failed" % (result.testsRun - bad, bad))
    sys.exit(1 if bad else 0)
