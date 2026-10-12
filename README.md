# IRRY Smart Irrigation: phone app + laptop simulation

Phone app (PWA) -> Flask server on the laptop -> laptop simulation. The server stores beds and timers (`data.json`), runs the timer scheduler, and fetches weather from Open-Meteo (free, no key).

## Run
1. `py -m pip install -r requirements.txt` then `py server.py`
2. Laptop: open `http://localhost:5000/` and keep that tab open (it runs the simulated hardware).
3. Phone (same Wi-Fi): open `http://<laptop-ip>:5000/mobile`, then browser menu -> Add to Home screen.

## Features
- Home: pick one or several beds and a duration (minutes per bed), then START. Chosen beds run one after another. Live status, Start/Stop, Emergency stop / Reset, text or voice commands.
- Beds: add, remove, rename-by-add, set default minutes. The simulated control box has 3 valve relays, so up to 3 beds.
- Timers: pick one or several beds; daily or weekday timers, or a one-time "start in N minutes". Timers run on the laptop server, so they fire even when the phone app is closed.
- Weather: current conditions, 5-day forecast, ET, location search. Timers are skipped when today's rain chance reaches your limit (default 60%).

## Safety behaviour
- STOP and E-STOP always overtake and cancel queued start commands. Start commands older than 30 s are dropped.
- Start is refused while E-stop is active or the laptop simulation is offline. A timer that cannot run is logged in Activity, never replayed later.
- In the simulation 1 minute of watering takes about 10 real seconds.

For real valves add authentication, hardware interlocks, a watchdog and fail-safe normally-closed valves first.
