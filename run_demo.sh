#!/bin/bash
# Start screen recording for 15 seconds
screencapture -v -V 15 ~/Desktop/Bounty_Meta_Demo.mp4 &
CAP_PID=$!

sleep 1

# Open Terminal and run the commands so the user can see it happen live
osascript -e 'tell application "Terminal"
    activate
    do script "cd ~/teamwork_projects/universal_bounty_v2 && clear && echo '\''==============================================='\'' && echo '\''[DEMO] TRIGGERING META-PRIORITY INTAKE SWEEP'\'' && echo '\''==============================================='\'' && PYTHONPATH=. python3 src/cli.py intake"
end tell'

# Wait for recording to finish
wait $CAP_PID
