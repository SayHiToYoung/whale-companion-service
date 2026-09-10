"""Render selected, grounded candidates without another model call or state mutation."""


def render_proactive(candidate: dict) -> str:
    seed = str(candidate.get("content") or "").strip().rstrip("。！!？?")[:200]
    if not seed:
        return ""
    kind = candidate.get("kind")
    if kind == "open_emotion":
        feeling = {"sad": "难过", "frustrated": "烦", "angry": "生气", "tired": "累",
                   "anxious": "担心", "wronged": "委屈", "happy": "开心", "excited": "激动"}.get(seed, seed)
        return f"上次你说有些{feeling}，我还记着。后来有没有一点变化？"
    if kind == "follow_up":
        return f"你之前留给我的约定是「{seed}」。我来接着这件事了，后来怎么样？"
    if kind == "story_beat":
        if str(candidate.get("signature") or "").startswith("story_beat:shared:"):
            return "说到我们，" + seed.replace("你们", "我们").replace("她", "我") + "。我偏爱慢慢熟起来的过程，不用赶着把每一页翻完。"
        return f"我的虚拟小故事又有一小段：{seed.replace('她', '我')}。我偏爱这种不起眼的小变化，比大转折更耐看。"
    if kind == "agenda_share":
        reaction = "我偏爱这种不用赶着做下一件事的片刻。"
        for word, detail in (("雨", "我偏爱雨声胜过完全的安静，像给发呆配了背景音。"),
                             ("歌", "老歌有时候比照片还像一扇小门，一响就能把人拉回某个时候。"),
                             ("收拾", "我觉得收拾最有成就感的不是整间屋子，是终于空出来的那一小块桌面。"),
                             ("糊", "这一锅大概只能算厨艺的反面教材了。")):
            if word in seed:
                reaction = detail
                break
        return f"我这边的虚拟日常是「{seed}」。{reaction}"
    if kind == "memory_echo":
        return f"我想起了「{seed}」。回头看，你最记得哪个细节？"
    if kind == "grounded_opening":
        return f"{seed}。如果给这段经历起个小标题，你会怎么叫它？"
    return f"{seed}。我偏爱这种不用赶着做下一件事的片刻。"
