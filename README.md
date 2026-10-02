# Game Token Market（游戏内代币交易市场后端）

单进程、零外部依赖的游戏内代币交易市场后端。支持限价委托、价格优先 / 时间优先撮合、
部分成交、资金托管与结算、请求幂等、重启恢复与稳定分页查询。

- 语言/运行时：Python 3.14，FastAPI（标准库线程池承载同步命令）
- 存储：进程内存 + 本地仅追加 WAL（`data/events.log`），不依赖数据库等外部服务
- 并发：撮合引擎内单把可重入锁串行化所有写命令，命令结果等价于某种合法串行执行

## 运行与测试

```bash
uv sync                                  # 安装依赖
uv run python main.py                    # 启动服务（端口 0 = 随机端口）
MARKET_DATA_DIR=./data uv run uvicorn main:app --host 127.0.0.1 --port 8000
uv run python -m pytest -q               # 全量自动化测试
```

数据目录由环境变量 `MARKET_DATA_DIR` 指定，默认 `./data`。

## 数据模型

- 交易对（Instrument）：`GEM-GOLD`（base=GEM，quote=GOLD）、`ORE-GOLD`。
  符号使用连字符，便于直接作为 URL 路径段；成交价以最小货币单位的**整数**表示。
- 账户（Account）：每种资产分 `available`（可用）与 `frozen`（托管冻结）两个账桶，
  守恒恒等式 `available + frozen = 总量`。
- 委托（Order）：限价单，`BUY` / `SELL`，含 `orig_qty / filled_qty / status`。
- 成交（Trade）：吃单方与挂单方按**挂单价格**成交。

## 撮合语义

1. 委托进入市场后先与对手盘撮合，未成交（或部分成交剩余）部分进入挂单簿。
2. 优先级：**价格优先，时间优先**。买盘取价格最高、卖盘取价格最低；同价按委托
   到达顺序（单调时钟 + 委托序号）排序。
3. 允许部分成交。部分成交后委托以 `PARTIALLY_FILLED` 留在簿上；完全成交置为
   `FILLED` 并在下次撮合时惰性移出堆；撤单置为 `CANCELLED` 并立即释放冻结。
4. 成交价等于被吃的挂单价（maker price）。
5. 价格不交叉不成交（卖一价 > 买价，或买一价 < 卖价）。

## 托管（Escrow）与结算规则

提交委托时立即按委托价 × 量冻结：

- `BUY`：冻结 quote 资产 `price * qty`；
- `SELL`：冻结 base 资产 `qty`。

可用余额不足则拒绝（返回 `accepted=false`，`reject_reason=INSUFFICIENT_FUNDS`），
不产生挂单、不产生冻结。

成交时按成交量结算（以买入吃单为例）：

- 买方：按 `limit * qty` 解冻 quote（冻结按限价预留），其中实际花费
  `成交价 * qty` 付给卖方，价格改善差额 `(limit - 成交价) * qty` 退回买方可用；
  同时收到 `qty` 的 base。
- 卖方：从冻结中扣除 `qty` base，收到 `成交价 * qty` quote。

撤单时，剩余未成交量对应的冻结**按原冻结口径**释放：买单释放
`剩余量 * limit` quote，卖单释放剩余量 base。

任意时刻对每种资产满足：全账户 `available + frozen` 之和不变（守恒），
且簿上所有卖单剩余量之和 = GEM 冻结总量、买单 `剩余量 * limit` 之和 =
GOLD 冻结总量（测试中的 `assert_books_are_consistent` 持续校验）。

## 并发与原子性

- 所有写命令在引擎的同一把锁内执行，模拟撮合 → 持久化 → 应用 三阶段串行。
- 撮合“模拟”阶段只读不写（不会修改挂单堆），先产出整批事件；随后整批一次性追加
  WAL 并 fsync；最后才把事件折叠进内存状态。
- 持久化失败时内存状态从未被改动，因此不会留下部分冻结 / 部分成交 / 半个挂单，
  失败后可立即继续提交（见 `tests/test_failure_injection.py`）。
