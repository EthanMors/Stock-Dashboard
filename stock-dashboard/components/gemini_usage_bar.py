import streamlit as st

from data.gemini_tracker import get_today_stats, FLASH_DAILY_LIMIT, PRO_DAILY_LIMIT
from data.ai_router import get_active_models


def render_gemini_usage_bar() -> None:
    """Render a compact AI usage bar with progress indicators for Flash and Pro tiers."""
    stats = get_today_stats()
    cfg = get_active_models()

    flash = stats["flash"]
    pro = stats["pro"]
    last = stats["last_request"]
    last_tier = stats.get("last_tier")

    flash_pct = min(flash / FLASH_DAILY_LIMIT, 1.0) if FLASH_DAILY_LIMIT > 0 else 0.0
    pro_pct = min(pro / PRO_DAILY_LIMIT, 1.0) if PRO_DAILY_LIMIT > 0 else 0.0

    flash_remaining = max(FLASH_DAILY_LIMIT - flash, 0)
    pro_remaining = max(PRO_DAILY_LIMIT - pro, 0)

    last_str = f" · Last: {last} ({last_tier.title() if last_tier else ''})" if last else ""
    cli_status = "🟢 agy CLI Active" if cfg.get("available") else "🔴 CLI Not Found"

    with st.container():
        st.caption(f"🤖 **AI Daily Usage** · {cli_status}{last_str}")
        col_flash, col_pro = st.columns(2)

        with col_flash:
            st.caption(
                f"⚡ **Gemini 3.8 Flash** — {flash_remaining} remaining "
                f"({flash}/{FLASH_DAILY_LIMIT} · {flash_pct*100:.1f}% used)"
            )
            st.progress(flash_pct)

        with col_pro:
            st.caption(
                f"🧠 **Gemini 3.1 Pro** — {pro_remaining} remaining "
                f"({pro}/{PRO_DAILY_LIMIT} · {pro_pct*100:.1f}% used)"
            )
            st.progress(pro_pct)

    st.markdown("---")
