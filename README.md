# 裁纸排版 API（Guillotine Cutting Plan API）

面向小型印刷店的裁纸排版服务：输入原纸尺寸、切缝宽度和成品清单，按**可旋转的铡刀式切割**规则（每一刀都贯穿当前子矩形）生成多张原纸的排版方案。每张原纸返回成品坐标、旋转标记、利用率，以及与坐标完全一致的二叉切割树；切缝计入每一次分割，成品互不重叠、不越界。支持 `locked_placements` 预先定位成品：服务先校验锁定布局能按切缝递归分解为铡刀切割树，再只在余料叶上放置剩余数量。

## 一键启动

```bash
docker compose up --build
```

依赖安装已包含在镜像构建中，无需任何额外步骤。

- API 地址：**http://localhost:8000**
- 交互式文档（Swagger UI）：http://localhost:8000/docs
- 健康检查：http://localhost:8000/health

### 配置

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `PORT` | `8000` | 宿主机映射端口，如 `PORT=9000 docker compose up --build` |

业务约束在 `app/models.py` 顶部以常量定义（最大尺寸 100000、最大切缝 1000、单种成品最大数量 100000、单批次成品总数上限 20000、锁定放置上限 200 等），可按需调整。

## API

### `POST /plans`

**请求体**

```json
{
  "stock": { "width": 900, "height": 600 },
  "kerf": 3,
  "items": [
    { "id": "flyer", "width": 210, "height": 148, "quantity": 8 },
    { "id": "card",  "width": 90,  "height": 55,  "quantity": 12 }
  ],
  "locked_placements": [
    { "item": "flyer", "x": 0, "y": 0, "rotated": false }
  ]
}
```

| 字段 | 类型 | 约束 | 说明 |
| --- | --- | --- | --- |
| `stock.width` / `stock.height` | number | `> 0` | 原纸宽/高 |
| `kerf` | number | `>= 0`，可省略，默认 `0` | 切缝宽度，每刀损耗的材料宽度 |
| `items[].id` | string | 可选，需唯一 | 成品标识，缺省自动生成 `item-1`、`item-2`… |
| `items[].width` / `items[].height` | number | `> 0` | 成品宽/高 |
| `items[].quantity` | integer | `>= 1` | 需求数量 |
| `locked_placements` | array | 可选，最多 200 项 | 预先定位在**第一张原纸**上的成品（见下文） |
| `locked_placements[].item` | string | 必填，须为某个 `items[].id` | 成品标识（也可引用自动生成的 `item-N`） |
| `locked_placements[].x` / `.y` | number | `>= 0` | 左上角坐标 |
| `locked_placements[].rotated` | boolean | 可选，默认 `false` | 是否旋转 90° |

**响应（200）**

```json
{
  "stock": { "width": 900.0, "height": 600.0 },
  "kerf": 3.0,
  "sheets": [
    {
      "index": 0,
      "width": 900.0,
      "height": 600.0,
      "utilization": 0.570444,
      "placements": [
        { "item": "flyer", "x": 0.0, "y": 0.0, "width": 210.0, "height": 148.0, "rotated": false, "locked": true }
      ],
      "cut_tree": {
        "type": "cut",
        "direction": "vertical",
        "x": 0.0, "y": 0.0, "width": 900.0, "height": 600.0,
        "children": [ { "…": "…" }, { "…": "…" } ]
      }
    }
  ],
  "summary": {
    "sheet_count": 1,
    "piece_count": 20,
    "used_area": 308040.0,
    "stock_area": 540000.0,
    "utilization": 0.570444
  }
}
```

- `placements[]`：每件成品的左上角坐标、放置后的宽高与 `rotated`（是否旋转 90°；旋转后 `width`/`height` 为原高/宽）。坐标系原点在**原纸左上角**，x 向右、y 向下。`locked` 标记是否为请求中预先定位的成品（`true`＝锁定，`false`＝排版器自动放置）。
- `utilization`：成品面积 ÷ 原纸面积，切缝与边角废料均计入损耗。
- `summary`：总用纸数、总件数与整体利用率，**锁定与自动放置的成品全部计入**。

### 锁定放置（`locked_placements`）

