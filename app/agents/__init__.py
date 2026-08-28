# -*- coding: utf-8 -*-
"""Agent 编排层。

    调度器 Orchestrator
        ├─ ChiefAgent        中央主 Agent：抓取前研判质量，产出计划
        ├─ Collector         采集（不调 LLM）
        ├─ CleanerAgent      清洗：LLM 兜底抽取 + 型号归一化
        ├─ PriceAuditAgent   价格审计：剔除第三方溢价/配件/翻新/错价
        └─ IntelAgent        情报：扫全球与拉美信息源，发现新品与上市节奏

全部继承 BaseAgent，每一步都写 agent_step 留痕。
"""
from .base import BaseAgent
from .chief import ChiefAgent
from .cleaner import CleanerAgent
from .intel import IntelAgent
from .llm import LLMClient
from .orchestrator import Orchestrator
from .brandintel import BrandIntelAgent
from .price_audit import PriceAuditAgent
from .pricemove import PriceMoveAgent
from .strategy import StrategyAgent
from .weekly import WeeklyReportAgent
from .spec_filler import SpecFillerAgent
from .voc_agent import VocAgent

AGENT_ROSTER = [
    {"name": "chief", "zh": "中央主 Agent", "role": "研判",
     "desc": "抓取前读渠道健康档案，判定每个渠道该正常抓/降频/换引擎/跳过，产出本轮计划"},
    {"name": "collector", "zh": "采集 Agent", "role": "抓取",
     "desc": "按计划驱动 Playwright/Selenium 抓列表页与详情页，不调用大模型"},
    {"name": "cleaner", "zh": "清洗 Agent", "role": "结构化",
     "desc": "前两级解析失败的页面交模型抽取；把电商标题归一化成型号并挂接友商产品"},
    {"name": "price_audit", "zh": "价格审计 Agent", "role": "把关",
     "desc": "判定每条挂牌价能否进价格分析：剔除第三方溢价、配件误判、翻新、错价"},
    {"name": "intel", "zh": "情报 Agent", "role": "情报",
     "desc": "每天扫全球 top 科技站与六国新闻，发现新品、判定重要度、记录上市节奏"},
    {"name": "voc", "zh": "VOC 分析 Agent", "role": "口碑",
     "desc": "翻译分析消费者评论，提炼好评/差评点；对评论量异常高的产品做归因"
             "（真热销 / 上市久 / 大促冲量 / 质量吐槽 / 刷评，五种含义完全相反）"},
    {"name": "price_move", "zh": "价格变动检测", "role": "事实层",
     "desc": "同SKU同渠道同币种比价，检出真实涨跌；区分官方渠道与第三方 —— "
             "官网降价是厂商动作，第三方降价可能只是卖家甩货"},
    {"name": "strategy", "zh": "价格策略分析", "role": "解读",
     "desc": "从价格变动模式推断厂商意图：老品清库/新品前降价/大促铺垫/成本涨价/价格战"},
    {"name": "brand_intel", "zh": "品牌动态分析", "role": "情报",
     "desc": "追踪竞品的经营动作：开体验店、发布会、营销战役、渠道合作、进入或退出市场"},
    {"name": "weekly", "zh": "周报汇总", "role": "汇总",
     "desc": "把一周的价格变动、策略信号、品牌动态汇总成给 销售团队 看的中文周报"},
    {"name": "spec_filler", "zh": "规格补全 Agent", "role": "补数",
     "desc": "给我方与友商产品补齐芯片/屏幕/内存等规格 —— 竞品匹配的必要输入。"
             "强制要求不确定填 null，并用发布年份做可验证锚点，防止模型编造规格"},
]

__all__ = ["BaseAgent", "ChiefAgent", "CleanerAgent", "IntelAgent", "LLMClient",
           "Orchestrator", "PriceAuditAgent", "PriceMoveAgent", "StrategyAgent",
           "BrandIntelAgent", "WeeklyReportAgent",
           "SpecFillerAgent", "VocAgent",
           "AGENT_ROSTER"]
