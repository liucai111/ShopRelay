# 开发环境与本地运行

本指南面向新检出源码的读者。先运行本地合成数据和替身路径，再按需配置高级集成。维护者已有容器、模型缓存和账号不是可直接复用的公共部署服务。

## 安装依赖

Python版本要求见`pyproject.toml`；核心CI使用Python 3.12，本机也验证过3.13。前端CI使用Node.js 24。在仓库根目录执行：

```sh
python -m venv .venv
```

Windows PowerShell激活：

```powershell
.\.venv\Scripts\Activate.ps1
```

Linux/macOS激活：

```sh
. .venv/bin/activate
```

安装依赖：

```sh
python -m pip install -e ".[dev,llm]" -r requirements-langgraph-g1.lock -r requirements-langgraph-g2.lock
cd frontend
npm ci
cd ..
```

依赖安装需要网络。安装`llm` SDK不会自动调用模型。扩展依赖见`requirements-probe.lock`与`pyproject.toml`的`probe`组。本文整理时没有重新验证全新机器安装，依赖可用性和平台兼容以实际安装结果为准。

## 最小离线政策问答

```sh
python -m app.m0.cli ingest
python -m app.m0.cli ask "定制商品质量问题签收30天内可以退货吗"
```

`ingest`从合成政策生成`data/index.json`；`ask`使用BM25和抽取式回答，不需要API Key。输出不是模型质量评测，示例政策不是商家承诺。

## 初始化SQLite Web Demo

仅对新本地演示环境执行初始化。已使用的数据库不要为了“重新开始”删除。

```sh
python -m app.m0.cli seed
python -m app.m0.cli ingest
python -m app.m1.migrations upgrade --database-url sqlite:///data/shoprelay.sqlite3
python -m app.m1.demo_users --database-url sqlite:///data/shoprelay.sqlite3
```

最后一条命令显示种子用户标识，并交互设置本机口令，不提供通用默认密码。若已有`.env.m0.local`或`DATABASE_URL`，先确认其指向本地演示库。

Windows使用现有启动器：

```powershell
.\scripts\start_shoprelay.ps1 -Mode Demo
.\scripts\check_shoprelay.ps1 -Mode Demo
```

它使用仓库`.venv`，启动API与Vite，默认替身Provider、关闭业务写入与管理员授权。结束后：

```powershell
.\scripts\stop_shoprelay.ps1
```

其他平台可在两个终端分别运行API和前端；这是手动启动路径，不是已验证的跨平台一键脚本：

```sh
export DATABASE_URL=sqlite:///data/shoprelay.sqlite3
export SHOPRELAY_RUNTIME_MODE=demo
export SHOPRELAY_PROVIDER_MODE=stub
export SHOPRELAY_ORCHESTRATOR=legacy
export SHOPRELAY_M3_SUPERVISOR_ENABLED=false
export SHOPRELAY_ENABLE_ISOLATED_TEST_ACTIONS=false
export SHOPRELAY_OBSERVABILITY_ADMIN_USER_IDS=
python -m uvicorn app.m1.api:app --host 127.0.0.1 --port 8000
```

另一个终端：

```sh
cd frontend
npm run dev
```

打开Vite显示的本地地址。开发代理默认指向本机8000，登录与CSRF经同源代理完成。

## 高级集成

- PostgreSQL和LangGraph需要应用迁移及独立Checkpointer schema；启动不隐式初始化数据库。
- Supervisor需要服务端配置开启，不由用户消息授予权限。
- Hybrid需要固定版本BGE模型缓存和Qdrant配置；缺缓存不能宣称已运行向量模式。
- 真实模型需自行配置凭据和调用预算；请求会产生费用。
- 模拟写动作只用于隔离测试环境，且仍需用户确认。不要连接生产库运行示例。

历史本机容器、卷、账号与授权记录保留在维护者本地，不作为公共部署配置。

## 检查

前端在`frontend`目录运行`npm run typecheck`、`npm test`、`npm run build`。后端核心选择见[CI定义](../.github/workflows/core.yml)。

部分历史测试依赖未公开原档、专用数据库或模型缓存；全仓pytest不是无条件的新环境验收命令，缺依赖跳过也不能算通过。
