# pipsgox

A lightweight web charting and custom-indicator application.

## Scope

- Candlestick, bar, and line charts
- Moving Average and Volume
- Custom Python/JavaScript indicators
- Indicator plots, markers, values, and tables
- One watchlist with up to 1000 stocks
- Pluggable market-data providers
- Broker/API independent architecture

## Architecture

Frontend (React + TypeScript + Vite)
→ FastAPI backend
→ Market-data provider
→ Indicator engine
→ Chart renderer

The first milestone is the data → chart → indicator pipeline. Real market-data providers will be connected after the core pipeline is working.

## Primary market-data master

PIPSGOX keeps trading/broker connectivity separate from market-data access.

- Default source: `yfinance`
- India sources: `nse`, `bse`
- Broker source: `broker` (legacy/optional)
- Configuration: `PIPSGOX_MARKET_DATA_SOURCE=yfinance`
- Providers are imported and initialized lazily, so there is no startup data download or continuous source-loading indicator.
- US and crypto sources are intentionally reserved for a later phase and can be added behind the same provider contract.

The current chart, header quote, and watchlist quote APIs use the primary source when it is not `broker`.
