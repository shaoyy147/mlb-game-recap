# baseball-replay

把 MLB 已完成比赛的 play-by-play 数据拉取下来，渲染成静态 HTML，并发布到 GitHub Pages。

## 功能

- 定时抓取最近 14 天内已完成比赛
- 增量同步：已存在且 HTML 仍在的比赛会跳过重抓
- 为每场比赛生成：
  - 回放 HTML 页面
- 生成导航首页，列出最近两周比赛
- 生成 `games.json` 作为已有比赛索引
- 自动清理两周之前的旧页面
- 通过 GitHub Actions 自动发布到 GitHub Pages

## 目录结构

- `scripts/sync_site.py`
  同步最近两周比赛，生成 `docs/`
- `scripts/render_play_by_play.py`
  将单场比赛 JSON 渲染为 HTML
- `templates/play_by_play_page.html.j2`
  单场比赛页面模板
- `templates/index_page.html.j2`
  GitHub Pages 首页模板
- `docs/`
  GitHub Pages 实际发布目录

## 本地运行

安装依赖：

```bash
python -m pip install -r requirements.txt
```

同步最近两周比赛并生成站点：

```bash
python scripts/sync_site.py
```

生成完成后，主要文件在：

- `docs/index.html`
- `docs/games/`
- `docs/games.json`

## GitHub Actions

工作流文件：

- `.github/workflows/publish-pages.yml`

行为：

- 每天 `16:00 UTC` 到次日 `02:30 UTC` 之间，每 10 分钟自动运行一次
- 也可以手动触发
- 自动抓取最近两周已完成比赛
- 自动提交 `docs/` 变更
- 自动部署到 GitHub Pages
- workflow 的 cron 以 `UTC` 配置；运行环境显式设置为 `America/Los_Angeles`

## GitHub Pages 配置

仓库设置里需要：

1. 打开 GitHub 仓库的 `Settings`
2. 进入 `Pages`
3. 将 Source 设置为 `GitHub Actions`

## 当前规则

- 首页只作为回放导航，不展示比分
- 比赛页面展示：
  - 半局分节
  - at-bat 结果
  - 换投
  - 盗垒
  - runner 出局相关信息
  - at-bat 前的垒包、outs、比分

## 数据来源与取数逻辑

### 数据来源

项目当前使用 MLB Stats API 官方接口数据。

主要接口有两类：

1. 赛程接口

```text
https://statsapi.mlb.com/api/v1/schedule
```

用途：
- 获取最近两周比赛列表
- 判断比赛是否已经结束
- 获取比赛日期、球场、主客队信息

2. 单场 live feed 接口

```text
https://statsapi.mlb.com/api/v1.1/game/<gamePk>/feed/live
```

用途：
- 获取完整比赛 live feed
- 从中裁剪出 play-by-play 数据
- 获取 box score、球队信息、比分、先发投手、先发阵容

### 同步脚本如何工作

同步脚本在：

- `scripts/sync_site.py`

整体流程：

1. 以洛杉矶时区计算“今天”
2. 回看最近 14 天日期
3. 对每一天调用赛程接口
4. 只保留已经完成的比赛
5. 对每场完成比赛调用 live feed 接口
6. 如果 `docs/games.json` 里已有该比赛且对应 HTML 仍存在，则直接复用并跳过重抓
7. 对新增比赛生成比赛 HTML、导航页和比赛清单
8. 删除 14 天之外的旧页面

### 页面实际使用的数据

虽然同步时抓的是完整 live feed，但比赛页实际主要使用两类数据：

1. play-by-play

用于生成：
- 半局分节
- at-bat 事件流
- 盗垒
- 换投
- runner 出局
- 比分变化
- 垒包与 outs 状态推导

同步脚本会从 live feed 里在内存中裁剪出这些字段：
- `allPlays`
- `currentPlay`
- `scoringPlays`
- `playsByInning`

2. box score

box score 信息当前主要用于补充：
- 先发阵容守位
- batting order
- 球员/球队补充信息

同步脚本不会把 box score 单独保存为站点文件，而是直接从单场 live feed 中读取并用于渲染页面。

### 比赛页各模块的数据拼装方式

1. Header

来源：
- live feed / schedule 中的主客队信息

用途：
- 生成 `ARI @ PHI` 这类对阵标题

2. Starting Pitchers

来源：
- play-by-play 中最早出现的 `top` / `bottom` 打席

规则：
- `top` 半局面对的投手视为主队先发
- `bottom` 半局面对的投手视为客队先发

3. Starting Lineups

来源优先级：

1. live feed 中的 box score
2. play-by-play 回退

规则：
- 优先从 box score 的 `players + battingOrder + position.abbreviation` 取 1-9 棒和守位
- 如果缺少 box score 信息，则回退为从 play-by-play 中按出现顺序抽取前 9 个不同 batter

4. at-bat 事件流

来源：
- `allPlays[*].result`

用途：
- 蓝色事件标签
- 描述文本
- at-bat 最终结果

5. 前置补充事件

来源：
- `allPlays[*].playEvents`

当前保留：
- `pitching_substitution`
- `stolen_base*`
- 非 pitch 的 runner 出局事件

规则：
- 插在对应 at-bat 结果前面
- 如果过程事件已经被最终结果吸收，则不重复展示

6. 垒包 / Outs / 当前比分

来源：
- `allPlays[*].runners`
- `allPlays[*].result.awayScore/homeScore`

规则：
- 按比赛顺序维护一个状态机
- 对每个 at-bat，显示该打席发生前的：
  - 垒包占用情况
  - outs
  - 比分
- 如果同一打席前有盗垒或牵制等补充事件，会先更新状态，再渲染该 at-bat 的信息栏

### 导航页使用的数据

导航页模板在：

- `templates/index_page.html.j2`

当前展示内容包括：
- 对阵缩写
- 比赛日期
- 球场
- 主客队全称
- 两队先发投手

这些信息来自：

- `docs/games.json`

而 `docs/games.json` 是同步脚本在抓取并整理最近两周比赛后统一生成的。

## 说明

- 当前同步脚本按洛杉矶时区理解 MLB 日期
- GitHub Actions 使用 Python 3.12
