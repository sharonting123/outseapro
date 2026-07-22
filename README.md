# soutui — 甄选电商 + 搜推广告

可真实注册登录、管理商品并通过 Stripe Checkout 支付的电商闭环：SPU/SKU 目录、搜索/推荐、购物车、订单、签名 Webhook，以及从曝光/点击/支付事件训练并在线加载 CTR/CVR 模型。

```text
SPU（款）──< SKU（规格：价/库存）
  ├─ 搜索/推荐：按 spu_id 召回精排，再 pick 可售 sku
  └─ 广告(Ad 绑 spu_id，可选 sku_id) → oCPX/GSP/预算
           ↓
         Mixer（按 spu_id 去重）+ 行为埋点(events)
           ↓
      详情选 SKU → 购物车 → Stripe Checkout → Webhook 确认支付
```

## 快速跑

```bash
cd /home/ubuntu/soutui
pip3 install -r requirements.txt
PYTHONPATH=src python3 -m pytest tests/ -q

export STRIPE_SECRET_KEY=sk_test_...
export STRIPE_WEBHOOK_SECRET=whsec_...
export SOUTUI_BOOTSTRAP_MERCHANT_EMAIL=merchant@example.com
export SOUTUI_BOOTSTRAP_MERCHANT_PASSWORD='请使用强密码'
export SOUTUI_BOOTSTRAP_ADMIN_EMAIL=admin@example.com
export SOUTUI_BOOTSTRAP_ADMIN_PASSWORD='请使用独立强密码'
export SOUTUI_SECURE_COOKIE=1  # HTTPS 生产环境
export SOUTUI_BASE_PATH=/shop  # 仅在子路径部署时设置
PYTHONPATH=src uvicorn soutui.api:app --host 0.0.0.0 --port 8088
# 打开 http://127.0.0.1:8088/
```

Stripe Webhook 地址为 `https://你的域名/webhooks/stripe`，订阅
`checkout.session.completed` 和 `checkout.session.expired`。未配置密钥时支付会安全失败，绝不会把订单伪造为已支付。

## 页面

| 路径 | 说明 |
|------|------|
| `/` | 为你推荐 |
| `/search?q=` | 搜索 |
| `/item/{spu_id}` | 详情选 SKU、加购 |
| `/cart` | 购物车 / Stripe 支付 |
| `/order/{id}` | 订单结果 |
| `/login` `/register` | 登录 / 注册 |
| `/merchant` | 商家商品、库存、订单、模型管理 |
| `/admin/login` `/admin` | 独立管理员登录 / 算法诊断后台 |
| `/api/search` `/api/feed` | JSON 接口（兼容） |
| `/admin/trace/stream` | 管理员专属算法步骤 SSE |

管理员与商城账号完全隔离：商城及商家使用 `users/sessions`，管理员使用 `admin_users/admin_sessions` 与独立 Cookie，二者不能互相登录。

本地开发默认使用 `data/soutui.db`；设置 `DATABASE_URL` 后自动切换到 Supabase/PostgreSQL。目录、库存、购物车、订单、两套身份会话、events 和训练模型都会进入 PostgreSQL。

## Cloud Run + Supabase

生产部署文件位于 [`deploy/cloudrun`](deploy/cloudrun/README.md)：

```bash
export DATABASE_URL='postgresql://...'
python scripts/migrate_sqlite_to_postgres.py --source data/soutui.db --replace

export PROJECT_ID='your-gcp-project'
export STRIPE_SECRET_KEY='sk_...'
export STRIPE_WEBHOOK_SECRET='whsec_...'
bash deploy/cloudrun/deploy.sh
```

同一 Docker 镜像会部署为 FastAPI Service 和 CTR/CVR Cloud Run Job。本地 SQLite 数据不会进入镜像。

## CTR/CVR 衔接

- `events` 表：`impress/click/add_cart/order`，impress 带 `features_json` + `pctr/pcvr` 快照
- 训练命令：`PYTHONPATH=src python3 -m soutui.training --db data/soutui.db --output data/ctr_cvr_model.json`
- 训练会按 request/user/item/time-window 归因曝光→点击→已支付订单，分别训练 CTR 与 click-conditioned CVR LR，记录 AUC/logloss，并原子发布模型文件
- 服务启动或商家后台训练完成后自动加载 `TrainedCtrCvrModel`；样本不足或模型文件损坏时安全回退到 `LogisticHeuristicModel`
