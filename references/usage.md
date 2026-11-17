# 密钥与使用说明

## 密钥配置

本 Skill 不附带密钥。首次使用高德或百度时，需要用户申请自己的密钥；已有配置则直接使用。两家凭证不能互相替代，iGPSPORT 导出不需要地图密钥。

### 高德

1. 注册并登录[高德开放平台控制台](https://console.amap.com/)，按平台要求完成开发者认证。
2. 在[应用管理](https://console.amap.com/dev/key/app)创建应用（名称可填 `cycling-route-gpx`）、添加 Key，平台选择 **Web 服务**。
3. 按[官方申请指引](https://lbs.amap.com/api/webservice/create-project-and-key)检查骑行规划和地点查询接口权限；如设置 IP 白名单，填写实际执行环境的公网出口 IP。
4. 按下方“如何提供密钥”配置 `AMAP_WEBSERVICE_KEY` 或安全输入，然后继续原请求。

### 百度

1. 注册并登录[百度地图开放平台控制台](https://lbsyun.baidu.com/apiconsole/key)，按平台要求完成开发者认证。
2. 创建应用（名称可填 `cycling-route-gpx`），类型选择 **服务端**，启用 **骑行路线规划（轻量）**；按名称规划时还需要 **地点检索 3.0**。
3. 请求校验方式选择 **IP 白名单校验**，当前脚本暂不支持 SN 签名。白名单填写运行 Skill 的环境访问百度时使用的 **公网出口 IP**，不是 `192.168.x.x` 或 `127.0.0.1`。换网络或代理后可能需要更新；云端 Agent 应填写云端出口 IP。
4. 按下方“如何提供密钥”配置 `BAIDU_MAP_AK` 或安全输入，然后继续原请求。

临时测试也可把百度白名单填为 `0.0.0.0/0`，表示允许任意 IPv4 来源；持有 AK 的人也可能使用配额，测试后建议收紧为实际出口 IP。参见[百度官方校验说明](https://lbsyun.baidu.com/faq/api?title=webapi%2Fappendix)。

### 如何提供密钥

- **首次配置并记住**：运行 `python3 scripts/route_gpx.py configure-key amap`（高德）或 `configure-key baidu`（百度），等提示后输入，不回显。地图命令末尾加 `--prompt-key` 也会输入并保存；再次运行可替换旧密钥。
- **后续使用**：正常执行原命令，不加 `--prompt-key`，自动读取已存凭证，不再询问。环境变量优先于凭证库；如更换密钥后仍使用旧值，检查同名环境变量。
- **macOS**：直接使用系统钥匙串，无额外 Python 依赖。服务名为 `cycling-route-gpx-amap` 或 `cycling-route-gpx-baidu`，账户为当前用户短用户名；可在“钥匙串访问”中删除对应项目，让 Skill 忘记密钥。读写均通过 Python 调用系统接口，系统可能提示授权 Python 访问。
- **Windows/Linux**：可选安装 `python3 -m pip install keyring`，并配置 Windows Credential Manager、Secret Service 或 KWallet 系统后端。当前脚本不接受明文文件后端；无可用后端时说明未保存，不创建明文密钥文件。
- **无交互终端或云端 Agent**：使用产品的持久密钥管理，向运行环境注入 `AMAP_WEBSERVICE_KEY` 或 `BAIDU_MAP_AK`。仅设置一次进程环境变量并不代表跨会话保存。
- **仅临时使用**：通过进程环境变量提供密钥，脚本不会将环境变量另存到凭证库。

无需把密钥发到聊天中，也不要写入 Skill 文件夹、代码、示例、测试、日志或分发包。首次提供后保存到 Skill 之外的系统凭证库；保存成功后，后续会话直接读取。此记忆属于当前系统用户和运行环境：云端 Agent 无法自动使用本机钥匙串，换设备需重新配置。保存失败时应说明原因，不声称已记住。配置完成后，在新的命令进程运行 `python3 scripts/route_gpx.py check-key amap --verify-api`（百度换为 `baidu`）。成功表示凭证可读取且地点查询接口可用，不代表所有路线接口权限或未来配额都正常。确认 `source` 为 `system_store` 才说明此次使用了系统凭证库；环境变量优先时显示 `environment`。然后继续原请求，无需重新提供路线。

若 Agent 执行环境禁止写入钥匙串，在系统终端运行上述 `configure-key` 命令完成一次配置。若后续读取也受限，应解决钥匙串访问权限或改用 Agent 的持久密钥管理，而不是反复索取密钥。

## 命令示例

自然语言规划未指定地图时，先让用户明确选择高德或百度，并遵循下方“预览、确认与导出”流程。分享链接按链接所属平台处理。以下命令在 Skill 目录中运行，默认输出 JSON，加入 `--format text` 可显示中文摘要。省略输出路径时写入当前目录的 `outputs/`，自动避免同名覆盖。`amap-plan` 和 `baidu-plan` 会立即规划并保存 GPX；Agent 不应在自然语言路线待确认时运行它们，也不应在确认后用它们替代已展示的缓存轨迹。

```bash
python3 scripts/route_gpx.py auto 18154 --format text
python3 scripts/route_gpx.py auto '高德或百度骑行分享链接' --format text
python3 scripts/route_gpx.py igpsport 18154 --output ./route.gpx
python3 scripts/route_gpx.py baidu-preview --city 北京市 \
  --point 奥林匹克森林公园 --point 鸟巢 \
  --output ./beijing.json
```

高德按地点规划使用 `amap-plan`，参数对应相同。高德按名称规划可省略城市，但为减少歧义建议提供；百度按名称规划需要城市。地点坐标查询只在规划内部使用，不提供独立的地点搜索命令。遇到同名候选时，先确认点位再继续导出。

显式坐标示例：`--point '起点|116.400000,39.900000' --coordinates gcj02`。CLI 一律使用“经度,纬度”；脚本会按百度接口要求自动调整参数顺序。GPS 坐标选择 `wgs84`。百度全程显式坐标还可选择 `bd09ll`；不支持将 bd09ll 显式坐标与名称搜索结果混用。

## 百度分享支持范围

- 支持 `j.map.baidu.com` 短链，及其展开后的网页骑行参数 `s=cycle&c=…&sn=…&en=…`。
- 支持 `baidumap://map/bikenavi` 坐标导航链接，以及官方格式的 `viaPoints` 有序途经点。
- 暂不解析仅在 App/网页脚本内部保存的路线；网页链接含有尚未支持的额外路线参数时也会停止，提示提供有序地点列表。地点分享、驾车、电动车及其他模式不能当作自行车路线导出。
- 网页短链只含起终点时按这两个点规划。没有原方案轨迹或里程就不声称已复原原方案或进行过里程比对。

## 规划与输出

地图规划采用各平台返回的第一个自行车方案，逐段保留用户的地点顺序。百度明确传入普通自行车模式 `riding_type=0`，高德使用自行车专用接口。返回道路轨迹统一为 GCJ-02，再近似转换为 WGS84 写入 GPX。

自然语言的路线偏好由 Agent 转为经过核实的途经点，并检查接口返回的道路。脚本不能单凭“骑行”模式保证满足“全程滨江骑行道”等额外条件；不满足或无法核实时，应在路线图和说明中标注差异，让用户确认。

### 预览、确认与导出

高德和百度都执行“规划 → 绘图展示 → 等待确认 → 缓存导出”。无需临时编写 Python 脚本。

```bash
# 先规划，生成 route.json 和 route.html；不生成 GPX
python3 scripts/route_gpx.py amap-preview --city 上海市 \
  --point '已确认的起点|121.500000,31.200000' \
  --point '已确认的终点|121.510000,31.210000' --coordinates gcj02 \
  --note '示例坐标，请替换为已核实点位；此段道路类型待确认' \
  --output ./outputs/route.json

# 需要重新生成展示文件时，不联网规划
python3 scripts/route_gpx.py render-preview ./outputs/route.json \
  --output ./outputs/route-view.html

# 仅在用户看图并确认后执行，填入展示时返回的完整 sha256
python3 scripts/route_gpx.py export-preview ./outputs/route.json \
  --confirmed-sha256 '展示时返回的sha256' --output ./outputs/route.gpx
```

百度使用 `baidu-preview`，其余参数对应相同。按名称规划需要 `--city`；跨城市先分别查询并核实点位，再传显式坐标。首末点是起终点，中间保持用户指定顺序。

- 预览 JSON 保存原始 GCJ-02 轨迹、分段、道路名称、点位和待确认事项；HTML 绘图时转换为 WGS84，导出从原始缓存转换一次。缓存中没有 Key/AK，也不保存到 Skill。
- HTML 可在浏览器中查看，支持缩放、道路选择和起终点标记；在线底图使用 OpenStreetMap。若当前 Agent 支持内嵌地图，可从同一缓存绘制。需实际展示并检查图，不只发送路线文字。
- 地图组件或底图加载失败会明确提示；离线保留真实轨迹示意，不能声称已展示完整地图。此时修复网络或用 Agent 可用的地图绘制方式再展示。绿道颜色只依据道路名称，不能当作现场通行证明。
- 用户调整后使用新文件名生成、展示并重新确认。提问、查看图或沉默不是导出许可；`--confirmed-sha256` 也不是用户授权的替代品。
- 导出不读取密钥、不调用地图 API；确认标识不匹配时停止。缓存丢失需重规划、重展示并重确认。已有文件不会覆盖。


自动去除连续相同的轨迹点，不删除路线回环。相邻路段间隙大于 2 米时保留不同 GPX 分段；同一路段内部 API 步骤之间的间隙超过 2 米时视为数据异常并停止。能比较输入坐标与道路端点时，超过 100 米给出核对提示；分享里程存在且差值超过 10% 和 500 米时提示里程差异。这些阈值是本工具的提示规则。

JSON 结果包含 `provider`、`mode`、`warnings`；地图路线还包含有序 `stops` 和逐段 `legs`。`mode` 为 `download`、`planned` 或 `replanned`，分别代表原始下载、新规划或分享重新规划。

网络默认单次超时 25 秒、额外重试 2 次。可用 `--timeout 60 --retries 1` 调整。证书错误、权限错误、错误编号、无效密钥和配额耗尽不会用自动重试掩盖。重试耗尽后停止并报告原因。

## 接口依据与验证

- [高德骑行规划](https://lbs.amap.com/api/webservice/guide/api/newroute)
- [百度轻量级骑行规划](https://lbsyun.baidu.com/docs/webapi?title=directionlite%2Fguide%2Fwebservice-lwrouteplanapi%2Fcycling)
- [百度地点检索 3.0](https://lbsyun.baidu.com/docs/webapi?title=placev3%2Fguide%2Fwebservice-placeapiV3%2FinterfaceDocumentV3)
- [百度导航 URI 与 viaPoints](https://lbsyun.baidu.com/docs/webapi?title=mapadjustment%2Furi%2Fharmony)

离线回归测试：在仓库根目录运行 `python3 -B -m unittest discover -s tests -v`。测试使用模拟接口响应和虚构点位，不需要密钥。百度网页短链解析、两家官方自行车规划、规划内部的地点坐标查询与 GPX 导出均已完成真实验证；高德分享使用构造参数调用真实接口，App 短链展开仍待验证。单次成功不代表所有分享格式均受支持，离线测试不代表在线服务成功。

路线图使用 [Leaflet 1.9.4](https://leafletjs.com/reference-1.9.4.html) 和 OpenStreetMap 在线底图；离线测试不等于底图联网加载成功。
