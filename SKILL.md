---
name: cycling-route-gpx
description: Export iGPSPORT (迹驰) roadbooks by route ID, convert a saved Amap (高德地图) cycling route share link to GPX, or plan a new Amap cycling route from an ordered start, waypoints, and destination and export it as GPX.
---

# 骑行路线导出 GPX

用本 Skill 目录下的 `scripts/route_gpx.py`。需要 Python 3.10+ 和 curl。以下命令中的脚本相对本 Skill 目录，输出路径按当前任务的工作区选择。每次交付 GPX 文件链接、路线名、里程和轨迹点数。

## iGPSPORT 路书编号

```bash
python3 scripts/route_gpx.py igpsport 892051 --output /path/to/892051.gpx
```

直接用官方导出端点。国内站是默认值，用户明确使用国际站时加 `--region intl`。编号可能在站点之间重叠，通过默认站点导出失败时先确认账号所在站点。若服务明确需要授权，用用户可访问的官方网页导出，或接收用户提供的原始文件。直接下载成功后保存原始轨迹，脚本会修复已知的 GPX 1.1 元数据格式问题。

## 高德已有骑行路线

```bash
python3 scripts/route_gpx.py amap-share 'https://surl.amap.com/…' --output /path/to/amap.gpx
```

支持含有起终点和有序途经点的高德自行车路线短链，也接受该短链展开后的高德网页链接。脚本使用高德返回的道路轨迹，保留经过同一地点的次数和顺序。结果为按分享点位重新规划的路线，可能与 App 原方案不同。回复时标明重新规划，并对照分享里程（若有）。普通地点链接、无法解出路线的分享或其他交通模式需要可用的自行车路线分享，或明确的地点列表。

用户分享有效骑行链接后，优先用本机已配置的 Key 继续导出。缺少 Key 时，按下方的简短提示引导用户去申请。Key 无效时，提示先回高德检查它的服务平台是否选了「Web 服务」，并附上申请说明。

## 高德新骑行路线

```bash
python3 scripts/route_gpx.py amap-plan --city 上海市 \
  --point 世纪公园 --point 上海科技馆 --point 东方明珠广播电视塔 \
  --output /path/to/amap.gpx
```

按用户说出的顺序传 `--point`，第一个为起点，最后一个为终点。可用城市、区和入口名称消歧。有多个候选时，脚本返回候选名称、地址和坐标。按上下文能确定时用对应的高德坐标重试，否则询问用户选哪一个地点。单独查候选可用 `amap-search '地点' --city '城市'`。指定高德坐标时用 `--point '名称|经度,纬度' --coordinates gcj02`，GPS 原始坐标用 `--coordinates wgs84`。查询出的地点坐标始终由高德提供。

## 获取高德 Web 服务 Key

缺少 Key 时，用下面的步骤引导用户。链接保留为可点击的文字。

> 已识别这条骑行路线。第一次导出，需要先在高德申请一个 Key。
>
> 1. 打开[高德开放平台控制台](https://console.amap.com/)登录。没有账号就先注册。
> 2. 打开[应用管理](https://console.amap.com/dev/key/app)，点「创建新应用」，名字可以填「骑行路线导出」。
> 3. 点「添加 Key」，服务平台选 **Web 服务**。 [高德图文指引](https://lbs.amap.com/api/webservice/create-project-and-key)
> 4. 把生成的 **Key 发到这里**，我就用刚才的链接继续导出 GPX。

已有 Web 服务 Key 的用户可以直接从第 4 步开始。收到 Key 后，从当前对话找到尚未完成的高德路线请求并继续。用交互式终端重试原命令，加 `--prompt-key`，等终端出现提示再输入 Key。将 Key 用于本次导出，回复中省略 Key。

用户也可选择在电脑上预先设置 Key。macOS 在「钥匙串访问」创建通用密码项目，名称用 `cycling-route-gpx-amap`，账户用当前用户的短用户名，密码填 Key。也可为运行 Skill 的进程设置环境变量 `AMAP_WEBSERVICE_KEY`。设置后在原对话回复「继续导出」即可。

## 运行细节

- 高德 Web 服务自行车接口每次规划两点。脚本按顺序逐段调用，处理频率限制，并将道路轨迹的 GCJ-02 坐标近似转换为 WGS84。若相邻路段接不上，GPX 会保留分段，回复时说明。
- 路段失败时，说明哪一段以及原因。只有完整的道路轨迹才会写出 GPX。若输出已存在，另选文件名。
