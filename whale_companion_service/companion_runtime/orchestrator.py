"""Read orchestration only: collect module projections and assemble one frame."""
from datetime import datetime, timezone

from .context_adapters import collect_context_fragments
from .context_assembler import ContextAssembler
from .context_contract import CompanionFrame


def build_companion_frame(*, user_text: str, conversation: list[dict], memories: list[dict],
                          user_facts: list[dict], boundaries: list[dict], persona: dict,
                          now=None, assembler=None, **module_inputs) -> CompanionFrame:
    moment = now or datetime.now(timezone.utc)
    fragments, recent, compatibility = collect_context_fragments(
        user_text=user_text, conversation=conversation, memories=memories, user_facts=user_facts,
        boundaries=boundaries, persona=persona, now=moment, **module_inputs)
    return (assembler or ContextAssembler()).assemble(
        fragments, query=user_text, scene=compatibility["sharedScene"]["intent"], now=moment,
        recent_conversation=recent, compatibility=compatibility)
