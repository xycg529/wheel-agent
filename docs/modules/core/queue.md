# `core/queue.py` 逐段讲解

> 本篇讲运行中的用户交互通道（`TurnQueue` + `AskWaiter`）。上游是 [`ui/app/__init__.py`](../ui/app.md)（创建队列并分发输入），下游是 [`core/loop.py`](../../../core/loop.py)（消费三条通道）和 [`core/model.py`](model.md)（读 `abort` 旗取消流式）。

把「用户在键盘上敲的东西」和「工作线程里正在跑的 agent 循环」解耦成一组线程安全的对象：三条输入通道（steer / follow / abort）加一个跨线程 y/N 交接件。

- 行数：84 行
- 依赖：**无项目内依赖**，只用标准库 `collections.deque` + `threading.Event/Lock`。这是四个子包里最孤立的一个模块，因此也最容易被单独读懂。
- 被谁用：
  - [`ui/app/__init__.py`](../ui/app.md)（154–160 行）—— `ask_yes_no` 在非主线程时把提问代理进队列；`start_task` 每个任务新建一个 `TurnQueue`；`dispatch` 把运行中输入的文字转成 `steer()` / `follow()`。
  - [`core/loop.py`](../../../core/loop.py)（132 / 172 / 210–211 / 260 / 265–271 / 280 行）—— 消费三条通道。
  - [`core/model.py`](model.md)（596、866–871 行）—— UI 把 `queue.abort` 挂到 `model.abort`，模型客户端据此中途取消 HTTP 请求。
  - [`ui/app/live.py`](../ui/app-live.md)（292 行）—— `abort` 置位后抑制后续事件渲染。

## 目录

