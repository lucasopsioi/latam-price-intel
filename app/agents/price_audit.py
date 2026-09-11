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

★★ 品类价格地板（2026-09-04 加，确定性规则，跑在型号基线**之前**）
  型号级基线在「配件占多数的桶」里必然自毁：rival_product #2292「Lenovo Tab」的
  分组键 ('CL','lenovo tab','unknown',月) 里 rom 未知的桶以贴膜为主，中位数 13,990 就是
  配件价，8,500 CLP 的钢化膜 = 0.61× 基线 → 落在"合理带" → accepted → 上了价格曲线。
  所以要一道**品类级**的确定性地板：同国同品类 accepted 整机中位价的 X 倍以下一律剔除。
  X 是拿 132,507 行实测出来的（scratchpad/floor_rule.py，见 FLOOR_X 注释），不是拍的。
  ★★ 2026-09-05 重标定（scratchpad/floor_rule2.py + floor_bias.py + floor_option.py）：
     原标定的"真整机代理"带了「有 sku_code 或 rival_product_id」这个闸，把白牌/未挂接的
     便宜真机整体排除在误杀分母外 —— 误杀率因此低估 3~14 倍。纠正后 phone/tablet/pc 的 X
     仍然成立（被杀的确实是背包/支架/面霜），只有 wearable 真出事（杀的是白牌真手表），
     改用**条件地板**（FLOOR_DEVICE_EVIDENCE）而不是整类移除。详见 FLOOR_X 上方注释。

★★ 取行契约（2026-09-04 改）
  旧版 `ORDER BY country_code, model_guess, id LIMIT 2000` + 每天只跑一次 = 2000 个名额
  永远按字母序先给 BR/CL，MX 全库 59,850 行 accepted=0、PE 只有小批次日才轮到；
  且三处调用点裸调 run() 不传日期，跨午夜批次审的是"次日"那几百行。
  现在：ORDER BY id（无国别偏置）+ run_all(obs_date) 循环到该日 pending 取尽。
  ★ 调用方**必须传 obs_date**（按本批次真实观测日期循环，同 Cleaner/PriceMove），
    tests/test_audit_status_consumers.py 用 ast 守着这条。
