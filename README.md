# 骑行路线导出 GPX

一个用于导出骑行路书的通用 Agent Skill，采用 `SKILL.md` 与配套脚本的目录结构，不依赖 Codex 专有接口，支持 **iGPSPORT（迹驰）路书导出**、**高德骑行分享路线转 GPX**，以及**按地点顺序规划高德骑行路线**。也可以直接通过命令行使用。

生成的 GPX 文件可导入支持该格式的码表或骑行软件。导出结果包含文件路径、路线名称、里程和轨迹点数。

## 支持哪些功能？

| 场景 | 需要提供什么 | 如何生成路线 |
| --- | --- | --- |
| iGPSPORT 路书导出 | 路书编号 | 从官方导出接口下载轨迹 |
| 高德骑行分享转 GPX | 高德自行车路线分享链接 | 提取起终点和有序途经点，调用高德骑行接口逐段重新规划 |
| 规划新的骑行路线 | 城市、起点、途经点和终点 | 查询地点后，按指定顺序调用高德骑行接口规划 |

> **高德路线确实使用高德官方的自行车骑行规划。** 脚本调用 `/v5/direction/bicycling`，使用接口返回的道路轨迹，每段采用返回的第一个方案。分享路线保留点位及经过顺序，但重新规划结果可能与高德 App 中原先选择的方案不同。

## Agent 兼容性

