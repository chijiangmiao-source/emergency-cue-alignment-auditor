# 应急插播计划比对服务

Python 3.13 + FastAPI 纯后端。接收含 `planned`、`actual` 两数组的 JSON，用加权序列差异匹配对齐应急插播计划与实播日志，找出漏播、多播与漂移，并给出确定性的首个违规位置。

## 代价模型

| 操作 | 条件 | 代价 |
| --- | --- | --- |
| `MATCH` | 两项 `code` 相同且时间差绝对值 ≤ **2000 ms** | 时间差绝对值（ms） |
| `DELETE` | 计划项无对应实播（漏播） | **2500** |
| `INSERT` | 实播项无对应计划（多播） | **2500** |

对齐使**总成本最小**。总成本并列时，按**完整操作串字典序**裁决，操作顺序固定为 `MATCH` < `DELETE` < `INSERT`。操作串与路径一一对应，因此重复 `code` 也只有一条最优路径，同一请求逐次调用结果逐字节一致。

## 合规判定

仅当路径**全为 `MATCH`** 且每项漂移 **≤ 500 ms** 时 `compliant = true`。否则响应携带总成本、完整配对，以及路径中**从左到右首个缺陷**。

缺陷机器码（稳定）：

| 代码 | 含义 |
| --- | --- |
| `MISS` | 计划项被删除（漏播） |
| `EXTRA` | 实播项被插入（多播） |
| `DRIFT` | 匹配成功但漂移 > 500 ms |

## API

### `POST /align`

请求体（JSON 对象，`planned`、`actual` 两数组参与校验，可选 `alternative_limit` 开启候选路径；其余顶层字段被忽略）：

```json
{
  "planned": [{"code": "ADS1", "at_ms": 1000}],
  "actual": [{"code": "ADS1", "at_ms": 1200}]
}
```

每个数组项**仅含**两个字段：

- `code`：字符串，必须匹配 `[A-Z0-9]{1,16}`
- `at_ms`：非负整数（布尔、浮点、字符串均拒绝）

每个数组的 `at_ms` 必须**严格递增**，否则整体返回 422。

可选顶层字段 `alternative_limit`：**1 至 20 的非布尔整数**，省略时请求解析、响应字段与序列化顺序完全保持现状。它表示在最优路径之外最多附带多少条候选路径；候选与首选路径一起按“总成本、完整操作串”全局排序，`MATCH`、`DELETE`、`INSERT` 的先后不变，由每个后缀至多保留 `limit + 1` 个候选的动态规划与回溯生成——既不枚举路径，也不反复复制完整操作串（候选只保存本步操作与后继后缀的节点引用，后缀在候选间共享）。合法路径不足 `limit + 1` 条时返回实际数量，结果中不会出现重复路径。

200 响应：

```json
{
  "compliant": true,
  "total_cost": 200,
  "pairs": [
    {
      "op": "MATCH",
      "cost": 200,
      "code": "ADS1",
      "planned_index": 0,
      "actual_index": 0,
      "planned_at_ms": 1000,
      "actual_at_ms": 1200,
      "drift_ms": 200
    }
  ],
  "first_defect": null
}
```

不合规时 `first_defect` 为路径中从左到右首个缺陷，例如：

```json
"first_defect": {
  "code": "MISS",
  "pair_index": 1,
  "pair": {"op": "DELETE", "cost": 2500, "code": "ADS1", "planned_index": 1, "planned_at_ms": 2000}
}
```

`pairs` 中 `DELETE` 项只带 `planned_*` 字段，`INSERT` 项只带 `actual_*` 字段，`MATCH` 项两者俱全并附 `drift_ms`。

启用 `alternative_limit` 后，200 响应在原有字段之后追加 `alternatives` 数组（字段顺序即序列化顺序，旧字段位置不变），每项包含 `total_cost`（该候选总成本）、`cost_gap`（相对首选的非负成本差）、`pairs`（与顶层同构的完整配对）、`compliant`、`first_defect` 与 `first_divergence_index`——候选与首选两条完整操作串从左到右**首次不同的操作索引**（双方前缀到该索引前完全一致，该索引处操作不同），供播控复核判断首个缺陷是否依赖裁决：