"""
from __future__ import annotations

import logging
import re
import statistics
from typing import Callable

from .. import db
from ..scraping import extract
from .base import BaseAgent
from .llm import K_RULE, K_SPEC, as_dicts, batch_key, pick_by_key

log = logging.getLogger("price_audit")

# 相对基线的倍数阈值
HIGH_MULTIPLIER = 1.8      # 高于基线 1.8 倍 → 可疑
LOW_MULTIPLIER = 0.35      # 低于基线 35% → 大概率是配件
MIN_BASELINE_SAMPLES = 3   # 基线样本少于这个数就不做倍数判定（基线本身不可信）
ALERT_REJECT_RATE = 0.01   # 剔除率超 1% 触发告警

# ★★ 品类价格地板：同国同品类当月 accepted 整机中位价 × X 以下 → 剔除（配件/错价）。
#   实证（2026-09-04，132,507 行，按同国同品类 accepted 整机中位价归一，
#   A=标题含明确配件词却判 device / B=无配件词且有 sku 或产品挂接（真整机代理）/
#   C=分类器已判 accessory）：
#     phone    B 的 P0.5=0.090；X=0.10 误杀 129/22,796（0.57%）且最低的 B 全是
#              "Flex de Carga / Vidrio Cámara / Pantalla Compatible con"（本身就是配件）
#              与 EasyFit 3 手环（品类错），真手机零误杀；抓 C 55.9%。
#     tablet   B 的 P0.5=0.098、最低 B 是 "Láminas PaperLike Para Honor Pad"（带 sku 的配件）；
#              真最便宜平板在 0.19~0.28（BR 儿童机 323/1,149、MX 1,699/8,999）；
#              X=0.10 误杀 64/12,120（0.53%）、抓 C 61.8%；X=0.15 起碰真机（1.02%）。
#     pc       C 的 94% 在 0.10 以下；X=0.10 误杀 64/11,597，全是背包/螺丝刀/65 PEN 月供价。
#     wearable 真 Smart Band 9 Active 在 0.105~0.119 ⇒ X 必须 <0.10；
#              X=0.08 误杀 2/9,800（10,000 CLP 的 Galaxy Watch9 = 错价，杀对）。★★这一行是错的，见下。
#     audio    **不可分，故意缺席**：真耳机 JBL Tune 110 在 0.173、B 的 P0.5=0.210，
#              而配件 C 的 P50 落在 0.38~0.59；X≤0.15 一条抓不到、X=0.25 误杀 3.06% 真耳机。
#   用户三个案例：8,500 CLP→0.030、13,990 CLP→0.049、CO 150,000→0.083，全在 0.10 以下。
#   ★ [X, LOW_MULTIPLIER) 区间仍走型号基线的灰区逻辑，地板只管"确定是配件/错价"的那一段。
#
# ★★★ 2026-09-05 重新标定：上面那组"误杀率"是**用错的分母算出来的**，全部偏低 3~14 倍。
#   原标定的"真整机代理 B"写成「无配件词 **AND（有 sku_code 或 rival_product_id）**」——
#   而白牌/未挂接的机器天生两样都没有，于是**真整机里最便宜的那一段被整体排除在分母外**，
#   恰好就是地板会杀到的那一段。构造偏差，不是抽样误差：换成不带这个闸的代理
#   （Bcls = 分类器 detect_product_kind 判 device，scratchpad/floor_rule2.py + floor_bias.py）后 ——
#     品类   旧 B（带闸）  新 Bcls（不带闸）   逐条人查killed标题的结论
#     pc     0.26%        2.12% (417 行)     全是背包/散热底座/扩展坞/靠垫/计算器/榨汁机 —— 分类器漏判，**杀对了**
#     phone  0.07%        0.36%  (98 行)     全是支架/风扇/自拍杆/欧莱雅面霜(!)，真机只有 9 行（迷你机+固话）
#     tablet 0.04%        0.16%  (22 行)     全是包/白板/桌子，真机 4 行（MX 700~856 的超低端安卓平板）
#     wearable 0.02%      0.28%  (53 行)     ★**反过来：绝大多数是真白牌手表**（见下）
#   ⇒ phone/tablet/pc 的 X 不动（构造偏差存在，但纠正后仍然杀对；tablet 那 4 行超低端机
#     与 phone 那 9 行是**已知残留**，为救它们把 X 压到 0.05 会让配件抓获率 69%→24%，不划算）。
#
# ★★ wearable 是唯一真出事的：X=0.08 杀掉 75 行 27 种标题，逐条看**全是真白牌手表**
#   （'Reloj Smartwatch Inteligente T2 Pro Active' 14,990 CLP=0.075×、'GOSHGG SMARTWATCH SERIE 6'
#   13,990、'SMART BRACELET 1.0 SMARTWATCH' 12,990、Amazon BR 'PEJE Smartwatch' 142 / 'relogio
#   smartwatch feminino' 119 BRL），其中 **19 行已经是 accepted** —— --floor-recheck 会把它们改成
#   rejected，即"修复"反而把真机从价格分析里删掉。
#   病根与 audio 同族：白牌真手表 9,990~15,990 CLP vs 品类 accepted 中位 ~200,000 CLP（Galaxy Watch7），
#   真机价格带整体压在 0.05~0.08，与配件带重叠。
#   ★ 但和 audio **有一处关键不同：这里两者在标题上可分**。便宜的真机都写着
#     smartwatch / reloj inteligente / smart band，便宜的配件都写着 correa / mica / cargador / funda。
#     audio 两边都叫 audífonos，所以只能整类缺席；wearable 可以用**条件地板**（见 FLOOR_DEVICE_EVIDENCE）。
#   ⇒ 选条件地板而不是整类移除：移除会把 wearable 的 2,264 条正确剔除全部丢掉，
#     其中 49 行 product_kind='device' + 553 行 'unknown' 会直接回到
#     `product_kind <> 'accessory'` 口径的图上（表带/充电器/汽车罩/童裤混进穿戴价格带）。
#     （以上三个数出自 scratchpad/floor_option.py，口径 = 当月 device+accessory 全量、
#      新品/非捆绑/有货/价格合理，不只是 pending，所以与 audit_backlog --dry 的数不同量级。）
#   ★ 残留体检（改完必须做，见知识页）：改后 wearable 里仍被地板剔除的 device 行 49 条 20 种标题，
#     逐条看全是 Manilla/Pulso/Lámina/Batería（product_kind 列是旧值，分类器现在判配件）——
#     真手表零残留。
FLOOR_X = {"phone": 0.10, "tablet": 0.10, "pc": 0.10, "wearable": 0.08}

# ★★ 条件地板：这些品类里"真整机价格带"与配件带重叠，**低于地板不足以定案**，
#   必须再看标题有没有本品类的整机证据。命中守卫 → 不剔，落回型号基线/灰区逻辑。
#   守卫 = 分类器判 device（吃 accessory_para_form / 位置规则）**且** category_evidence 给出本品类证据
#   （category_evidence 自带头部闸：'Correa **Compatible Con** Fitbit Charge 4' 的证据在闸之后，不算）。
#   ★ 只对 wearable 开。实测把同一条守卫开给 pc/phone/tablet 会放回 288/76/27 行 ——
#     'Mochila Laptop HP'、'Base laptop ajustable'、'Bolso Tablet Tucano'、'Crema Facial … Celular'，
#     因为那三个品类的通用词（laptop/notebook/portátil/tablet/celular）同时是**依附对象名**。
#     wearable 没有这个问题：便宜配件的主语词（correa/mica/cargador）都在 _ACCESSORY_WORDS 里。
#   ★ 守卫只会**放行**、不会剔除，失败方向永远是"多留一条"（与知识页"宁可不分类，
#     也不能把主机当配件"同向）。
#   ★ 已知词表边界：守卫的穿戴词来自 extract._CAT_EVIDENCE['wearable']（共享的那一份），
#     里面有 smartwatch / reloj inteligente / smart band / pulsera inteligente，
#     **没有** "pulsera de actividad"。全库当前没有这个形态的真机被误杀（逐条查过），
#     真要补词得改 extract 那张表 —— 分类器/回填/审计共用它，绝不在这里另抄一份。
FLOOR_DEVICE_EVIDENCE = frozenset({"wearable"})
FLOOR_MIN_SAMPLES = 30        # 同国同品类 accepted 样本 ≥30 才用 accepted 中位数，否则回落
FLOOR_HEAD_WINDOW = 20        # 地板样本剔配件时，裸配件词只在标题前 20 字符算（知识页实测值）
FLOOR_REASON_PREFIX = "地板剔除："   # 地板判定的理由前缀，统计/回滚按它识别

# ★ 「第三方且基线样本不足」的处置策略（2026-09-04）：
#   历史 LLM 灰区裁决 2,223 条里 2,211 条拒（99.5%），其中 1,453 条的理由文本全是
#   "第三方卖家且基线样本不足" —— 与提示词"无基线但合理→保留"正好相反，LLM 层事实上是
#   "第三方无基线一律拒"的橡皮图章，1.21M token 买一个可以写成一行 if 的结论。
#   accept = 暂留并打标（reason 以 NO_BASELINE_REASON 开头），基线成熟后由
#            tools/audit_backlog.py --recheck-no-baseline 重审（只允许 accepted→rejected）；
#   reject = 与历史 LLM 行为一致，但 MX 会少 3,145 条、CL 少 4,094 条第三方观测；
#   gray   = 旧行为，交 LLM（每行 ≈135 token）。
NO_BASELINE_THIRD_PARTY = "accept"
NO_BASELINE_REASON = "第三方且无基线，暂留"

# 地板样本剔配件：消费 extract 里现成的词表（单一实现多处消费，不另写词表）。
# 词边界匹配 + 只看标题头部 —— 裸子串会让 "Memoria 128GB" 把整机踢出样本。
_ACC_WORD_RE = re.compile(
    r"\b(?:" + "|".join(re.escape(w.strip()) for w in
                        sorted(extract._ACCESSORY_WORDS, key=len, reverse=True)) + r")\b",
    re.I)


def _floor_ratio(r: dict, floors: dict | None) -> tuple[float, float, float] | None:
    """这一行是否落在品类地板之下 → (price, med, X)；否则 None。

    ★ 地板阈值的**唯一一份**算法：判定（_floor_check）与守卫统计（_floor_veto_hit）
      都从这里取，免得两处各写一遍不等式、日后只改一处。
    """
    if not floors:
        return None
    price = r.get("sale_price")
    if price is None:
        return None
    cat = r.get("category_code")
    X = FLOOR_X.get(cat)
    if not X:
        return None
    med = floors.get((r.get("country_code"), cat))
    if not med or price >= X * med:
        return None
    return price, med, X


def _device_evidence_veto(r: dict) -> str | None:
    """条件地板的守卫：这一行是不是"带本品类整机证据的整机"→ 返回依据，否则 None。

    两条都要成立（少一条就会放回配件）：
      ① 分类器判 device —— 吃 accessory_para_form / 位置规则，
         'Correa … Galaxy Watch'、'Mica … Smartwatch AMAZFIT'、'Cargador … Galaxy Watch' 全被它挡在外面；
      ② category_evidence 给出**本品类**证据 —— 它自带头部闸，
         'Correa **Compatible Con** Fitbit Charge 4' 的证据在闸之后不算数。
    单一实现多处消费：两个判据都是 extract 里现成的，这里不另写词表。
    """
    cat = r.get("category_code")
    if cat not in FLOOR_DEVICE_EVIDENCE:
        return None
    title = r.get("title") or ""
    kind, why = extract.detect_product_kind(title)
    if kind != "device":
        return None
    ev = [w for _, c, w in extract.category_evidence(title) if c == cat]
    if not ev:
        return None
    return (f"低于地板但标题带 {cat} 整机证据「{ev[0]}」且分类器判整机（{why}）"
            f"—— 白牌真机与配件价格带重叠，按条件地板放行")


def _title_looks_accessory(title: str | None) -> bool:
    """地板**样本**的配件过滤（不是判定）。漏掉几条只会把中位数往下拉一点 ——
    地板 = 0.1×中位数，中位数被配件拉低的失败方向是"少抓"，不是"误杀整机"。"""
    t = (title or "").lower().strip()
    if not t:
        return False
    if extract.detect_product_kind(t)[0] == "accessory":
        return True
    m = _ACC_WORD_RE.search(t[:FLOOR_HEAD_WINDOW + 24])
    return bool(m and m.start() < FLOOR_HEAD_WINDOW)


class PriceAuditAgent(BaseAgent):
    name = "price_audit"
    role = "price_audit"
    description = "判定挂牌价是否可用于价格分析，剔除第三方溢价/配件/翻新/错价"

    def run(self, obs_date: str | None = None, limit: int = 2000, *,
            after_id: int = 0, gray_policy: str = "llm",
            floors: dict | None = None) -> dict:
        """审一批该日的待审行。

        after_id     游标：只取 id > after_id 的行（run_all 用它翻页；gray_policy=
                     keep_pending 时灰区留 pending，没有游标会每轮重取同一批、永不终止）
        gray_policy  "llm"（有 Key 交模型、无 Key 保留并标注 —— 旧行为）
                     / "keep_pending"（灰区不回写，留给后续 LLM 或人工复核）
        floors       预建好的品类地板（run_all 每几轮刷一次，省得每轮扫全月）
        ★ 调用方必须传 obs_date：不传就是审"今天"，跨午夜批次会审到次日那几百行。
        """
        obs_date = obs_date or db.today()
        self.tokens = 0                       # 同一实例多轮调用时 token 按轮计，别累加
        self.start(f"审计 {obs_date} 的待审价格" + (f"（id>{after_id}）" if after_id else ""))

        # ★ ORDER BY id，不按国家：LIMIT + 非重要性 ORDER BY 在多国面板上 = 按字母序分配配额
        #   （BR→CL→CO→MX→PE，MX 永远轮不到），与 chart-and-color 那条 `ORDER BY brand LIMIT 60` 同族。
        rows = db.q("""
            SELECT po.*, c.name AS channel_name, c.kind AS channel_kind,
                   c.default_seller_type
            FROM price_obs po JOIN channel c ON c.id = po.channel_id
            WHERE po.obs_date = ? AND po.audit_status = 'pending' AND po.id > ?
            ORDER BY po.id
            LIMIT ?
        """, (obs_date, int(after_id), int(limit)))
        if not rows:
            self.finish("ok", "没有待审记录", 0, 0)
            # ★ 键集合与正常返回保持一致：调用方（orchestrator/postprocess_date/audit_backlog）
            #   拿 .get(k) 取值，缺键会静默变成 None 而不是 0。
            return {"total": 0, "accepted": 0, "rejected": 0, "gray_pending": 0,
                    "floor_rejected": 0, "floor_vetoed": 0, "reject_rate": 0.0,
                    "judged_rejected": 0, "judged_base": 0, "judged_reject_rate": 0.0,
                    "warning": "", "last_id": int(after_id),
                    "run_id": self.run_id, "tokens": 0}

        baselines = self._build_baselines(obs_date)
        self.log_step("建立基线", input_ref=obs_date,
                      parsed={"分组数": len(baselines),
                              "分组键": "country × model_key × month"},
                      decision="ok",
                      reason=f"共 {len(baselines)} 个「国家×型号×月」分组，"
                             f"样本≥{MIN_BASELINE_SAMPLES} 的才参与倍数判定")
        if floors is None:
            floors = self._build_category_floors(obs_date)
        self.log_step("品类地板", input_ref=obs_date,
                      parsed={"分组数": len(floors), "X": FLOOR_X,
                              "样本来源": dict(
                                  (f"{k[0]}/{k[1]}", f"{m['src']}:{m['n']}")
                                  for k, m in (self._floor_meta or {}).items())},
                      decision="ok",
                      reason=f"{len(floors)} 个「国家×品类」有地板；audio 故意无地板"
                             f"（真耳机 0.173 与配件 P50 0.38~0.59 不可分）")

        accepted, rejected, gray = [], [], []
        floor_vetoed, veto_samples = 0, []
        for r in rows:
            verdict, reason = self._rule_check(r, baselines, floors)
            if verdict == "accepted":
                accepted.append((r, reason))
            elif verdict == "rejected":
                rejected.append((r, reason))
            else:
                gray.append((r, reason))
            # 条件地板放行的行要留痕：静默放行与静默剔除一样危险。
            # ★ 只数**真的因此活下来**的行：被捆绑/翻新/缺货这些口径规则先剔掉的行，
            #   守卫放没放行都不改变结果，算进来会把这个诊断数字灌水。
            v = self._floor_veto_hit(r, floors) if verdict != "rejected" else None
            if v:
                floor_vetoed += 1
                if len(veto_samples) < 5:
                    veto_samples.append(f"{r.get('country_code')} {r.get('sale_price')}"
                                        f" {r.get('currency')}｜{(r.get('title') or '')[:48]}")
        floor_rejected = sum(1 for _, why in rejected if why.startswith(FLOOR_REASON_PREFIX))

        self.log_step("规则层判定", parsed={
            "通过": len(accepted), "剔除": len(rejected), "其中地板剔除": floor_rejected,
            "地板守卫放行": floor_vetoed, "守卫样例": veto_samples,
            "移交模型": len(gray)},
            decision="ok",
            reason=f"规则层处理了 {len(accepted) + len(rejected)}/{len(rows)} 条，"
                   f"{len(gray)} 条灰色地带交模型"
                   + (f"；条件地板守卫放行 {floor_vetoed} 条（低于地板但带整机证据）"
                      if floor_vetoed else ""))

        # 第二层：模型判灰色地带
        gray_pending = 0
        if gray and gray_policy == "keep_pending":
            # 灰区不回写：留 pending 给后续 LLM 轮或人工，游标保证下一轮不会再取到它们
            gray_pending = len(gray)
            self.log_step("模型层跳过", parsed={"留待复核": len(gray)},
                          decision="deferred", status="ok",
                          reason="gray_policy=keep_pending：灰色地带留 pending 不回写")
        elif gray and self.llm and self.llm.available():
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
        # ★★ 2026-09-05：**地板剔除也是"定义题"**，同样不计入。
        #   地板是确定性规则（同国同品类中位价的固定倍数），干跑实测占 pending 的 7.1%
        #   （13,158/184,229）—— 把它算进"判定型"，每一轮 agent_run 都会 degraded 告警。
        #   常亮的告警等于没有告警，这条本来就是为了抓"分组键漏维度"而设的。
        #   它单列在 floor_rejected 里，异常了看那个数。
        scope_words = ("refurb", "非全新", "捆绑", "缺货")
        judged = [x for x in rejected
                  if not str(x[1]).startswith(FLOOR_REASON_PREFIX)
                  and not any(w in str(x[1]) for w in scope_words)]
        judged_base = len(accepted) + len(judged)
        judged_rate = len(judged) / judged_base if judged_base else 0.0

        warn = ""
        if judged_rate > ALERT_REJECT_RATE:
            warn = (f"★判定型剔除率 {judged_rate:.1%} 超过 {ALERT_REJECT_RATE:.0%} 告警线"
                    f"（{len(judged)}/{judged_base}，已排除翻新/捆绑/缺货/地板这类按口径定义的排除）。"
                    f"经验上这更可能是基线分组键出问题（少了国家/型号维度），"
                    f"而不是当天脏数据特别多 —— 请先核对分组再信结论。")
            self.log_step("剔除率告警", parsed={
                "判定型剔除率": round(judged_rate, 4),
                "总剔除率": round(reject_rate, 4),
                "地板剔除（不计入判定型）": floor_rejected},
                decision="alert", reason=warn, status="degraded")

        summary = (f"审计 {total} 条：通过 {len(accepted)}，剔除 {len(rejected)}"
                   f"（总 {reject_rate:.2%}，其中判定型 {len(judged)} 条 "
                   f"{judged_rate:.2%}；地板 {floor_rejected}）"
                   f"{f'，灰区留待 {gray_pending}' if gray_pending else ''}"
                   f"{f'，地板守卫放行 {floor_vetoed}' if floor_vetoed else ''}"
                   f"{'｜' + warn if warn else ''}")
        self.finish("ok", summary, total, len(accepted))
        return {"total": total, "accepted": len(accepted), "rejected": len(rejected),
                "gray_pending": gray_pending, "floor_rejected": floor_rejected,
                "floor_vetoed": floor_vetoed,
                "reject_rate": reject_rate, "judged_rejected": len(judged),
                "judged_base": judged_base, "judged_reject_rate": judged_rate, "warning": warn,
                "last_id": rows[-1]["id"], "run_id": self.run_id, "tokens": self.tokens}

    FLOOR_REFRESH_ROUNDS = 5   # run_all 每几轮重建一次品类地板（全月扫描，不必每轮）

    def run_all(self, obs_date: str, batch: int = 2000, max_rounds: int = 200, *,
                gray_policy: str = "llm",
                on_round: Callable[[dict], None] | None = None,
                should_stop: Callable[[], bool] | None = None) -> dict:
        """把某个观测日的 pending 审到取尽：循环 run(obs_date, batch, after_id=游标)。

        每轮一条 agent_run（留痕已内建在 run 里）。终止条件：该日已无 id>游标 的 pending
        （本轮取到的行数 < batch）、本轮 total=0、should_stop() 为真、或达 max_rounds。
        返回累计 dict；pending_left 是收尾时该日剩余 pending（keep_pending 下 = 灰区数）。

        ★★ 2026-09-05：agg 里必须有 "warning"。三处调用点
          （orchestrator._audit_day、tools/postprocess_date.py、tools/audit_backlog.py）
          写的都是 `r.get("warning")`，而 run_all 之前只搬运 6 个计数键 ——
          告警**每一轮都算了、每一轮都被丢掉**，那三行消费代码是死的，
          日志里看不出任何异常（silent-failures-detection 的第一种伪装：告警被静默吞掉）。
          按日汇总重算一次，而不是拼接每轮的字符串：分批本身是实现细节，
          "这一天的判定型剔除率"才是要看的量。
        """
        agg = {"obs_date": obs_date, "rounds": 0, "total": 0, "accepted": 0,
               "rejected": 0, "gray_pending": 0, "floor_rejected": 0, "floor_vetoed": 0,
               "judged_rejected": 0, "tokens": 0,
               "run_ids": [], "warning": "", "warned_rounds": 0, "stopped": ""}
        after_id, floors = 0, None
        for i in range(int(max_rounds)):
            if should_stop is not None and should_stop():
                agg["stopped"] = "should_stop"
                break
            if floors is None or i % self.FLOOR_REFRESH_ROUNDS == 0:
                floors = self._build_category_floors(obs_date)
            r = self.run(obs_date=obs_date, limit=batch, after_id=after_id,
                         gray_policy=gray_policy, floors=floors)
            if not r["total"]:
                break
            agg["rounds"] += 1
            for k in ("total", "accepted", "rejected", "gray_pending",
                      "floor_rejected", "floor_vetoed", "judged_rejected", "tokens"):
                agg[k] += r.get(k) or 0
            if r.get("warning"):
                agg["warned_rounds"] += 1
            agg["run_ids"].append(r.get("run_id"))
            after_id = r["last_id"]
            r["round"] = agg["rounds"]
            r["obs_date"] = obs_date
            if on_round is not None:
                on_round(r)
            if r["total"] < batch:
                break
        else:
            agg["stopped"] = "max_rounds"
        left = db.q1("SELECT COUNT(*) n FROM price_obs WHERE obs_date=? "
                     "AND audit_status='pending'", (obs_date,)) or {}
        agg["pending_left"] = left.get("n") or 0
        # ★ 按日重算判定型剔除率并生成 agg["warning"]（口径与 run() 逐字一致：
        #   分母 = 通过 + 判定型剔除，翻新/捆绑/缺货/地板都不算）
        base = agg["accepted"] + agg["judged_rejected"]
        agg["judged_base"] = base
        agg["judged_reject_rate"] = (agg["judged_rejected"] / base) if base else 0.0
        if agg["judged_reject_rate"] > ALERT_REJECT_RATE:
            agg["warning"] = (
                f"★{obs_date} 判定型剔除率 {agg['judged_reject_rate']:.1%} 超过 "
                f"{ALERT_REJECT_RATE:.0%} 告警线（{agg['judged_rejected']}/{base}，"
                f"已排除翻新/捆绑/缺货/地板这类按口径定义的排除；地板另有 "
                f"{agg['floor_rejected']} 条）。经验上这更可能是基线分组键出问题"
                f"（少了国家/型号维度），而不是当天脏数据特别多 —— 请先核对分组再信结论。")
        return agg

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
        """用当月已通过审计的数据 + 当日全部数据建基线（中位数抗尖峰）

        ★ 这里的 `<> 'rejected'` 是**审计自身**的取样口径（含待审行），不是分析消费方：
          刚采完的一天全是 pending，只用 accepted 建基线在积压期会空掉一整国。
          SQL 里的 `-- audit-self` 标记就是给 tests/test_audit_status_consumers.py 认的。
        """
        month = obs_date[:7]
        rows = db.q("""
            SELECT country_code, model_guess, title, obs_date, sale_price, rom_gb
            FROM price_obs
            WHERE substr(obs_date,1,7) = ? AND sale_price IS NOT NULL
              AND audit_status <> 'rejected'  -- audit-self: 审计建基线含待审行
              AND is_bundle = 0 AND condition = 'new'
        """, (month,))
        groups: dict[tuple, list[float]] = {}
        for r in rows:
            groups.setdefault(self._bkey(r), []).append(r["sale_price"])
        return {k: statistics.median(v) for k, v in groups.items()
                if len(v) >= MIN_BASELINE_SAMPLES}

    # ------------------------------------------------ 品类地板

    _floor_meta: dict | None = None

    def _build_category_floors(self, obs_date: str) -> dict:
        """→ {(country, category): 当月 accepted 整机中位价}。

        样本口径：当月 product_kind='device' AND condition='new' AND is_bundle=0
        AND is_in_stock=1 AND sale_price 合理，标题先过配件词过滤（消费 extract 词表）。
        accepted 样本 ≥ FLOOR_MIN_SAMPLES 才用 accepted 中位数；否则回落到 `<> 'rejected'`
        同口径（MX 现在 accepted=0，回落中位数实测合理：tablet 8,999 / phone 5,529 / pc 16,799）；
        两者都不足 → 该组无地板（宁可不剔）。
        样本来源与 n 记在 self._floor_meta 供留痕/干跑打印。
        """
        month = obs_date[:7]
        rows = db.q("""
            SELECT country_code cc, category_code cat, title, sale_price p, currency,
                   audit_status st
            FROM price_obs
            WHERE substr(obs_date,1,7) = ? AND product_kind = 'device'
              AND condition = 'new' AND is_bundle = 0 AND is_in_stock = 1
              AND sale_price IS NOT NULL AND category_code IS NOT NULL
              AND audit_status <> 'rejected'  -- audit-self: 地板样本，accepted 不足时回落到未剔除行
        """, (month,))
        groups: dict[tuple, dict[str, list[float]]] = {}
        for r in rows:
            if r["cat"] not in FLOOR_X:
                continue
            if not extract.price_is_sane(r["p"], r["currency"]):
                continue
            if _title_looks_accessory(r["title"]):
                continue
            g = groups.setdefault((r["cc"], r["cat"]), {"acc": [], "all": []})
            g["all"].append(r["p"])
            if r["st"] == "accepted":
                g["acc"].append(r["p"])
        floors: dict[tuple, float] = {}
        meta: dict[tuple, dict] = {}
        for k, g in groups.items():
            if len(g["acc"]) >= FLOOR_MIN_SAMPLES:
                src, vals = "accepted", g["acc"]
            elif len(g["all"]) >= FLOOR_MIN_SAMPLES:
                src, vals = "non_rejected", g["all"]
            else:
                continue
            floors[k] = statistics.median(vals)
            meta[k] = {"src": src, "n": len(vals), "med": floors[k]}
        self._floor_meta = meta
        return floors

    @staticmethod
    def _floor_check(r: dict, floors: dict | None) -> str | None:
        """命中品类地板 → 返回剔除理由（以 FLOOR_REASON_PREFIX 开头）；否则 None。

        ★ 条件地板：低于地板的行若带本品类整机证据（FLOOR_DEVICE_EVIDENCE），放行不剔。
        """
        hit = _floor_ratio(r, floors)
        if hit is None:
            return None
        price, med, X = hit
        if _device_evidence_veto(r):
            return None
        return (f"{FLOOR_REASON_PREFIX}{price:.0f} {r.get('currency') or ''} 仅为同国同品类"
                f"当月中位价 {med:.0f} 的 {price / med:.1%}（<{X:.0%} 地板）—— 配件/错价，剔除")

    @staticmethod
    def _floor_veto_hit(r: dict, floors: dict | None) -> str | None:
        """低于地板、但被整机证据守卫放行 → 返回守卫依据；否则 None。

        ★ 只用于留痕/统计，不参与判定 —— 守卫一旦开始成批放行（词表被污染、品类串味），
          必须在 agent_run 里看得见；静默放行与静默剔除一样危险。
        """
        if _floor_ratio(r, floors) is None:
            return None
        return _device_evidence_veto(r)

    # ------------------------------------------------ 规则层

    def _rule_check(self, r: dict, baselines: dict,
                    floors: dict | None = None) -> tuple[str, str]:
        """返回 (accepted / rejected / gray, 理由)。floors 不传则不做品类地板判定。"""
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

        # ★★ 品类地板在型号基线**之前**：型号基线在"配件占多数的桶"里会自毁
        #   （#2292 CL 'lenovo tab' rom 未知桶中位 13,990 = 配件价，8,500 钢化膜 0.61× 通过）。
        floor_why = self._floor_check(r, floors)
        if floor_why:
            return "rejected", floor_why

        base = baselines.get(self._bkey(r))
        if base is None:
            # 基线样本不足：不做倍数判定。新品刚上市时全是这种情况，
            # 此时把它判成异常是错的 —— 交给下面的卖家类型规则。
            if r.get("seller_type") == "third_party":
                if NO_BASELINE_THIRD_PARTY == "accept":
                    # ★★ 2026-09-05 补 product_kind 闸：accept 分支原来不看这一列，
                    #   干跑实测 4,238 条走这条路的行里 unknown 1,094 + accessory 570 ——
                    #   标题是书籍/裙子/电视/表带，它们"没有基线"恰恰是**因为不是整机**
                    #   （型号猜不出来 ⇒ 分组键里全是各自一条 ⇒ 样本永远不足 3）。
                    #   把"没有基线"当成"新品上市"是把因果读反了。
                    #   只有 device 才 accept + 打标；accessory 直接剔；unknown 留灰区。
                    kind = (r.get("product_kind") or "").strip().lower()
                    if kind == "accessory":
                        return "rejected", ("第三方且基线样本不足，且已判定为配件 —— "
                                            "配件不进整机价格分析，剔除")
                    if kind != "device":
                        return "gray", (f"第三方卖家且基线样本不足，整机/配件判定为 "
                                        f"{kind or 'unknown'} —— 无基线不足以定案，留待复核")
                    return "accepted", (f"{NO_BASELINE_REASON}（rule:no_baseline，"
                                        f"基线成熟后由 audit_backlog --recheck-no-baseline 重审）")
                if NO_BASELINE_THIRD_PARTY == "reject":
                    return "rejected", ("第三方卖家且基线样本不足 —— 按策略剔除"
                                        "（历史 LLM 灰区裁决 2,211/2,223 拒）")
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
            # ★ 核对码：判决只按它对回观测，不信模型写的序号（错位 = 误收/误拒价格）
            keys = [batch_key(r.get("id") or r.get("row_hash") or r.get("title"))
                    for r, _ in batch]
            lines = []
            for idx, (r, hint) in enumerate(batch):
                base = baselines.get(self._bkey(r))
                lines.append(
                    f"{idx}. (k={keys[idx]}) 标题「{(r.get('title') or '')[:90]}」\n"
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
                '只返回 JSON 数组，每条：{"idx":序号,' + K_SPEC + ',"keep":true或false,'
                '"reason":"不超过40字的中文理由","category":"整机|配件|翻新|错价|溢价|捆绑"}\n'
                + K_RULE)

            parsed = self.ask_json(
                f"复核灰色地带 {i}~{i + len(batch) - 1}", prompt,
                system="你是严谨的价格数据审计员，不确定时倾向保留并说明原因。",
                input_ref=f"batch:{i}", default=[])
            # ★ 判决按核对码对回观测。抄错/没答的走下面「保守保留」分支 ——
            #   错位的失败方向必须是"多留一条"，绝不能是"把别人的判决扣到这条头上"。
            picked, kstats = pick_by_key(parsed, keys)
            verdicts = {j: item for j, item in picked}
            if kstats["rejected"]:
                self.log_step("复核序号核对", input_ref=f"batch:{i}", parsed=kstats,
                              decision="rejected", status="degraded",
                              reason="核对码抄错 = 判决属于别的观测，整条不采信，按保守保留处理")

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
