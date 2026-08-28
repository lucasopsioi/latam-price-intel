# MANIFEST · latam-price-intel

> 多Agent竞品价格情报管道｜源：拉美竞品情报中枢｜构建：本文件由 build-staging.js 生成，逐文件处置如下
> COPY=原样文本 RENAMED=做了名称脱敏 ZIPFIX=办公文件内部XML脱敏 BINARY=二进制原样 EXCLUDE=不进仓 REDACTED=打码模板

BINARY   assets/hub.ico
BINARY   assets/hub.png
BINARY   deploy/latam-hub-health.timer
BINARY   deploy/secrets.env.example
COPY     1-install.bat
COPY     2-start.bat
COPY     3-doctor.bat
COPY     app/__init__.py
COPY     app/agents/base.py
COPY     app/agents/chief.py
COPY     app/agents/llm.py
COPY     app/agents/price_audit.py
COPY     app/agents/pricemove.py
COPY     app/api/__init__.py
COPY     app/config.py
COPY     app/dashboard.py
COPY     app/livelog.py
COPY     app/matching/__init__.py
COPY     app/reprocess.py
COPY     app/scheduler.py
COPY     app/scraping/__init__.py
COPY     app/scraping/browser.py
COPY     app/scraping/channels/base.py
COPY     app/scraping/channels/meli.py
COPY     app/scraping/collector.py
COPY     app/scraping/engine.py
COPY     app/scraping/health.py
COPY     app/scraping/news.py
COPY     app/scraping/seller.py
COPY     app/scraping/specsource/__init__.py
COPY     app/scraping/voc.py
COPY     app/tgbot.py
COPY     app/trends.py
COPY     app/web/boards.js
COPY     app/web/style.css
COPY     app/web/vendor/README.md
COPY     app/web/vendor/echarts-LICENSE.txt
COPY     app/web/vendor/echarts.min.js
COPY     deploy/README.md
COPY     deploy/health-check.sh
COPY     deploy/install.sh
COPY     deploy/latam-hub-health.service
COPY     deploy/latam-hub.service
COPY     requirements.txt
COPY     tests/e2e_smoke.py
COPY     tests/test_categorybackfill.py
COPY     tests/test_chartgrammar.py
COPY     tests/test_extract.py
COPY     tests/test_launchkind.py
COPY     tests/test_normalize.py
COPY     tests/test_reviewvolume.py
COPY     tests/test_seller.py
COPY     tests/test_skunorm.py
COPY     tests/test_specsource.py
COPY     tests/test_storestock.py
COPY     tests/test_trends.py
COPY     tests/test_ui_context.py
COPY     tests/test_voc.py
COPY     tests/test_vocaspects.py
COPY     tests/test_voctargets.py
COPY     tests/test_weeklybrief.py
COPY     tools/alert-popup.ps1
COPY     tools/audit_title_chrome.py
COPY     tools/backfill_category_crosscheck.py
COPY     tools/check_robots.py
COPY     tools/clean_voc_dirty.py
COPY     tools/fix_skumap_scope.py
COPY     tools/merge_polluted_models.py
COPY     tools/ml_login.py
COPY     tools/open-dashboard.cmd
COPY     tools/renorm_skus.py
COPY     tools/restart.ps1
COPY     tools/site_login.py
COPY     tools/supervisor.py
COPY     看板.cmd
OVERLAY  LICENSE（docs-src 提供）
OVERLAY  README.md（docs-src 提供）
OVERLAY  config/my_products.example.csv（docs-src 提供）
REDACTED config/runtime.example.yaml（值全部机械打码为占位符）
RENAMED  README.md
RENAMED  README.md -> README.zh.md（中文版保留，英文主README来自docs-src）
RENAMED  app/agents/__init__.py
RENAMED  app/agents/brandintel.py
RENAMED  app/agents/chat.py
RENAMED  app/agents/cleaner.py
RENAMED  app/agents/intel.py
RENAMED  app/agents/orchestrator.py
RENAMED  app/agents/spec_filler.py
RENAMED  app/agents/strategy.py
RENAMED  app/agents/voc_agent.py
RENAMED  app/agents/weekly.py
RENAMED  app/api/server.py
RENAMED  app/archive.py
RENAMED  app/boards.py
RENAMED  app/db.py
RENAMED  app/matching/matcher.py
RENAMED  app/matching/modelkey.py
RENAMED  app/matching/specs.py
RENAMED  app/notify.py
RENAMED  app/phone_sync.py
RENAMED  app/products.py
RENAMED  app/rag.py
RENAMED  app/report_export.py
RENAMED  app/schema.sql
RENAMED  app/scraping/channels/__init__.py
RENAMED  app/scraping/channels/sites.py
RENAMED  app/scraping/extract.py
RENAMED  app/scraping/relevance.py
RENAMED  app/scraping/selenium_driver.py
RENAMED  app/scraping/specsource/gsmarena.py
RENAMED  app/skumap.py
RENAMED  app/skunorm.py
RENAMED  app/voc_aspects.py
RENAMED  app/web/app.js
RENAMED  app/web/charts.js
RENAMED  app/web/index.html
RENAMED  config/brands.yaml
RENAMED  config/channels.yaml
RENAMED  config/sku_rules.yaml
RENAMED  docs/VOC-情报站-20个方向.md
RENAMED  docs/看板改造三十条.html
RENAMED  docs/看板改造三十条.md
RENAMED  main.py
RENAMED  tests/test_accessory_kind.py
RENAMED  tests/test_acmestore.py
RENAMED  tests/test_archive.py
RENAMED  tests/test_boards.py
RENAMED  tests/test_categorycrosscheck.py
RENAMED  tests/test_collector.py
RENAMED  tests/test_llmhygiene.py
RENAMED  tests/test_matching.py
RENAMED  tests/test_modelkey.py
RENAMED  tests/test_phonesync.py
RENAMED  tests/test_pipeline.py
RENAMED  tests/test_position.py
RENAMED  tests/test_pptlayout.py
RENAMED  tests/test_products.py
RENAMED  tests/test_skumap.py
RENAMED  tests/test_supervisor.py
RENAMED  tests/test_title_chrome.py
RENAMED  tools/archive.py
RENAMED  tools/backfill_accessory_kind.py
RENAMED  tools/fetch_specs.py
RENAMED  tools/install-service.ps1
RENAMED  tools/launcher.py
RENAMED  tools/pyenv.cmd

## 未进仓（按顶层路径归并，共 186953 个文件）

- `data` — 排除 186811 个文件
- `app` — 排除 65 个文件
- `exports` — 排除 39 个文件
- `logs` — 排除 25 个文件
- `.pytest_cache` — 排除 5 个文件
- `tools` — 排除 4 个文件
- `config` — 排除 2 个文件
- `tests` — 排除 2 个文件
