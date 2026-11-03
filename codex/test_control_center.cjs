"use strict";

// Source-only widget contract tests. No browser, host, network, npm, or real timers.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { test } = require("node:test");
const html = fs.readFileSync(path.join(__dirname, "../plugins/agent-efficiency/assets/control_center.html"), "utf8");
const source = html.match(/<script>([\s\S]*?)<\/script>/)[1];
const HANG = Symbol("no response");
const TOKEN = "0123456789abcdef0123456789abcdef";
const toolResult = result => ({ structuredContent: { result }, isError: false });
// MCP Apps uses appInfo, unlike core MCP's clientInfo. Validate before replying
// so a malformed request cannot appear to mount a working widget in this host.
function validUiInitialize(params) {
  return params && typeof params === "object" && !Array.isArray(params)
    && params.appInfo && typeof params.appInfo.name === "string" && params.appInfo.name.length > 0
    && typeof params.appInfo.version === "string" && params.appInfo.version.length > 0
    && typeof params.protocolVersion === "string" && params.protocolVersion.length > 0
    && params.appCapabilities && typeof params.appCapabilities === "object" && !Array.isArray(params.appCapabilities);
}
function center(mode = "balanced") {
  return {
    version: "0.5.0", selected_mode: mode, estimate_scope: "selected", modes: [],
    task_summary: "Synthetic task",
    descriptions: ["throughput", "balanced", "stretch"].map(mode => ({ mode, label: mode[0].toUpperCase() + mode.slice(1), objective: "Objective", behavior: "Behavior", tradeoff: "Tradeoff" })),
  };
}
function populatedCenter(mode = "balanced") {
  const result = center(mode);
  result.modes = result.descriptions.map(({ mode }, index) => ({
    mode, eta_minutes: { low: 1, high: 3 }, estimated_tokens: { low: 100, high: 300 },
    confidence: "low", confidence_basis: "Synthetic estimate",
    capacity: index === 0 ? { available: false } : {
      available: true, confidence: "low", estimated_remaining: { low: 2, high: 4 },
      constraining_window: "synthetic", windows: [{
        limit_id: "synthetic", used_percent: 25, forecast_verdict: "on-pace",
      }],
    },
  }));
  return result;
}
class Element {
  constructor(tag = "div") {
    this.tagName = tag; this.children = []; this.dataset = {}; this.attributes = {};
    this.className = ""; this.textContent = ""; this.disabled = false; this.value = "";
    this.classList = { toggle: (name, enabled) => {
      const classes = new Set(this.className.split(" ").filter(Boolean));
      enabled ? classes.add(name) : classes.delete(name); this.className = [...classes].join(" ");
    } };
  }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.children = children; }
  setAttribute(name, value) { this.attributes[name] = value; }
}
function harness(bridge, options = {}) {
  const initial = Object.hasOwn(options, "initial") ? options.initial : center();
  const elements = new Map([...html.matchAll(/id="([^"]+)"/g)].map(([, id]) => [id, new Element(["use", "save", "recalculate"].includes(id) ? "button" : id === "scope" ? "select" : "div")]));
  const listeners = new Map(); const timers = new Map(); let timerId = 0; let clock = 0;
  const advance = milliseconds => {
    const target = clock + milliseconds;
    for (;;) {
      const next = [...timers].filter(([, timer]) => timer.at <= target).sort((a, b) => a[1].at - b[1].at)[0];
      if (!next) break;
      const [id, timer] = next; clock = timer.at; timers.delete(id); timer.callback();
    }
    clock = target;
  };
  const calls = []; const posts = []; const followups = []; const wireMessages = []; const widgetStates = [];
  const controls = () => [elements.get("recalculate"), elements.get("save"), elements.get("use"), elements.get("scope"), ...elements.get("cards").children];
  const defaultTool = (name, args) => {
    if (name === "show_control_center") return toolResult(center(args.selected_mode));
    if (name === "update_settings") return toolResult({ default_mode: args.default_mode });
    if (name === "select_task_mode") return toolResult({ mode: args.mode, handoff_token: TOKEN });
    throw Error("Unexpected tool");
  };
  const invokeTool = (name, args) => { calls.push({ name, args }); return (options.onTool || defaultTool)(name, args); };
  const invokeFollowup = value => { followups.push(value); return options.onFollowup ? options.onFollowup(value) : {}; };
  const emit = (type, event = {}) => { for (const listener of [...(listeners.get(type) || [])]) listener(event); };
  const respond = (id, result, extra = {}) => emit("message", { source: window.parent, data: { jsonrpc: "2.0", id, result, ...extra } });
  const parent = { postMessage(message, origin) {
    assert.equal(origin, "*"); wireMessages.push(message);
    if(message.method === "ui/initialize") {
      const params = options.transformInitialize ? options.transformInitialize(message.params) : message.params;
      if(!validUiInitialize(params)) {
        emit("message", {source:parent,data:{jsonrpc:"2.0",id:message.id,error:{code:-32602,message:"Invalid UI initialization"}}});
        return;
      }
      if(bridge==="native"&&!options.sharedSupport){
        emit("message",{source:parent,data:{jsonrpc:"2.0",id:message.id,error:{code:-32601,message:"Legacy host"}}});return;
      }
      if(!options.holdInitialize)respond(message.id, {protocolVersion:options.protocolVersion || "2026-01-26",hostCapabilities:{},hostInfo:{name:"synthetic",version:"1"}});
      return;
    }
    if(message.method === "ui/notifications/initialized") {
      if(initial)emit("message", {source:parent,data:{jsonrpc:"2.0",method:"ui/notifications/tool-result",params:{structuredContent:initial}}});
      return;
    }
    if(message.id === undefined)return;
    posts.push(message);
    if (options.postThrows) throw Error("Synthetic post failure");
    let result;
    try { result = message.method === "tools/call" ? invokeTool(message.params.name, message.params.arguments) : invokeFollowup(message.params); }
    catch (_) { emit("message", { source: parent, data: { jsonrpc: "2.0", id: message.id, error: { code: -1, message: "Synthetic failure" } } }); return; }
    if (result === HANG) return;
    // Deliberately synchronous to catch register-after-post races.
    if (!result?.then) respond(message.id, result);
    else result.then(value => respond(message.id, value), () => emit("message", { source: parent, data: { jsonrpc: "2.0", id: message.id, error: {} } }));
  } };
  const window = {
    parent,
    addEventListener(type, callback) { if (!listeners.has(type)) listeners.set(type, new Set()); listeners.get(type).add(callback); },
    removeEventListener(type, callback) { listeners.get(type)?.delete(callback); },
  };
  if (bridge === "native") window.openai = {
    toolOutput: initial,
    widgetState: options.widgetState,
    callTool(name, args) { const value = invokeTool(name, args); return value === HANG ? new Promise(() => {}) : value; },
    sendFollowUpMessage(value) { const result = invokeFollowup(value); return result === HANG ? new Promise(() => {}) : result; },
    setWidgetState(value) { widgetStates.push(value); },
  };
  const context = vm.createContext({
    window,
    document: {
      body: {scrollHeight:480,scrollWidth:800},
      getElementById: id => elements.get(id), createElement: tag => new Element(tag),
      querySelectorAll: selector => selector === ".card" ? elements.get("cards").children : controls(),
    },
    setTimeout(callback, delay) { const id = ++timerId; timers.set(id, { callback, delay, at: clock + delay }); return id; },
    clearTimeout(id) { timers.delete(id); },
  });
  vm.runInContext(source, context, { filename: "control_center.html" });
  return {
    elements, controls, calls, posts, followups, timers, window, emit, respond, advance, wireMessages, widgetStates,
    click: id => elements.get(id).onclick(),
    status: () => elements.get("status").textContent,
    state: () => JSON.parse(vm.runInContext("JSON.stringify({pending:pending.size,inFlight:inFlight.size,busy,closed,selected,bridgeReady,sharedBridge})", context)),
    expire() { if (timers.size) advance(Math.max(...[...timers.values()].map(timer => timer.at)) - clock); },
  };
}
async function flush() { for (let index = 0; index < 12; index++) await Promise.resolve(); }
function clean(h) { assert.equal(h.state().pending, 0); assert.equal(h.state().inFlight, 0); assert.equal(h.timers.size, 0); assert.equal(h.state().busy, false); }

for (const bridge of ["native", "postMessage"]) {
  test(`${bridge}: populated estimates render ETA, tokens, and capacity`, async () => {
    const h = harness(bridge, { onTool: () => toolResult(populatedCenter("stretch")) });
    await h.click("recalculate");
    const cards = h.elements.get("cards").children;
    const metricValues = card => card.children.at(-1).children.map(metric => metric.children[1].textContent);
    assert.deepEqual(metricValues(cards[0]).slice(0, 2), ["1–3 min", "100–300"]);
    assert(metricValues(cards[0]).includes("UNCALIBRATED"));
    assert(metricValues(cards[1]).includes("2–4"));
    assert(metricValues(cards[1]).includes("25% used"));
    assert(metricValues(cards[1]).includes("on pace"));
    assert.equal(h.state().selected, "stretch");
    assert.equal(h.status(), "Estimates updated."); clean(h);
  });
  for (const [field, corrupt] of [
    ["ETA", data => { data.modes[1].eta_minutes = null; }],
    ["tokens", data => { delete data.modes[2].estimated_tokens.high; }],
    ["capacity", data => { data.modes[2].capacity.windows = {}; }],
  ]) {
    test(`${bridge}: malformed later-card ${field} preserves the previous complete view`, async () => {
      const invalid = populatedCenter("stretch"); invalid.task_summary = "Must not replace the previous task";
      corrupt(invalid);
      const h = harness(bridge, { initial: populatedCenter(), onTool: () => toolResult(invalid) });
      const previousCards = [...h.elements.get("cards").children];
      await h.click("recalculate");
      assert.equal(h.elements.get("task").textContent, "Synthetic task");
      assert.equal(h.state().selected, "balanced");
      previousCards.forEach((card, index) => assert.equal(h.elements.get("cards").children[index], card));
      assert.match(h.status(), /invalid response/);
      assert(h.controls().every(element => !element.disabled)); clean(h);
    });
  }
  for (const [name, initial] of [["missing", null], ["malformed", {}]]) {
    test(`${bridge}: ${name} initial center blocks mutations but permits recalculation recovery`, async () => {
      const h = harness(bridge, { initial });
      assert(h.elements.get("use").disabled); assert(h.elements.get("save").disabled);
      assert.equal(h.elements.get("recalculate").disabled, false);
      await h.click("use"); await h.click("save"); assert.equal(h.calls.length, 0);
      await h.click("recalculate");
      assert.equal(h.status(), "Estimates updated."); assert.equal(h.elements.get("use").disabled, false); assert.equal(h.elements.get("save").disabled, false); clean(h);
    });
  }
  test(`${bridge}: busy blocks repeated actions and mode changes, then restores all controls`, async () => {
    let release; const h = harness(bridge, { onTool: () => new Promise(resolve => { release = resolve; }) });
    const action = h.click("recalculate"); await flush();
    assert(h.controls().every(element => element.disabled));
    await h.click("save"); await h.click("use"); await h.click("recalculate");
    h.elements.get("cards").children[2].onclick();
    assert.equal(h.calls.length, 1); assert.equal(h.state().selected, "balanced");
    release(toolResult(center())); await action;
    assert.equal(h.status(), "Estimates updated."); assert(h.controls().every(element => !element.disabled)); clean(h);
  });
  test(`${bridge}: successful save validates returned default`, async () => {
    const h = harness(bridge); await h.click("save");
    assert.equal(h.status(), "Balanced is now the default."); assert.equal(h.calls[0].args.default_mode, "balanced"); clean(h);
  });
  for (const [name, onTool] of [
    ["tool isError", () => ({ isError: true, content: [{ type: "text", text: "private diagnostic" }] })],
    ["bridge exception", () => { throw Error("private diagnostic"); }],
    ["malformed response", () => null],
  ]) {
    test(`${bridge}: ${name} does not announce success or expose raw errors`, async () => {
      const h = harness(bridge, { onTool }); await h.click("save");
      assert.match(h.status(), /not confirmed/); assert.doesNotMatch(h.status(), /now the default|private diagnostic/);
      assert(h.controls().every(element => !element.disabled)); clean(h);
    });
  }
  test(`${bridge}: invalid recalculation data cannot announce updated estimates`, async () => {
    const h = harness(bridge, { onTool: () => toolResult({}) }); await h.click("recalculate");
    assert.match(h.status(), /invalid response/); assert.doesNotMatch(h.status(), /Estimates updated/); clean(h);
  });
  test(`${bridge}: mismatched saved default is not reported as saved`, async () => {
    const h = harness(bridge, { onTool: () => toolResult({ default_mode: "stretch" }) }); await h.click("save");
    assert.match(h.status(), /invalid response/); clean(h);
  });
  test(`${bridge}: timeout restores controls, clears bookkeeping, and never retries`, async () => {
    const h = harness(bridge, { onTool: () => HANG }); const action = h.click("save"); await flush();
    assert.equal(h.timers.size, 1);
    assert.deepEqual([...h.timers.values()].map(timer => timer.delay), [15000]);
    const pendingStatus = h.status();
    h.advance(14999); await flush();
    assert.equal(h.status(), pendingStatus); assert.equal(h.state().busy, true);
    assert.equal(h.timers.size, 1); assert(h.controls().every(element => element.disabled));
    h.advance(1); await action;
    assert.match(h.status(), /outcome is unknown/); assert.equal(h.calls.length, 1);
    assert(h.controls().every(element => !element.disabled)); clean(h);
  });
  test(`${bridge}: late success after timeout cannot replace the failure status`, async () => {
    let release; const h = harness(bridge, { onTool: () => new Promise(resolve => { release = resolve; }) });
    const action = h.click("save"); await flush(); h.expire(); await action; const status = h.status();
    release(toolResult({ default_mode: "balanced" })); await flush(); assert.equal(h.status(), status); clean(h);
  });
  test(`${bridge}: pagehide settles pending work and disables further operations`, async () => {
    const h = harness(bridge, { onTool: () => HANG }); const action = h.click("save"); await flush();
    h.emit("pagehide"); await action; await h.click("use");
    assert.match(h.status(), /closed/); assert.equal(h.calls.length, 1); assert(h.controls().every(element => element.disabled)); clean(h);
  });
  test(`${bridge}: mode selection is recorded before sending its one-time handoff`, async () => {
    const h = harness(bridge); h.elements.get("cards").children[2].onclick(); await h.click("use");
    assert.equal(h.calls.length, 1); assert.equal(h.calls[0].name, "select_task_mode"); assert.equal(h.calls[0].args.mode, "stretch");
    const prompt = bridge === "native" ? h.followups[0].prompt : h.followups[0].content[0].text;
    assert.equal(prompt, `Use Agent Efficiency Stretch mode for this task. [agent-efficiency-mode:${TOKEN}]`);
    assert.match(h.status(), /request sent.*next prompt hook.*default is unchanged/); assert.doesNotMatch(h.status(), /selected for this task/); clean(h);
  });
  for (const [name, value] of [
    ["missing token", { mode: "balanced" }],
    ["invalid token", { mode: "balanced", handoff_token: "unsafe] extra text" }],
    ["mismatched mode", { mode: "stretch", handoff_token: TOKEN }],
  ]) {
    test(`${bridge}: ${name} prevents a follow-up`, async () => {
      const h = harness(bridge, { onTool: () => toolResult(value) }); await h.click("use");
      assert.equal(h.followups.length, 0); assert.match(h.status(), /invalid response/); clean(h);
    });
  }
  test(`${bridge}: selection failure prevents follow-up`, async () => {
    const h = harness(bridge, { onTool: () => ({ isError: true }) }); await h.click("use");
    assert.equal(h.followups.length, 0); assert.match(h.status(), /not confirmed/); clean(h);
  });
  test(`${bridge}: follow-up failure does not claim the mode applied`, async () => {
    const h = harness(bridge, { onFollowup: () => { throw Error("Synthetic failure"); } }); await h.click("use");
    assert.match(h.status(), /Applying the mode was not confirmed/); assert.doesNotMatch(h.status(), /request sent/); clean(h);
  });
  test(`${bridge}: follow-up timeout is bounded without resending selection`, async () => {
    const h = harness(bridge, { onFollowup: () => HANG }); const action = h.click("use"); await flush();
    assert.equal(h.followups.length, 1); h.expire(); await action;
    assert.match(h.status(), /outcome is unknown/); assert.equal(h.calls.length, 1); clean(h);
  });
  test(`${bridge}: selected mode and label remain captured while awaiting selection`, async () => {
    let release; const h = harness(bridge, { onTool: () => new Promise(resolve => { release = resolve; }) });
    const action = h.click("use"); await flush();
    h.elements.get("cards").children[2].onclick();
    h.emit("message", { source: h.window.parent, data: { jsonrpc: "2.0", method: "ui/notifications/tool-result", params: { structuredContent: center("stretch") } } });
    release(toolResult({ mode: "balanced", handoff_token: TOKEN })); await action;
    const prompt = bridge === "native" ? h.followups[0].prompt : h.followups[0].content[0].text;
    assert.match(prompt, /Balanced mode/); assert.match(h.status(), /^Balanced request sent/); clean(h);
  });
}
test("postMessage: ignores wrong sources, unknown IDs, wrong protocol, and malformed envelopes", async () => {
  const h = harness("postMessage", { onTool: () => HANG }); const action = h.click("save"); const id = h.posts[0].id;
  h.emit("message", { source: {}, data: { jsonrpc: "2.0", id, result: toolResult({ default_mode: "balanced" }) } });
  h.respond(id + 1, {}); h.respond(id, {}, { jsonrpc: "1.0" });
  assert.equal(h.state().pending, 1); assert.equal(h.state().busy, true);
  h.respond(id, toolResult({ default_mode: "balanced" })); await action; clean(h);
});
test("postMessage: known response with both result and error is rejected and cleaned up", async () => {
  const h = harness("postMessage", { onTool: () => HANG }); const action = h.click("save");
  h.respond(h.posts[0].id, toolResult({ default_mode: "balanced" }), { error: {} }); await action;
  assert.match(h.status(), /invalid response/); clean(h);
});
test("postMessage: known response without result or error is rejected", async () => {
  const h = harness("postMessage", { onTool: () => HANG }); const action = h.click("save");
  h.emit("message", { source: h.window.parent, data: { jsonrpc: "2.0", id: h.posts[0].id } }); await action;
  assert.match(h.status(), /invalid response/); clean(h);
});
test("postMessage: send exception clears timer and pending request", async () => {
  const h = harness("postMessage", { postThrows: true }); await h.click("save");
  assert.match(h.status(), /not confirmed/); clean(h);
});
test("postMessage: error tool-result notification cannot load or change a selection", () => {
  const h = harness("postMessage", { initial: null });
  h.emit("message", { source: h.window.parent, data: { jsonrpc: "2.0", method: "ui/notifications/tool-result", params: { isError: true, structuredContent: center("stretch") } } });
  assert(h.elements.get("use").disabled); assert(h.elements.get("save").disabled);
  assert.match(h.status(), /not confirmed/); clean(h);
});
test("native: closing before callback dispatch prevents a new host mutation", async () => {
  const h = harness("native"); const action = h.click("save"); h.emit("pagehide"); await action;
  assert.equal(h.calls.length, 0); clean(h);
});

test("postMessage: initialization precedes host result delivery and size notification", () => {
  const h = harness("postMessage");
  assert.equal(h.wireMessages[0].method, "ui/initialize");
  assert.equal(h.wireMessages[1].method, "ui/notifications/initialized");
  assert(h.wireMessages.some(message => message.method === "ui/notifications/size-changed" && message.params.height === 480));
  assert.equal(h.state().bridgeReady, true);
  assert.equal(h.elements.get("cards").children.length, 3);
  clean(h);
});

test("postMessage: initialization timeout is bounded and cannot authorize a mutation", async () => {
  const h = harness("postMessage", {holdInitialize:true,initial:null});
  h.expire(); await flush();
  assert.equal(h.state().bridgeReady, false);
  assert.match(h.status(), /Connecting.*not confirmed/);
  await h.click("save");
  assert.equal(h.calls.length, 0);
  clean(h);
});

test("postMessage: unsupported UI protocol does not claim readiness", async () => {
  const h = harness("postMessage", {protocolVersion:"unsupported",initial:null});
  await flush(); assert.equal(h.state().bridgeReady, false);
  assert.match(h.status(), /invalid response/); clean(h);
});

test("native: finalized host output can hydrate the widget without another tool call", () => {
  const h = harness("native", {initial:null});
  h.emit("openai:set_globals", {detail:{globals:{toolOutput:populatedCenter("stretch")}}});
  assert.equal(h.elements.get("cards").children.length, 3);
  assert.equal(h.state().selected, "stretch");
  assert.equal(h.calls.length, 0);
  h.elements.get("cards").children[1].onclick();
  assert.equal(h.widgetStates[0].selectedMode, "balanced"); clean(h);
});

test("native: remounted widget restores only its ephemeral mode selection", () => {
  const h = harness("native", {widgetState:{selectedMode:"stretch"}});
  assert.equal(h.state().selected, "stretch");
  assert.equal(h.calls.length, 0);
  assert.equal(h.followups.length, 0); clean(h);
});

test("postMessage: core-MCP clientInfo cannot replace required appInfo", async () => {
  const h = harness("postMessage", {initial:null,transformInitialize:params=>{
    const invalid={...params,clientInfo:params.appInfo};delete invalid.appInfo;return invalid;
  }});
  await flush(); assert.equal(h.state().bridgeReady, false);
  assert.equal(h.wireMessages.filter(message=>message.method==="ui/notifications/initialized").length, 0);
  assert.equal(h.calls.length, 0); assert.equal(h.elements.get("cards").children.length, 0);
  assert.match(h.status(), /Connecting.*not confirmed/);clean(h);
});

for(const [label,transform] of [
  ["missing app version",params=>({...params,appInfo:{name:"test"}})],
  ["missing capabilities",params=>({...params,appCapabilities:null})],
])test(`postMessage: ${label} is rejected by the host before mounting`, async()=>{
  const h=harness("postMessage",{initial:null,transformInitialize:transform});
  await flush();assert.equal(h.state().bridgeReady,false);assert.equal(h.calls.length,0);clean(h);
});

test("mixed host: negotiates shared bridge despite native compatibility aliases", async()=>{
  const h=harness("native",{sharedSupport:true});
  assert.equal(h.state().sharedBridge,true);
  await h.click("save");
  assert.equal(h.posts[0].method,"tools/call");
  assert.equal(h.calls.length,1);clean(h);
});

test("legacy host: rejected shared negotiation retains usable native actions", async()=>{
  const h=harness("native");await flush();
  assert.equal(h.state().sharedBridge,false);
  await h.click("save");assert.equal(h.calls.length,1);
  assert.equal(h.posts.length,0);assert.match(h.status(),/now the default/);clean(h);
});

test("finalized output preserves a user's ephemeral selection without saving defaults",()=>{
  const h=harness("native");h.elements.get("cards").children[2].onclick();
  h.emit("openai:set_globals",{detail:{globals:{toolOutput:populatedCenter("balanced")}}});
  assert.equal(h.state().selected,"stretch");assert.equal(h.calls.length,0);clean(h);
});
