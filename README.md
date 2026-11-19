# pattern-workbench（图案提取工作台 · 源码镜像）

> 这是 https://chule.top/pattern/ 的完整应用源码（含 duotu 子应用）。
> **本仓库为私有镜像，用于交付/协作/复刻参考，不参与线上部署。**
> 线上部署走原仓库 `<your-host>:/opt/pw-git/app.git`，push 即上线。

## 这是什么

一个 FastAPI + 原生 HTML/CSS/JS 的图案提取与生图工作台，**无 npm / 无构建链 / 无前端框架**。

| 板块 | 路由 | 说明 |
|---|---|---|
| 图案提取 | `/pattern/pt` | 1–5 张同图案不同角度原图 → 互补遮挡合并 → 出图 |
| 批量去水印 | `/pattern/wm` | 批量去水印 |
| 多图编辑 | `/pattern/duotu` | duotu 子应用（独立 Flask，动态挂载） |
| 高清修复 | `/pattern/hd` | 高清化 |
| 详情页生成 | `/pattern/dtl` | 电商详情页逐屏生成 |
| 详情页复刻 | `/pattern/fk` | 按参考图逐屏复刻 |
| 商品替换 | `/pattern/swap` | 场景图 + 物品图 → 替换 |
| AI 生图 | `/pattern/aigc` | 通用生图 |
| 素材库 | `/pattern/library` | 历史产物聚合 |

## 目录结构

```
.
├── server.py              入口（FastAPI，mount 7 个子应用）
├── accounts.py            账户 / 会话 / 积分账本
├── ai_sites.py            生图站点与三线分流（t2i / i2i / multi）
├── pipeline.py            AI 调用链（gen_image / chat_vision）
├── storage.py             文件存储
├── image2psd.py           PSD 分层导出
├── psd_recompress.py      PSD 压缩
├── static/                外壳 index.html + ui.css + 各页面
├── dtl/ fk/ aigc/ swap/ hd/ wm/    六个子应用（各自 app.py + frontend/）
├── duotu/                 多图编辑（Flask，⚠️ 目录名不可改，见下）
├── config/
│   └── ai_sites.example.json   站点配置模板（已脱敏）
└── docs/                  交付清单 / 架构说明 / 复刻任务书
```

## 本地跑起来

```bash
python -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate
pip install -r requirements.txt   # 若缺失，见下方"依赖"

cp config/ai_sites.example.json ./ai_sites.json
#   然后编辑 ai_sites.json，把 REPLACE_WITH_YOUR_API_KEY 换成真实 key

uvicorn server:app --host 127.0.0.1 --port 17700
# 打开 http://127.0.0.1:17700/
```

**依赖**（线上实测 36 个，核心 7 个）：
`fastapi` `uvicorn` `pillow` `opencv-contrib-python-headless` `psd-tools` `cos-python-sdk-v5` `requests`

## 🔴 三条必须知道的约束

### 1. `duotu/` 目录名不可更改

`server.py` 用 importlib 动态加载：

```python
_dt_spec = _ilu.spec_from_file_location("duotu_server", os.path.join(BASE, "duotu", "server.py"))
app.mount("/duotu", _WSGI(_dt_mod.app), name="duotu")   # except 里只 print，不抛
```

**挂载失败不会报错，只在日志里打一行 `[duotu] 多图编辑子应用挂载失败`。** 所以看到"整站跑起来了但 `/duotu` 打不开"，先查这个目录。

### 2. API key 已全部脱敏

本仓库所有密钥字段都是占位符 `REPLACE_WITH_YOUR_API_KEY`。要真跑起来必须填自己的 key，位置：

- `ai_sites.py` 的 `DEFAULT_SITES`（4 个站点）
- `duotu/server.py` 顶部的 `api_key=`
- `ai_sites.json`（运行时配置，优先于 `DEFAULT_SITES`）

### 3. 前端样式全部走 `ui.css` 的语义令牌

`static/ui.css` 定义了 `--pri / --bg / --card / --bd / --r-* / --font` 等。
**子应用不重复定义**，直接 `var()` 引用。

⚠️ 历史坑：`dtl/fk/aigc/swap` 四页各有一份 `<style>:root{...}</style>` 兜底块，位置在 `<link ui.css>` **之后**——改令牌必须同步这四份，否则被覆盖回旧值。

## 部署（仅参考，本仓库不参与线上）

线上是 systemd + 反向代理（Caddy 剥 `/pattern/` 前缀）：

```ini
WorkingDirectory=/opt/pattern-workbench/app
ExecStart=/opt/pattern-workbench/app/venv/bin/uvicorn server:app --host 127.0.0.1 --port 17700
```

细节见 `DEPLOY.md` 与 `docs/架构与运行参数.md`。

## 未包含在本仓库的内容

| 缺什么 | 为什么 | 影响 |
|---|---|---|
| `data/accounts.db` | 含真实用户与积分账本 | 空库，需重新建管理员 |
| `cos_config.json` | 含云存储密钥 | 退本地存储，能跑但访问慢 |
| `duotu/data/ai_config.json` | 含模型密钥 | 多图编辑的 AI 不可用 |
| `data/` `models/` `deliver/` `venv/` | 运行期产物，体积大 | 无影响，首次运行自建 |

详见 `docs/交付清单与缺失项.md`。
