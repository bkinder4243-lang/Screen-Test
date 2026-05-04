#!/bin/bash
# Double-click this file to launch the Options Screener

APP_DIR="/Users/williamkinder/Desktop/Trading System"
STREAMLIT="/Library/Frameworks/Python.framework/Versions/3.12/bin/streamlit"

echo "========================================="
echo "  Options Screener — Starting up..."
echo "========================================="
echo ""
echo "Opening browser at http://localhost:8501"
echo "Press Ctrl+C in this window to stop."
echo ""

cd "$APP_DIR"
"$STREAMLIT" run app.py \
  --server.port 8501 \
  --server.enableCORS false \
  --server.enableXsrfProtection false \
  --browser.gatherUsageStats false
