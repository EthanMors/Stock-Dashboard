from typing import Optional
import streamlit as st

_STATUS_DELTA = {"good": "normal", "neutral": "off", "warn": "inverse"}
_STATUS_LABEL  = {"good": "✅ Good", "neutral": "⚠️ Neutral", "warn": "🔴 Warn"}
_STATUS_COLOR  = {"good": "#00c853",  "neutral": "#ffd600",    "warn": "#ff1744"}


def render_metric_card(
    label: str,
    value: Optional[float],
    benchmark_status: str,
    description: str,
    benchmark_text: str,
) -> None:
    """Render a single metric as an st.metric card with color-coded benchmark status."""
    display_value = f"{value:.2f}" if value is not None else "—"
    status_label  = _STATUS_LABEL.get(benchmark_status, "")
    delta_color   = _STATUS_DELTA.get(benchmark_status, "off")

    st.metric(
        label=label,
        value=display_value,
        delta=status_label,
        delta_color=delta_color,
        help=f"{description}\n\n**Benchmark:** {benchmark_text}",
    )


def render_metric_group(title: str, metrics: list[dict]) -> None:
    """Render a titled section of metric cards in equal-width columns.

    Each dict in metrics must have keys: label, value, benchmark_status,
    description, benchmark_text.
    """
    st.subheader(title)
    cols = st.columns(len(metrics))
    for col, m in zip(cols, metrics):
        with col:
            render_metric_card(
                label=m.get("label", ""),
                value=m.get("value"),
                benchmark_status=m.get("benchmark_status", "neutral"),
                description=m.get("description", ""),
                benchmark_text=m.get("benchmark_text", ""),
            )
    with st.expander(f"{title} — benchmark guide"):
        for m in metrics:
            color = _STATUS_COLOR.get(m.get("benchmark_status", "neutral"), "#ffffff")
            st.markdown(
                f"<span style='color:{color}'>**{m.get('label', '')}**</span> — "
                f"{m.get('benchmark_text', '')}",
                unsafe_allow_html=True,
            )
