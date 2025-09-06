# 电动车收运规划 — 完整产品设计

目标：本地收运规划实验工作台v1。内部routeforge。准确任务是双隔舱取货/回收，沿途载重增加；此前“配送”称呼过宽，本设计修正。

## 1. 产品任务

面向研究收运路线、容量和充电取舍的调度学习者。编辑一个二维场景 → 求greedy/ACO方案 → 查看逐站载重、电量和时间 → 改一个约束生成新版本 → 比较可行性与代价。
画面始终标“平面距离模型，非道路导航”。不接真实路网、交通、GPS、收费地图或生产调度；不保证最优，也不能把搜索没找到方案称为数学上无解。

## 2. 已有能力与先修问题

models.py已有 Node、Vehicle、CostModel、ProblemInstance 和携带 vehicle_indices 的 RoutePlan；evaluator使用双隔舱收集量，充电/能耗与载重相关。solver包含greedy、ACO、路线内swap。
CLI目前永远generate_instance，只能改变随机生成参数；没有用户场景JSON导入。evaluator只给路线汇总，没有逐站时间/电量日志。某些数值校验会放过NaN，先补全部有限数检查。
新增逐站轨迹由同一个 evaluator 在评估过程中记录，禁止前端按另一套公式补画。旧 RoutePlan 的vehicle_indices是数组索引，API必须同时返回实际vehicle_id，避免删车或排序导致对应错误。
当前负面消融结果保留，不在文案中宣传算法全面提升。

## 3. 视觉与页面

暖白 #f7f7f2、深墨 #24322b，约束表面白色，路线珊瑚/蓝/绿固定对应车辆，违规琥珀/红并加图标。SVG二维场景占主要面积，不做后台指标墙。
1440：左场景/约束280px、中平面图弹性、右逐站详情300px；可收起侧栏。顶部场景名/版本/撤销/求解，底部路线时间轴。1100右详情为抽屉；390切“点位/约束/方案”页，地图独立横向拖动，有列表替代。

### 场景 /scenarios/:id/edit
打开12点示例或导入JSON。工具：选点、添加收集点、添加充电站、删除、适应视图；滚轮缩放、拖动平移，点位拖动改变坐标并可撤销。
点的表单：类型、x/y(km)、干垃圾/可回收物需求(kg)、最早/最晚分钟、服务时长。只有customer可有需求；唯一depot不可直接删除，修改仓库用明确命令。
车辆表：battery_capacity_kwh、dry/recyclable_capacity_kg、speed_kmph、charging_power_kw、安全电量比例、基础/载荷能耗、固定成本。两个隔舱独立，不允许把剩余容量相加代替约束。
批量导入先给逐行错误；失败保留草稿。编辑有undo/redo，保存生成场景revision；运行绑定版本，已完成方案不随草稿改变。

### 求解与比较 /scenarios/:id/results
设置算法、seed、iterations默认100（1–500）、ants默认20（1–100）；先产出greedy基线，再显示实际ACO迭代与best-so-far。仅最终可行方案用主色“可行”；正在搜索的临时结果标不完整。
每辆车有独立颜色和路线列表；点击路线突出其点位和行程，其他线路淡化。比较切换“基线/优化/叠加”，共有点位置一致。
指标同时显示车辆使用数、里程、能耗、成本、迟到、未服务、重复访问；可行性优先，不仅按总成本排第一。
点击某站，在右侧看 arrival/start_service/departure、等待、充电分钟、电量前后、两个隔舱载重、违规。时间轴与地图选中同步；单位完整。
可拖动单条路线的站点顺序后“评估此方案”，保留原优化结果，手动方案有标签；不能前端改路线后继续显示旧指标。

### 诊断
结构错误阻止求解（重复ID、非法数字、无仓库等）。单点需求超过任一车辆容量等确定条件给具体说明，但不推广为完整无解证明。
搜索结束而未找到可行方案：保留最佳候选、明确违规/遗漏，提供“定位问题点”“复制场景调整”，不显示成功绿色。计算任务可以succeeded而 result.feasibility=search_infeasible。
取消/进程中断保留最后完整迭代；首版重新运行同配置，不承诺续接ACO状态。SSE断线仍可取回后台结果。

