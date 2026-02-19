import { PointerEvent, useEffect, useMemo, useRef, useState, WheelEvent } from "react";
import { createRoot } from "react-dom/client";
import "./style.css";

type Kind = "depot" | "customer" | "charger";
type Node = { id: number; kind: Kind; x: number; y: number; dry_demand: number; recyclable_demand: number; earliest_minute: number; latest_minute: number; service_minutes: number };
type Vehicle = { id: number; battery_capacity_kwh: number; dry_capacity_kg: number; recyclable_capacity_kg: number; speed_kmph: number; charging_power_kw: number; base_consumption_kwh_per_km: number; load_consumption_kwh_per_kg_km: number; safety_battery_fraction: number; fixed_cost: number };
type Scene = { schema_version: 1; coordinate_unit: "km"; nodes: Node[]; vehicles: Vehicle[]; costs: { distance_cost_per_km: number; energy_cost_per_kwh: number; lateness_cost_per_minute: number; missed_customer_penalty: number } };
type Stored = { scenario_id: string; revision: number };
type FieldError = { path: string; message: string };
type SavedScene = { scenario_id: string; revision: number; nodes: number; customers: number; vehicles: number };
type Tool = "select" | "customer" | "charger" | "delete";
type Job = { job_id: string; status: string; progress: { done: number; total: number } };
type View = { x: number; y: number; size: number };
type Stop = { node_id: number; sequence: number; kind: Kind; arrival_minute: number; service_start_minute: number; departure_minute: number; waiting_minutes: number; charge_minutes: number; battery_before_kwh: number; battery_after_kwh: number; dry_load_kg: number; recyclable_load_kg: number; violations: string[] };
type RouteDetail = { vehicle_id: number | null; route: number[]; distance_km: number; energy_kwh: number; lateness_minutes: number; violations: string[]; stops: Stop[] };
type Evaluation = { feasible: boolean; total_cost: number; distance_km: number; energy_kwh: number; lateness_minutes: number; vehicles_used: number; missing_customers: number[]; duplicate_customers: number[]; violations: string[]; route_details: RouteDetail[] };
type PlanRecord = { plan_id: string; scenario_id: string; revision: number; result: { method: string; complete: boolean; plan: { routes: number[][] }; evaluation: Evaluation; baseline_plan: { routes: number[][] } | null; baseline_evaluation: Evaluation | null } };
type ExportArtifact = { artifact_id: string; kind: string; display_name: string; content_type: string; sha256: string; bytes: number };
type Comparison = { comparable: boolean; reasons: string[]; runs: { plan_id: string; method: string; feasibility: string; metrics: { total_cost: number; distance_km: number; energy_kwh: number; lateness_minutes: number; vehicles_used: number; missing_customers: number[]; duplicate_customers: number[] } }[] };
type MapMode = "draft" | "baseline" | "optimized" | "overlay";

const routeColors = ["#e07155", "#3e73a3", "#4b8d65", "#9d6cae", "#be8d3b", "#27827b", "#ba5d81", "#6179c4"];

const example: Scene = { schema_version: 1, coordinate_unit: "km", nodes: [
  { id: 0, kind: "depot", x: 12, y: 12, dry_demand: 0, recyclable_demand: 0, earliest_minute: 0, latest_minute: 480, service_minutes: 0 },
  { id: 1, kind: "customer", x: 28, y: 24, dry_demand: 120, recyclable_demand: 65, earliest_minute: 30, latest_minute: 180, service_minutes: 8 },
  { id: 2, kind: "customer", x: 54, y: 18, dry_demand: 80, recyclable_demand: 110, earliest_minute: 60, latest_minute: 260, service_minutes: 10 },
  { id: 3, kind: "customer", x: 62, y: 51, dry_demand: 180, recyclable_demand: 45, earliest_minute: 100, latest_minute: 360, service_minutes: 12 },
  { id: 4, kind: "charger", x: 40, y: 43, dry_demand: 0, recyclable_demand: 0, earliest_minute: 0, latest_minute: 1440, service_minutes: 0 },
], vehicles: [{ id: 1, battery_capacity_kwh: 120, dry_capacity_kg: 1200, recyclable_capacity_kg: 1000, speed_kmph: 40, charging_power_kw: 90, base_consumption_kwh_per_km: .75, load_consumption_kwh_per_kg_km: .0002, safety_battery_fraction: .2, fixed_cost: 280 }], costs: { distance_cost_per_km: 1.2, energy_cost_per_kwh: .8, lateness_cost_per_minute: 8, missed_customer_penalty: 10000 } };
const clone = <T,>(value: T): T => JSON.parse(JSON.stringify(value)) as T;
class ApiError extends Error { constructor(readonly status: number, message: string, readonly fieldErrors: FieldError[] = []) { super(message); } }
const api = async <T,>(path: string, init?: RequestInit) => {
  const response = await fetch(`/api/v1${path}`, init);
  const body = await response.json();
  if (!response.ok) {
    const error = body.detail?.error;
    const message = typeof error?.message === "string" ? error.message : typeof body.detail === "string" ? body.detail : "请求失败";
    const fieldErrors = Array.isArray(error?.field_errors) ? error.field_errors as FieldError[] : [];
    throw new ApiError(response.status, message, fieldErrors);
  }
  return body as T;
};
const field = (label: string, value: number, set: (next: number) => void, step = "1") => <label>{label}<input aria-label={label} type="number" value={value} step={step} onChange={(e) => set(Number(e.target.value))}/></label>;
const terminal = new Set(["succeeded", "cancelled", "failed", "interrupted"]);

