# -*- coding: utf-8 -*-
"""端到端冒烟：真实联网跑一遍完整流水线，验证各环节真的接得上。

  主 Agent 研判 → 采集(真抓) → 清洗(型号归一化) → 价格审计 → 落库

范围压到最小（1 国 × 1 产业 × 2 品牌 × 每次取 3 个商品），
目的是验证链路通不通，不是取全量数据。

跑法： python tests\e2e_smoke.py
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import logging  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)-11s %(message)s",
                    datefmt="%H:%M:%S")

from app import db  # noqa: E402
from app.agents import Orchestrator  # noqa: E402

db.init_db()

print("=" * 74)
print("端到端冒烟：MX × 手机 × [Samsung, Motorola] × 每次取3个")
print("=" * 74)

before = db.q1("SELECT COUNT(*) c FROM price_obs")["c"]

orch = Orchestrator(mode="smoke", categories=["phone"], countries=["MX"],
                    brands=["Samsung", "Motorola"], max_items=3)
result = orch.run_daily()

print("\n" + "=" * 74)
print("结果")
print("=" * 74)
print(f"批次 #{result['run_id']}")
print(f"主 Agent: {result['plan_summary']}")
print(f"采集: {result['collect']}")
print(f"清洗: {result['clean']}")
print(f"审计: {result['audit']}")
print(f"耗时: {result['elapsed_sec']} 秒")
if result["warnings"]:
    print("\n警告:")
    for w in result["warnings"][:10]:
        print(f"  · {w[:150]}")

after = db.q1("SELECT COUNT(*) c FROM price_obs")["c"]
print(f"\n价格观测: {before} → {after}（新增 {after - before}）")

print("\n--- 采集单元明细 ---")
for u in db.q("""SELECT c.name, b.name AS brand, su.status, su.engine, su.items,
                        su.duration_ms, su.message
                 FROM scrape_unit su
                 LEFT JOIN channel c ON c.id=su.channel_id
                 LEFT JOIN brand b ON b.id=su.brand_id
                 WHERE su.run_id=? ORDER BY su.id""", (result["run_id"],)):
    msg = f"  {u['message'][:70]}" if u["message"] else ""
    print(f"  {(u['name'] or '?'):<22} {(u['brand'] or ''):<10} {u['status']:<12}"
          f" {u['items']:>3}条 {(u['duration_ms'] or 0)/1000:>5.1f}s {u['engine'] or ''}{msg}")

print("\n--- 抓到的商品（前12条）---")
for r in db.q("""SELECT po.title, po.sale_price, po.currency, po.seller_type,
                        po.audit_status, po.audit_reason, c.name AS ch,
                        rp.model_name
                 FROM price_obs po
                 LEFT JOIN channel c ON c.id=po.channel_id
                 LEFT JOIN rival_product rp ON rp.id=po.rival_product_id
                 WHERE po.run_id=? ORDER BY po.id LIMIT 12""", (result["run_id"],)):
    print(f"  [{r['ch']}] {r['title'][:52]}")
    print(f"      {r['sale_price']} {r['currency']} | 归一化型号={r['model_name'] or '—'}"
          f" | 卖家={r['seller_type']} | 审计={r['audit_status']}")
    if r["audit_reason"]:
        print(f"      理由: {r['audit_reason'][:90]}")

print("\n--- Agent 留痕 ---")
for a in db.q("""SELECT ar.id, ar.agent_name, ar.status, ar.output_summary,
                        (SELECT COUNT(*) FROM agent_step WHERE run_id=ar.id) steps
                 FROM agent_run ar ORDER BY ar.id DESC LIMIT 6"""):
    print(f"  #{a['id']} {a['agent_name']:<13} {a['status']:<8} {a['steps']:>2}步"
          f"  {(a['output_summary'] or '')[:80]}")

ok = (after - before) > 0
print("\n" + ("✅ 端到端链路打通" if ok else "❌ 没有写入任何价格数据，见上方单元明细"))
sys.exit(0 if ok else 1)
