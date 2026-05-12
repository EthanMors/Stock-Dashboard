import streamlit as st

from data.gemini_tracker import get_today_stats, FLASH_DAILY_LIMIT, PRO_DAILY_LIMIT


def render_gemini_usage_bar() -> None:
    """Render a compact Gemini usage bar with progress indicators for Flash and Pro."""
    stats = get_today_stats()
    flash = stats["flash"]
    pro = stats["pro"]
    last = stats["last_request"]

    flash_pct = min(flash / FLASH_DAILY_LIMIT, 1.0)
    pro_pct = min(pro / PRO_DAILY_LIMIT, 1.0)

    flash_remaining = max(FLASH_DAILY_LIMIT - flash, 0)
    pro_remaining = max(PRO_DAILY_LIMIT - pro, 0)

    last_str = f" · Last request: {last}" if last else ""

    with st.container():
        st.caption(f"🤖 **Gemini Daily Usage**{last_str}")
        col_flash, col_pro = st.columns(2)

        with col_flash:
            st.caption(
                f"**Flash** — {flash_remaining} remaining "
                f"({flash}/{FLASH_DAILY_LIMIT} · {flash_pct*100:.1f}% used)"
            )
            st.progress(flash_pct)

        with col_pro:
            st.caption(
                f"**2.5 Pro** — {pro_remaining} remaining "
                f"({pro}/{PRO_DAILY_LIMIT} · {pro_pct*100:.1f}% used)"
            )
            st.progress(pro_pct)

    st.markdown("---")
