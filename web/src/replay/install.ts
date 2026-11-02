import recording from "./recording.json";

type Entry = (typeof recording.entries)[number];
type Json = Record<string, unknown>;

const API = "/api/v1";
const ITERATION_MS = 35; // replay pacing only; every iteration payload is the recorded one

const canonical = (value: unknown): string => JSON.stringify(value, (_key, v) =>
  v && typeof v === "object" && !Array.isArray(v) ? Object.fromEntries(Object.entries(v as Json).sort(([a], [b]) => a < b ? -1 : 1)) : v);
const sceneKey = new Map<string, Entry>(recording.entries.map((entry) => [canonical(entry.request_scene), entry]));
const byScenario = (id: string, revision: number) => recording.entries.find((e) => e.create.scenario_id === id && e.create.revision === revision);
const byJob = (id: string) => recording.entries.find((e) => e.solve_created.job_id === id);
const byPlan = (id: string) => recording.entries.find((e) => e.plan.plan_id === id);

const reply = (status: number, body: unknown) => new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
const refuse = (message: string, status = 422) => reply(status, { detail: { error: { code: "REPLAY_READONLY", message, field_errors: [], retryable: false } } });

type Playback = { status: string; done: number; finished: boolean };
const playbacks = new Map<string, Playback>();

function jobView(entry: Entry): Json {
  const state = playbacks.get(entry.solve_created.job_id);
  if (!state || state.finished) return entry.job as unknown as Json;
  return { ...entry.solve_created, status: state.status, progress: { ...entry.solve_created.progress, done: state.done } };
}

function route(method: string, path: string, body: Json | null): Response {
  const m = (re: RegExp) => path.match(re);
  let hit: RegExpMatchArray | null;
  if (method === "GET" && path === "/health") return reply(200, { status: "ok", mode: "replay" });
  if (method === "GET" && path.startsWith("/scenarios") && !m(/^\/scenarios\/[^/]+\/\d+$/)) {
    return reply(200, { ...recording.list, scenarios: recording.list.scenarios.map((item) => ({ ...item, label: (recording.labels as Record<string, string>)[item.scenario_id] })) });
  }
  if (method === "GET" && (hit = m(/^\/scenarios\/([^/]+)\/(\d+)$/))) {
    const entry = byScenario(hit[1], Number(hit[2]));
    return entry ? reply(200, entry.scenario) : reply(404, { detail: "scenario revision not found" });
  }
  if (method === "POST" && path === "/scenarios/validate") {
    const entry = sceneKey.get(canonical(body?.scene));
    return entry ? reply(200, entry.validate) : refuse("回放版无法验证改动后或新导入的场景（需要在线后端）。仅录制过的原始场景可通过验证。");
  }
  if (method === "POST" && path === "/scenarios") {
    const entry = sceneKey.get(canonical(body?.scene));
    return entry ? reply(201, entry.create) : refuse("回放版不能保存改动后的场景或新场景（没有后端）。请打开已录制的版本后直接求解。");
  }
  if (method === "PATCH" && path.startsWith("/scenarios/")) return refuse("回放版不能保存场景改动（没有后端）。");
  if (method === "POST" && path === "/solve-runs") {
    const entry = body && byScenario(String(body.scenario_id), Number(body.revision));
    const rec = entry?.solve_request;
    if (!entry || !rec || body.algorithm !== rec.algorithm || body.iterations !== rec.iterations || body.ants !== rec.ants || body.seed !== rec.seed) {
      return refuse("回放版只含已录制场景的求解结果（aco · 100 次迭代 · 20 只蚂蚁 · seed 42），不能现场求解其他场景或参数。");
    }
    return reply(202, entry.solve_created);
  }
  if (method === "GET" && (hit = m(/^\/solve-runs\/([^/]+)$/))) {
    const entry = byJob(hit[1]);
    return entry ? reply(200, jobView(entry)) : reply(404, { detail: "job not found" });
  }
  if (method === "GET" && (hit = m(/^\/solve-runs\/([^/]+)\/plans$/))) {
    const entry = byJob(hit[1]);
    return entry ? reply(200, entry.plans) : reply(404, { detail: "job not found" });
  }
  if (method === "GET" && (hit = m(/^\/plans\/([^/]+)$/))) {
    const entry = byPlan(hit[1]);
    return entry ? reply(200, entry.plan) : reply(404, { detail: "plan not found" });
  }
  if (method === "POST" && path === "/plans/evaluate") return refuse("回放版不能重评人工调整后的方案（需要在线求解器/评估器）。");
  if (method === "POST" && path === "/comparisons") return refuse("回放版不提供方案比较（没有录制）。");
  if (method === "POST" && /^\/plans\/[^/]+\/exports$/.test(path)) return refuse("回放版不能导出 plan（需要后端生成文件）。");
  return refuse("回放版不支持该操作（没有后端）。", 404);
}

