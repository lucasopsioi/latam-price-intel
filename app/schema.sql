-- ============================================================
-- 拉美竞品情报中枢 —— 数据库 Schema
-- 设计原则：
--   1. 价格观测是"只增不改"的事实表，任何一天的价格都可回溯
--   2. seller_type / stock 是一等公民（价格审计 Agent 依赖它们剔除脏价）
--   3. 我方产品(my_*)与友商产品(rival_*)分表，匹配关系单独一张表并保留证据
-- ============================================================

PRAGMA journal_mode = WAL;
PRAGMA synchronous  = NORMAL;
PRAGMA foreign_keys = ON;

-- ---------- 维度 ----------

CREATE TABLE IF NOT EXISTS country (
  code        TEXT PRIMARY KEY,       -- MX/CO/CL/PE/BR/AR
  name_zh     TEXT NOT NULL,
  name_local  TEXT,
  currency    TEXT NOT NULL,          -- MXN/COP/CLP/PEN/BRL/ARS
  lang        TEXT NOT NULL,          -- es / pt
  locale      TEXT NOT NULL,          -- es-MX / pt-BR ...
  timezone    TEXT NOT NULL,
  meli_site   TEXT,
  meli_domain TEXT,
  enabled     INTEGER NOT NULL DEFAULT 1,
  sort_order  INTEGER NOT NULL DEFAULT 0
);

-- 渠道（每国的零售商/运营商/品牌官方商城）
CREATE TABLE IF NOT EXISTS channel (
  id            INTEGER PRIMARY KEY,
  code          TEXT NOT NULL,        -- liverpool / falabella / meli ...
  country_code  TEXT NOT NULL REFERENCES country(code),
  name          TEXT NOT NULL,
  kind          TEXT NOT NULL,        -- retailer | marketplace | carrier | brand_store
  base_url      TEXT,
  search_url    TEXT,                 -- 带 {q} 占位符的搜索 URL 模板
  -- 品牌官方商城往往没有可用的站内搜索，但品类页带完整 JSON-LD。
  -- 这里按产业配直达 URL（JSON: {"phone":"...","tablet":"..."}），优先于 search_url。
  category_urls TEXT,
  -- 强制指定引擎：实测 Liverpool 只有 Selenium(undetected) 能过，
  -- 配上就不浪费一轮必然失败的 Playwright 尝试
  force_engine  TEXT,                 -- NULL=自动 | playwright | selenium
  adapter       TEXT NOT NULL DEFAULT 'generic',  -- channels/ 下的适配器名
  -- 官方性：marketplace 上第三方卖家泛滥，retailer 自营则天然官方
  default_seller_type TEXT NOT NULL DEFAULT 'unknown',  -- official | third_party | unknown
  priority      INTEGER NOT NULL DEFAULT 100,  -- 越小越优先抓
  enabled       INTEGER NOT NULL DEFAULT 1,
  notes         TEXT,
  UNIQUE(code, country_code)
);

CREATE TABLE IF NOT EXISTS brand (
  id        INTEGER PRIMARY KEY,
  name      TEXT NOT NULL UNIQUE,     -- Apple / Samsung / Honor ...
  aliases   TEXT,                     -- JSON 数组：搜索别名/子品牌
  is_ours   INTEGER NOT NULL DEFAULT 0,  -- 1 = Acme（我方）
  enabled   INTEGER NOT NULL DEFAULT 1
);

-- 产业/品类
CREATE TABLE IF NOT EXISTS category (
  code      TEXT PRIMARY KEY,         -- phone / wearable / audio / tablet / pc
  name_zh   TEXT NOT NULL,
  icon      TEXT,
  enabled   INTEGER NOT NULL DEFAULT 1,
  sort_order INTEGER NOT NULL DEFAULT 0
);

-- ---------- 我方产品主数据（对应用户手绘草图①）----------

