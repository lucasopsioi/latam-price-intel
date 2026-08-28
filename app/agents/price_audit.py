# -*- coding: utf-8 -*-
"""价格审计 Agent —— 判定一条挂牌价能不能进价格分析。

用户原话的场景：
  "去墨西哥的利物浦搜一个产品，发现价格特别高，这个 agent 要审视一下
   价格高是因为非官方商家挂的链接，还是别的原因，该不该剔除掉。"

两层判定，能用规则判死的绝不调模型：

  【第一层·规则】可复现、零 token、瞬时
    - 捆绑装 / 翻新二手 / 缺货 → 剔除（这些本来就不是可比价）
    - 价格超出该币种合理区间 → 剔除（多打一位、把分期金额当总价）
    - 第三方卖家 且 价格显著偏离基线 → 剔除
    - 价格显著低于基线 → 大概率是配件被当成整机（保护壳挂在手机类目下）

  【第二层·模型】只处理规则判不了的灰色地带
    - 第三方卖家但价格正常（可能是正规授权经销商，不该剔）
    - 官方渠道但价格异常（可能是页面标错、或确实是高配版）
    - 标题含混，判不出是整机还是配件

★ 基线分组键 = 国家 × 型号 × 月（三个都不能少）
  少了国家：阿根廷 895,999 ARS 与巴西 3,499 BRL 是同一台机器，
           跨币种混算会把整个高汇率国家判成异常（上个项目实测剔掉 59% 的行）。
  少了型号：128G 和 512G 是两个价位，混在一起基线没意义。
  用月不用周：周样本太少时中位数本身就不稳，拿不稳的基线去剔异常会误杀。

★ 剔除比例是健康度指标，不是结论
  正常应该 < 1%。超了先怀疑分组键写错，而不是先信"今天脏数据特别多"。
"""
from __future__ import annotations

import logging
import statistics

from .. import db
from ..scraping import extract
from .base import BaseAgent
from .llm import as_dicts

log = logging.getLogger("price_audit")

# 相对基线的倍数阈值
HIGH_MULTIPLIER = 1.8      # 高于基线 1.8 倍 → 可疑
LOW_MULTIPLIER = 0.35      # 低于基线 35% → 大概率是配件
MIN_BASELINE_SAMPLES = 3   # 基线样本少于这个数就不做倍数判定（基线本身不可信）
ALERT_REJECT_RATE = 0.01   # 剔除率超 1% 触发告警