- [1. 模块定位与导入（1–8 行）](#1-模块定位与导入18-行)
- [2. `AskWaiter`：一次 y/N 询问的交接件（10–29 行）](#2-askwaiter一次-yn-询问的交接件1029-行)
- [3. `wait()`：为什么分段等 0.1 秒（23–29 行）](#3-wait为什么分段等-01-秒2329-行)
- [4. `TurnQueue` 的状态（32–40 行）](#4-turnqueue-的状态3240-行)
- [5. `_push` / `_drain`：两条通道的读写原语（42–54 行）](#5-_push--_drain两条通道的读写原语4254-行)
- [6. `steer` / `follow` / `drain_*`：四个薄包装（56–66 行）](#6-steer--follow--drain_-四个薄包装5666-行)
- [7. `request_ask`：工作线程发起询问（68–78 行）](#7-request_ask工作线程发起询问6878-行)
- [8. `pending_ask`：主线程轮询消费（80–84 行）](#8-pending_ask主线程轮询消费8084-行)
- [9. 从 queue 到 loop：三条通道的消费时机](#9-从-queue-到-loop三条通道的消费时机)

---

## 1. 模块定位与导入（1–8 行）

模块 docstring 把职责说全了：*运行中的三条输入通道 + y/N 询问的跨线程交接，线程安全*。

导入只有三个名字，各自有明确理由：

```python
from collections import deque   # 而非 list：append/popleft 都是 O(1)，不会被 list.pop(0) 拖成 O(n)
from threading import Event, Lock
```

**为什么需要线程安全。** 交互模式下任务跑在独立线程（`ui/app/__init__.py` 的 `start_task` 起的 `wheel-run` 线程，`run_task` → `run_agent` 全在里头），而**读键盘的权利属于主线程**（主线程的 `LineEditor` 占着 stdin 和终端的 raw mode）。两边要交换数据，就必须有个两边都能碰的对象——这个模块就是那个对象。主线程写（`steer` / `follow` / `abort.set` / `resolve`），工作线程读（`drain_*` / `abort.is_set` / `wait`）。

对比 `--json` 一次性任务：它不传 `queue`（`run_json_task` 里 `run_agent` 调用没有 `queue=` 参数），整个模块不参与，`run_agent` 里所有 `if queue` 分支直接跳过。

---

## 2. `AskWaiter`：一次 y/N 询问的交接件（10–29 行）

一次询问 = 一个 `AskWaiter` 实例，三个字段：

| 字段 | 用途 |
|---|---|
| `prompt` | 要问用户的话（主线程拿去显示） |
| `answer` | 回填的答案，默认 `False` |
| `_done` | `Event`，答案就绪的信号 |

两个方法对应两侧：

- **主线程侧** `resolve(yes)`（18–21 行）：拿到答案 → 填 `answer` → `set()` 唤醒等待方。
- **工作线程侧** `wait(abort=None)`（23–29 行）：阻塞等答案，返回 `bool`。

`answer` 默认 `False` 不是随手写的：**默认拒绝**。任何"没拿到明确同意"的路径（abort、异常、线程被杀）都落在 `False` 上，和 [`tools/safety.py`](../tools/safety.md) 里安全门"不确定就 ask/deny"的取向一致——宁可不动作，不擅自动作。

---

## 3. `wait()`：为什么分段等 0.1 秒（23–29 行）

这是整个模块最值得看的三行：

```python
while not self._done.wait(0.1):   # 分段等待：每 0.1s 检查一次 abort，保持可中断
    if abort is not None and abort.is_set():
        self.answer = False
        return False
return self.answer
```

直接 `self._done.wait()`（不带超时）会**一直阻塞到有答案为止**。而 Python 的 `Event.wait()` 只能等一个 `Event`，没有"等这个或那个"的原语。于是：

- 用户按了 `/stop`（`abort.set()`），但没人来回答这个 y/N —— 不带超时的 `wait()` 会让工作线程**永久挂起**，任务停不下来，终端也回不来。
- 分段等待把它变成：每 100ms 醒一次看 abort，置位就按"拒绝"返回。

**代价是明摆着的**：答案延迟最多 100ms（人眼看不出来），以及等待期间每秒 10 次空转唤醒（一次 isinstance 级别的成本，可忽略）。换来的是"任何阻塞点都可被 `/stop` 打断"。

同一个模式在项目里出现三次：[`core/model.py`](model.md) 的 `_sleep_abortable`（912–923 行，重试退避按 0.1s 切片轮询 abort）和 `_await_abortable`（843–877 行，HTTP 请求在后台线程跑、主线程按 0.05s 轮询）——**凡是会阻塞的地方，都切成小片顺便查 abort**。这是全局一致的可中断策略，`queue.py` 是最短的那个例子，适合先读它建立直觉。

注意 abort 分支**不置 `_done`**（只改 `answer` 并 return），这一点在第 10 节里有个衍生竞态。

---

## 4. `TurnQueue` 的状态（32–40 行）

```python
self._steer: deque[str] = deque()
self._follow: deque[str] = deque()
self._lock = Lock()
self.abort = Event()
self._ask: AskWaiter | None = None
```

五份状态，可见性分三档：

- **私有**（`_steer` / `_follow` / `_lock`）：只经方法访问，外部碰不到。
- **公开**（`abort`）：**故意不加下划线**。它是要跨模块传递的信号——UI 把它赋值到 `model.abort`（`ui/app/__init__.py:213`），模型客户端的流式循环和 HTTP 等待线程都读它。做成属性而不是方法，就是为了能直接当句柄传出去。
- **半公开**（`_ask`）：有下划线，但跨模块被读了——`ui/app/__init__.py:434` 和 `471` 直接访问 `waiter._done`。见第 10 节。

类 docstring 一句话定死三条通道的语义，后面全部代码都围着它：

> `steer` = 并入下一轮模型调用；`follow` = 本轮正常结束后作为新回合；`abort` = 当前调用后停机。

---

## 5. `_push` / `_drain`：两条通道的读写原语（42–54 行）

```python
def _push(self, q, text):
    text = text.strip()
    if text:
        with self._lock:
            q.append(text)

def _drain(self, q):
    with self._lock:
        out = list(q)
        q.clear()
    return out
```

两个约定：

1. **空文本不排队**（45 行注释）：用户在忙时敲个回车，`strip()` 后是空串，没有语义，直接丢掉。
2. **一次取空**（50 行注释）：`drain` 不是 pop 一条，是把队列整个取走并清空。

"一次取空"和循环结构绑定：[`core/loop.py`](../../../core/loop.py) **只在两次模型调用之间看队列**，从不在模型调用或工具执行中途插话。所以一次 drain 拿到的是"这段时间内攒下的全部输入"，取完即清，不会残留到下一轮被重复消费。

锁的作用在这里才显出来：`list(q)` + `q.clear()` 是**两步操作**，必须原子，否则两条线程同时 drain 会丢消息或重复消息。而 `deque.append` 本身在 GIL 下单步是原子的——锁保护的是复合操作，不是单步。

---

## 6. `steer` / `follow` / `drain_*`：四个薄包装（56–66 行）

每个方法一行，转发到 `_push` / `_drain`：

```python
def steer(self, text):        self._push(self._steer, text)
def follow(self, text):       self._push(self._follow, text)
def drain_steer(self):        return self._drain(self._steer)
def drain_follow(self):       return self._drain(self._follow)
```

两条队列共用同一把锁——争用可以忽略（用户打字频率远低于模型调用），换来的好处是实现简单、不用考虑锁顺序（也就不会发生死锁）。

`steer` 和 `follow` 在**写入端完全一样**，区别只在**读取时机**（第 9 节）。这是个干净的设计：把"什么时候投递"的语义放在消费者（loop）而不是生产者（queue），queue 只管顺序和线程安全。

---

## 7. `request_ask`：工作线程发起询问（68–78 行）

工具执行需要用户批准时（比如 [`tools/safety.py`](../tools/safety.md) 判定 `ask`），调用链是：

```
工具（工作线程）→ SafetyGate.check → ask 回调 → ui/app.ask_yes_no → TurnQueue.request_ask
```

`ask_yes_no`（`ui/app/__init__.py:154–159`）先分流：**只有非主线程才走队列**：

```python
if queue is not None and threading.current_thread() is not threading.main_thread():
    return queue.request_ask(prompt)
return _ask_on_main(prompt)
```

主线程时直接问（没有别的线程在等，不存在争用 stdin）；工作线程时才需要交接。

```python
def request_ask(self, prompt):
    waiter = AskWaiter(prompt)
    with self._lock:
        self._ask = waiter          # ① 短暂持锁：挂上 waiter
    try:
        return waiter.wait(self.abort)   # ② 锁外阻塞！
    finally:
        with self._lock:
            if self._ask is waiter:
                self._ask = None    # ③ 再短暂持锁：摘下来
```

三个细节：

1. **阻塞必须发生在锁外**（71–73 行）：`wait()` 要等用户回答，可能几秒到几分钟。如果持着锁等，主线程的 `pending_ask()` 会立刻死锁——整个终端冻住。所以持锁范围被切成"挂上"和"摘下"两小段，中间那段不持锁。
2. **`finally` 里用身份比较**（76–77 行）：`if self._ask is waiter` 而不是直接置 `None`。万一等待期间有别的 waiter 顶上来了（比如异常路径后又发起一次询问），这条判断保证不会把新 waiter 误清掉。
3. **abort 传进 wait**（73 行）：`waiter.wait(self.abort)` —— 用户等得不耐烦按 `/stop`，这次询问自动按拒绝返回，工具拿到 `False`，安全门走 deny 路径。

---

## 8. `pending_ask`：主线程轮询消费（80–84 行）

```python
def pending_ask(self) -> AskWaiter | None:
    """主线程轮询：有未决询问就回到主线程弹窗，避免嵌套 readline 抢输入。"""
    with self._lock:
        return self._ask
```

**为什么是轮询而不是回调。** 回答 y/N 必须回到主线程做，因为：

- 读 stdin 要在主线程的终端状态下进行；
- 工作线程里调 `input()` 会**嵌套一层 readline**，把主线程的 `>` 提示藏掉、抢走正在编辑的那半行输入（`_ask_on_main` 的 docstring 明确写了这个理由）。

主线程在自己的忙等循环里（`ui/app/__init__.py:429–445`，以及非 TTY 版本 467–478）每隔一轮检查一下 `pending_ask()`，拿到就：

```python
waiter.resolve(_ask_on_main(waiter.prompt))
```

忙等循环本身的节奏（读键超时 0.12s / `select` 超时 0.15s）就是轮询周期——**不需要额外线程，也不需要信号**。

代价：从工具提问到主线程显示，最坏有 0.12–0.15s 延迟，期间工具线程阻塞在 `wait()`。对这个量级的交互完全够用。

---

## 9. 从 queue 到 loop：三条通道的消费时机

queue 只保证"顺序 + 线程安全"，语义由 [`core/loop.py`](../../../core/loop.py) 的读取位置决定。对照 [docs/loop-explained.md](../../loop-explained.md) 第 5、8、11 节：

### abort —— 一回合查三次

| 位置 | 时机 |
|---|---|
| loop.py:132 | **循环顶部**：上一轮结束、这一轮开始前 |
| loop.py:172 | 模型调用返回后 |
| loop.py:260 | 工具批次执行完后 |

关键设计：**abort 不打断正在进行的模型调用**。三个检查点全在"轮与轮之间"，所以 `/stop` 的效果是"跑完这一轮就停"，而不是硬杀。真正的硬中断交给模型客户端——UI 把同一个 `abort` 旗挂到 `model.abort`（`ui/app/__init__.py:213`），[`core/model.py`](model.md) 在流式读取每个事件时查一次（595–597 行，置位就抛 `KeyboardInterrupt`），HTTP 等待线程按 0.05s 轮询并调 `cancel()`（866–871 行）。

于是中止是**两层**的：loop 负责轮间的干净收尾（保留已完成回合、写 `agent_end` 事件），model 负责流式途中的快速放弃。

`run_agent` 捕获 `KeyboardInterrupt` 时也会反向置旗（loop.py:280），让 UI 侧（`live.py:292` 抑制渲染、`on_delta` 丢弃增量）同步进入中止态。

### steer —— 两处消费，最多一轮延迟

- **轮尾**（loop.py:265–271）：工具执行完、下一轮模型调用前 `drain_steer()`，消息作为 user 消息 push 进上下文，发 `steer_delivered` 事件。
- **无工具调用分支**（loop.py:210）：模型说完了、准备停了，也先看一眼 steer，有就 `continue` 再跑一轮。

所以 steer 的承诺是"**下一次模型调用就能看到**"，延迟上限是一次模型调用（通常几百毫秒到几秒）。这就是 README 里"回车输入文字 = steer"能即时生效的原因。

### follow —— 只在一处消费

**只有 loop.py:211 一处**，在"模型本轮没有调任何工具"（即任务本来要正常结束）的分支里，和 steer 一起被 drain：

```python
pending.extend(queue.drain_steer())
pending.extend(queue.drain_follow())
```

顺序是 **steer 优先于 follow**：两条通道都有内容时先发 steer。这个分支的语义是"任务本来要停了，但用户还排了话"——把排队的消息变成新的 user 回合，`continue` 接着跑。

**因此 follow 的承诺是"任务正常结束后接着干"**，它不会打断任务：模型还在调工具的回合里，`follow` 的内容只是静静排队。

---
