# Phira × Phigros 静态替换（GitHub Actions 一键构建）

把 **Phira App**「谱面库 → 未上架(Unstable) → 观赏(Visual)」分区替换为 **Phigros 全曲谱面**（327 张 IN + 56 张 AT，共 383 张），
所有资源托管在 **GitHub Releases**，列表/附属数据托管在 **GitHub Pages**。目标设备只需要 **Shadowrocket**，
**不需要**本地服务器、隧道或常开的电脑。

> 用法：把这个仓库推到你的 GitHub → 运行一次 Action（约 25-40 分钟）→ 手机上导入生成的 Shadowrocket 配置即可。

---

## 一、一次性准备（约 2 分钟）

1. 在 GitHub 新建一个 **Public** 仓库（名字随意，例如 `phira-phigros`）。
   - 必须是 Public：公开仓库的 Actions 分钟数与 Release 流量免费。
2. 把本目录推上去（二选一）：

   **A. 有 GitHub CLI（推荐）**
   ```bash
   gh auth login                     # 若尚未登录
   gh repo create phira-phigros --public --source=. --remote=origin --push
   ```
   **B. 没有 CLI**
   ```bash
   git remote add origin git@github.com:<你的用户名>/phira-phigros.git
   git push -u origin main           # 仓库里先建好（不要勾选 README/.gitignore 等文件）
   ```

## 二、运行一次 Action

GitHub 仓库页面 → **Actions** → 左侧 **Build & Publish Phira Static Data** → 右侧 **Run workflow** → Run。

- 默认参数：`songs=all`、`levels=IN,AT`（即全部 383 张：IN+AT）。
- 首次运行约 25-40 分钟：抓取 Phigros_Resource → 生成 383 个谱面包/曲绘 → 上传到 Releases → 部署 Pages。
- 完成后 Actions 页面会显示部署地址：`https://<你的用户名>.github.io/<仓库名>/`
- **只跑一次即可**；以后想更新数据再跑一次（数据有变化会自动换新 URL，App 缓存不会过期）。

## 三、手机（iPhone / iPad）使用

1. Shadowrocket → 配置 → 当前配置 `ⓘ` → **HTTPS 解密** → 生成 CA 证书 → 安装证书
   → 设置 → 通用 → 关于本机 → **证书信任设置** 打开信任 → 回到配置开启 **HTTPS 解密**。
2. Safari 打开生成的配置地址导入（或用模块）：
   - 独立配置：`https://<你的用户名>.github.io/<仓库名>/shadowrocket/phira-static.conf`
   - 已有自己代理配置的用户用模块：`.../shadowrocket/phira-static.sgmodule`
3. Shadowrocket 中使用该配置（模块用户：安装模块即可）。
4. Phira → 谱面库 → **未上架(Unstable)** → 筛选(漏斗) → 分区选 **观赏(Visual)**。
5. 即可浏览（翻页 1/14）、点开、下载、游玩。

> 部署完成后，`https://<你的用户名>.github.io/<仓库名>/` 页面里有完整的图文步骤。

## 四、可选参数

运行时可设置：
- `songs`：`all`（默认）或数字（如 `28` 只构建前 28 首，用于快速试跑）。
- `levels`：`IN,AT`（默认）或 `EZ,HD,IN,AT`（全部难度，数据量约 5-6GB、耗时更久）。

## 五、已知限制（静态版）

1. **搜索/排序/筛选不生效**：静态托管无法按查询动态过滤，搜索框与排序/标签/评分筛选都会返回固定顺序的分页列表；
   翻页本身正常。如需完整搜索，请使用仓库附带的 `../server/` + Cloudflare 隧道模式（动态列表接口，支持搜索/排序）。
2. 「热门 / 上架 / 特殊 / 本地 / 收藏 / 登录」等页面保持官方内容，不受影响。
3. 自定义谱面 id（99xxxx）的评分/排行榜/成绩为占位数据；游玩、曲绘、音乐不受影响。
4. 首次构建依赖 GitHub Actions 与 Releases/Pages 的可用性；GitHub CDN 在国内可能偏慢（可用代理节点加速资源下载）。
5. Phigros 曲绘/音乐/谱面版权归原权利人；本仓库只用于个人学习研究，请勿传播资源或商用。

## 六、目录结构

```
.github/workflows/build.yml   # 一键构建 + 发布
pipeline/build.py             # Phigros_Resource -> Phira 谱面包 + catalog.json
pipeline/validate.py          # 深度校验
publish/publish.py            # 生成静态分页/附属 JSON + 改写资源 URL + 生成配置
shadowrocket/*.tmpl           # Shadowrocket 配置模板（{{PAGES_BASE}} 自动替换）
```

## 七、数据来源

[7aGiven/Phigros_Resource](https://github.com/7aGiven/Phigros_Resource)（`info` / `chart` / `music` / `illustration` 分支）。
谱面包格式：`info.yml + chart.json(PGR) + music.ogg + illustration.jpg`，Phira 内置 PGR 解析器直接支持。
