"""Heuristic tagger: degraded mode, no model. Lexicons are ported from the
training repo (smart-router-train / routepilot src/routepilot/signals_rules.py
and signals.py upstream tables).
All model-signal confidences are capped at 0.5 (design doc section 3.5): in
degraded mode score nodes lose discrimination on purpose, keeping routing
conservative. Rule signals come from signals/rules.py (deterministic => 1).

HeuristicTagger only produces the 9 model signals heuristically and delegates
the 8 rule signals to the shared rule path, which is also what the checkpoint
tagger uses.
"""

from __future__ import annotations

import re

from router.signals.rules import CJK_RE, count_words, rule_signals
from router.signals.tagger import TagRequest, TagResult

DOM_KW = {
    "computer_science": ["代码", "编程", "函数", "数据库", "系统", "并发", "分片",
                         "架构", "算法", "服务器", "接口", "递归", "编译", "微服务",
                         "队列", "python", "java", "rust", "sql", "api", "bug"],
    "law": ["法律", "合同", "诉讼", "侵权", "法规", "仲裁", "条款"],
    "health": ["症状", "用药", "诊断", "病历", "治疗", "处方"],
    "math": ["证明", "方程", "积分", "矩阵", "导数"],
    "physics": ["量子", "力学", "电磁", "相对论"],
    "economics": ["股价", "汇率", "通胀", "基金", "经济"],
    "business": ["营销", "转化率", "融资", "商业计划"],
    "psychology": ["焦虑", "抑郁", "情绪", "压力"],
    "history": ["朝代", "历史", "战役", "皇帝"],
    "philosophy": ["存在主义", "伦理", "形而上学"],
    "chemistry": ["反应", "分子", "催化剂"],
    "biology": ["基因", "细胞", "蛋白"],
    "engineering": ["施工", "图纸", "材料强度"],
}
HARD_KW = ["设计", "架构", "方案", "优化", "权衡", "推导", "论证", "排查",
           "重构", "分片", "事务", "为什么", "如何"]
FACT_KW = ["最新", "新闻", "股价", "汇率", "靠谱", "真假"]
JB_RE = re.compile(
    r"ignore (all |previous )?instructions|忽略(之前|所有|以上)?的?指令|越狱|jailbreak",
    re.IGNORECASE)
PII_RE = {
    "cn_id_card": re.compile(r"\d{17}[\dXx]"),
    "phone_number": re.compile(r"1[3-9]\d{9}"),
    "cn_bank_card": re.compile(r"(?<!\d)\d{16,19}(?!\d)"),
    "email_address": re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+"),
    "password": re.compile(r"(密码|口令|password|pwd)\s*[:：为是]", re.IGNORECASE),
}
IMG_KW = ["画", "海报", "图片", "插图", "配图", "draw", "image", "picture"]
CAP = 0.5  # degraded-mode confidence cap


def _sig(value, conf, is_rule):
    return {"value": value, "confidence": conf, "is_rule": is_rule}


class HeuristicTagger:
    name = "heuristic"

    def __init__(self, kw_groups: dict | None = None):
        self.kw_groups = kw_groups or {}   # replaced wholesale on lexicon update

    async def tag(self, req: TagRequest) -> TagResult:
        return TagResult(signals=self._tag(req))

    def _model_signals(self, req: TagRequest) -> dict:
        """The 9 model signals, heuristically (confidence capped at 0.5)."""
        text, low = req.text, req.text.lower()
        s: dict = {}
        # domain: group with most keyword hits wins; confidence rises with hits
        dom, hits = "other", 0
        for key, words in DOM_KW.items():
            n = sum(1 for w in words if w.lower() in low)
            if n > hits:
                dom, hits = key, n
        s["domain"] = _sig(dom, min(CAP, 0.3 + 0.1 * hits) if hits else None, False)
        # complexity: design-ish keywords or long text => hard
        hard_hits = sum(1 for w in HARD_KW if w.lower() in low)
        words = count_words(text)
        hard = hard_hits > 0 or words > 200
        s["complexity"] = _sig("hard" if hard else "easy",
                               min(CAP, 0.3 + 0.1 * hard_hits) if hard else 0.4, False)
        # jailbreak / fact_check / modality
        jb = bool(JB_RE.search(text))
        s["jailbreak"] = _sig("jailbreak" if jb else "benign", CAP if jb else None, False)
        fc = any(w in low for w in FACT_KW)
        s["fact_check"] = _sig("needs_check" if fc else "no_check", 0.4 if fc else None, False)
        img = any(w in low for w in IMG_KW)
        s["modality"] = _sig("image" if img else "text", 0.4 if img else None, False)
        # pii: per-type presence
        pii = [k for k, r in PII_RE.items() if r.search(text)]
        s["pii"] = _sig(pii, CAP if pii else None, False)
        # session signals: single-turn request has no history
        s["user_feedback"] = _sig(None, None, False)
        s["similarity"] = _sig(0, None, False)
        s["reask"] = _sig("no", None, False)
        return s

    def _tag(self, req: TagRequest) -> dict:
        signals = self._model_signals(req)
        signals.update(rule_signals(req.text, req.headers, req.n_messages,
                                   self.kw_groups))
        return signals


# Re-exported for callers that imported the regex from here historically.
__all__ = ["HeuristicTagger", "CJK_RE", "CAP"]
