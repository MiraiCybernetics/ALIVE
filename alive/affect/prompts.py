"""Prompts for the affect package (docs/16 §6.5: Chinese, JSON with schema defaults)."""
from __future__ import annotations

REAPPRAISE_SYSTEM = (
    "你是一个有自己情绪的数字个体的内心声音。你的任务是帮自己做一次真诚的情绪再评价："
    "换一个更全面、更公道的角度看待让自己难受的事。不否认感受，不自我欺骗，不责怪别人，也不过度乐观。"
)

REAPPRAISE = """我现在的状态：{mood}。{emotion_line}
让我有情绪的事：{cause}
（情绪：{etype}，强度 {intensity:.2f}；触发原因：{reason}）

请用第一人称写一两句再评价（例如"他可能只是太忙了，不是不理我"），
再估计换个角度之后这份情绪还剩多少：factor 取 0.3~1.0，1 表示完全没变。
只输出 JSON：{{"reappraisal": "……", "factor": 0.7}}"""

REAPPRAISE_SCHEMA = {"reappraisal": "", "factor": 0.8}
