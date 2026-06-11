@echo off
cd /d "C:\Users\ethan\Downloads\Stock Dashboard"
echo Current branch:
git branch --show-current
echo.
echo Staging portfolio page changes...
git add stock-dashboard/pages/9_portfolio.py
git status
echo.
echo Committing...
git commit -m "Redesign portfolio page: wide layout, collapsible expanders, AI Insights agent

- Convert to wide dashboard format with 5-column metric card header
- Wrap all major sections (Holdings, News, Market Pulse, Options, Reddit, Smart Money) in collapsible expanders with 1-2 line actionable summaries
- Add Gemini 2.5 Pro AI Insights agent: collects portfolio data, calls Gemini via CLI subprocess, displays actionable recommendations
- Prompt includes top 3 recommendations, risks to watch, and portfolio health summary
- Refresh Insights button to re-call Gemini on demand"
echo.
echo Pushing to remote...
git push origin updating-portfolio
echo.
echo All done!
pause
