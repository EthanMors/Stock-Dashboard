# Stock Intelligence Dashboard

A high-performance financial analytics platform for equity research, options analysis, and sentiment-driven market intelligence. Built with a focus on data science, quantitative finance, and large language model (LLM) integration.

---

## 🚀 Key Features

### 1. **Sentiment & Social Intelligence (NLP / DS)**
*   **Reddit "Sub-Vibe" Analysis**: Real-time monitoring of `r/wallstreetbets` and other subreddits. Extracts tickers via custom regex and analyzes community sentiment using **Gemini LLM** to identify retail hype cycles and "DD" (Due Diligence) conviction.
*   **News Catalyst Engine**: Aggregates sector and stock-specific news, performing automated sentiment scoring (-1.0 to 1.0) and impact level assessment (1-10) using advanced prompt engineering to filter noise from alpha.

### 2. **Market Microstructure & Greeks Analysis (Quant / DS)**
*   **Gamma Exposure (GEX) Profiling**: Models dealer positioning and market liquidity by calculating aggregate Gamma, Vanna, and Charm across the entire option chain. Identifies "Volatility Triggers" and potential "Gamma Flips."
*   **Max Pain Calculation**: Implements an algorithm to find the strike price where the most options (in dollar value) expire worthless, a key indicator for institutional hedging behavior.
*   **Implied Volatility (IV) Surfaces**: Interpolates IV across deltas and expirations (Linear/Cubic) to visualize the "Volatility Smile" and skew.
*   **Sentiment Metrics**: Calculates **Risk Reversal** (25-delta call vs 25-delta put IV) to quantify market-implied directional bias.

### 3. **Fundamental & Quantitative Metrics (DA)**
*   **Put/Call Ratio Analysis**: Tracks volume and open-interest ratios across various expirations to identify sentiment extremes.
*   **Performance Benchmarking**: Automated calculation of P/E, PEG, EV/EBITDA, and Profit Margins with dynamic thresholding to flag overvalued or undervalued assets.
*   **Hedge Fund Tracking**: Visualizes institutional positioning and "Whale" activity through aggregated data sources.

### 4. **Portfolio & Data Engineering**
*   **Multi-Source Ingestion**: Unified pipeline for data from **yfinance**, **Webull SDK**, **SEC (EDGAR)**, and **Reddit API**.
*   **Performance Optimization**: SQLite-based persistence layer for caching expensive API responses and portfolio snapshots, significantly reducing latency and compute costs.

---

## 🛠 Tech Stack

*   **Language**: Python 3.10+
*   **Frontend**: Streamlit (Interactive Dashboard)
*   **Data Analysis**: Pandas, NumPy, SciPy (Statistics & Interpolation)
*   **Visualization**: Plotly (Dynamic Financial Charts)
*   **AI/LLM**: Google Gemini (via `google-genai` and CLI integration)
*   **Data Sources**: yfinance, BeautifulSoup4 (Scraping), edgartools (SEC), Webull OpenAPI SDK.

---

## 🔬 Implementation Deep Dive (Resume Highlights)

### **ML & NLP Implementation**
Developed a custom NLP pipeline for financial sentiment analysis. Unlike basic VADER sentiment, this implementation uses **Google's Gemini LLM** with specialized "Personas" (e.g., `news-analyst`, `reddit-analyst`).
*   **Prompt Engineering**: Leverages context-aware prompts that understand Reddit-specific slang (e.g., "diamond hands," "tendies," "Guh") and financial nuances (e.g., differentiating between sector-level headwinds and stock-specific catalysts).
*   **Structured Output**: Implemented robust regex and JSON parsing to transform unstructured LLM responses into structured data for quantitative modeling.

### **Quantitative Financial Engineering**
Implemented a volatility analysis module that computes institutional-grade metrics:
*   **Greeks & Exposure Modeling**: Programmatically calculates Gamma, Vanna, and Charm. Developed a **GEX Profile** generator that estimates dealer hedging impact on market volatility.
*   **IV Surface Construction**: Uses `scipy.interpolate` to handle missing data points in options chains, allowing for consistent Greeks and IV modeling.
*   **Risk Reversal Logic**: Programmatically identifies the 25-delta strike to calculate the skew between calls and puts, a key indicator used by professional traders to gauge market fear/greed.

### **Data Engineering & Performance**
Designed a resilient data layer to handle high-frequency requests and large datasets:
*   **Regex Ticker Extraction**: Developed a high-speed ticker extraction utility with a curated blacklist of 200+ common English words that overlap with tickers (e.g., "THE", "CAN", "ARE") to ensure high precision in social media analysis.
*   **Caching Strategy**: Built a local SQLite database to store portfolio and sentiment history, reducing redundant API calls by ~70% and ensuring dashboard stability during high market volatility.

---

## 📊 Impact for Data Roles

*   **Data Scientist**: Demonstrated ability to integrate LLMs into production workflows, perform complex statistical interpolations, and handle unstructured text data.
*   **Data Analyst**: Expert-level proficiency in translating raw financial data into actionable insights through benchmarking, visualization, and metric design.
*   **Machine Learning Engineer**: Experience in prompt engineering, model output validation, and building end-to-end data pipelines for AI-driven applications.
