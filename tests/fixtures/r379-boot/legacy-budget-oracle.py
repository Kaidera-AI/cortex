def truncate_boot_tier(text: str, max_chars: int) -> str:
    """Trim optional boot tiers without cutting in the middle of a line.

    Compact boots are often read by another model as startup instructions.
    A dangling bullet such as ``"  - ["`` is worse than dropping the optional
    tier because it looks like corrupted context instead of omitted context.
    """
    if max_chars <= 0:
        return ''
    if len(text) <= max_chars:
        return text
    candidate = text[:max_chars].rstrip()
    last_newline = candidate.rfind('\n')
    if last_newline > 0:
        candidate = candidate[:last_newline].rstrip()
    lines = candidate.splitlines()
    while lines:
        tail = lines[-1].strip()
        if tail.endswith(':') or (tail.startswith('---') and tail.endswith('---')):
            lines.pop()
            continue
        break
    if not lines:
        return ''
    return '\n'.join(lines) + '\n  ... [truncated]'

def legacy_budget(p0_text, p1_text, p2_text, p3_text, budget):
    budget = min(max(int(budget), 50), 500)
    tiers = {'P0': p0_text, 'P1': p1_text, 'P2': p2_text, 'P3': p3_text}
    total_tokens = sum((len(v) // 4 for v in tiers.values()))
    for tier_key in ['P3', 'P2', 'P1']:
        if total_tokens <= budget:
            break
        excess = (total_tokens - budget) * 4
        available = len(tiers[tier_key])
        cut = min(excess, available)
        tiers[tier_key] = truncate_boot_tier(tiers[tier_key], available - cut)
        total_tokens = sum((len(v) // 4 for v in tiers.values()))
    boot_text = '\n\n'.join((v for v in tiers.values() if v.strip()))
    return boot_text
