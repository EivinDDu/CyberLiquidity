"""The app's logo, drawn as inline SVG so it needs no image files and follows the light/dark theme."""

from __future__ import annotations

from .theme import Palette

_ACCENT = {"light": "#1757ac", "dark": "#338ced"}

# A droplet (liquidity) with a pulse line (a monitored signal) on a rounded tile.
_MARK = ('<rect x="2" y="4" width="32" height="32" rx="8" fill="{accent}"/>'
         '<path d="M18 9C18 9 10 18.5 10 23.5a8 8 0 0 0 16 0C26 18.5 18 9 18 9Z" fill="#ffffff"/>'
         '<path d="M12.5 24h3l1.5-3 2.5 6 1.5-3h2.5" fill="none" stroke="{accent}" stroke-width="1.6" '
         'stroke-linecap="round" stroke-linejoin="round"/>')


def mark(pal: Palette) -> str:
    """Square icon (shown when the sidebar is collapsed)."""
    accent = _ACCENT.get(pal.mode, _ACCENT["light"])
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 36 40" width="36" height="40" role="img" '
            f'aria-label="CyberLiquidity">{_MARK.format(accent=accent)}</svg>')


def wordmark(pal: Palette) -> str:
    """Icon plus the product name (shown at the top of the sidebar)."""
    accent = _ACCENT.get(pal.mode, _ACCENT["light"])
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 200 40" width="200" height="40" role="img" '
            f'aria-label="CyberLiquidity">{_MARK.format(accent=accent)}'
            f'<text x="44" y="27.5" font-family="Inter, \'Helvetica Neue\', \'Segoe UI\', Arial, sans-serif" '
            f'font-size="19" font-weight="650" letter-spacing="-0.2" fill="{pal.text}">CyberLiquidity</text></svg>')