本项目的目录结构符合 [Agent Skills 开放规范](https://agentskills.io/specification)的基本要求：`SKILL.md` 包含 `name`、`description` 和操作说明，`scripts/` 提供可独立运行的 Python 脚本。代码不依赖特定模型、Agent SDK 或 Codex 专有工具。

| Agent 能力 | 使用方式 |
| --- | --- |
| 支持 Agent Skills，且允许执行脚本 | 按该产品的方式导入整个 Skill 目录，由 Agent 读取指令并运行脚本 |
| 不支持原生 Skill，但能读取文件、执行命令 | 让 Agent 阅读 `SKILL.md`，并按说明调用 `scripts/route_gpx.py`；可能需要配置工具入口 |
| 只能聊天，无法执行脚本或访问网络 | 无法直接完成导出，需要接入具备这些能力的执行环境 |

**不局限于 Codex，但不保证所有 Agent 产品都能零配置运行。** 实际导出需要 Python 3.10+、curl、网络访问、输出文件写入权限，以及目标产品授予的相应工具权限；高德功能还需要 Web 服务 Key。

`--prompt-key` 需要交互式终端。没有交互式终端的 Agent 应通过其运行环境或密钥配置功能提供 `AMAP_WEBSERVICE_KEY`。macOS 钥匙串只是可选的本机配置方式，云端 Agent 无法自动读取本机钥匙串。

目前仅在本仓库的 Codex 使用流程中完成过 iGPSPORT 实际导出验证；其他 Agent 产品及高德导出流程尚未逐一实测。跨产品兼容性判断基于文件格式和运行依赖检查。

## 安装

运行环境：**Python 3.10+**、**curl**，以及可访问对应服务的网络。脚本仅使用 Python 标准库，无需额外安装 Python 包。

不同 Agent 产品的 Skill 导入方式和目录可能不同，请以目标产品的说明为准。下面以 `~/.agents/skills/` 为安装目录示例；仅当目标产品支持该目录或已配置读取该目录时使用：

```bash
mkdir -p ~/.agents/skills
git clone https://github.com/PIXELLL7-UX/cycling-route-gpx.git ~/.agents/skills/cycling-route-gpx
```

如果目标目录已存在，请先检查已有版本。也可以下载仓库 ZIP，解压后将包含 `SKILL.md` 和 `scripts/` 的文件夹命名为 `cycling-route-gpx`，放入目标 Agent 产品支持的 Skill 目录。

## 在对话中使用

在支持 Skill 加载与脚本执行的 Agent 中安装后，可以提出这样的请求：

- “使用 cycling-route-gpx，导出 iGPSPORT 路书 3074600 的 GPX。”
- “把这个高德自行车路线分享链接转成 GPX：〈粘贴链接〉。”
- “规划上海世纪公园 → 上海科技馆 → 东方明珠广播电视塔的骑行路线，导出 GPX。”

高德功能需要自己的 **高德 Web 服务 Key**；iGPSPORT 导出不需要这个 Key。

## 命令行使用

先进入实际安装的 Skill 目录，以下仍以 `~/.agents/skills/` 为例：

```bash
cd ~/.agents/skills/cycling-route-gpx
```

### 1. 导出 iGPSPORT 路书

```bash
python3 scripts/route_gpx.py igpsport 3074600 --output ./3074600.gpx
```

默认使用国内站。使用国际站时加上 `--region intl`。不同站点的路书编号可能重叠，请选择路书实际所在的站点。若官方服务要求登录或访问权限，需要通过有权限的官方网页导出。

### 2. 高德分享路线转 GPX

将示例中的链接替换为从高德 App 复制的自行车路线分享链接：

```bash
python3 scripts/route_gpx.py amap-share 'https://surl.amap.com/你的分享链接' --output ./amap-share.gpx --prompt-key
```

支持包含起终点和有序途经点的骑行路线短链，也支持短链展开后的高德网页链接。普通地点分享链接不包含完整的路线信息。

### 3. 按地点顺序规划路线

```bash
python3 scripts/route_gpx.py amap-plan --city 上海市 \
  --point 世纪公园 --point 上海科技馆 --point 东方明珠广播电视塔 \
  --output ./amap-plan.gpx --prompt-key
```

第一个 `--point` 是起点，最后一个是终点，中间的是途经点。地点存在多个候选时，脚本会返回候选名称、地址和坐标，供确认后重试。

也可以指定坐标，例如 `--point '起点|121.500000,31.200000'`；同时用 `--coordinates gcj02` 表示高德坐标，或用 `--coordinates wgs84` 表示 GPS 坐标。

## 配置高德 Key

1. 登录[高德开放平台控制台](https://console.amap.com/)。
2. 在[应用管理](https://console.amap.com/dev/key/app)中创建应用并添加 Key，服务平台选择 **Web 服务**。
3. 运行高德相关命令时加上 `--prompt-key`，根据终端提示输入 Key，输入内容不会回显。

申请步骤可参考[高德官方图文指引](https://lbs.amap.com/api/webservice/create-project-and-key)。

也支持以下配置方式；配置后可省略 `--prompt-key`：

- 为运行脚本的进程设置环境变量 `AMAP_WEBSERVICE_KEY`。
- macOS：在“钥匙串访问”中创建通用密码项目，名称为 `cycling-route-gpx-amap`，账户为当前用户的短用户名，密码填写 Key。

请勿将真实 Key 提交到仓库或公开的 Issue。

## 导出行为与限制

- **高德路线**：按顺序逐段规划，保留重复经过同一地点的次数和顺序；将高德返回的 GCJ-02 坐标近似转换为 GPX 使用的 WGS84 坐标。
- **路线衔接**：相邻路段无法衔接时，GPX 保留分段，结果中会报告未衔接处的数量。
- **失败处理**：任何一段规划失败时，报告对应路段和原因，不生成不完整的路线文件。
- **文件保护**：不会覆盖已有输出文件，需要更换文件名。
- **iGPSPORT 轨迹**：保留下载的原始轨迹，必要时修复已知的 GPX 1.1 元数据格式问题。

## 文件说明

- [SKILL.md](SKILL.md)：供兼容 Agent 读取的 Skill 指令。
- [scripts/route_gpx.py](scripts/route_gpx.py)：下载、规划和 GPX 导出入口。
- [scripts/coordinates.py](scripts/coordinates.py)：GCJ-02 与 WGS84 坐标转换。
- [scripts/coordinates-LICENSE.txt](scripts/coordinates-LICENSE.txt)：坐标转换代码的第三方许可证。

坐标转换实现改编自 [eviltransform](https://github.com/googollee/eviltransform)，相关版权与许可说明保留在上述许可证文件中。