CREATE TABLE IF NOT EXISTS my_product (
  id                INTEGER PRIMARY KEY,
  marketing_name    TEXT NOT NULL,        -- 产品营销名
  internal_code     TEXT,                 -- 产品内部代号
  cert_model        TEXT,                 -- 入网认证型号
  category_code     TEXT NOT NULL REFERENCES category(code),
  series            TEXT,                 -- 产品系列（看板按系列分组）
  chipset           TEXT,                 -- 芯片
  screen            TEXT,                 -- 屏幕描述
  is_flagship_screen INTEGER DEFAULT 0,   -- 是否旗舰屏（草图里的"是/否"）
  box_contents      TEXT,                 -- JSON 数组：充电器/QSG/电子保卡/保护壳...
  id_image_path     TEXT,                 -- 产品 ID 图（本地附件路径）
  launch_earliest   TEXT,                 -- 最早发货时间 YYYY-MM-DD
  launch_latest     TEXT,                 -- 最晚发货时间
  status            TEXT NOT NULL DEFAULT 'active',  -- active | eol | planned
  notes             TEXT,
  created_at        TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at        TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ★ 唯一键必须是「营销名 + 内部代号」，且 NULL 要折叠。
--   用户清单里有 7 组同名不同代号的产品（WATCH GT 6 有 Aris / Kelo 两个平台），
--   只用营销名会把它们合并；而 SQL 里 NULL != NULL，代号为空时
--   普通 UNIQUE 约束永不冲突 → 每次导入都新建一份重复产品。
CREATE UNIQUE INDEX IF NOT EXISTS ux_my_product
  ON my_product(marketing_name, COALESCE(internal_code, ''));

-- SKU：颜色 × 内存配置 × EAN
CREATE TABLE IF NOT EXISTS my_sku (
  id            INTEGER PRIMARY KEY,
  product_id    INTEGER NOT NULL REFERENCES my_product(id) ON DELETE CASCADE,
  sku_name      TEXT,                 -- SKU1 / SKU2...
  color         TEXT,
  ram_gb        INTEGER,
  rom_gb        INTEGER,
  ean_code      TEXT,
  is_flagship_screen INTEGER,         -- 草图：哪个 SKU 是旗舰屏
  notes         TEXT
);

-- 同样的 NULL 折叠问题：颜色/内存留空时普通 UNIQUE 约束不生效
CREATE UNIQUE INDEX IF NOT EXISTS ux_my_sku
  ON my_sku(product_id, COALESCE(color, ''), COALESCE(ram_gb, -1), COALESCE(rom_gb, -1));

-- 我方分国定价与销量档（草图底部表格）
CREATE TABLE IF NOT EXISTS my_pricing (
  id            INTEGER PRIMARY KEY,
  product_id    INTEGER NOT NULL REFERENCES my_product(id) ON DELETE CASCADE,
  sku_id        INTEGER REFERENCES my_sku(id) ON DELETE CASCADE,
  country_code  TEXT NOT NULL REFERENCES country(code),
  rrp_local     REAL,                 -- 定价本币（RRP）
  currency      TEXT,
  fx_rate       REAL,                 -- 定价汇率
  rrp_usd       REAL,                 -- 定价 USD
  street_price_local REAL,            -- 实际街价（可选）
  discount_pct  REAL,                 -- 教育折扣/常促折扣 XX%
  on_sale       INTEGER NOT NULL DEFAULT 1,   -- 是否在该国在售
  sales_volume_band TEXT,             -- 销量档：用户只给大致量，如 "5k-10k/月"
  sales_period  TEXT,                 -- 该销量对应的周期，如 "2026-06~2026-08"
  updated_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ★ 最要命的一处：sku_id 常常是 NULL（产品级定价，不分 SKU）。
--   SQL 里 NULL != NULL ⇒ 普通 UNIQUE 约束永不冲突 ⇒
--   ON CONFLICT DO UPDATE 从来不触发 ⇒ 改价重导后旧价那行还在，
--   竞品匹配的 `ORDER BY ... LIMIT 1` 可能取到旧价去算价差。
CREATE UNIQUE INDEX IF NOT EXISTS ux_my_pricing
  ON my_pricing(product_id, COALESCE(sku_id, 0), country_code);

-- ---------- 友商产品（抓取中逐步发现并沉淀）----------

CREATE TABLE IF NOT EXISTS rival_product (
  id              INTEGER PRIMARY KEY,
  brand_id        INTEGER NOT NULL REFERENCES brand(id),
  category_code   TEXT NOT NULL REFERENCES category(code),
  model_name      TEXT NOT NULL,       -- 归一化型号 "Galaxy S25 Ultra"
  model_key       TEXT NOT NULL,       -- 小写无空格键，用于去重
  series          TEXT,
  -- 规格（由情报 Agent 通过 MiniMax 补全）
  ram_gb          INTEGER,
  rom_gb          INTEGER,
  chipset         TEXT,
  screen_size     REAL,
  screen_tech     TEXT,
  battery_mah     INTEGER,
  camera_main_mp  REAL,
  os              TEXT,
  extra_specs     TEXT,                -- JSON：其余规格
  spec_source     TEXT,                -- 规格来源（minimax/gsmarena/manual）
  spec_confidence REAL,                -- 0~1
  global_launch_date TEXT,             -- 全球首发日期
  created_at      TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at      TEXT NOT NULL DEFAULT (datetime('now')),
  UNIQUE(brand_id, model_key, category_code)
);

-- 友商 SKU（同型号不同容量是不同价位，必须分开）
CREATE TABLE IF NOT EXISTS rival_sku (
  id            INTEGER PRIMARY KEY,
  product_id    INTEGER NOT NULL REFERENCES rival_product(id) ON DELETE CASCADE,
  ram_gb        INTEGER,
  rom_gb        INTEGER,
  color         TEXT,
  sku_key       TEXT NOT NULL,
  UNIQUE(product_id, sku_key)
);

-- ---------- 价格观测（核心事实表，只增不改）----------

CREATE TABLE IF NOT EXISTS price_obs (
  id              INTEGER PRIMARY KEY,
  obs_date        TEXT NOT NULL,        -- YYYY-MM-DD 采集日
  captured_at     TEXT NOT NULL DEFAULT (datetime('now')),
  country_code    TEXT NOT NULL REFERENCES country(code),
  channel_id      INTEGER NOT NULL REFERENCES channel(id),
  brand_id        INTEGER REFERENCES brand(id),
  category_code   TEXT REFERENCES category(code),
  rival_product_id INTEGER REFERENCES rival_product(id),   -- 归一化后回填
  my_product_id   INTEGER REFERENCES my_product(id),       -- 我方自己的挂牌
  title           TEXT NOT NULL,        -- 页面原始标题
  model_guess     TEXT,                 -- 归一化型号猜测
  ram_gb          INTEGER,
  rom_gb          INTEGER,
  color           TEXT,
  -- 价格
  list_price      REAL,                 -- 划线原价
  sale_price      REAL,                 -- 现价（生效价）
  currency        TEXT NOT NULL,
  fx_rate         REAL,                 -- 当日汇率（本币→USD）
  sale_price_usd  REAL,
  installments    TEXT,                 -- 分期信息（拉美电商核心卖点）
  -- 卖家与库存（价格审计 Agent 的判据）
  seller_name     TEXT,
  seller_type     TEXT NOT NULL DEFAULT 'unknown',  -- official | third_party | unknown（粗分）
  -- ★ 细分：self_operated（渠道自营）/ brand_official（品牌在平台的官方店）/
  --        third_party（第三方卖家）/ unknown。三者价格含义不同：
  --        自营=该渠道官方零售价，品牌官方店≈品牌指导价，第三方常有溢价/水货/翻新。
  --        用户明确要求分清 Liverpool 自营 vs marketplace、MELI/Amazon 官方 vs 非官方。
  seller_kind     TEXT NOT NULL DEFAULT 'unknown',
  seller_shipper  TEXT,                             -- 配送方（≠卖家，Amazon 上常见分离）
  -- device | accessory | unknown。手机类目下混着保护壳/贴膜/表带/充电器，
  -- 它们进价格基线会把中位数拽低，让真整机被误判成"溢价"而剔除。
  product_kind    TEXT NOT NULL DEFAULT 'unknown',
  -- ★ 权威 SKU（来自用户的 PowerQuery SKU_Short 表，见 app/skumap.py）。
  --   这是跨渠道对齐的**权威口径**，优先于 model_guess 的通用归一化结果。
  sku_code        TEXT,
  -- off 力度 = (list_price - sale_price) / list_price × 100，入库时算好。
  -- 不在查询时现算：list_price 缺失/为 0/小于售价 的行要能明确区分
  -- "没有折扣"(0) 与 "算不出折扣"(NULL)，现算会把两者糊成一样。
  discount_pct    REAL,
  is_in_stock     INTEGER NOT NULL DEFAULT 1,
  condition       TEXT NOT NULL DEFAULT 'new',      -- new | refurb | used
  is_bundle       INTEGER NOT NULL DEFAULT 0,
  -- 审计结论
  audit_status    TEXT NOT NULL DEFAULT 'pending',  -- pending | accepted | rejected
  audit_reason    TEXT,
  audit_by        TEXT,                 -- rule:xxx / agent:price_audit
  url             TEXT,
  row_hash        TEXT NOT NULL UNIQUE, -- 幂等去重
  run_id          INTEGER REFERENCES scrape_run(id)
);

CREATE INDEX IF NOT EXISTS ix_price_date    ON price_obs(obs_date, country_code);
CREATE INDEX IF NOT EXISTS ix_price_rival   ON price_obs(rival_product_id, country_code, obs_date);
CREATE INDEX IF NOT EXISTS ix_price_channel ON price_obs(channel_id, obs_date);
CREATE INDEX IF NOT EXISTS ix_price_brand   ON price_obs(brand_id, country_code, obs_date);
CREATE INDEX IF NOT EXISTS ix_price_audit   ON price_obs(audit_status);

-- 汇率表（本币 → USD），按日
CREATE TABLE IF NOT EXISTS fx_rate (
  rate_date  TEXT NOT NULL,
  currency   TEXT NOT NULL,
  rate       REAL NOT NULL,          -- 1 USD = rate 本币
  source     TEXT NOT NULL DEFAULT 'currency-api',
  PRIMARY KEY (rate_date, currency)
);

-- ---------- 竞品匹配 ----------

CREATE TABLE IF NOT EXISTS competitor_match (
  id              INTEGER PRIMARY KEY,
  my_product_id   INTEGER NOT NULL REFERENCES my_product(id) ON DELETE CASCADE,
  rival_product_id INTEGER NOT NULL REFERENCES rival_product(id) ON DELETE CASCADE,
  country_code    TEXT NOT NULL REFERENCES country(code),
  -- 三条规则的分项得分（保留证据，便于人工推翻）
  spec_score      REAL,               -- 规格相似度 0~1
  price_score     REAL,               -- 价格接近度 0~1
  avail_score     REAL,               -- 本地在售可购买 0~1
  total_score     REAL NOT NULL,
  rank_in_country INTEGER,
  reasons         TEXT,               -- JSON：为什么判定为竞品
  -- 参与计算的价格快照
  my_price_local    REAL,
  rival_price_local REAL,
  currency          TEXT,
  -- ★ 存百分数（-3.85 表示便宜 3.85%），与 price_obs.discount_pct 同单位。
  --   曾经存小数（-0.0385），同库两套单位是隐蔽 bug 的温床。
  price_gap_pct     REAL,             -- (rival - my) / my × 100
  source          TEXT NOT NULL DEFAULT 'auto',  -- auto | manual | agent
  is_confirmed    INTEGER NOT NULL DEFAULT 0,    -- 人工确认过
  is_excluded     INTEGER NOT NULL DEFAULT 0,    -- 人工排除
  computed_at     TEXT NOT NULL DEFAULT (datetime('now')),
  UNIQUE(my_product_id, rival_product_id, country_code)
);

CREATE INDEX IF NOT EXISTS ix_match_my ON competitor_match(my_product_id, country_code, total_score DESC);

-- ---------- 上市节奏（对应草图②）----------

CREATE TABLE IF NOT EXISTS launch_event (
  id              INTEGER PRIMARY KEY,
  rival_product_id INTEGER REFERENCES rival_product(id) ON DELETE CASCADE,
  my_product_id   INTEGER REFERENCES my_product(id) ON DELETE CASCADE,
  brand_id        INTEGER REFERENCES brand(id),
  country_code    TEXT REFERENCES country(code),   -- NULL = 全球发布
  event_type      TEXT NOT NULL,      -- global_launch | country_available | price_cut | eol
  event_date      TEXT NOT NULL,
  -- 首次在该国被观测到的证据
  first_seen_price REAL,
  currency        TEXT,
  first_seen_usd  REAL,
  channel_id      INTEGER REFERENCES channel(id),
  evidence_url    TEXT,
  -- 相对全球首发的滞后天数（看板核心指标）
  days_after_global INTEGER,
  country_rank    INTEGER,            -- 该产品在拉美的上市国家顺序
  detected_by     TEXT,               -- scraper | agent:intel | manual
  confidence      REAL DEFAULT 1.0,
  notes           TEXT,
  created_at      TEXT NOT NULL DEFAULT (datetime('now')),
  UNIQUE(rival_product_id, country_code, event_type)
);

-- ★ 同一个 NULL != NULL 的坑，第二次踩（第一次见 ux_my_pricing）。
--   全球首发的 country_code 是 NULL ⇒ 上面那条 UNIQUE 永不冲突 ⇒
--   INSERT OR IGNORE 每次都插入 ⇒ 实测 iOS 27 / AirPods Pro 3 / iPhone Ultra
--   在 21 条上市事件里各出现两遍，上市看板直接重复计数。
--   凡是唯一键里含可空列，都必须用 COALESCE 表达式索引兜住。
CREATE UNIQUE INDEX IF NOT EXISTS ux_launch_event
  ON launch_event(rival_product_id, COALESCE(country_code, ''), event_type);

CREATE INDEX IF NOT EXISTS ix_launch_brand ON launch_event(brand_id, event_date);

-- ---------- 情报动态（新品/促销/广告/社媒）----------

CREATE TABLE IF NOT EXISTS dynamics (
  id            INTEGER PRIMARY KEY,
  brand_id      INTEGER REFERENCES brand(id),
  country_code  TEXT REFERENCES country(code),
  category_code TEXT REFERENCES category(code),
  source_type   TEXT NOT NULL,        -- news | x | instagram | youtube | official_site | newsroom
  source_name   TEXT,
  title         TEXT NOT NULL,
  url           TEXT,
  published_at  TEXT,
  raw_text      TEXT,
  summary_zh    TEXT,                 -- MiniMax 翻译/摘要
  tag           TEXT,                 -- 新品 | 促销 | 广告 | 渠道 | 财报 | 其他
  importance    INTEGER DEFAULT 0,    -- 0~5，情报 Agent 打分
  url_hash      TEXT NOT NULL UNIQUE,
  is_pushed     INTEGER NOT NULL DEFAULT 0,   -- 是否已 Telegram 推送
  created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS ix_dyn_date ON dynamics(published_at DESC);
CREATE INDEX IF NOT EXISTS ix_dyn_tag  ON dynamics(tag, importance DESC);

-- ---------- 运行记录 ----------

CREATE TABLE IF NOT EXISTS scrape_run (
  id            INTEGER PRIMARY KEY,
  started_at    TEXT NOT NULL DEFAULT (datetime('now')),
  finished_at   TEXT,
  run_date      TEXT NOT NULL,
  mode          TEXT NOT NULL,        -- daily | manual | smoke | backfill
  scope         TEXT,                 -- JSON：本次范围
  status        TEXT NOT NULL DEFAULT 'running',  -- running | ok | partial | failed
  pages_ok      INTEGER DEFAULT 0,
  pages_blocked INTEGER DEFAULT 0,
  pages_failed  INTEGER DEFAULT 0,
  rows_written  INTEGER DEFAULT 0,
  warnings      TEXT                  -- JSON 数组
);

-- 每个"渠道×国家×品牌"采集单元的结果，用于补采
CREATE TABLE IF NOT EXISTS scrape_unit (
  id            INTEGER PRIMARY KEY,
  run_id        INTEGER NOT NULL REFERENCES scrape_run(id) ON DELETE CASCADE,
  channel_id    INTEGER REFERENCES channel(id),
  country_code  TEXT,
  brand_id      INTEGER REFERENCES brand(id),
  category_code TEXT,
  query         TEXT,
  status        TEXT NOT NULL,        -- ok | empty | blocked | failed | login_wall
  engine        TEXT,                 -- playwright | selenium
  items         INTEGER DEFAULT 0,
  duration_ms   INTEGER,
  message       TEXT,
  created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS ix_unit_run ON scrape_unit(run_id, status);

CREATE TABLE IF NOT EXISTS agent_run (
  id            INTEGER PRIMARY KEY,
  agent_name    TEXT NOT NULL,
  role          TEXT,                 -- chief | collector | cleaner | price_audit | intel | spec_filler
  parent_run_id INTEGER REFERENCES agent_run(id),  -- 主 Agent 派生的子任务
  plan_id       INTEGER REFERENCES crawl_plan(id),
  started_at    TEXT NOT NULL DEFAULT (datetime('now')),
  finished_at   TEXT,
  status        TEXT NOT NULL DEFAULT 'running',
  input_summary TEXT,
  output_summary TEXT,
  items_in      INTEGER DEFAULT 0,
  items_out     INTEGER DEFAULT 0,
  tokens_used   INTEGER DEFAULT 0,
  error         TEXT
);

CREATE INDEX IF NOT EXISTS ix_agent_run ON agent_run(agent_name, started_at DESC);

-- ★ 留痕：每个 Agent 的每一步都记（用户硬性要求"执行过程要有留痕"）
-- 包含送进模型的提示词摘要与模型原始回复，出错时可完整复盘
CREATE TABLE IF NOT EXISTS agent_step (
  id            INTEGER PRIMARY KEY,
  run_id        INTEGER NOT NULL REFERENCES agent_run(id) ON DELETE CASCADE,
  step_no       INTEGER NOT NULL,
  step_name     TEXT NOT NULL,        -- 这一步在做什么
  input_ref     TEXT,                 -- 处理对象（url / price_obs.id / 产品名）
  prompt_digest TEXT,                 -- 送进模型的提示词（截断存档，不含密钥）
  raw_response  TEXT,                 -- 模型原始回复（未解析）
  parsed_result TEXT,                 -- 解析后的 JSON
  decision      TEXT,                 -- 这一步的结论（accepted/rejected/skipped/…）
  reason        TEXT,                 -- 为什么这么判
  tokens        INTEGER DEFAULT 0,
  duration_ms   INTEGER,
  status        TEXT NOT NULL DEFAULT 'ok',   -- ok | failed | degraded
  created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS ix_agent_step ON agent_step(run_id, step_no);

-- ★ 中央主 Agent 的抓取计划：抓之前先研判质量，产出本轮怎么抓
CREATE TABLE IF NOT EXISTS crawl_plan (
  id            INTEGER PRIMARY KEY,
  plan_date     TEXT NOT NULL,
  mode          TEXT NOT NULL,        -- daily | manual | catchup
  rotation_category TEXT,             -- 本轮轮值的产业
  -- 主 Agent 的研判结论
  health_summary TEXT,                -- JSON：各渠道健康度评分与依据
  strategy      TEXT,                 -- JSON：本轮策略（跳过谁/降频谁/换引擎谁）
  unit_count    INTEGER DEFAULT 0,
  reasoning     TEXT,                 -- 主 Agent 的自然语言研判理由（给人看）
  created_by    TEXT NOT NULL DEFAULT 'agent:chief',
  created_at    TEXT NOT NULL DEFAULT (datetime('now')),
  UNIQUE(plan_date, mode, rotation_category)
);

-- 渠道健康度档案：主 Agent 研判的输入，也是"选择器失效"的早期告警
CREATE TABLE IF NOT EXISTS channel_health (
  channel_id      INTEGER NOT NULL REFERENCES channel(id) ON DELETE CASCADE,
  check_date      TEXT NOT NULL,
  attempts        INTEGER DEFAULT 0,
  ok_count        INTEGER DEFAULT 0,
  empty_count     INTEGER DEFAULT 0,   -- 连续 empty = 大概率选择器失效
  blocked_count   INTEGER DEFAULT 0,   -- 连续 blocked = 风控盯上了
  failed_count    INTEGER DEFAULT 0,
  avg_items       REAL DEFAULT 0,
  avg_duration_ms REAL DEFAULT 0,
  health_score    REAL DEFAULT 1.0,    -- 0~1
  verdict         TEXT,                -- healthy | degraded | selector_broken | rate_limited | dead
  suggested_action TEXT,               -- 主 Agent 建议：continue / slow_down / switch_engine / skip / fix_selector
  PRIMARY KEY (channel_id, check_date)
);

CREATE INDEX IF NOT EXISTS ix_health_verdict ON channel_health(verdict, check_date DESC);

-- 详情页缓存：规格抓一次就够，之后只刷价格（"全部进详情页"的减负闸）
CREATE TABLE IF NOT EXISTS product_page_cache (
  url             TEXT PRIMARY KEY,
  channel_id      INTEGER REFERENCES channel(id),
  country_code    TEXT,
  rival_product_id INTEGER REFERENCES rival_product(id),
  specs_json      TEXT,               -- 已抽取的规格（不随时间变）
  specs_fetched_at TEXT,
  last_price_at   TEXT,
  fetch_count     INTEGER DEFAULT 0,
  last_status     TEXT
);

-- 网页原文暂存（采集阶段零 LLM，分析阶段再抽取）
CREATE TABLE IF NOT EXISTS raw_page (
  id            INTEGER PRIMARY KEY,
  url           TEXT NOT NULL,
  channel_id    INTEGER REFERENCES channel(id),
  country_code  TEXT,
  page_kind     TEXT,                 -- search | product | newsroom
  text_hash     TEXT NOT NULL,
  text          TEXT,
  status        TEXT NOT NULL DEFAULT 'pending',  -- pending | done | failed
  created_at    TEXT NOT NULL DEFAULT (datetime('now')),
  UNIQUE(url, text_hash)
);

CREATE INDEX IF NOT EXISTS ix_raw_status ON raw_page(status);

-- ---------- VOC 消费者评论 ----------
-- 用户要求："主销款的消费者评论要都抓出来做 agent 分析；
--            评论数非常多的产品，要看消费者评论多到底是因为什么"

CREATE TABLE IF NOT EXISTS review (
  id            INTEGER PRIMARY KEY,
  rival_product_id INTEGER REFERENCES rival_product(id) ON DELETE CASCADE,
  my_product_id INTEGER REFERENCES my_product(id) ON DELETE CASCADE,
  channel_id    INTEGER REFERENCES channel(id),
  country_code  TEXT REFERENCES country(code),
  product_title TEXT,                 -- 评论所属商品的页面标题
  product_url   TEXT,
  rating        REAL,                 -- 星级 1~5
  title         TEXT,
  content       TEXT NOT NULL,
  content_zh    TEXT,                 -- 中文翻译（analyze 阶段填）
  sentiment     TEXT,                 -- positive | neutral | negative
  aspects       TEXT,                 -- JSON：模型抽出的关注点（电池/屏幕/相机/售后…）
  author        TEXT,
  review_date   TEXT,
  helpful_count INTEGER,
  is_verified   INTEGER DEFAULT 0,    -- 是否已购买verified
  lang          TEXT,
  content_hash  TEXT NOT NULL UNIQUE, -- 去重
  run_id        INTEGER REFERENCES scrape_run(id),
  created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS ix_review_product ON review(rival_product_id, country_code);
CREATE INDEX IF NOT EXISTS ix_review_sentiment ON review(sentiment, review_date DESC);
CREATE INDEX IF NOT EXISTS ix_review_channel ON review(channel_id, created_at DESC);

-- ══════════════ 重点关注清单与价格预警 ══════════════

-- 关注清单：用户手工挑出来要长期盯的对象。
-- ★ 为什么要有优先级：友商产品有两千多个，全都盯等于都不盯。
--   优先级决定两件事 —— 预警阈值（P0 更敏感）与推送方式（P0 才推 Telegram）。
-- ★ scope 三选一，用同一张表承载：
--     product  盯一个具体机型（rival_product_id）
--     brand    盯一个品牌在某国的整体价盘
--     category 盯一个品类在某国的价格带
--   三者的曲线口径不同（见 app/trends.py），但"关注"这件事是同一回事。
CREATE TABLE IF NOT EXISTS watchlist (
  id            INTEGER PRIMARY KEY,
  scope         TEXT NOT NULL,          -- product | brand | category
  rival_product_id INTEGER REFERENCES rival_product(id) ON DELETE CASCADE,
  brand_id      INTEGER REFERENCES brand(id) ON DELETE CASCADE,
  category_code TEXT REFERENCES category(code),
  country_code  TEXT REFERENCES country(code),   -- NULL = 该对象的所有国家
  priority      TEXT NOT NULL DEFAULT 'P1',      -- P0 重点 | P1 常规 | P2 观察
  -- 自定义阈值；留空则用 priority 的默认值（见 trends.PRIORITY_THRESHOLD）
  drop_pct      REAL,                   -- 降价多少个百分点触发
  rise_pct      REAL,                   -- 涨价多少个百分点触发
  note          TEXT,                   -- 为什么盯它（写给未来的自己）
  enabled       INTEGER NOT NULL DEFAULT 1,
  created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);
-- ★ 唯一键必须**折叠 NULL**，而且只能写成独立的表达式索引 ——
--   SQLite 不允许在 CREATE TABLE 的 UNIQUE 约束里写表达式。
--   不折 NULL 的话："全国范围"（country_code 为空）的条目能重复插无数次，
--   因为 SQL 里 NULL != NULL。这个坑本库已经踩过两次（my_pricing、launch_event）。
CREATE UNIQUE INDEX IF NOT EXISTS ux_watchlist ON watchlist(
  scope, COALESCE(rival_product_id, -1), COALESCE(brand_id, -1),
  COALESCE(category_code, ''), COALESCE(country_code, ''));
CREATE INDEX IF NOT EXISTS ix_watch_prio ON watchlist(enabled, priority);

-- 价格预警：命中阈值的事件。
-- ★ 与 price_move 的区别：price_move 是**所有**价格变动的事实层，
--   这里只留"用户盯着的对象 + 幅度够大 + 可信度过关"的那些。
--   两者分开是因为它们的读者不同：前者给分析用，后者是要人**去处理**的。
CREATE TABLE IF NOT EXISTS price_alert (
  id            INTEGER PRIMARY KEY,
  watch_id      INTEGER REFERENCES watchlist(id) ON DELETE CASCADE,
  fired_at      TEXT NOT NULL DEFAULT (datetime('now')),
  alert_date    TEXT NOT NULL,          -- 触发所依据的观测日
  scope         TEXT NOT NULL,
  label         TEXT NOT NULL,          -- 人看的对象名
  country_code  TEXT,
  direction     TEXT NOT NULL,          -- up | down
  change_pct    REAL NOT NULL,
  prev_price    REAL, curr_price REAL, currency TEXT,
  channel_id    INTEGER REFERENCES channel(id),
  rival_product_id INTEGER REFERENCES rival_product(id) ON DELETE CASCADE,
  priority      TEXT,
  tier          TEXT,                   -- credible | suspect | implausible
  reason        TEXT,                   -- 为什么报（含口径与阈值）
  is_pushed     INTEGER NOT NULL DEFAULT 0,
  is_read       INTEGER NOT NULL DEFAULT 0
);
-- ★ 同一个对象、同一天、同一方向只报一次。
--   不去重的话每轮采集都会重报同一件事，几轮下来通知就没人看了。
CREATE UNIQUE INDEX IF NOT EXISTS ux_price_alert ON price_alert(
  watch_id, alert_date, direction,
  COALESCE(rival_product_id, -1), COALESCE(channel_id, -1));
CREATE INDEX IF NOT EXISTS ix_alert_new ON price_alert(is_read, fired_at DESC);

-- 评论 × 维度：口碑雷达图的事实层
-- ★ 为什么单独一张表而不是继续用 review.aspects 那个 JSON 列：
--   JSON 列**没法在 SQL 里聚合**。要画"某品牌某国某品类各维度的好评率"，
--   拿 JSON 只能全表拉回 Python 里循环，而且维度名是自由文本、拼不起来
--   （实测 814 条里出现了 43 种写法，同一件事被拆成四个标签）。
-- ★ sentiment 单独存在维度这一层，不复用 review.sentiment：
--   一条"相机很棒但电池垃圾"的评论，整条判 negative 会让**相机也记一票差评**。
--   雷达图恰恰是用来区分维度好坏的，这么记等于把要看的信号抹平。
-- ★ sentiment_from 记这一票的来源：'aspect'=模型逐维度判的（可信）；
--   'review'=从整条继承的（旧数据回填，会有上面说的偏差）。
--   聚合时要能把两者分开看，否则回填数据会**冒充**逐维度判断。
CREATE TABLE IF NOT EXISTS review_aspect (
  id            INTEGER PRIMARY KEY,
  review_id     INTEGER NOT NULL REFERENCES review(id) ON DELETE CASCADE,
  aspect_code   TEXT NOT NULL,        -- app/voc_aspects.py 里的固定 code
  sentiment     TEXT,                 -- positive | neutral | negative | NULL(未判)
  sentiment_from TEXT NOT NULL DEFAULT 'aspect',  -- aspect | review
  created_at    TEXT NOT NULL DEFAULT (datetime('now')),
  UNIQUE(review_id, aspect_code)
);
CREATE INDEX IF NOT EXISTS ix_raspect_code ON review_aspect(aspect_code, sentiment);

-- 商品的评论量画像：识别"主销款"与"评论异常多"的产品
CREATE TABLE IF NOT EXISTS review_profile (
  id              INTEGER PRIMARY KEY,
  rival_product_id INTEGER REFERENCES rival_product(id) ON DELETE CASCADE,
  channel_id      INTEGER REFERENCES channel(id),
  country_code    TEXT,
  product_url     TEXT,
  total_reviews   INTEGER,            -- 页面标称的评论总数
  fetched_reviews INTEGER,            -- 实际抓到多少条
  avg_rating      REAL,
  rating_dist     TEXT,               -- JSON: {"5":120,"4":30,...}
  is_hot          INTEGER DEFAULT 0,  -- 评论数超阈值 = 主销款信号
  hot_reason      TEXT,               -- ★ Agent 归因："评论为什么这么多"
  last_fetched    TEXT,
  UNIQUE(product_url)
);

CREATE INDEX IF NOT EXISTS ix_rprofile_hot ON review_profile(is_hot, total_reviews DESC);

-- VOC 分析结论（按产品×国家聚合）
CREATE TABLE IF NOT EXISTS voc_insight (
  id              INTEGER PRIMARY KEY,
  rival_product_id INTEGER REFERENCES rival_product(id) ON DELETE CASCADE,
  country_code    TEXT,
  period_start    TEXT,
  period_end      TEXT,
  review_count    INTEGER,
  avg_rating      REAL,
  praise_points   TEXT,               -- JSON 数组：好评点
  complaint_points TEXT,              -- JSON 数组：差评点
  watch_signals   TEXT,               -- JSON 数组：需关注信号（质量问题/售后/对比Acme）
  vs_acme       TEXT,               -- 评论里直接提到Acme的内容摘要
  summary_zh      TEXT,
  created_at      TEXT NOT NULL DEFAULT (datetime('now')),
  UNIQUE(rival_product_id, country_code, period_start)
);

-- ---------- 设置（API Key 等，值加密存储）----------

-- ---------- 竞争动态（看板的三张事实表）----------
--
-- 设计原则：**存"变化"和"判断"，不存快照**。
--   price_obs 已经是逐日快照了；看板要回答的是「发生了什么」，
--   所以这里存的是从快照里检测出来的**事件**与 Agent 给出的**解读**。

-- ① 价格变动事件：同一 SKU 在同一渠道的价格发生变化时记一条
CREATE TABLE IF NOT EXISTS price_move (
  id              INTEGER PRIMARY KEY,
  move_date       TEXT NOT NULL,          -- 变动被观测到的日期
  country_code    TEXT NOT NULL REFERENCES country(code),
  channel_id      INTEGER NOT NULL REFERENCES channel(id),
  brand_id        INTEGER REFERENCES brand(id),
  category_code   TEXT REFERENCES category(code),
  rival_product_id INTEGER REFERENCES rival_product(id),
  sku_key         TEXT,                   -- 容量+颜色，保证同 SKU 比价
  prev_price      REAL NOT NULL,
  curr_price      REAL NOT NULL,
  change_pct      REAL NOT NULL,          -- 百分数：-12.5 表示降 12.5%
  direction       TEXT NOT NULL,          -- down | up
  prev_date       TEXT NOT NULL,
  days_span       INTEGER,                -- 距上次观测多少天
  -- ★ 官方渠道的价格变动权重完全不同：官网/自营降价 = 厂商的定价动作，
  --   第三方降价可能只是某个卖家在甩货。用户明确说要盯官网。
  is_official     INTEGER NOT NULL DEFAULT 0,
  currency        TEXT,
  created_at      TEXT NOT NULL DEFAULT (datetime('now')),
  UNIQUE(move_date, channel_id, rival_product_id, sku_key)
);
CREATE INDEX IF NOT EXISTS ix_move_date ON price_move(move_date, country_code);
CREATE INDEX IF NOT EXISTS ix_move_prod ON price_move(rival_product_id, move_date);

-- ② 策略信号：Agent 从价格变动模式里推断出的**意图**
CREATE TABLE IF NOT EXISTS strategy_signal (
  id              INTEGER PRIMARY KEY,
  signal_date     TEXT NOT NULL,
  country_code    TEXT REFERENCES country(code),
  brand_id        INTEGER REFERENCES brand(id),
  category_code   TEXT REFERENCES category(code),
  rival_product_id INTEGER REFERENCES rival_product(id),
  -- clearance=老品清库 / pre_launch_cut=新品上市前降价 / promo_prep=大促前铺垫
  -- / cost_hike=成本上涨提价 / price_war=价格战 / new_launch_premium=新品高定
  -- / restore=促销结束回价
  signal_type     TEXT NOT NULL,
  confidence      REAL,                   -- 0~1
  summary_zh      TEXT NOT NULL,          -- 一句话结论
  evidence        TEXT,                   -- JSON：支撑这个判断的具体数据
  impact_zh       TEXT,                   -- 对我方（Acme）意味着什么
  suggested_action TEXT,                  -- 建议动作
  agent           TEXT,
  created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS ix_sig_date ON strategy_signal(signal_date, country_code);

-- ③ 周报（Agent 汇总产出，可回溯）
CREATE TABLE IF NOT EXISTS weekly_report (
  id            INTEGER PRIMARY KEY,
  week_start    TEXT NOT NULL,
  week_end      TEXT NOT NULL,
  scope         TEXT NOT NULL DEFAULT 'all',   -- all | 国家码 | 国家码:品类
  title         TEXT,
  content_md    TEXT NOT NULL,
  highlights    TEXT,                     -- JSON 数组：要点
  metrics       TEXT,                     -- JSON：本周关键数字
  created_at    TEXT NOT NULL DEFAULT (datetime('now')),
  UNIQUE(week_start, scope)
);

-- 知识库分块（Obsidian vault 的 RAG 索引，见 app/rag.py）
-- 存的是「数据库里没有的知识」：渠道破解记录、判定依据、踩过的坑。
-- embedding 可为空 —— 没配 Key 时退化成纯 BM25，仍然可用。
CREATE TABLE IF NOT EXISTS kb_chunk (
  id         INTEGER PRIMARY KEY,
  path       TEXT NOT NULL,          -- 相对 vault 的路径
  title      TEXT,                   -- 文件名（不含扩展名）
  heading    TEXT,                   -- 所属小节标题
  text       TEXT NOT NULL,
  embedding  BLOB,                   -- float32 打包；NULL = 还没生成向量
  file_sig   TEXT,                   -- 文件内容签名，用于增量重建
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS ix_kb_path ON kb_chunk(path);

CREATE TABLE IF NOT EXISTS setting (
  key         TEXT PRIMARY KEY,
  value       TEXT,
  is_secret   INTEGER NOT NULL DEFAULT 0,
  updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ---------- 视图：便于前端直接查 ----------

-- 每个"国家×友商产品"的当前有效价（只取审计通过的、最近一次观测）
CREATE VIEW IF NOT EXISTS v_current_price AS
SELECT p.*
FROM price_obs p
JOIN (
  SELECT country_code, channel_id, COALESCE(rival_product_id, -1) AS rp,
         COALESCE(model_guess, title) AS mk, MAX(obs_date) AS d
  FROM price_obs
  WHERE audit_status <> 'rejected'
  GROUP BY country_code, channel_id, rp, mk
) x
  ON p.country_code = x.country_code
 AND p.channel_id  = x.channel_id
 AND COALESCE(p.rival_product_id, -1) = x.rp
 AND COALESCE(p.model_guess, p.title) = x.mk
 AND p.obs_date = x.d
WHERE p.audit_status <> 'rejected';

-- ---------- 外部规格源缓存（GSMArena 等）----------
-- ★ 缓存是硬需求，不是优化：站方 robots 给爬虫的 crawl-delay 是 5~20 秒，
--   几百个机型跑一轮要小时级。重跑时不该再打一次人家的站。
--   同时它也是"这条规格是从哪来的"的凭证 —— 规格必须可追溯到 URL，
--   否则和模型猜的没区别。
CREATE TABLE IF NOT EXISTS spec_source_index (
  source        TEXT NOT NULL,          -- gsmarena | ...
  brand         TEXT NOT NULL,
  model_key     TEXT NOT NULL,          -- 归一化型号名（只剩字母数字）
  model_name    TEXT NOT NULL,          -- 站点上的原始写法
  url           TEXT NOT NULL,
  indexed_at    TEXT NOT NULL DEFAULT (datetime('now')),
  PRIMARY KEY (source, brand, model_key)
);
CREATE INDEX IF NOT EXISTS ix_specidx_key ON spec_source_index(model_key);

CREATE TABLE IF NOT EXISTS spec_source_cache (
  url           TEXT PRIMARY KEY,
  source        TEXT NOT NULL,
  model_name    TEXT,
  specs_json    TEXT NOT NULL,
  fetched_at    TEXT NOT NULL DEFAULT (datetime('now')),
  attribution   TEXT
);

-- 导出文件 → 手机「工作」文件夹的同步台账（app/phone_sync.py）。
-- 用户 2026-08-25：「只要手机连着电脑，导出的报告就自动转进手机存储」。
-- (file_name, md5) 唯一：同名文件重新导出（内容变了）算新的待传任务。
CREATE TABLE IF NOT EXISTS phone_export_sync (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  file_name  TEXT NOT NULL,
  md5        TEXT NOT NULL,
  size       INTEGER NOT NULL,
  status     TEXT NOT NULL,            -- synced / failed / skipped
  detail     TEXT,
  synced_at  TEXT NOT NULL DEFAULT (datetime('now','localtime')),
  UNIQUE(file_name, md5)
);
