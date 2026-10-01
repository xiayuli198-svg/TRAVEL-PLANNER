$env:PYTHONPATH = Join-Path $PSScriptRoot "..\src"
python -m travel_planner.web
