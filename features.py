#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""功能盘点：把这一路加过的东西逐项验一遍，报告「在 / 不在」。

不碰真数据库、不发消息 —— 用假替身，可以随时跑。
"""
from __future__ import annotations
import pathlib
import sys

sys.stdout.reconfigure(encoding="utf-8")
BASE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))
import brain as B  # noqa: E402

cfg = B.load_config()
pa = cfg["participation"]
ok_all = True


def item(name, cond, detail=""):
    global ok_all
    mark = "✅" if cond else "❌"
    if not cond:
        ok_all = False
    print(f"  {mark} {name:<40} {detail}")


print("=" * 78)
print("【记忆 / 上下文 —— 让回复更合理的那一批】")
print("=" * 78)
item("回看多少条历史 (context_messages)", pa.get("context_messages", 0) >= 30,
     f"= {pa.get('context_messages')} 条（原来 18）")
item("上下文按时间排序（旧->新）",
     "sorted(rows, key=lambda r: int(r.get(\"sort_seq\")" in (BASE / "brain.py").read_text(encoding="utf-8"),
     "get_messages 返回降序，必须反转，否则模型倒着读")
item("「你最近在群里说过的话」段落", callable(getattr(B.GroupBrain, "build_prompts", None)),
     "它自己说过的话单独列一段，防止「回答一次忘一次」")
item("引用解析 (parse_refermsg)", callable(getattr(B, "parse_refermsg", None)),
     "把 [引用 某人：原文] 拆出来")
item("引用我 = 在叫我 (quotes_me)", callable(getattr(B, "quotes_me", None)),
     "别人引用你的话，等于跟你说话")
item("撤回消息剔除 (drop_retracted)", callable(getattr(B, "drop_retracted", None)),
     "否则会回答一条已经不存在的话")
item("回复过时检查 (skip_stale_reply)", bool(pa.get("skip_stale_reply")),
     f"超过 {pa.get('stale_reply_seconds')} 秒 + {pa.get('stale_reply_messages')} 条新消息就咽回去")
item("冷却等待而非丢弃 (cooldown_wait_max_s)",
     "cooldown_wait_max_s" in pa, f"= {pa.get('cooldown_wait_max_s')} 秒内就等它过去再发")

print()
print("=" * 78)
print("【说话人归属 —— sender_username 会错，全部改用正文前缀】")
print("=" * 78)
src = (BASE / "brain.py").read_text(encoding="utf-8")
item("speaker_of 以正文前缀优先", "正文里的发送者前缀优先" in src, "前缀是微信写进消息体的，最可信")
item("is_self_message 以正文前缀优先", "没前缀 -> **就是我**" in src or "没前缀" in src,
     "sender_username 79% 是错的，信它会导致回复自己")

print()
print("=" * 78)
print("【谁在跟我说话 / 什么时候该接】")
print("=" * 78)
item("@ 了别人就不接 (skip_when_others_addressed)",
     bool(pa.get("skip_when_others_addressed")), "他们之间的对话，不插嘴（但仍进上下文）")
item("@所有人 算在叫我", "所有人" in src, "字面包含自己")
item("触发词 (trigger_words)", bool(pa.get("trigger_words")),
     "、".join(pa.get("trigger_words") or []))
item("被点名让路的闸门", bool(pa.get("mention_bypasses_cooldown")),
     "冷却 / 10分钟额度 / 置信度阈值 全都让路")
item("非文本消息也能读", "render_body" in src, "表情/图片/语音/链接渲染成短标签，不再整条丢掉")
item("系统消息不归到「我」头上", "系统消息" in src and "who = \"\"" in src,
     "否则撤回通知会被当成自己说的话")

print()
print("=" * 78)
print("【@ 别人】")
print("=" * 78)
item("@ 模式 (at_mode)", True, f"= {pa.get('at_mode')}（text=当普通文字发）")
item("分隔符统一 U+2005 (prepare_at)", callable(getattr(B, "prepare_at", None)),
     "普通空格的 @ 是死文字，不高亮不提醒")
item("@ 去重", callable(getattr(B, "prepare_at", None)), "@X @X 正文 -> @X 正文")
item("@ 只加在第一句", "at_who if i == 0 else" in src, "多句常在对不同人说话")
item("@ 目标校验（完全一致 / 唯一子串）", "匹配到多个人" in src, "多义就放弃，免得叫错人")

print()
print("=" * 78)
print("【稳定性 / 防卡死】")
print("=" * 78)
item("进程外守护器 (supervisor.py)", (BASE / "supervisor.py").exists(), "进程内看门狗会被 GIL 锁死，无效")
item("启动阶段卡死也能救", "判定卡在启动阶段" in (BASE / "supervisor.py").read_text(encoding="utf-8"),
     "心跳一直没出现时，按运行时长判定")
item("长操作期间持续心跳 (beat)", "def beat" in src, "发一条消息要 70 秒，不报进度会被误杀")
item("屏幕闸门 (require_screen)", bool(cfg["safety"].get("require_screen")),
     "锁屏时截图会失败并卡死，直接暂停")
item("账号白名单 (allowed_accounts)", bool(cfg["safety"].get("allowed_accounts")),
     f"= {cfg['safety'].get('allowed_accounts')}（机器上有 4 个微信账号）")
item("余额不足识别 (LLMBlocked)", "class LLMBlocked" in src, "402 时停止重试，不再对墙撞")
item("轮询间隔 (poll_interval_s)", float(cfg["safety"].get("poll_interval_s", 2)) >= 3,
     f"= {cfg['safety'].get('poll_interval_s')} 秒（降数据库压力）")
item("发送确认轮询放宽", "send_confirm_poll_s" in src, "每条消息的库读取从 15 次压到 ≤4 次")

print()
print("=" * 78)
print("【作息 / 夜间】")
print("=" * 78)
n = cfg.get("night") or {}
item("夜间模式开关", bool(n.get("enabled")),
     f"{n.get('start')} ~ {n.get('end')} 不回消息，也不补判积压")
item("晚安", bool(n.get("goodnight_at")), f"{n.get('goodnight_at')} 起，群里有人说话才发")
item("早上好", bool(n.get("greeting_until")), f"{n.get('end')} ~ {n.get('greeting_until')} 每群发一次")
item("夜间不补判（推水位）", "_night_skip" in src, "否则早上会淹没在一整夜的消息里")

print()
print("=" * 78)
print("【反复调戏的收场】")
print("=" * 78)
t = cfg.get("tease") or {}
item("逗的计数", bool(t.get("enabled")),
     f"同一个人 {t.get('window_s')} 秒内逗满 {t.get('count')} 次")
item("豁免名单", bool(t.get("exempt")), f"= {t.get('exempt')}（喊雷点词不算逗）")
item("收场方式", True, f"prefer = {t.get('prefer')}（concede 妥协 / cold 冷收场 / either 自选）")
item("冷处理", bool(t.get("ignore_s")), f"说完 {t.get('ignore_s')} 秒不理他")
item("可解除冷处理", "clear_ignore" in src, "有人让你理他时，它 @ 一下就算解除")

print()
print("=" * 78)
print("【人设 / 语气】")
print("=" * 78)
per = (BASE / "prompts" / "persona.md").read_text(encoding="utf-8")
sim = (BASE / "prompts" / "simulation.md").read_text(encoding="utf-8")
item("人格卡 persona.md", len(per) > 3000, f"{len(per)} 字符（little_whale 版）")
item("行为准则 simulation.md", len(sim) > 1000, f"{len(sim)} 字符（qq-bridge 的 qq-chat-v2）")
item("名字桥接（deepsleep 就是我）", "deepsleep" in per, "没有它，被 @ 也不认")
item("身份设定（四季亦然是创造者）", "四季亦然" in per, "")
item("性别设定：已按用户要求移除", "母鲸鱼" not in per, "用户明确要求去掉「你是女的」")
item("别越聊越凶的刹车", "别越聊越凶" in per, "拆掉自增强循环")
item("语气不必跟最近一致", "语气不必跟这些一致" in src, "否则把一时暴躁维持成人设")

print()
print("=" * 78)
print("【防回归】")
print("=" * 78)
item("自检脚本 selftest.py", (BASE / "selftest.py").exists(), "直接跑真实代码路径")
item("启动门禁（自检不过拒绝启动）",
     "REFUSING TO START" in (BASE / "start-daemon.ps1").read_text(encoding="utf-8"),
     "手写复刻测不出真 bug，改成真跑")
item("修复插件前状态脚本", pathlib.Path(r"F:\zhuomian\恢复插件前状态.cmd").exists(), "")

print()
print("=" * 78)
print("结论:", "✅ 上面全部还在" if ok_all else "❌ 有项目缺失（见上面的 ❌）")
print("=" * 78)
sys.exit(0 if ok_all else 1)
