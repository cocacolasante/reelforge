"""Which signals predict how a reel performed: Spearman rank correlations."""

from __future__ import annotations

from reelforge_core.learning.dataset import Row

MIN_N = 8  # below this a correlation is noise; reported but flagged


def _ranks(values: list[float]) -> list[float]:
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        avg = (i + j) / 2.0 + 1.0
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        i = j + 1
    return ranks


def spearman(xs: list[float], ys: list[float]) -> float | None:
    """Rank correlation (ties averaged); None when either side is constant
    or there are fewer than 3 pairs. Pure."""
    if len(xs) != len(ys) or len(xs) < 3:
        return None
    rx, ry = _ranks(xs), _ranks(ys)
    mx, my = sum(rx) / len(rx), sum(ry) / len(ry)
    cov = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    vx = sum((a - mx) ** 2 for a in rx)
    vy = sum((b - my) ** 2 for b in ry)
    if vx == 0 or vy == 0:
        return None
    return cov / (vx * vy) ** 0.5


def correlations(rows: list[Row], target: str) -> list[tuple[str, int, float]]:
    """(feature, n, rho) for every feature seen with this target, strongest
    first. Pure."""
    names = sorted({k for r in rows for k in r.features})
    out: list[tuple[str, int, float]] = []
    for name in names:
        pairs = [(r.features[name], r.targets[target]) for r in rows
                 if name in r.features and r.targets.get(target) is not None]
        rho = spearman([p[0] for p in pairs], [p[1] for p in pairs])
        if rho is not None:
            out.append((name, len(pairs), round(rho, 3)))
    return sorted(out, key=lambda t: -abs(t[2]))


def format_report(rows: list[Row], targets: tuple[str, ...]) -> str:
    joined = sum(1 for r in rows if r.features)
    lines = [f"{len(rows)} labelled clip(s), {joined} joined to their reel + scorecard"]
    for target in targets:
        corr = correlations(rows, target)
        lines.append(f"\n{target}:")
        if not corr:
            lines.append("  (not enough data)")
        for name, n, rho in corr[:15]:
            flag = "" if n >= MIN_N else "  (few labels — noise)"
            lines.append(f"  {name:32s} rho {rho:+.2f}  n={n}{flag}")
    return "\n".join(lines)
