#!/bin/bash
# Start the API server
python api_server.py &
sleep 5
# Start the Bot
python shiro.py