锁定放置用于保留预先定位的成品（例如按客户样张固定的位置），全部位于第一张原纸上：

- **计入需求**：某成品锁定 N 件，排版器只需再放置 `quantity - N` 件；锁定数量超过需求返回 `422`。
- **位置校验**：不得越界、不得互相重叠、`item` 必须存在，违反即返回 `422` 并指出具体锁定项。
- **可切割性校验**：锁定布局必须能按当前 `kerf` **递归分解为铡刀切割树**——任意两件成品之间、以及成品与原纸边缘之间的缝隙，要么为 0（kerf 为 0 时允许紧贴），要么至少容纳一个切缝，否则刀片无法把它们分开（即"占用切缝"）。无法分解时返回 `422`，`detail` 列出冲突的锁定项（编号、成品、坐标）。
- **余料放置**：校验通过后，排版器只在该切割树的可用余料叶上放置剩余数量（放不下时另开新纸），锁定成品的位置、朝向保持不变。
- 响应中锁定成品在 `placements` 里标记 `locked: true`；`cut_tree` 的 item 叶与锁定坐标及新增放置一一对应；`summary` 的数量与利用率覆盖全部成品。

### 二叉切割树（`cut_tree`）

每个节点都带有外接矩形 `x, y, width, height`，共三种节点：

- `cut`（内部节点）：一刀**贯穿当前整个子矩形**。
  - `direction: "vertical"`：竖直一刀，`children[0]` 为左侧、`children[1]` 为右侧；
  - `direction: "horizontal"`：水平一刀，`children[0]` 为上侧、`children[1]` 为下侧；
  - 第二个子节点的起点 = 第一个子节点的终点 + `kerf`（切缝被这刀消耗）。
- `item`（叶子）：一件成品，坐标与 `placements` 中的条目一一对应。
- `waste`（叶子）：剩余边角料（可回收利用的余料）。

切割顺序即自根向下的前序遍历。若余料宽度不足一个切缝（刀片会把它完全吃掉），则作为修边损耗，不再产生节点。

**示例**：竖切节点中 `children[1].x === children[0].x + children[0].width + kerf`。

### 错误

| 状态码 | 情形 |
| --- | --- |
| `422` | 输入校验失败：尺寸非正数、数量小于 1、成品列表为空、`id` 重复等，`detail` 含具体字段 |
| `422` | 某成品两种朝向都放不进一张原纸，`detail` 明确指出该成品，例如：<br>`item 'poster' (150.0 x 80.0) does not fit on stock 100.0 x 100.0 in either orientation` |
| `422` | 锁定放置非法：引用不存在的成品、越界、重叠、锁定数量超过需求，`detail` 指出具体锁定项 |
| `422` | 锁定布局无法按切缝分解为铡刀切割树，`detail` 列出冲突项，例如：<br>`locked placements cannot be decomposed into a guillotine cut tree with kerf 3.0; conflicting placements: #1 (item 'A', x=0.0, y=0.0, rotated=False), #2 (...)` |

## 本地开发

```bash
pip install -r requirements-dev.txt
pytest                 # 运行测试（含切割树一致性、不重叠、不越界校验）
uvicorn app.main:app --reload
```

## 排版算法说明

- 成品按尺寸从大到小排序，逐件放入所有已开纸张中**最紧凑的空余矩形**（最佳短边适配）；放不下则新开一张原纸。
- 每次放置用铡刀式切分空余矩形，切缝宽度从余料中扣除，因此结果天然可表示为二叉切割树。
- 成品可旋转 90°，引擎自动选择能放下的朝向；两种朝向都可行时优先不旋转。
- 若请求包含 `locked_placements`：先校验锁定布局（不越界、不重叠、数量不超过需求），再递归搜索能按切缝分解该布局的铡刀切割树——在任一子矩形内，若存在一刀贯穿的横切或竖切（切缝带不与任何成品相交）把成品分成两组，则分别递归；无法分解时返回 `422` 并指出冲突项。随后剩余数量只在第一张纸的余料叶（及后续新纸）上放置。
- 测试（`tests/check_plan.py`）对每份方案递归校验：每刀贯穿子矩形、切缝恰好等于 `kerf`、成品不重叠、不越界、树叶与坐标列表一致、利用率吻合。