function App() {
  const [scene, setScene] = useState<Scene>(() => clone(example)); const [history, setHistory] = useState<Scene[]>([]); const [redo, setRedo] = useState<Scene[]>([]);
  const [selected, setSelected] = useState(1); const [tool, setTool] = useState<Tool>("select"); const [stored, setStored] = useState<Stored | null>(null);
  const [notice, setNotice] = useState("未保存的示例场景"); const [warnings, setWarnings] = useState<string[]>([]); const [job, setJob] = useState<Job | null>(null);
  const [fieldErrors, setFieldErrors] = useState<FieldError[]>([]); const [savedScenes, setSavedScenes] = useState<SavedScene[]>([]);
  const [savedSelection, setSavedSelection] = useState(""); const [savedListError, setSavedListError] = useState(""); const [savedListHasMore, setSavedListHasMore] = useState(false);
  const [dirty, setDirty] = useState(false);
  const [mobilePanel, setMobilePanel] = useState<"points" | "constraints" | "plan">("points");
  const [selectedVehicle, setSelectedVehicle] = useState(0);
  const [plan, setPlan] = useState<PlanRecord | null>(null); const [selectedStop, setSelectedStop] = useState<Stop | null>(null);
  const [mapMode, setMapMode] = useState<MapMode>("draft");
  const [exports, setExports] = useState<ExportArtifact[]>([]);
  const [planScene, setPlanScene] = useState<Scene | null>(null); const [savedPlans, setSavedPlans] = useState<PlanRecord[]>([]);
  const [comparisonIds, setComparisonIds] = useState<string[]>([]); const [comparison, setComparison] = useState<Comparison | null>(null); const [selectedRoute, setSelectedRoute] = useState<number | null>(null);
  const jobBusy = Boolean(job && ["queued", "running", "cancelling"].includes(job.status));
  const hasBaseline = Boolean(plan?.result.baseline_plan && plan.result.baseline_evaluation);
  const effectiveMapMode = mapMode === "baseline" && !hasBaseline ? "optimized" : mapMode;
  const mapScene = effectiveMapMode !== "draft" && planScene ? planScene : scene;
  const displayedEvaluation = effectiveMapMode === "baseline" && plan?.result.baseline_evaluation
    ? plan.result.baseline_evaluation
    : plan?.result.evaluation;
  const displayedRoutes = displayedEvaluation?.route_details ?? [];
  const routeLines: Array<{ source: "baseline" | "optimized"; routeIndex: number; detail: RouteDetail }> = [];
  if (plan && planScene && effectiveMapMode !== "draft") {
    if (effectiveMapMode === "optimized" || effectiveMapMode === "overlay") {
      plan.result.evaluation.route_details.forEach((detail, routeIndex) => routeLines.push({ source: "optimized", routeIndex, detail }));
    }
    if (hasBaseline && (effectiveMapMode === "baseline" || effectiveMapMode === "overlay")) {
      plan.result.baseline_evaluation!.route_details.forEach((detail, routeIndex) => routeLines.push({ source: "baseline", routeIndex, detail }));
    }
  }
  const extent = useMemo(() => Math.max(80, ...mapScene.nodes.flatMap((item) => [item.x + 10, item.y + 10])), [mapScene]); const [view, setView] = useState<View>({ x: 0, y: 0, size: 80 });
  const svg = useRef<SVGSVGElement>(null); const dragNode = useRef<number | null>(null); const pan = useRef<{ clientX: number; clientY: number; view: View } | null>(null); const upload = useRef<HTMLInputElement>(null);
  const node = scene.nodes.find((item) => item.id === selected) ?? scene.nodes[0]; const vehicle = scene.vehicles[selectedVehicle] ?? scene.vehicles[0];
  const colorForVehicle = (vehicleId: number | null, fallbackIndex: number) => {
    const foundIndex = planScene?.vehicles.findIndex((item) => item.id === vehicleId) ?? -1;
    return routeColors[(foundIndex >= 0 ? foundIndex : fallbackIndex) % routeColors.length];
  };
  const savedKey = (item: Pick<SavedScene, "scenario_id" | "revision">) => `${item.scenario_id}:${item.revision}`;
  const refreshSavedScenes = async (preferred?: string) => {
    try {
      const result = await api<{ scenarios: SavedScene[]; has_more: boolean }>("/scenarios?limit=200");
      setSavedScenes(result.scenarios);
      setSavedListHasMore(result.has_more);
      setSavedListError("");
      setSavedSelection((current) => {
        if (preferred && result.scenarios.some((item) => savedKey(item) === preferred)) return preferred;
        if (current && result.scenarios.some((item) => savedKey(item) === current)) return current;
        return result.scenarios[0] ? savedKey(result.scenarios[0]) : "";
      });
    } catch {
      setSavedListError("暂时无法读取服务器保存的版本");
    }
  };
  const commit = (next: Scene, message = "有未保存更改") => { setHistory((old) => [...old.slice(-29), clone(scene)]); setRedo([]); setScene(next); setStored(stored); setDirty(true); setFieldErrors([]); setMapMode("draft"); setNotice(message); setWarnings([]); };
  const alterNode = (patch: Partial<Node>, target = node.id) => commit({ ...scene, nodes: scene.nodes.map((item) => item.id === target ? { ...item, ...patch } : item) });
  const alterVehicle = (patch: Partial<Vehicle>) => commit({ ...scene, vehicles: scene.vehicles.map((item, i) => i === selectedVehicle ? { ...item, ...patch } : item) });
  const alterCost = (key: keyof Scene["costs"], value: number) => commit({ ...scene, costs: { ...scene.costs, [key]: value } });
  const rememberPlan = (record: PlanRecord) => setSavedPlans((old) => old.some((item) => item.plan_id === record.plan_id) ? old : [...old, record]);
  const loadJobPlan = async (jobId: string) => { try { const listed = await api<{ plans: { plan_id: string }[] }>(`/solve-runs/${jobId}/plans`); if (!listed.plans[0]) return; const loaded = await api<PlanRecord>(`/plans/${listed.plans[0].plan_id}`); const source = await api<{ scene: Scene }>(`/scenarios/${loaded.scenario_id}/${loaded.revision}`); setPlan(loaded); setPlanScene(source.scene); setMapMode("optimized"); rememberPlan(loaded); setSelectedStop(null); setSelectedRoute(null); setExports([]); setNotice(!loaded.result.complete ? "求解已停止；保留最后完整迭代的部分方案。" : loaded.result.evaluation.feasible ? "ACO 搜索完成；可查看基线、优化与叠加路线。" : "搜索结束但未找到可行方案；已保留违规和遗漏诊断。"); } catch { setNotice("任务完成，但结果暂时无法读取；可刷新后重试"); } };
  const evaluateManual = async (routes: number[][]) => { if (!plan) return; const vehicleIds = plan.result.evaluation.route_details.map((detail) => detail.vehicle_id); if (vehicleIds.some((id) => id === null)) { setNotice("方案包含未知车辆，无法作为人工方案重评"); return; } try { const next = await api<PlanRecord>("/plans/evaluate", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ scenario_id: plan.scenario_id, revision: plan.revision, routes, vehicle_ids: vehicleIds }) }); setPlan(next); setMapMode("optimized"); rememberPlan(next); setSelectedStop(null); setSelectedRoute(null); setExports([]); setNotice("已用同一 evaluator 重评手动方案"); } catch (error) { setNotice(error instanceof Error ? `人工重评失败：${error.message}` : "人工重评失败"); } };
  const moveStop = (routeIndex: number, stopIndex: number, shift: -1 | 1) => { if (!plan) return; const route = plan.result.plan.routes[routeIndex]; const target = stopIndex + shift; if (stopIndex <= 0 || stopIndex >= route.length - 1 || target <= 0 || target >= route.length - 1) return; const routes = plan.result.plan.routes.map((item) => [...item]); [routes[routeIndex][stopIndex], routes[routeIndex][target]] = [routes[routeIndex][target], routes[routeIndex][stopIndex]]; void evaluateManual(routes); };
  const exportPlan = async () => { if (!plan) return; try { const result = await api<{ artifacts: ExportArtifact[] }>(`/plans/${plan.plan_id}/exports`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ formats: ["json", "stops_csv", "svg", "html"] }) }); setExports(result.artifacts); setNotice("已生成可下载的 plan、逐站 CSV、SVG 与 HTML"); } catch (error) { setNotice(error instanceof Error ? `导出失败：${error.message}` : "导出失败"); } };
  const compareSelected = async () => { if (comparisonIds.length < 2) { setNotice("请在方案列表中选择两个或三个方案比较"); return; } try { const result = await api<Comparison>("/comparisons", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ plan_ids: comparisonIds }) }); setComparison(result); setNotice(result.comparable ? "比较来自同一场景版本" : `不可直接比较：${result.reasons.join("；")}`); } catch (error) { setNotice(error instanceof Error ? `比较失败：${error.message}` : "比较失败"); } };
  const fit = () => setView({ x: 0, y: 0, size: extent });
  useEffect(() => { fit(); }, [extent]);
  useEffect(() => { void refreshSavedScenes(); }, []);
  useEffect(() => { const onKey = (event: KeyboardEvent) => { if (event.target instanceof HTMLInputElement) return; if ((event.key === "Delete" || event.key === "Backspace") && node.kind !== "depot") { event.preventDefault(); commit({ ...scene, nodes: scene.nodes.filter((item) => item.id !== node.id) }); setSelected(0); } if (event.key === "Escape") setTool("select"); }; window.addEventListener("keydown", onKey); return () => window.removeEventListener("keydown", onKey); });
  useEffect(() => { if (!job) return; let source: EventSource | null = null; const finish = (status: string, jobId: string) => { source?.close(); if (status === "succeeded" || status === "cancelled") void loadJobPlan(jobId); }; const refresh = async () => { try { const next = await api<Job>(`/solve-runs/${job.job_id}`); setJob(next); if (terminal.has(next.status)) finish(next.status, next.job_id); } catch { setNotice("无法读取求解进度；可刷新任务恢复"); source?.close(); } }; source = new EventSource(`/api/v1/solve-runs/${job.job_id}/events?after=0`); for (const type of ["queued", "running", "iteration", "cancelling", "cancelled", "succeeded", "failed", "interrupted"]) source.addEventListener(type, (event) => { const payload = JSON.parse((event as MessageEvent<string>).data).payload as { completed_iterations?: number; total_iterations?: number }; setJob((current) => current && ({ ...current, status: type === "iteration" ? "running" : type, progress: payload.completed_iterations === undefined ? current.progress : { done: payload.completed_iterations, total: payload.total_iterations ?? current.progress.total } })); if (terminal.has(type)) finish(type, job.job_id); }); source.onerror = () => source?.close(); void refresh(); return () => source?.close(); }, [job?.job_id]);
  const point = (event: PointerEvent<SVGSVGElement>) => { const rect = svg.current!.getBoundingClientRect(); return { x: Math.max(0, Math.min(200, view.x + (event.clientX - rect.left) / rect.width * view.size)), y: Math.max(0, Math.min(200, view.y + (event.clientY - rect.top) / rect.height * view.size)) }; };
  const add = (kind: "customer" | "charger", x: number, y: number) => { const id = Math.max(...scene.nodes.map((item) => item.id)) + 1; const created: Node = { id, kind, x: Math.round(x * 10) / 10, y: Math.round(y * 10) / 10, dry_demand: kind === "customer" ? 80 : 0, recyclable_demand: kind === "customer" ? 60 : 0, earliest_minute: 0, latest_minute: kind === "customer" ? 480 : 1440, service_minutes: kind === "customer" ? 8 : 0 }; commit({ ...scene, nodes: [...scene.nodes, created] }); setSelected(id); setTool("select"); };
  const mapClick = (event: PointerEvent<SVGSVGElement>) => { if (dragNode.current !== null || pan.current) return; if (tool === "customer" || tool === "charger") { const at = point(event); add(tool, at.x, at.y); } };
  const mapMove = (event: PointerEvent<SVGSVGElement>) => { if (dragNode.current !== null && effectiveMapMode === "draft") { const at = point(event); alterNode({ x: Math.round(at.x * 10) / 10, y: Math.round(at.y * 10) / 10 }, dragNode.current); } else if (pan.current && svg.current) { const rect = svg.current.getBoundingClientRect(); const dx = (event.clientX - pan.current.clientX) / rect.width * pan.current.view.size; const dy = (event.clientY - pan.current.clientY) / rect.height * pan.current.view.size; setView({ ...pan.current.view, x: Math.max(0, pan.current.view.x - dx), y: Math.max(0, pan.current.view.y - dy) }); } };
  const wheel = (event: WheelEvent<SVGSVGElement>) => { event.preventDefault(); const factor = event.deltaY > 0 ? 1.16 : .84; const nextSize = Math.max(20, Math.min(240, view.size * factor)); const rect = svg.current!.getBoundingClientRect(); const pointerX = (event.clientX - rect.left) / rect.width; const pointerY = (event.clientY - rect.top) / rect.height; setView({ x: Math.max(0, view.x + pointerX * (view.size - nextSize)), y: Math.max(0, view.y + pointerY * (view.size - nextSize)), size: nextSize }); };
  const openSavedVersion = async () => {
    const selectedVersion = savedScenes.find((item) => savedKey(item) === savedSelection);
    if (!selectedVersion) return;
    if (jobBusy) { setNotice("当前求解仍在运行；完成后再切换场景，避免丢失结果。"); return; }
    if (dirty && !window.confirm("当前草稿尚未保存。打开服务器版本会替换这份草稿，继续吗？")) return;
    try {
      const loaded = await api<{ scenario_id: string; revision: number; scene: Scene }>(`/scenarios/${selectedVersion.scenario_id}/${selectedVersion.revision}`);
      setScene(loaded.scene); setStored({ scenario_id: loaded.scenario_id, revision: loaded.revision }); setDirty(false);
      setHistory([]); setRedo([]); setFieldErrors([]); setWarnings([]); setSelected(loaded.scene.nodes[0]?.id ?? 0); setSelectedVehicle(0);
      setPlan(null); setPlanScene(null); setMapMode("draft"); setJob(null); setSelectedStop(null); setSelectedRoute(null); setExports([]);
      setNotice(`已打开场景 ${loaded.scenario_id.slice(-6)} · revision ${loaded.revision}`);
    } catch (error) {
      setNotice(error instanceof Error ? `打开版本失败：${error.message}` : "打开版本失败");
      void refreshSavedScenes();
    }
  };
  const validateDraft = async () => { try { const result = await api<{ warnings: string[] }>("/scenarios/validate", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ scene }) }); setFieldErrors([]); setWarnings(result.warnings); setNotice(result.warnings.length ? "结构有效，但存在求解前提示" : "结构验证通过"); } catch (error) { setWarnings([]); setFieldErrors(error instanceof ApiError ? error.fieldErrors : []); setNotice(error instanceof Error ? `无法保存：${error.message}` : "场景无效"); } };
  const save = async (): Promise<Stored | null> => { try { const result = stored ? await api<Stored>(`/scenarios/${stored.scenario_id}`, { method: "PATCH", headers: { "Content-Type": "application/json", "If-Match": String(stored.revision) }, body: JSON.stringify({ scene }) }) : await api<Stored>("/scenarios", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ scene }) }); setStored(result); setDirty(false); setFieldErrors([]); setWarnings([]); setNotice(`已保存为 revision ${result.revision}`); void refreshSavedScenes(savedKey(result)); return result; } catch (error) { setFieldErrors(error instanceof ApiError ? error.fieldErrors : []); if (error instanceof ApiError && error.status === 409) setNotice("保存冲突：草稿已保留。可打开最新版本，或另存为独立场景。"); else setNotice(error instanceof Error ? `无法保存：${error.message}` : "保存失败"); return null; } };
  const solve = async () => { const target = stored ?? await save(); if (!target) return; try { const result = await api<Job>("/solve-runs", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ ...target, algorithm: "aco", iterations: 100, ants: 20, seed: 42 }) }); setJob(result); setNotice("已排队，正在读取真实 ACO iteration"); } catch (error) { setNotice(error instanceof Error ? `求解无法启动：${error.message}` : "求解无法启动"); } };
  const importFile = async (file: File | undefined) => {
    if (!file) return;
    let source = "";
    try {
      source = await file.text();
      const parsed = JSON.parse(source) as Scene;
      if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
        setFieldErrors([{ path: "scene", message: "最外层必须是 JSON 对象" }]);
        setNotice("导入失败，草稿保持不变：请检查 scene 字段");
        return;
      }
      await api("/scenarios/validate", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ scene: parsed }) });
      setScene(parsed); setHistory([]); setRedo([]); setStored(null); setDirty(true); setPlan(null); setPlanScene(null); setMapMode("draft"); setJob(null); setSelectedStop(null); setSelectedRoute(null); setExports([]); setSelected(parsed.nodes[0]?.id ?? 0); setWarnings([]); setFieldErrors([]); setNotice(`已导入 ${file.name}；请保存为新 revision`);
    } catch (error) {
      if (error instanceof SyntaxError) {
        const match = error.message.match(/position\s+(\d+)/i);
        const lineColumn = error.message.match(/line\s+(\d+)\s+column\s+(\d+)/i);
        const position = match ? Number(match[1]) : null;
        const before = source.slice(0, position ?? 0);
        const line = lineColumn ? Number(lineColumn[1]) : before.split("\n").length;
        const column = lineColumn ? Number(lineColumn[2]) : position === null ? 1 : position - before.lastIndexOf("\n");
        setFieldErrors([{ path: `第 ${line} 行 · 第 ${column} 列`, message: "JSON 语法错误" }]);
        setNotice(`导入失败，草稿保持不变：${error.message}`);
      } else {
        setFieldErrors(error instanceof ApiError ? error.fieldErrors : []);
        setNotice(error instanceof Error ? `导入失败，草稿保持不变：${error.message}` : "导入失败，草稿保持不变");
      }
    }
  };
  return <main className={`mobile-${mobilePanel}`}><header className="app-header"><div className="header-top"><div><p className="eyebrow">ROUTEFORGE / 平面距离模型</p><h1>电动车收运规划</h1></div><p className="status" role="status">{notice}</p></div><div className="header-controls"><div className="actions"><button onClick={() => { if (history.length) { const previous = history.at(-1)!; setHistory(history.slice(0, -1)); setRedo((old) => [clone(scene), ...old]); setScene(previous); setDirty(true); setFieldErrors([]); setMapMode("draft"); } }} disabled={!history.length}>撤销</button><button onClick={() => { if (redo.length) { const next = redo[0]; setRedo(redo.slice(1)); setHistory((old) => [...old, clone(scene)]); setScene(next); setDirty(true); setFieldErrors([]); setMapMode("draft"); } }} disabled={!redo.length}>重做</button><button onClick={() => { upload.current?.click(); }}>导入 JSON</button><input ref={upload} className="hidden" type="file" accept="application/json" onChange={(event) => { const file = event.currentTarget.files?.[0]; event.currentTarget.value = ""; void importFile(file); }}/><button onClick={() => { setStored(null); setDirty(true); setNotice("已保留当前草稿；下次保存会创建独立场景副本"); }}>另存副本</button><button onClick={() => void validateDraft()}>验证</button><button onClick={() => void save()}>保存 revision</button><button className="primary" onClick={() => void solve()}>求解 ACO</button></div><div className="saved-scenes"><label htmlFor="saved-version">已保存版本</label><select id="saved-version" aria-label="选择已保存场景版本" value={savedSelection} onChange={(event) => setSavedSelection(event.target.value)}><option value="">选择版本…</option>{savedScenes.map((item) => <option key={savedKey(item)} value={savedKey(item)}>场景 {item.scenario_id.slice(-6)} · revision {item.revision} · {item.customers} 个收集点</option>)}</select><button onClick={() => void openSavedVersion()} disabled={!savedSelection || jobBusy}>打开</button>{savedListHasMore && <span className="saved-note">仅显示最近 200 个版本</span>}{savedListError && <span className="saved-note error">{savedListError}</span>}</div></div>{fieldErrors.length > 0 && <section className="field-errors" role="alert"><strong>请检查导入或保存字段</strong><ul>{fieldErrors.map((item, index) => <li key={`${item.path}-${index}`}><code>{item.path}</code><span>{item.message}</span></li>)}</ul></section>}<div className="mobile-tabs" role="tablist" aria-label="工作台页面">{([ ["points", "点位"], ["constraints", "约束"], ["plan", "方案"] ] as const).map(([id, label]) => <button key={id} role="tab" aria-selected={mobilePanel === id} className={mobilePanel === id ? "active" : ""} onClick={() => setMobilePanel(id)}>{label}</button>)}</div></header>
    <aside className="tools"><h2>场景工具</h2>{([ ["select", "选择 / 拖动"], ["customer", "添加收集点"], ["charger", "添加充电站"], ["delete", "删除点位"] ] as [Tool, string][]).map(([id, label]) => <button key={id} className={tool === id ? "active" : ""} aria-pressed={tool === id} onClick={() => setTool(id)}>{label}</button>)}<button onClick={fit}>适应视图</button><p>滚轮缩放，空白处拖动平移。Delete 删除选中非仓库点，Escape 回到选择工具。</p><h2>车辆</h2><div className="vehicle-tabs">{scene.vehicles.map((item, index) => <button key={item.id} className={selectedVehicle === index ? "active" : ""} onClick={() => setSelectedVehicle(index)}>#{item.id}</button>)}<button aria-label="新增车辆" onClick={() => { const id = Math.max(...scene.vehicles.map((item) => item.id)) + 1; commit({ ...scene, vehicles: [...scene.vehicles, { ...vehicle, id }] }); setSelectedVehicle(scene.vehicles.length); }}>＋</button><button aria-label="删除当前车辆" disabled={scene.vehicles.length === 1} onClick={() => { commit({ ...scene, vehicles: scene.vehicles.filter((_, index) => index !== selectedVehicle) }); setSelectedVehicle(Math.max(0, selectedVehicle - 1)); }}>−</button></div><h2>车辆 #{vehicle.id}</h2><div className="fields compact">{field("电池 (kWh)", vehicle.battery_capacity_kwh, (battery_capacity_kwh) => alterVehicle({ battery_capacity_kwh }))}{field("速度 (km/h)", vehicle.speed_kmph, (speed_kmph) => alterVehicle({ speed_kmph }))}{field("干舱容量 (kg)", vehicle.dry_capacity_kg, (dry_capacity_kg) => alterVehicle({ dry_capacity_kg }))}{field("可回收舱容量 (kg)", vehicle.recyclable_capacity_kg, (recyclable_capacity_kg) => alterVehicle({ recyclable_capacity_kg }))}{field("充电功率 (kW)", vehicle.charging_power_kw, (charging_power_kw) => alterVehicle({ charging_power_kw }))}{field("安全电量比例", vehicle.safety_battery_fraction, (safety_battery_fraction) => alterVehicle({ safety_battery_fraction }), ".01")}</div></aside>
    <section className="canvas"><div className="map-label"><span>XY 平面 / km · {effectiveMapMode === "draft" ? "当前草稿" : `revision ${plan?.revision} · ${effectiveMapMode === "baseline" ? "基线" : effectiveMapMode === "overlay" ? "叠加比较" : "优化方案"}`}</span><div className="map-modes" role="tablist" aria-label="路线图层"><button role="tab" aria-selected={effectiveMapMode === "draft"} className={effectiveMapMode === "draft" ? "active" : ""} onClick={() => { setMapMode("draft"); setSelectedStop(null); setSelectedRoute(null); }}>草稿</button><button role="tab" aria-selected={effectiveMapMode === "optimized"} className={effectiveMapMode === "optimized" ? "active" : ""} disabled={!plan} onClick={() => { setMapMode("optimized"); setSelectedStop(null); setSelectedRoute(null); }}>优化</button><button role="tab" aria-selected={effectiveMapMode === "baseline"} className={effectiveMapMode === "baseline" ? "active" : ""} disabled={!hasBaseline} onClick={() => { setMapMode("baseline"); setSelectedStop(null); setSelectedRoute(null); }}>基线</button><button role="tab" aria-selected={effectiveMapMode === "overlay"} className={effectiveMapMode === "overlay" ? "active" : ""} disabled={!hasBaseline} onClick={() => { setMapMode("overlay"); setSelectedStop(null); setSelectedRoute(null); }}>叠加</button></div><span className="map-disclaimer">平面距离模型，非道路导航</span></div>{effectiveMapMode === "overlay" && <div className="overlay-legend"><span><i/>实线：优化路线</span><span><i className="dashed"/>虚线：贪心基线</span></div>}<svg ref={svg} viewBox={`${view.x} ${view.y} ${view.size} ${view.size}`} onWheel={wheel} onPointerDown={(event) => { if (tool === "select" && event.target === event.currentTarget) { pan.current = { clientX: event.clientX, clientY: event.clientY, view }; event.currentTarget.setPointerCapture(event.pointerId); } }} onPointerUp={() => { dragNode.current = null; pan.current = null; }} onPointerMove={mapMove} onClick={mapClick} aria-label="场景平面图">{routeLines.map(({ detail, routeIndex, source }) => { const color = colorForVehicle(detail.vehicle_id, routeIndex); const visible = selectedRoute === null || selectedRoute === detail.vehicle_id; return <polyline key={`${source}-${detail.vehicle_id ?? routeIndex}`} className={`route-line ${source === "baseline" ? "source-baseline" : "source-optimized"} ${visible ? "visible" : "muted"}`} style={{ stroke: color }} points={detail.route.map((nodeId) => { const pointNode = planScene?.nodes.find((item) => item.id === nodeId); return pointNode ? `${pointNode.x},${pointNode.y}` : ""; }).filter(Boolean).join(" ")} onPointerDown={(event) => { event.stopPropagation(); setSelectedRoute(detail.vehicle_id); }} />;})}{Array.from({ length: 21 }, (_, i) => <path key={i} d={`M ${i * 10} 0 V 200 M 0 ${i * 10} H 200`} className="grid"/>)}{mapScene.nodes.map((item) => <g key={item.id} className={`node ${item.kind} ${effectiveMapMode === "draft" && item.id === selected ? "selected" : ""}`} transform={`translate(${item.x} ${item.y})`} tabIndex={0} role="button" aria-label={`${item.kind} ${item.id}`} onKeyDown={(event) => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); setSelected(item.id); if (effectiveMapMode !== "draft") setMapMode("draft"); } }} onPointerDown={(event) => { event.stopPropagation(); if (effectiveMapMode !== "draft") { setMapMode("draft"); setSelected(item.id); setSelectedStop(null); setNotice("已切回当前草稿；再次拖动可编辑点位"); return; } if (tool === "delete" && item.kind !== "depot") { commit({ ...scene, nodes: scene.nodes.filter((entry) => entry.id !== item.id) }); setSelected(0); return; } setSelected(item.id); if (tool === "select") { dragNode.current = item.id; event.currentTarget.setPointerCapture(event.pointerId); } }}><circle r={item.kind === "depot" ? 4.6 : 3.8}/><text x="5" y="-5">{item.kind === "customer" ? `#${item.id}` : item.kind === "charger" ? "⚡" : "仓"}</text></g>)}</svg>{job && <div className="job">job <code>{job.job_id.slice(0, 12)}</code> · {job.status} · {job.progress.done}/{job.progress.total} iterations</div>}</section>
    {plan && displayedEvaluation && <section className="result-panel">
      <div className="result-summary">
        <div>
          <p className="eyebrow">{!plan.result.complete ? "部分搜索结果" : effectiveMapMode === "baseline" ? "贪心基线 · 同源评估" : effectiveMapMode === "overlay" ? "优化方案 · 已叠加贪心基线" : plan.result.method === "manual" ? "人工方案 · 已重评" : "优化方案 · 同源评估"}</p>
          <h2>{displayedEvaluation.feasible ? "可行" : "存在约束问题"}</h2>
        </div>
        <dl>
          <div><dt>成本</dt><dd>{displayedEvaluation.total_cost.toFixed(1)}</dd></div>
          <div><dt>里程</dt><dd>{displayedEvaluation.distance_km.toFixed(1)} km</dd></div>
          <div><dt>能耗</dt><dd>{displayedEvaluation.energy_kwh.toFixed(1)} kWh</dd></div>
          <div><dt>迟到</dt><dd>{displayedEvaluation.lateness_minutes.toFixed(1)} min</dd></div>
        </dl>
        <div className="export-actions">
          <button onClick={() => void exportPlan()}>导出 plan</button>
          {exports.map((artifact) => <a key={artifact.artifact_id} href={`/api/v1/artifacts/${artifact.artifact_id}/download`}>{artifact.display_name}</a>)}
        </div>
      </div>
      <div className="route-list">
        {displayedRoutes.map((detail, routeIndex) => <article key={`${detail.vehicle_id}-${routeIndex}`} className={selectedRoute === detail.vehicle_id ? "route-selected" : ""} onClick={() => setSelectedRoute(detail.vehicle_id)}>
          <header><strong><i className="vehicle-swatch" style={{ backgroundColor: colorForVehicle(detail.vehicle_id, routeIndex) }}/>车辆 #{detail.vehicle_id ?? "?"}</strong><span>{detail.distance_km.toFixed(1)} km · {detail.energy_kwh.toFixed(1)} kWh</span></header>
          <ol>{detail.stops.map((stop, index) => <li key={`${stop.sequence}-${stop.node_id}`} className={selectedStop?.sequence === stop.sequence && selectedStop.node_id === stop.node_id ? "selected" : ""}>
            <button onClick={() => setSelectedStop(stop)}>#{stop.node_id} · {stop.kind}</button>
            {stop.kind === "customer" && <span className="reorder"><button aria-label={`上移 ${stop.node_id}`} disabled={effectiveMapMode !== "optimized" || index <= 0 || detail.stops[index - 1]?.kind !== "customer"} onClick={() => moveStop(routeIndex, stop.sequence, -1)}>↑</button><button aria-label={`下移 ${stop.node_id}`} disabled={effectiveMapMode !== "optimized" || index >= detail.stops.length - 1 || detail.stops[index + 1]?.kind !== "customer"} onClick={() => moveStop(routeIndex, stop.sequence, 1)}>↓</button></span>}
          </li>)}</ol>
        </article>)}
      </div>
      <section className="timeline-panel"><p className="eyebrow">{effectiveMapMode === "baseline" ? "基线路线时间轴 / min" : "路线时间轴 / min"}</p>
        {displayedRoutes.map((detail, routeIndex) => { const finish = Math.max(1, ...detail.stops.map((stop) => stop.departure_minute)); return <div className="timeline" key={`timeline-${detail.vehicle_id}-${routeIndex}`}>
          {detail.stops.map((stop) => <button key={`${stop.sequence}-${stop.node_id}`} className={selectedStop?.node_id === stop.node_id && selectedStop.sequence === stop.sequence ? "selected" : ""} style={{ left: `${Math.min(96, stop.departure_minute / finish * 96)}%` }} onClick={() => { setSelectedStop(stop); setSelectedRoute(detail.vehicle_id); }}><span>#{stop.node_id}</span><small>{stop.departure_minute.toFixed(0)}</small></button>)}
        </div>})}
      </section>
      <section className="comparison-panel"><div><p className="eyebrow">方案比较</p><strong>保留不同结果，再按相同 revision 比较</strong></div><div className="comparison-picks">
        {savedPlans.map((item) => <label key={item.plan_id}><input type="checkbox" checked={comparisonIds.includes(item.plan_id)} onChange={() => setComparisonIds((current) => current.includes(item.plan_id) ? current.filter((id) => id !== item.plan_id) : current.length >= 3 ? current : [...current, item.plan_id])}/><span>{item.result.method} · {item.plan_id.slice(-6)}</span></label>)}
        <button onClick={() => void compareSelected()}>比较所选方案</button>
      </div>{comparison && <div className="comparison-results"><strong>{comparison.comparable ? "相同场景版本，可比较" : comparison.reasons.join("；")}</strong>{comparison.runs.map((run) => <div key={run.plan_id}><span>{run.method} · {run.feasibility}</span><span>成本 {run.metrics.total_cost.toFixed(1)}</span><span>{run.metrics.distance_km.toFixed(1)} km</span><span>{run.metrics.energy_kwh.toFixed(1)} kWh</span><span>迟到 {run.metrics.lateness_minutes.toFixed(1)} min</span></div>)}</div>}</section>
      {selectedStop && <div className="stop-detail"><strong>站点 #{selectedStop.node_id}</strong><span>到达 {selectedStop.arrival_minute.toFixed(1)} min</span><span>服务 {selectedStop.service_start_minute.toFixed(1)}–{selectedStop.departure_minute.toFixed(1)} min</span><span>等待 {selectedStop.waiting_minutes.toFixed(1)} min · 充电 {selectedStop.charge_minutes.toFixed(1)} min</span><span>电量 {selectedStop.battery_before_kwh.toFixed(1)} → {selectedStop.battery_after_kwh.toFixed(1)} kWh</span><span>干 / 可回收 {selectedStop.dry_load_kg.toFixed(1)} / {selectedStop.recyclable_load_kg.toFixed(1)} kg</span>{selectedStop.violations.length > 0 && <em>{selectedStop.violations.join("；")}</em>}</div>}
    </section>}
    <aside className="inspector"><p className="eyebrow">点位约束</p><h2>{node.kind === "depot" ? "仓库" : node.kind === "charger" ? "充电站" : `收集点 #${node.id}`}</h2><div className="fields">{field("X (km)", node.x, (x) => alterNode({ x }), ".1")}{field("Y (km)", node.y, (y) => alterNode({ y }), ".1")}{node.kind === "customer" && <>{field("干垃圾 (kg)", node.dry_demand, (dry_demand) => alterNode({ dry_demand }))}{field("可回收 (kg)", node.recyclable_demand, (recyclable_demand) => alterNode({ recyclable_demand }))}</>}{field("最早 (min)", node.earliest_minute, (earliest_minute) => alterNode({ earliest_minute }))}{field("最晚 (min)", node.latest_minute, (latest_minute) => alterNode({ latest_minute }))}{node.kind === "customer" && field("服务 (min)", node.service_minutes, (service_minutes) => alterNode({ service_minutes }))}</div><h2>成本</h2><div className="fields compact">{field("距离 / km", scene.costs.distance_cost_per_km, (value) => alterCost("distance_cost_per_km", value), ".1")}{field("能量 / kWh", scene.costs.energy_cost_per_kwh, (value) => alterCost("energy_cost_per_kwh", value), ".1")}{field("迟到 / min", scene.costs.lateness_cost_per_minute, (value) => alterCost("lateness_cost_per_minute", value), ".1")}{field("遗漏罚金", scene.costs.missed_customer_penalty, (value) => alterCost("missed_customer_penalty", value))}</div>{warnings.length > 0 && <div className="warnings"><strong>求解前提示</strong>{warnings.map((warning) => <p key={warning}>{warning}</p>)}</div>}<p className="help">双隔舱分别约束。保存创建新版本；旧方案不会随草稿漂移。</p></aside></main>;
}
createRoot(document.getElementById("root")!).render(<App/>);