class ReplayEventSource extends EventTarget {
  static readonly CONNECTING = 0; static readonly OPEN = 1; static readonly CLOSED = 2;
  readonly url: string; readonly withCredentials = false;
  readyState = 0;
  onopen: ((e: Event) => void) | null = null; onmessage: ((e: MessageEvent) => void) | null = null; onerror: ((e: Event) => void) | null = null;
  private timer: number | undefined;
  constructor(url: string | URL) {
    super();
    this.url = String(url);
    const hit = new URL(this.url, location.href).pathname.match(/\/solve-runs\/([^/]+)\/events$/);
    const entry = hit && byJob(hit[1]);
    const jobId = hit?.[1] ?? "";
    if (!entry) { this.timer = window.setTimeout(() => { this.readyState = 2; this.onerror?.(new Event("error")); }, 0); return; }
    const events = entry.events;
    playbacks.set(jobId, { status: "queued", done: 0, finished: false });
    let index = 0;
    this.readyState = 1;
    const tick = () => {
      const event = events[index++];
      if (!event || this.readyState === 2) return;
      const payload = JSON.parse(event.data).payload as { completed_iterations?: number };
      const state = playbacks.get(jobId)!;
      state.status = event.type === "iteration" ? "running" : event.type;
      if (payload.completed_iterations !== undefined) state.done = payload.completed_iterations;
      if (event.type === "succeeded") state.finished = true;
      this.dispatchEvent(new MessageEvent(event.type, { data: event.data, lastEventId: event.id }));
      if (index < events.length) this.timer = window.setTimeout(tick, event.type === "iteration" ? ITERATION_MS : 120);
      else this.readyState = 2;
    };
    this.timer = window.setTimeout(tick, 120);
  }
  close() { this.readyState = 2; window.clearTimeout(this.timer); }
}

function banner() {
  const style = document.createElement("style");
  style.textContent = `.replay-banner{position:sticky;top:0;z-index:50;background:#fff7df;border-bottom:1px solid #e2b561;color:#5b4512;padding:9px 24px;font-size:12.5px;line-height:1.55}.replay-banner strong{color:#3f2e08}.replay-banner p{margin:0}.replay-banner p+p{margin-top:2px;color:#6d5217}@media(max-width:700px){.replay-banner{padding:8px 15px;font-size:12px}}`;
  const el = document.createElement("div");
  el.className = "replay-banner"; el.setAttribute("role", "note");
  el.innerHTML = `<p><strong>回放数据：</strong>以下结果由本仓库求解器真实运行后录制，在线版不能现场求解/保存改动。</p>
<p>可用：打开 ${recording.entries.length} 个已录制场景并点“求解 ACO”回放迭代进度（aco · 100 次迭代 · 20 只蚂蚁 · seed 42），查看基线/优化/叠加路线、成本拆分与逐站信息；拖点和改参数只改变本页草稿。不可用：保存或验证改动后的场景、导入新 JSON、求解改动后的草稿或未录制的场景、人工调序重评、导出 plan、方案比较。</p>`;
  document.head.append(style);
  document.body.prepend(el);
}

export async function installReplay() {
  const realFetch = window.fetch.bind(window);
  window.fetch = async (input, init) => {
    const request = input instanceof Request ? input : null;
    const url = new URL(request ? request.url : String(input), location.href);
    if (url.origin !== location.origin || !url.pathname.startsWith(API)) return realFetch(input, init);
    const method = (init?.method ?? request?.method ?? "GET").toUpperCase();
    const raw = init?.body ?? (request ? await request.text() : null);
    let body: Json | null = null;
    try { body = typeof raw === "string" ? JSON.parse(raw) as Json : null; } catch { body = null; }
    return route(method, url.pathname.slice(API.length), body);
  };
  window.EventSource = ReplayEventSource as unknown as typeof EventSource;
  banner();
}