```json
"alternatives": [
  {
    "total_cost": 3000,
    "cost_gap": 0,
    "pairs": [
      {"op": "DELETE", "cost": 2500, "code": "A", "planned_index": 0, "planned_at_ms": 0},
      {"op": "MATCH", "cost": 500, "code": "A", "planned_index": 1, "actual_index": 0, "planned_at_ms": 1000, "actual_at_ms": 500, "drift_ms": 500}
    ],
    "compliant": false,
    "first_defect": {
      "code": "MISS",
      "pair_index": 0,
      "pair": {"op": "DELETE", "cost": 2500, "code": "A", "planned_index": 0, "planned_at_ms": 0}
    },
    "first_divergence_index": 0
  }
]
```

候选按“总成本、完整操作串”全局有序（`cost_gap` 单调非降），路径两两不同；`first_divergence_index` 越大分叉越靠后。合法路径总数不足 `limit + 1` 时，`alternatives` 只含实际可生成的候选数（空序列对只有一条路径时为 `[]`）。

### `GET /healthz`

返回 `{"status": "ok"}`，供容器健康检查使用。

## 422 校验错误

任何校验失败整体返回 422，不做部分对齐。响应格式稳定：

```json
{
  "error": {
    "code": "VALIDATION_FAILED",
    "message": "request body failed validation",
    "details": [
      {"code": "INVALID_CODE", "path": "planned[0].code", "message": "must be a string matching [A-Z0-9]{1,16}"}
    ]
  }
}
```

`details` 按确定顺序（结构错误 → 数组项错误 → 单调性错误）携带稳定机器码：

| 机器码 | 含义 |
| --- | --- |
| `INVALID_PAYLOAD` | 请求体不是合法 JSON 或不是 JSON 对象 |
| `MISSING_FIELD` | 缺少 `planned` / `actual` / `code` / `at_ms` |
| `INVALID_TYPE` | 数组或数组项类型错误 |
| `UNEXPECTED_FIELD` | 数组项含 `code`、`at_ms` 以外的字段 |
| `INVALID_CODE` | `code` 不是匹配 `[A-Z0-9]{1,16}` 的字符串 |
| `INVALID_AT_MS` | `at_ms` 不是非负整数（含布尔、浮点、字符串、负数） |
| `NOT_STRICTLY_INCREASING` | 数组时间未严格递增 |
| `INVALID_ALTERNATIVE_LIMIT` | `alternative_limit` 不是 1–20 的非布尔整数（含 `0`、`21`、负数、布尔、浮点、字符串、`null`） |

同一非法请求逐次返回的响应体逐字节一致。

## 本地开发

```bash
pip install -r requirements-dev.txt
pytest                      # 穷举小序列对照暴力枚举（含候选前列名次）+ 平局/边界/422 用例
uvicorn app.main:app --port 8000
```

测试以暴力枚举全部对齐路径为独立判据：首选路径穷举长度为 0–4、时间网格含恰好 500/2000 ms 边界的全部序列对（81×81）；候选路径在长度 0–3 的全部网格序列对上逐项核对前 `limit + 1` 名（`limit` 取 1、2、3、20），另有种子化随机模糊用例与显式平局、正成本差、候选不足用例。

## Docker

```bash
docker compose up api                      # 默认映射 8000 端口
API_PORT=9000 docker compose up api        # 由 API_PORT 覆盖宿主机端口
docker compose up --exit-code-from verify  # 一次性验收：19 项检查，退出码即结果
```

`verify` 服务依赖 `api` 健康检查后启动，对运行中的 API 执行合规、500/2000 ms 边界、重复码平局、多条等成本路径、候选路径正成本差与零成本分叉、候选不足、旧请求逐字节兼容、逐次一致性与 422 机器码等验收，全部通过则以 0 退出。算法复杂度为 O(n·m·limit) 时间与空间（n、m 为两数组长度；省略 `alternative_limit` 时退化为 O(n·m)）。
