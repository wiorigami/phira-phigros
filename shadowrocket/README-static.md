# 静态模式（GitHub Pages / Releases）Shadowrocket 配置 — 验证记录

这是 **静态托管模式** 的配置模板说明与引擎验证记录：谱面 zip/图片等大资源放在 **GitHub Releases**，
列表/详情/stub JSON 放在 **GitHub Pages**，设备端只需要 Shadowrocket（+ 已信任的 MITM CA），
**不需要本地服务器或 Cloudflare 隧道**。

- 模板文件：`phira-static.conf.tmpl`（独立配置）、`phira-static.sgmodule.tmpl`（模块，适合已有配置的用户）
- 唯一占位符：`{{PAGES_BASE}}`（例：`https://<owner>.github.io/<repo>`），由 `publish.py` 在生成配置时替换。
  `{{PAGES_BASE}}/popular/page<N>.json`、`{{PAGES_BASE}}/chart/<id>.json`、`{{PAGES_BASE}}/stub/*.json` 都由发布脚本写入 Pages。
- 已验证客户端：Shadowrocket **2.2.77 (build 3014)**，macOS Mac Catalyst 版（本机真实引擎验证）。

---

## 1. POST `/play/upload` 的机制（关键结论）

**GitHub Pages 只接受 GET/HEAD，会拒绝 POST**，因此不能像 GET 那样 302 到 Pages。
在 Shadowrocket 2.2.77 上实测，**`[Map Local]` 可以在本地直接合成 200 响应，完全不走网络**，是静态模式的最佳机制：

```ini
[Map Local]
^https?://phira\.5wyxi\.com/play/upload data-type=base64 data="eyJpZCI6MSwiZXhwRGVsdGEiOjAuMCwibmV3QmVzdCI6ZmFsc2UsImltcHJvdmVtZW50IjowLCJuZXdSa3MiOjAuMH0=" header="Content-Type: application/json"
```

- 语法（与 Surge 兼容）：`<URL 正则> data-type=<类型> data="<数据>" [header="<响应头>"]`
  - `data-type` 可选：`text` / `file` / `tiny-gif` / `base64`；省略时默认 `file`
  - 这里用 **base64** 编码 JSON，避免 JSON 里的引号在配置文本里转义出错
  - `header="Content-Type: application/json"` 保证客户端按 JSON 解析
- 解码后的 body 恰好是：
  `{"id":1,"expDelta":0.0,"newBest":false,"improvement":0,"newRks":0.0}`（68 字节）
- 实测：`POST https://phira.5wyxi.com/play/upload` → `HTTP 200`、`Content-Type: application/json`、body 完全一致，Python `json.load` 解析通过。
- 该机制**不需要网络**、不受 Pages 限制，也比 `[Script]`（需脚本托管/引擎）更简单可靠。

**评分接口 `/chart/<id>/rate`** 同时支持 GET（查询我的评分）与 POST（提交评分），
也统一改用 `[Map Local]` 直接返回 `{"score":0}`（GET/POST 均实测 200 + JSON）：

```ini
^https?://phira\.5wyxi\.com/chart/9[0-9]{5}/rate data-type=base64 data="eyJzY29yZSI6MH0=" header="Content-Type: application/json"
```

> 模板里因此**没有** `/chart/<id>/rate` 的 302 规则（否则 POST 会跟随 302 变成对 Pages 的请求而失败）。

---

## 2. 列表分页规则（6 种参数顺序）

客户端 `/chart?...` 的 query 参数顺序随机（HashMap），而静态模式没有动态服务器做参数解析，
因此用 **6 条规则覆盖 `type=2` / `division=visual` / `page=<N>` 三者的 6 种相对顺序**，
每条规则用捕获组取页号（注意每条规则的捕获组序号不同）：

```ini
^https?://phira\.5wyxi\.com/chart\?(.*)type=2(.*)division=visual(.*)page=([0-9]+)(.*)$ {{PAGES_BASE}}/popular/page$4.json 302
^https?://phira\.5wyxi\.com/chart\?(.*)type=2(.*)page=([0-9]+)(.*)division=visual(.*)$ {{PAGES_BASE}}/popular/page$3.json 302
^https?://phira\.5wyxi\.com/chart\?(.*)division=visual(.*)type=2(.*)page=([0-9]+)(.*)$ {{PAGES_BASE}}/popular/page$4.json 302
^https?://phira\.5wyxi\.com/chart\?(.*)division=visual(.*)page=([0-9]+)(.*)type=2(.*)$ {{PAGES_BASE}}/popular/page$3.json 302
^https?://phira\.5wyxi\.com/chart\?(.*)page=([0-9]+)(.*)type=2(.*)division=visual(.*)$ {{PAGES_BASE}}/popular/page$2.json 302
^https?://phira\.5wyxi\.com/chart\?(.*)page=([0-9]+)(.*)division=visual(.*)type=2(.*)$ {{PAGES_BASE}}/popular/page$2.json 302
```

其他参数（`tags=`/`search=`/`rating=`/`order=`/`pageNum=`…）被 `(.*)` 吸收，不影响匹配。
`pageNum=` 不会误匹配 `page=`（要求字面 `page=`）。

## 3. 附属接口（GET）