class PriceAuditAgent(BaseAgent):
    name = "price_audit"
    role = "price_audit"
    description = "判定挂牌价是否可用于价格分析，剔除第三方溢价/配件/翻新/错价"

    def run(self, obs_date: str | None = None, limit: int = 2000) -> dict:
        obs_date = obs_date or db.today()
        self.start(f"审计 {obs_date} 的待审价格")

        rows = db.q("""
            SELECT po.*, c.name AS channel_name, c.kind AS channel_kind,
                   c.default_seller_type
            FROM price_obs po JOIN channel c ON c.id = po.channel_id
            WHERE po.obs_date = ? AND po.audit_status = 'pending'
            ORDER BY po.country_code, po.model_guess, po.id
            LIMIT ?
        """, (obs_date, limit))
        if not rows:
            self.finish("ok", "没有待审记录", 0, 0)
            return {"total": 0, "accepted": 0, "rejected": 0, "reject_rate": 0.0}

        baselines = self._build_baselines(obs_date)
        self.log_step("建立基线", input_ref=obs_date,
                      parsed={"分组数": len(baselines),
                              "分组键": "country × model_key × month"},
                      decision="ok",
                      reason=f"共 {len(baselines)} 个「国家×型号×月」分组，"
                             f"样本≥{MIN_BASELINE_SAMPLES} 的才参与倍数判定")

        accepted, rejected, gray = [], [], []
        for r in rows:
            verdict, reason = self._rule_check(r, baselines)
            if verdict == "accepted":
                accepted.append((r, reason))
            elif verdict == "rejected":
                rejected.append((r, reason))
            else:
                gray.append((r, reason))

        self.log_step("规则层判定", parsed={
            "通过": len(accepted), "剔除": len(rejected), "移交模型": len(gray)},
            decision="ok",
            reason=f"规则层处理了 {len(accepted) + len(rejected)}/{len(rows)} 条，"
                   f"{len(gray)} 条灰色地带交模型")

        # 第二层：模型判灰色地带
        if gray and self.llm and self.llm.available():
            for r, hint in self._llm_judge(gray, baselines):
                (accepted if r["_verdict"] == "accepted" else rejected).append(
                    (r, r["_reason"]))
        else:
            # 没配 Key：灰色地带一律保留，但标注出来（宁可多留不可错杀）
            for r, hint in gray:
                accepted.append((r, f"灰色地带未经模型复核，暂予保留（{hint}）"))
            if gray:
                self.log_step("模型层跳过", parsed={"待复核": len(gray)},
                              decision="skipped", status="degraded",
                              reason="未配置 API Key，灰色地带全部保留并标注")

        self._write_back(accepted, rejected)

        total = len(rows)
        reject_rate = len(rejected) / total if total else 0.0

        # ★ 告警率要**只算"判断题"**，不算"定义题"。
        #   翻新机 / 捆绑装 / 缺货 是按口径**定义**排除的（确定性规则，天生就该多），
        #   而错价 / 溢价 / 配件误判是模型与基线做出的**判断**（可能判错）。
        #   两者混在一个比率里，告警会长期常亮 —— 实测一轮 52.3%，其中 94%
        #   是翻新+捆绑+缺货。常亮的告警等于没有告警，等真的分组键漏了维度
        #   （那才是这条告警要抓的东西）就没人会当回事了。
        scope_words = ("refurb", "非全新", "捆绑", "缺货")
        judged = [x for x in rejected
                  if not any(w in str(x[1]) for w in scope_words)]
        judged_base = len(accepted) + len(judged)
        judged_rate = len(judged) / judged_base if judged_base else 0.0

        warn = ""
        if judged_rate > ALERT_REJECT_RATE:
            warn = (f"★判定型剔除率 {judged_rate:.1%} 超过 {ALERT_REJECT_RATE:.0%} 告警线"
                    f"（{len(judged)}/{judged_base}，已排除翻新/捆绑/缺货这类按口径定义的排除）。"
                    f"经验上这更可能是基线分组键出问题（少了国家/型号维度），"
                    f"而不是当天脏数据特别多 —— 请先核对分组再信结论。")
            self.log_step("剔除率告警", parsed={
                "判定型剔除率": round(judged_rate, 4),
                "总剔除率": round(reject_rate, 4)},
                decision="alert", reason=warn, status="degraded")

        summary = (f"审计 {total} 条：通过 {len(accepted)}，剔除 {len(rejected)}"
                   f"（总 {reject_rate:.2%}，其中判定型 {len(judged)} 条 "
                   f"{judged_rate:.2%}）{'｜' + warn if warn else ''}")
        self.finish("ok", summary, total, len(accepted))
        return {"total": total, "accepted": len(accepted), "rejected": len(rejected),
                "reject_rate": reject_rate, "judged_rejected": len(judged),
                "judged_reject_rate": judged_rate, "warning": warn}

    # ------------------------------------------------ 基线

    @staticmethod
    def _bkey(row: dict) -> tuple:
        """★ 分组键：国家 × 型号 × 容量 × 月 —— 四个维度一个都不能少。

        少了**国家**：阿根廷 895,999 ARS 与巴西 3,499 BRL 是同一台机器，
                     跨币种混算会把整个高汇率国家判成异常
                     （上个项目实测剔掉 59% 的行、中位数偏 85%）。
        少了**型号**：不同机器混在一起，基线毫无意义。
        少了**容量**：★同型号的 128G 与 1TB 是两个价位段。混算出来的中位数
                     卡在中间 ⇒ 1TB 版本被判成"第三方溢价"剔除、
                     128G 版本被判成"疑似配件"。两头都错，且看起来很合理。
        用月不用周：周样本太少时中位数本身不稳，拿不稳的基线去剔异常会误杀。
        """
        model = (row.get("model_guess") or row.get("title") or "")[:60].lower()
        month = (row.get("obs_date") or "")[:7]
        # 容量分桶而不是精确值：256 与 258（页面写法差异）应算同一档
        rom = row.get("rom_gb")
        rom_bucket = _rom_bucket(rom)
        return (row.get("country_code"), model, rom_bucket, month)

    def _build_baselines(self, obs_date: str) -> dict:
        """用当月已通过审计的数据 + 当日全部数据建基线（中位数抗尖峰）"""
        month = obs_date[:7]
        rows = db.q("""
            SELECT country_code, model_guess, title, obs_date, sale_price, rom_gb
            FROM price_obs
            WHERE substr(obs_date,1,7) = ? AND sale_price IS NOT NULL
              AND audit_status <> 'rejected' AND is_bundle = 0 AND condition = 'new'
        """, (month,))
        groups: dict[tuple, list[float]] = {}
        for r in rows:
            groups.setdefault(self._bkey(r), []).append(r["sale_price"])
        return {k: statistics.median(v) for k, v in groups.items()
                if len(v) >= MIN_BASELINE_SAMPLES}

    # ------------------------------------------------ 规则层

    def _rule_check(self, r: dict, baselines: dict) -> tuple[str, str]:
        """返回 (accepted / rejected / gray, 理由)"""
        price, cur = r.get("sale_price"), r.get("currency") or ""

        if price is None:
            return "rejected", "无价格"
        if not extract.price_is_sane(price, cur):
            return "rejected", (f"{price} {cur} 超出该币种合理区间 —— "
                                f"典型成因：多打一位、或把分期月供当成总价")
        if r.get("is_bundle"):
            return "rejected", "捆绑装（主商品+配件），与单品价不可比"
        if r.get("condition") != "new":
            return "rejected", f"成色为 {r.get('condition')}，非全新机不参与比价"
        if not r.get("is_in_stock"):
            return "rejected", "页面显示缺货 —— 缺货价不代表真实可购买价"

        base = baselines.get(self._bkey(r))
        if base is None:
            # 基线样本不足：不做倍数判定。新品刚上市时全是这种情况，
            # 此时把它判成异常是错的 —— 交给下面的卖家类型规则。
            if r.get("seller_type") == "third_party":
                return "gray", "第三方卖家且基线样本不足，无法用倍数判定"
            return "accepted", "官方/自营渠道，基线样本不足但卖家可信"

        ratio = price / base
        if ratio >= HIGH_MULTIPLIER:
            if r.get("seller_type") == "third_party":
                return "rejected", (f"第三方卖家挂价 {price:.0f}，是同型号同国当月"
                                    f"中位价 {base:.0f} 的 {ratio:.1f} 倍 —— "
                                    f"非官方渠道溢价，剔除")
            return "gray", (f"价格是基线的 {ratio:.1f} 倍，但卖家是"
                            f"{r.get('seller_type')}，需复核是标错还是高配版")
        if ratio <= LOW_MULTIPLIER:
            return "gray", (f"价格仅为基线的 {ratio:.0%}，大概率是配件"
                            f"（保护壳/贴膜）被挂在整机类目下，需复核")
        if r.get("seller_type") == "third_party":
            return "accepted", (f"第三方卖家，但价格 {price:.0f} 落在基线 "
                                f"{base:.0f} 的合理带内（{ratio:.2f}×），"
                                f"视为正规经销商，保留")
        return "accepted", f"价格 {ratio:.2f}× 基线，卖家 {r.get('seller_type')}，正常"

    # ------------------------------------------------ 模型层

    def _llm_judge(self, gray: list[tuple[dict, str]], baselines: dict):
        """灰色地带交模型。分批处理控制 token。"""
        BATCH = 10
        out = []
        for i in range(0, len(gray), BATCH):
            batch = gray[i:i + BATCH]
            lines = []
            for idx, (r, hint) in enumerate(batch):
                base = baselines.get(self._bkey(r))
                lines.append(
                    f"{idx}. 标题「{(r.get('title') or '')[:90]}」\n"
                    f"   价格 {r.get('sale_price')} {r.get('currency')}"
                    f"{f'，同型号同国当月中位价 {base:.0f}' if base else '，无基线'}\n"
                    f"   渠道 {r.get('channel_name')}（{r.get('channel_kind')}）"
                    f"，卖家「{r.get('seller_name') or '未知'}」"
                    f"（判定={r.get('seller_type')}）\n"
                    f"   规则层疑点：{hint}")
            prompt = (
                "你是消费电子竞品价格分析师。下面每条是某拉美电商上的一条商品挂牌，"
                "请判断它**能否用于竞品价格分析**。\n\n"
                "剔除的情形：①这不是整机而是配件（保护壳/贴膜/充电器/支架等）；"
                "②第三方卖家的不合理溢价（远高于同型号市场价）；"
                "③翻新/二手/展示机；④页面价格明显标错；⑤捆绑套装。\n"
                "保留的情形：①正规授权经销商的合理价格；"
                "②高配版本导致的价格偏高（如更大容量/更高配色）；"
                "③新品刚上市尚无基线，但卖家与价格都合理。\n\n"
                "★注意：价格高于基线本身不构成剔除理由——要先看它是不是更高配的版本。\n\n"
                f"待判定：\n" + "\n".join(lines) + "\n\n"
                '只返回 JSON 数组，每条：{"idx":序号,"keep":true或false,'
                '"reason":"不超过40字的中文理由","category":"整机|配件|翻新|错价|溢价|捆绑"}')

            parsed = self.ask_json(
                f"复核灰色地带 {i}~{i + len(batch) - 1}", prompt,
                system="你是严谨的价格数据审计员，不确定时倾向保留并说明原因。",
                input_ref=f"batch:{i}", default=[])
            verdicts = {}
            for item in as_dicts(parsed):
                try:
                    verdicts[int(item.get("idx"))] = item
                except (TypeError, ValueError):
                    continue

            for idx, (r, hint) in enumerate(batch):
                v = verdicts.get(idx)
                if v is None:
                    r["_verdict"] = "accepted"
                    r["_reason"] = f"模型未给出判定，保守保留（{hint}）"
                else:
                    keep = bool(v.get("keep"))
                    r["_verdict"] = "accepted" if keep else "rejected"
                    r["_reason"] = (f"[{v.get('category', '?')}] "
                                    f"{str(v.get('reason') or '')[:80]}")
                out.append((r, hint))
        return out

    # ------------------------------------------------ 回写

    def _write_back(self, accepted: list, rejected: list) -> None:
        with db.tx() as conn:
            for r, reason in accepted:
                conn.execute("""UPDATE price_obs SET audit_status='accepted',
                                audit_reason=?, audit_by=? WHERE id=?""",
                             (reason[:300], self._by(reason), r["id"]))
            for r, reason in rejected:
                conn.execute("""UPDATE price_obs SET audit_status='rejected',
                                audit_reason=?, audit_by=? WHERE id=?""",
                             (reason[:300], self._by(reason), r["id"]))

    @staticmethod
    def _by(reason: str) -> str:
        return "agent:price_audit" if reason.startswith("[") else "rule:price_audit"


def _rom_bucket(rom) -> str:
    """把容量归到档位。同型号不同容量是不同价位段，必须分开建基线。
    分桶而非精确值：页面写法差异（256 / 256GB / 0.25TB）应落进同一档。"""
    try:
        v = int(rom) if rom else 0
    except (TypeError, ValueError):
        return "unknown"
    if v <= 0:
        return "unknown"          # 未知容量单独一组，不与已知容量混算
    for edge in (32, 64, 128, 256, 512, 1024, 2048):
        if v <= edge:
            return f"<={edge}"
    return ">2048"