- 持久化成功但进程在“落盘之后、应用之前”崩溃时，重启重放会补齐该批事件，
  账户与挂单恢复为一致状态，客户端凭原 `client_request_id` 只会得到首次结果。

## 幂等

带 `client_request_id` 的提交 / 撤单请求按 `(操作类型, 账户, 请求标识)` 去重：

- 重复请求返回首次响应（`duplicate=true`），响应是首次结果的逐字快照；
- 不产生第二份委托、不发生第二次冻结或结算；
- 被拒绝的请求也记录幂等结果；幂等记录本身是 WAL 事件，重启后继续生效；
- 不带 `client_request_id` 的请求不做幂等（每次为新委托）。

## 持久化与重启恢复

WAL 文件：`events.log`，帧格式（小端）：

```
[payload_len: uint32][crc32(payload): uint32][payload: JSON UTF-8]
```

- 一个命令产生的多个事件作为一次 `write` 批量追加并 `fsync`，要么整批可见、
  要么整批不可见；崩溃在写入中产生的残缺尾帧（长度不足或 CRC 不符）在下次打开时
  被丢弃并截断，之后服务继续正常追加。
- 事件类型：`instrument` / `deposit` / `order` / `trade` / `cancel` /
  `request_record`。启动时顺序重放全部事件重建账户、挂单簿、成交序列、
  id/时钟序列与幂等表；重放后可继续撮合且 id 不冲突。
- 当前为单文件 WAL（无快照 / 无压缩）；数据量超大时可在此格式之上增加快照。

## HTTP 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET  | `/` | 健康检查（保留原入口） |
| GET  | `/instruments` | 交易对列表 |
| POST | `/accounts/{account}/deposit?asset=&amount=` | 测试/运营用入账 |
| GET  | `/accounts/{account}` | 账户各资产 available/frozen/total |
| POST | `/orders` | 提交限价委托（body 见下） |
| POST | `/orders/cancel` | 撤单 `{account, order_id, client_request_id?}` |
| GET  | `/orders?account=&symbol=&start_time=&end_time=&cursor=&limit=` | 活动挂单分页 |
| GET  | `/depth/{symbol}?levels=` | 聚合买卖盘口 |
| GET  | `/trades/{symbol}?start_time=&end_time=&cursor=&limit=` | 成交时间范围 + 游标分页 |

提交委托 body：

```json
{
  "account": "alice",
  "symbol": "GEM-GOLD",
  "side": "BUY",
  "price": 5,
  "qty": 20,
  "client_request_id": "optional-unique-id"
}
```

分页：`cursor` 为不透明 base64（内含“已返回的最后一个 id”），按 id 升序稳定翻页；
时间字段为引擎单调时钟（纳秒，进程内严格递增，等于 `created_at`）。
非法 cursor 返回 400，参数校验失败返回 422；未知交易对 / 委托返回 404；
撤销非活动委托返回 409。

## 目录结构

```
main.py              # FastAPI 入口（启动钩子装配服务，保留 / 路由）
app/models.py        # 领域模型与 API schema
app/errors.py        # 领域错误
app/storage.py       # 仅追加 WAL（CRC 校验、坏尾截断、失败注入钩子）
app/engine.py        # 账户台账、挂单簿、撮合、事件应用与重放
app/service.py       # 进程级装配：store + engine + 默认交易对 + 恢复
app/api.py           # HTTP 路由
tests/               # 撮合 / 并发 / 幂等 / 恢复 / 失败注入 / HTTP 测试
```

## 测试覆盖

- `test_matching.py`：价格/时间优先、部分成交、冻结、撤单释放、拒绝不冻结、分页
- `test_concurrency.py`：并发撮合守恒、撤单单赢家、并发幂等只冻结一次
- `test_idempotency.py` / `test_cancel_idempotency.py`：提交/撤单幂等与重启生效
- `test_recovery.py`：余额/冻结/挂单/成交恢复、恢复后继续撮合、WAL 坏尾截断
- `test_failure_injection.py`：写入失败原子性、失败后可继续、落盘后崩溃恢复、并发失败
- `test_api.py`：HTTP 全链路、状态码、盘口深度与稳定分页
