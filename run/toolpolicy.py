"""Operator-selected execution authority, independent of capability and cadence."""
MODES = ("chat-only", "tools")


def validate(mode):
    if mode not in MODES:
        raise ValueError("tool mode must be chat-only or tools")
    return mode


def instruction(mode):
    if mode is None:
        return "Legacy tool policy: retain per-contract tools settings; no new authority granted."
    validate(mode)
    if mode == "tools":
        return ("Operator tool mode: tools. Every new packet executes with workspace tools. "
                "For coding work inspect staged interfaces and run bounded self-checks before delivery. "
                "Use only authorized workspace material, not unrelated filesystem paths. "
                "Tool service executes code on its host. Hidden evaluation code is not worker input.")
    return ("Operator tool mode: chat-only. Set tools:false. Workers cannot read files or run "
            "self-checks through tools; supply public interfaces and requirements in the brief.")


def apply(contract, mode):
    result = dict(contract)
    if mode is not None:
        validate(mode)
        if mode == "chat-only" and result.get("tools"):
            raise ValueError("TOOL_POLICY: packet requires tools but operator selected chat-only")
        result["tools"] = mode == "tools"
    return result


def check_card(card):
    mode = card.get("tool_mode")
    if mode is not None:
        validate(mode)
        if bool(card.get("tools")) != (mode == "tools"):
            raise ValueError("TOOL_POLICY: card differs from recorded operator mode")