```ini
^https?://phira\.5wyxi\.com/chart/(9[0-9]{5})$                    {{PAGES_BASE}}/chart/$1.json 302
^https?://phira\.5wyxi\.com/record/best/(9[0-9]{5})(\?.*)?$        {{PAGES_BASE}}/stub/best.json 302
^https?://phira\.5wyxi\.com/record/list15/(9[0-9]{5})(\?.*)?$      {{PAGES_BASE}}/stub/record-list.json 302
^https?://phira\.5wyxi\.com/user/(9[0-9]{5})(\?.*)?$               {{PAGES_BASE}}/stub/user.json 302
```

## 4. MITM / 规则

- `[MITM] hostname = phira.5wyxi.com`（模块版：`hostname = %APPEND% phira.5wyxi.com`）
- 不含任何 CA/私钥；用户需在 Shadowrocket 生成 CA → 安装 → 在系统「证书信任设置」信任 → 对当前配置开启 HTTPS 解密
- `[Rule]`：`phira.5wyxi.com`、`github.io`、`githubusercontent.com`、`github.com`、`objects.githubusercontent.com` 全部 `DIRECT`，`FINAL,DIRECT`

---

## 5. 引擎实测证据（Shadowrocket 2.2.77 Mac，本地 Pages 模拟）

测试方法：把模板的 `{{PAGES_BASE}}` 替换为本地静态站 `http://127.0.0.1:18888`
（用 `python3 -m http.server` 提供 `popular/page1..15.json`、`chart/990001.json`、`stub/*.json`），
生成临时配置（内嵌本机测试用 CA）导入 Shadowrocket 并「使用配置」，
通过本地代理 `curl -x http://127.0.0.1:1082 -k` 请求真实域名 `https://phira.5wyxi.com/...`（MITM 生效，叶子证书由 MITM CA 签发）。

| 检查项 | 结果 |
|---|---|
| 6 种参数顺序（page=3/4/5/6/7/8）+ 两种额外顺序（page=1、page=14） | 全部 `302`，`Location: http://127.0.0.1:18888/popular/page<N>.json`，页号正确 |
| `GET /chart/990001` | `302 → {{PAGES_BASE}}/chart/990001.json`；跟随得到 Chart JSON |
| `GET /record/best/990001` | `302 → stub/best.json`；跟随 `{"score":0,"accuracy":0.0,"fullCombo":false}` |
| `GET /record/list15/990001?std=false` | `302 → stub/record-list.json`；跟随 `[]` |
| `GET /user/990001` | `302 → stub/user.json`；跟随最小 user JSON |
| `GET /chart/990001/rate` | `200`（Map Local）`{"score":0}`，`Content-Type: application/json` |
| `POST /chart/990001/rate` | `200`（Map Local）`{"score":0}` |
| `POST /play/upload` | `200`（Map Local），body 精确为 `{"id":1,"expDelta":0.0,"newBest":false,"improvement":0,"newRks":0.0}`，JSON 解析通过 |
| 负向对照 `/chart/popular` | 未改写：官方 `200`，`count=56`，无 `Location` |
| 负向对照 `type=2&division=regular` | 未改写：官方 `200`，`count=9114` |
| 负向对照 `type=0&division=visual` | 未改写：官方 `200`，`count=0` |
| 负向对照 `type=1&division=regular` | 未改写：官方 `200`，`count=50` |
| 负向对照 `/me` | 未改写：官方 `401 UNAUTHENTICATED` |
| `page=99`（超出静态页范围） | 模板仍会 302 到 `page99.json`（Pages 上不存在 → 404）；客户端按列表 `count` 计算总页数，正常不会请求越界页，见限制 |

示例（真实全参 query，参数顺序随机）：

```
GET https://phira.5wyxi.com/chart?search=&rating=0.3%2C1&order=-updated&division=visual&pageNum=28&page=14&tags=&type=2
→ 302 Location: http://127.0.0.1:18888/popular/page14.json
```

## 6. 已知限制（静态模式）

1. **搜索无效**：App 的搜索请求仍是 `/chart?...search=...`，静态规则按 `page` 直接返回固定页 JSON，
   不会过滤内容（选搜索后可能看到与当前页相同的固定列表）。后续可用 `[Script]` 在前端过滤，但尚未实现。
2. **排序/标签/评分筛选无效**：同上，静态页为发布时固定顺序（`order/`、`tags=`、`rating=` 参数被忽略）。
3. **翻页边界**：`page` 超过发布页数时会重定向到不存在的 `pageN.json`（Pages 404）。
   页面 JSON 里的 `count` 让 App 计算出正确页数，正常使用只会请求 1..N 页。
4. **GitHub Pages 大小限制**：单文件 <100MB、仓库/Pages 站点建议 <1GB（大 zip 放 Releases 规避）。
5. 静态版没有服务端逻辑，筛选/搜索等高级功能需改成客户端脚本（`[Script]`）实现。
6. 模块版（.sgmodule）的语法与配置相同、机制已验证；但**模块形态**（通过 Shadowrocket「模块」安装）未单独做引擎级验证，
   独立 `.conf` 形态已完整验证。若模块安装后不生效，优先使用独立 `.conf`。

---

*本文件是验证记录，不随用户发布；用户实际导入的是 `publish.py` 用真实 `{{PAGES_BASE}}` 生成的
`phira-static.conf` / `phira-static.sgmodule`。*
