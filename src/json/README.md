# jsonReader 模块（src/json/jsonReader.py）

> 改代码前先读本文件。只负责读配置并缓存，无业务逻辑。

## 路径
- 实现：`src/json/jsonReader.py`（单文件，无第三方依赖）
- 配置目录：`<项目根>/configs`（`parents[2]/"configs"` 推导，与 CWD 无关）
- 文件：`configs/<name>.json`，如 `jsonReader("config")` → `configs/config.json`

## 生命周期与 API
`import jsonReader` 时预构造并缓存 `config`、`modules`（各读一次；文件缺失/非法 JSON 抛 `RuntimeError`，空文件视为 `{}`）。此后只读内存，改配置需重启；同名唯一（`jsonReader("config") is config`）。

外部唯一用法：`from jsonReader import config, modules`，不要构造实例。`__all__ = ["config", "modules"]`。

| 对象 | 文件 |
| --- | --- |
| `config` | `config.json` |
| `modules` | `modules.json` |

实例成员：
- `data`：整份配置（只读）
- `get(path="", default=None)`：点号路径取值，缺失返回 `default`；空串返回整份
- `reader["a.b"]`：同 `get`，缺失抛 `KeyError`
- 只读属性 `name`、`path`
- list 用下标：`get("items.0.name")`；键名含点时优先整键匹配

## 日志（写 `core.log`）
自动把 `src/logger` 补进 `sys.path`，用 logger 模块的 `core_log` 对象；每次加载写一条：
- 成功：`读取XXX配置文件完成`（info）
- 失败：`读取XXX配置文件失败, 原因: REASON`（error），随后仍抛 `RuntimeError`；REASON 为文件不存在 / 读取文件出错 / 第 N 行第 M 列不是合法 JSON

XXX 为配置名（不含路径）；命中缓存不重复读、不重复写。

## 名字校验
构造时校验，非法抛 `ValueError`（类型错 `TypeError`），不静默改写：非空；无 `<>:"/\|?*` 与控制字符；不以点结尾/不全为点；主体 ≤ 250 字符；`"config.json"` 等价 `"config"`。

## 新增配置文件
`configs/xxx.json` + 底部加一行并加入 `__all__`：
```python
__all__ = ["config", "modules", "xxx"]
...
xxx: Final[jsonReader] = jsonReader("xxx")
```

## 注意
- 导入：`sys.path.insert(0, "src/json")` 后 `import jsonReader`。
- 本目录与标准库 `json` 同名：不要再加 `json.py` / `__init__.py`，否则遮蔽标准库。
- 业务代码禁止拼 `configs/...` 路径，统一走本模块。