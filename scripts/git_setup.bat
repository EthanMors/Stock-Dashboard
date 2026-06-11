@echo off
cd /d "C:\Users\ethan\Downloads\Stock Dashboard"
echo Current branch:
git branch --show-current
echo.
echo Creating/switching to updating-portfolio branch...
git checkout -b updating-portfolio 2>nul
if errorlevel 1 (
    echo Branch already exists, switching...
    git checkout updating-portfolio
)
echo.
echo Branch is now:
git branch --show-current
echo.
echo Done! Close this window and run git_commit.bat after changes are made.
pause
