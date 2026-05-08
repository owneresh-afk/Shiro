#!/bin/bash
# Start the Flask API server in the background
python api_server.py &

# Start the Shiro Telegram bot
python shiro.py
