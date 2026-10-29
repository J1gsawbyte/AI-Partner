# logger 模块（src/logger/logger.py）

> 改 `logger.py` 前先读本文件。只负责写日志文件，无业务逻辑。

## 路径
- 实现：`src/logger/logger.py`（单文件，无第三方依赖）
- 日志根：`<项目根>/logs`（由 `logger.py` 位置推导 `parents[2]/"logs"`，与 CWD 无关）
- 会话目录：`<项目根>/logs/YYYY-MM-DD-HH-MM-SS/`（模块首次 import 时刻命名）
- 日志文件：`<会话目录>/<logname>.log`
- 会话目录上限：`configs/config.json` 的 `logger_folders_max`（默认 `10`，含本次新建的）

## 生命周期
1. `import logger`：读 `logger_folders_max`（失败或非法按 `10`）→ 若已有目录数达到上限，删除最旧的一个 → 生成会话目录名并 `mkdir -p`；创建失败抛 `RuntimeError`。
2. 构造 `logger(name)`：只校验名字、算路径，不打开文件、不预建空文件。
3. 每次 `info/warn/error/debug`：`open("a", encoding="utf-8", newline="\n")` 写入后立即关闭。调用返回即落盘；换行固定 `\n`。
4. 同进程实例共享会话目录；每次重启新目录。

## API
外部唯一推荐用法：`from logger import core_log, transport_log`，不要构造实例（`__all__ = ["core_log","transport_log"]`）。

| 对象 | 文件 |
| --- | --- |
| `core_log` | `core.log` |
| `transport_log` | `transport.log` |

**类**：`logger(name) -> instance`
- `name`：日志名，须符合 Windows 命名（见下）；`"core.log"` 等价 `"core"`。
- 方法（均接受任意对象，返回 `None`；非字符串用 `str()`，`None` 写空行）：
    info, warn, error, debug
- 只读属性：`name`（无后缀）、`path`（绝对路径 `Path`）。

**模块常量**：`LOG_ROOT`(Path)、`STARTUP_TIME`(datetime)、`SESSION_DIR`(Path)、`MAX_FOLDERS`(int)。排查路径时打印 `logger.path` 或 `SESSION_DIR`。

## 格式
- 每行 `[HH:MM:SS] [LEVEL] msg`；日期由会话目录承载。
- 不做级别过滤、不限单文件大小、不压缩；滚动只发生在重启时。

## 会话目录上限（`logger_folders_max`）
- 每次启动在 `logs/` 下新建一个会话目录，`logs/` 里最多保留该数量的会话目录。
- 已达上限时，删掉**最旧**的一个再建新的，因此第 11 次启动后仍只有 10 个目录。
- 「最旧」按目录名的字典序取（时间戳定宽，字典序即时间序），不看 `stat` 时间。
- 只处理名字严格符合 `YYYY-MM-DD-HH-MM-SS` 的子目录，`logs/` 里别的文件/文件夹不动。
- 删除失败（被占用等）只跳过不报错，保证程序照常启动；本次删除会在新会话的 `core.log` 里留一条 `WARN`。
- `logger.py` 用标准库 `json` 直接读 `configs/config.json`（`jsonReader` 依赖本模块，反向引用会循环导入），读不到就用默认值。

## 日志名校验（构造时；非法抛 `ValueError`，类型错抛 `TypeError`，不静默改写）
- 去首尾空白后非空
- 禁字符 `< > : " / \ | ? *` 及 ASCII 控制字符 0x00–0x1F
- 名字主体 ≤ 251 字符
- 不能以点结尾 / 不能全为点
- 保留设备名（忽略大小写，`con.txt` 也拒绝）：`CON PRN AUX NUL COM1-9 LPT1-9`
- 示例：`"123"`✓、`"core.log"`✓、`"a/b"`✗、`"con"`✗

## 新增日志文件
只改 `logger.py` 底部一处 + 头部 `__all__`：
```python
__all__ = ["core_log", "transport_log", "db_log"]
...
db_log: Final[logger] = logger("db")
```