## 4. 契约（拟建）

坐标单位km，时间为场景起点后的分钟，重量kg，能量kWh。范围：customer≤200、charger≤30、vehicle≤20、坐标0–200km、时间0–1440分钟；成本与能耗都要求有限数。显示的日期只是场景起点标签，不暗示交通时变。
~~~json
{"schema_version":1,"coordinate_unit":"km","nodes":[{"id":0,"kind":"depot","x":0,"y":0,"dry_demand":0,"recyclable_demand":0,"earliest_minute":0,"latest_minute":480,"service_minutes":0},{"id":1,"kind":"customer","x":10,"y":5,"dry_demand":80,"recyclable_demand":40,"earliest_minute":30,"latest_minute":180,"service_minutes":10}],"vehicles":[{"id":1,"battery_capacity_kwh":120,"dry_capacity_kg":1200,"recyclable_capacity_kg":1000,"speed_kmph":40,"charging_power_kw":90,"base_consumption_kwh_per_km":0.75,"load_consumption_kwh_per_kg_km":0.0002,"safety_battery_fraction":0.2,"fixed_cost":280}],"costs":{"distance_cost_per_km":1.2,"energy_cost_per_kwh":0.8,"lateness_cost_per_minute":8,"missed_customer_penalty":10000}}
~~~
示例仅说明输入结构，完整场景由实际求解验证。

| API | 契约 |
|---|---|
| POST /scenarios | 示例或scene JSON → id/revision/validation |
| PATCH /scenarios/{id} | 完整草稿+If-Match → 新revision |
| POST /scenarios/{id}/validate | → structural_errors、necessary_condition_warnings |
| POST /solve-runs | scenario_id/revision、algorithm、seed、iterations、ants → job |
| GET /solve-runs/{id}/plans | baseline/best，plan_id、vehicle_id、evaluation、feasibility |
| GET /plans/{id}/stops | 按vehicle → canonical逐站轨迹 |
| POST /plans/evaluate | scenario_id/revision、routes、vehicle_ids → 新manual plan及同一评估 |
| POST /comparisons | plan_ids≤3 → 可比性、相同约束/不同场景标识、指标 |
| POST /plans/{id}/exports | JSON、逐站CSV、SVG、HTML → artifact |

Stop字段：node_id、sequence、arrival_minute、service_start_minute、departure_minute、waiting_minutes、charge_minutes、battery_before/after_kwh、dry/recyclable_load_kg、violations[]。事件按实际评估流程产生，禁止仅凭终点电量线性插值。
结果包含 scene_revision、solver_version、seed、complete、feasibility、unserved_ids、duplicated_ids、violations、objective_terms、vehicle_plan。用户图上改节点后，旧结果仍指向旧revision。

## 5. 实施

A. 严格场景schema与导入，修NaN/车辆对应，扩展evaluator逐站轨迹；CLI能读取同一JSON。
B. API、immutable场景/方案、求解回调与取消；先无UI运行场景→JSON轨迹。
C. SVG编辑工作台、点位列表、约束表、undo/redo、求解与进度。
D. 路线/时间轴联动、greedy/ACO比较、手工排序后重评、问题定位与导出。
E. 制作三类案例：普通可行、需要充电、收紧容量/时间窗后搜索不可行；所有指标由实际引擎生成。

## 6. 完成场景

12点样例求解与baseline可复现；选择任一站，载重相对上一站增加其收集需求，等待/服务/充电与总时间一致。
改车辆隔舱或某点时间窗后保存新版本，旧路线不漂移，能看见结果差异及违规点。
手动交换两个站后全量重评；所有点一次服务、遗漏和重复真实检测。搜索失败不写“已证明无解”。
导出场景再导入，数值/ID保持；逐站CSV与图表同源。取消、刷新、断线以及大窗口/窄屏列表都能操作。

