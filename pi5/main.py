# main.py - Flask app entry point for DDM Horse Dashboard

from flask import Flask, render_template, jsonify, request, Response, make_response, url_for
from flask_socketio import SocketIO, emit
import sys
import os
import requests
import json
import queue
import time
from datetime import datetime
from threading import Lock, Thread, Event

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import (FLASK_HOST, FLASK_PORT, FLASK_DEBUG, SYSTEM_NAME, VERSION, NUM_CUPS, TOTAL_LEDS,
                   WEATHER_API_KEY, WEATHER_LOCATION, WEATHER_CACHE_MINUTES,
                   TOTE_IP, TOTE_PORT, TOTE_TIMEOUT, TOTE_ENABLED,
                   PARAMS_FILE, ANTHROPIC_API_KEY,
                   ANIMATION_REGISTRY_FILE, ANIMATION_ASSIGNMENTS_FILE)
from communication.esp32_client import esp32, check_esp32_connection
from communication.tote_client import init_tote_client
from routes.racing_routes import racing_bp, init_racing_service
from routes.guest import guest_ui
from la_subasta import la_subasta_bp, init_la_subasta, follow_la_quiniela
from la_quiniela import (la_quiniela_bp, init_la_quiniela, start_la_quiniela,
                         quiniela_board_bp, init_board, start_board, get_board)
from la_quiniela import racetime
from la_quiniela.board import migrate_race_setup
from la_quiniela.odds import init_odds
import config as _config

# Initialize Flask app
app = Flask(__name__)
app.config['SECRET_KEY'] = 'ddm-horse-controller-2025'

# Initialize Socket.IO
socketio = SocketIO(app, cors_allowed_origins="*")

# Initialize tote board client
tote = None
if TOTE_ENABLED:
    tote = init_tote_client(TOTE_IP, TOTE_PORT, TOTE_TIMEOUT)
    print(f"Tote board client initialized: {TOTE_IP}:{TOTE_PORT}")

# Data directory for persistence
DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data')
RESULTS_FILE = os.path.join(DATA_DIR, 'results.json')
# The old Race Setup store. Race information is La Quiniela's now: pi5 copies
# what it can from this file once, at start (migrate_race_setup), and leaves
# the file where it is.
RACE_SETUP_FILE = getattr(_config, 'RACE_SETUP_FILE', None) or os.path.join(DATA_DIR, 'race_setup.json')


def static_url(filename):
    """url_for('static') with ?v=<the file's modification time>. Flask sends
    Last-Modified and no max-age, and a browser then keeps a file for a
    tenth of its age without asking: after a pull the touchscreen kept the
    dashboard's old JavaScript. A changed file is a new URL, so a pull and a
    restart always serve the new code without a hard reload."""
    try:
        version = int(os.path.getmtime(os.path.join(app.static_folder, filename)))
    except OSError:
        version = 0
    return url_for('static', filename=filename, v=version)


app.jinja_env.globals['static_url'] = static_url

# Ensure data directory exists
os.makedirs(DATA_DIR, exist_ok=True)

# The results are kept when pi5 starts: they are facts about the race, and a
# restart during the draw must come back to them (La Quiniela's TV board and
# cups read this file). They go with Reset betting (La Quiniela's admin page)
# and with the dashboard's RESET (/api/results/clear), nothing else.

# Weather cache
weather_cache = {
    'data': None,
    'timestamp': None
}

# SSE (Server-Sent Events) for real-time results push
sse_clients = []
sse_lock = Lock()


def tote_send(action, *args, **kwargs):
    """
    Safely send command to tote board
    
    Args:
        action: Method name to call on tote client (e.g., 'welcome', 'official')
        *args: Positional arguments to pass to the method
        **kwargs: Keyword arguments to pass to the method
    
    Returns:
        Response string or None if tote is disabled/offline
    """
    if not TOTE_ENABLED or not tote:
        return None
    
    try:
        method = getattr(tote, action, None)
        if method and callable(method):
            response = method(*args, **kwargs)
            return response
        else:
            print(f"[TOTE] Invalid action: {action}")
            return None
    except Exception as e:
        print(f"[TOTE] Error calling {action}: {e}")
        return None


# The splash display's board (the TV page), for the menu's "La Quiniela
# Board" link. {host} is replaced by the host name the dashboard was opened
# with, so the link works from the touchscreen (localhost) and from a phone
# (joeydevpi.local) alike. SPLASH_BOARD_URL in config.py, or
# DDM_SPLASH_BOARD_URL in pi5/.env, replaces it (a full URL when the splash
# runs on another machine).
SPLASH_BOARD_URL = (os.environ.get('DDM_SPLASH_BOARD_URL')
                    or getattr(_config, 'SPLASH_BOARD_URL', None)
                    or 'http://{host}:5001/')


def splash_board_url(host_header):
    """SPLASH_BOARD_URL with {host} filled in from a request's Host header
    (its port dropped)."""
    from urllib.parse import urlsplit
    try:
        host = urlsplit('//' + (host_header or '')).hostname or 'localhost'
    except ValueError:
        host = 'localhost'
    if ':' in host:                      # an IPv6 literal goes back in brackets
        host = '[' + host + ']'
    return SPLASH_BOARD_URL.replace('{host}', host)


@app.route('/')
def dashboard():
    """Main dashboard page"""
    return render_template('dashboard.html', 
                         system_name=SYSTEM_NAME,
                         version=VERSION,
                         num_cups=NUM_CUPS,
                         total_leds=TOTAL_LEDS,
                         board_url=splash_board_url(request.host))


@app.route('/spectator')
def spectator():
    """Full-screen tote board display for spectator TV"""
    return render_template('spectator.html')


@app.route('/api/ping', methods=['GET'])
def api_ping():
    """Test connection to ESP32"""
    is_connected = check_esp32_connection()
    return jsonify({
        'success': is_connected,
        'status': 'ONLINE' if is_connected else 'OFFLINE',
        'response': esp32.get_last_response()
    })


@app.route('/api/command', methods=['POST'])
def api_command():
    """Send a command to ESP32"""
    data = request.get_json()
    command = data.get('command', '')
    
    if not command:
        return jsonify({
            'success': False,
            'error': 'No command provided'
        }), 400
    
    response = esp32.send_command(command)
    success = not response.startswith('ERROR')
    
    return jsonify({
        'success': success,
        'command': command,
        'response': response
    })


@app.route('/api/led/all_on', methods=['POST'])
def api_led_all_on():
    """Turn all LEDs on"""
    response = esp32.all_on()
    return jsonify({
        'success': not response.startswith('ERROR'),
        'response': response
    })


@app.route('/api/led/all_off', methods=['POST'])
def api_led_all_off():
    """Turn all LEDs off"""
    response = esp32.all_off()
    
    # Stop tote board display
    tote_send('stop')
    
    return jsonify({
        'success': not response.startswith('ERROR'),
        'response': response
    })


@app.route('/api/led/brightness', methods=['POST'])
def api_led_brightness():
    """Set LED brightness"""
    data = request.get_json()
    brightness = data.get('brightness', 50)
    
    response = esp32.set_brightness(brightness)
    return jsonify({
        'success': not response.startswith('ERROR'),
        'brightness': brightness,
        'response': response
    })


@app.route('/api/led/color', methods=['POST'])
def api_led_color():
    """Set all LEDs to a color"""
    data = request.get_json()
    color = data.get('color', 'FFFFFF')
    
    response = esp32.set_color(color)
    return jsonify({
        'success': not response.startswith('ERROR'),
        'color': color,
        'response': response
    })


@app.route('/api/led/cup', methods=['POST'])
def api_led_cup():
    """Set a specific horse to a color"""
    data = request.get_json()
    cup_number = data.get('cup', 1)
    color = data.get('color', 'FFFFFF')
    
    response = esp32.set_cup(cup_number, color)
    return jsonify({
        'success': not response.startswith('ERROR'),
        'horse': cup_number,
        'color': color,
        'response': response
    })


@app.route('/api/animation/<anim_name>', methods=['POST'])
def api_animation(anim_name):
    """Start an animation"""
    # Check if this is RESULTS_ACTIVE with parameters
    if anim_name == 'RESULTS_ACTIVE':
        data = request.get_json()
        if data and 'win' in data and 'place' in data and 'show' in data:
            win = data.get('win', 1)
            place = data.get('place', 2)
            show = data.get('show', 3)
            
            # Send command directly to ESP32 with parameters
            command = f"ANIM:RESULTS_ACTIVE:{win}:{place}:{show}"
            response = esp32.send_command(command)
            
            # Send official results to tote board
            tote_send('official', win, place, show)
            
            return jsonify({
                'success': not response.startswith('ERROR'),
                'animation': anim_name,
                'response': response
            })
    
    # Map animations to tote board commands
    tote_mapping = {
        'WELCOME': ('welcome',),
        'RACE_START': ('race_start',),
        'BETTING_60': ('betting_open', 60),
        'BETTING_30': ('betting_open', 30),
        'BETTING_15': ('betting_open', 15),
        'BETTING_5': ('final_call',),
    }
    
    # Send to tote board if mapping exists
    if anim_name in tote_mapping:
        tote_action = tote_mapping[anim_name]
        if len(tote_action) > 1:
            tote_send(tote_action[0], tote_action[1])
        else:
            tote_send(tote_action[0])
    
    # Regular animation (no parameters)
    response = esp32.start_animation(anim_name)
    return jsonify({
        'success': not response.startswith('ERROR'),
        'animation': anim_name,
        'response': response
    })


def load_params_cache():
    """Load cached animation params from JSON file."""
    try:
        if os.path.exists(PARAMS_FILE):
            with open(PARAMS_FILE, 'r') as f:
                return json.load(f)
    except Exception as e:
        print(f'[PARAMS] Error loading cache: {e}')
    return {}

def save_params_cache(params: dict):
    """Save animation params to JSON cache file."""
    try:
        with open(PARAMS_FILE, 'w') as f:
            json.dump(params, f, indent=2)
    except Exception as e:
        print(f'[PARAMS] Error saving cache: {e}')

def parse_params_response(response: str) -> dict:
    """Parse PARAMS:key=value,key=value response from ESP32 into a dict."""
    params = {}
    if not response.startswith('PARAMS:'):
        return params
    pairs = response[7:].split(',')
    for pair in pairs:
        if '=' in pair:
            key, val = pair.split('=', 1)
            try:
                params[key.strip()] = int(val.strip())
            except ValueError:
                params[key.strip()] = val.strip()
    return params


def quiniela_mode(mode):
    """One race state: tell La Quiniela which mode the dashboard is in, and
    its race state (the cups, the TV board) follows by pi5's table
    (la_quiniela.betting.MODE_STATES). The mode buttons say so themselves
    (POST /api/quiniela/mode); this is for the two modes that are routes
    here: the results applied, and the reset. Never raises: the LEDs and the
    results do not wait on La Quiniela. Returns what was set, or None."""
    try:
        return get_board().set_mode(mode)
    except Exception as e:
        print(f'[La Quiniela] mode {mode} not set: {e}')
        return None


def save_results(win, place, show):
    """Save results to file. The file outlives a restart, so it is written
    whole or not at all (a temporary file, flushed to the card, renamed over
    it): a power cut mid-write leaves the old file, never half a new one."""
    results = {
        'win': win,
        'place': place,
        'show': show,
        'timestamp': datetime.now().isoformat()
    }
    tmp = RESULTS_FILE + '.tmp'
    try:
        with open(tmp, 'w') as f:
            json.dump(results, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, RESULTS_FILE)
        return True
    except Exception as e:
        print(f"Error saving results: {e}")
        return False


def load_results():
    """Load results from file"""
    try:
        if os.path.exists(RESULTS_FILE):
            with open(RESULTS_FILE, 'r') as f:
                return json.load(f)
    except Exception as e:
        print(f"Error loading results: {e}")
    return None


def load_animation_registry():
    """Load animation registry from JSON file."""
    try:
        if os.path.exists(ANIMATION_REGISTRY_FILE):
            with open(ANIMATION_REGISTRY_FILE, 'r') as f:
                return json.load(f)
    except Exception as e:
        print(f'[ANIM REGISTRY] Error loading: {e}')
    return {'animations': {}, 'race_states': []}


def load_animation_assignments():
    """Load saved animation assignments, falling back to registry defaults."""
    registry = load_animation_registry()
    defaults = {}
    for state in registry.get('race_states', []):
        for slot in state.get('slots', []):
            defaults[slot['id']] = slot['default']

    try:
        if os.path.exists(ANIMATION_ASSIGNMENTS_FILE):
            with open(ANIMATION_ASSIGNMENTS_FILE, 'r') as f:
                saved = json.load(f)
                defaults.update(saved)
    except Exception as e:
        print(f'[ANIM ASSIGNMENTS] Error loading: {e}')

    return defaults


def save_animation_assignments(assignments: dict):
    """Save animation assignments to JSON file."""
    try:
        os.makedirs(os.path.dirname(ANIMATION_ASSIGNMENTS_FILE), exist_ok=True)
        with open(ANIMATION_ASSIGNMENTS_FILE, 'w') as f:
            json.dump(assignments, f, indent=2)
        return True
    except Exception as e:
        print(f'[ANIM ASSIGNMENTS] Error saving: {e}')
        return False


def broadcast_sse(event, data):
    """Broadcast SSE message to all connected clients"""
    with sse_lock:
        dead_clients = []
        for client_queue in sse_clients:
            try:
                client_queue.put({'event': event, 'data': data}, block=False)
            except queue.Full:
                dead_clients.append(client_queue)
        
        # Remove dead clients
        for dead_client in dead_clients:
            sse_clients.remove(dead_client)


@app.route('/api/results', methods=['GET', 'POST'])
def api_results():
    """Get or set race results"""
    if request.method == 'GET':
        # GET - return current results
        results = load_results()
        if results:
            return jsonify({
                'success': True,
                'results': {
                    'win': results.get('win'),
                    'place': results.get('place'),
                    'show': results.get('show'),
                    'timestamp': results.get('timestamp')
                }
            })
        else:
            return jsonify({
                'success': False,
                'message': 'No results available'
            })
    
    else:
        # POST - set new results. They are facts about the race, not about
        # the LEDs: saved first, always, and La Quiniela goes to WINNER with
        # them; the LED controller is told after, and whether it answered is
        # reported ('leds'), never a reason to drop the results.
        data = request.get_json()
        win = data.get('win', 1)
        place = data.get('place', 2)
        show = data.get('show', 3)

        # Validate unique cups
        if len(set([win, place, show])) != 3:
            return jsonify({
                'success': False,
                'error': 'Win, Place, and Show must be different cups'
            }), 400

        # Save to file, first
        if not save_results(win, place, show):
            return jsonify({
                'success': False,
                'error': 'results not saved: pi5 could not write results.json (its console says why)',
                'results': {'win': win, 'place': place, 'show': show},
                'race': None
            }), 500

        # SET WINNERS, results applied: La Quiniela goes to WINNER with them
        race = quiniela_mode('RESULTS')

        # Send official results to tote board
        tote_send('official', win, place, show)

        # Broadcast to all connected clients via SSE
        broadcast_sse('results', {
            'win': win,
            'place': place,
            'show': show
        })

        # Then the LED controller. The three cups were locked as they were
        # picked (CUP:LOCK); what is left of the results on the LEDs is
        # RESULTS:FINALIZE, the winners' chase settling into the heartbeat,
        # which the page used to send itself (/api/results/finalize). The
        # results stand whatever it answers.
        response = esp32.send_command('RESULTS:FINALIZE')
        leds = 'unreachable' if response.startswith('ERROR') else 'ok'

        return jsonify({
            'success': True,
            'results': {
                'win': win,
                'place': place,
                'show': show
            },
            'leds': leds,
            'response': response,
            'race': race
        })


@app.route('/api/spectator/state')
def spectator_state():
    """Return current race state and horse data for spectator display."""
    try:
        state_data = racing_service.get_current_state_data()
        results = load_results()
        return jsonify({
            'success': True,
            'state': state_data.get('state', 'DORMANT'),
            'horses': state_data.get('horses', []),
            'race_name': state_data.get('race_name', 'Derby de Mayo'),
            'results': results
        })
    except Exception as e:
        return jsonify({
            'success': False,
            'error': str(e),
            'state': 'DORMANT',
            'horses': [],
            'results': None
        })


@app.route('/api/results/clear', methods=['DELETE', 'POST'])
def api_results_clear():
    """Clear race results"""
    try:
        # Delete the results file if it exists
        if os.path.exists(RESULTS_FILE):
            os.remove(RESULTS_FILE)

        # RESET ends the race: La Quiniela goes to AFTER_PARTY, results cleared
        race = quiniela_mode('RESET')

        # Turn off all LEDs
        esp32.all_off()
        
        # Send welcome to tote board
        tote_send('welcome')
        
        return jsonify({
            'success': True,
            'message': 'Results cleared',
            'race': race
        })
    except Exception as e:
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500


@app.route('/api/results/stream')
def results_stream():
    """SSE endpoint for real-time results notifications"""
    def event_stream():
        # Create a queue for this client
        client_queue = queue.Queue(maxsize=10)
        
        # Add client to list
        with sse_lock:
            sse_clients.append(client_queue)
        
        try:
            # Send initial connection message
            yield f"data: {json.dumps({'type': 'connected'})}\n\n"
            
            # Keep connection alive and send events
            while True:
                try:
                    # Wait for messages (with timeout for keep-alive)
                    message = client_queue.get(timeout=30)
                    event = message.get('event', 'message')
                    data = message.get('data', {})
                    yield f"event: {event}\ndata: {json.dumps(data)}\n\n"
                except queue.Empty:
                    # Send keep-alive comment
                    yield ": keep-alive\n\n"
        finally:
            # Remove client on disconnect
            with sse_lock:
                if client_queue in sse_clients:
                    sse_clients.remove(client_queue)
    
    return Response(event_stream(), mimetype='text/event-stream')


@app.route('/api/cup/lock', methods=['POST'])
def api_cup_lock():
    """Lock a cup to specific color during animations"""
    data = request.get_json()
    cup = data.get('cup')
    r = data.get('r', 255)
    g = data.get('g', 255)
    b = data.get('b', 255)
    
    if not cup:
        return jsonify({
            'success': False,
            'error': 'Cup number required'
        }), 400
    
    command = f"CUP:LOCK:{cup}:{r}:{g}:{b}"
    response = esp32.send_command(command)
    
    return jsonify({
        'success': not response.startswith('ERROR'),
        'cup': cup,
        'color': {'r': r, 'g': g, 'b': b},
        'response': response
    })


@app.route('/api/cup/unlock', methods=['POST'])
def api_cup_unlock():
    """Unlock cup(s) to return to animation"""
    data = request.get_json()
    cup = data.get('cup', 'ALL')
    
    if cup == 'ALL':
        command = "CUP:UNLOCK:ALL"
    else:
        command = f"CUP:UNLOCK:{cup}"
    
    response = esp32.send_command(command)
    
    return jsonify({
        'success': not response.startswith('ERROR'),
        'cup': cup,
        'response': response
    })


@app.route('/api/results/finalize', methods=['POST'])
def api_results_finalize():
    """Signal ESP32 to begin seamless winner blend (no LED interruption)"""
    response = esp32.send_command('RESULTS:FINALIZE')
    return jsonify({
        'success': response == 'OK:RESULTS:FINALIZE',
        'response': response
    })


@app.route('/api/params', methods=['GET'])
def api_params_get():
    """Get all animation params — fetch from ESP32 and cache locally."""
    try:
        response = esp32.send_command('PARAM:GET:ALL')
        if response and response.startswith('PARAMS:'):
            params = parse_params_response(response)
            save_params_cache(params)
            return jsonify({'success': True, 'params': params})
        else:
            # ESP32 unreachable — return cached values
            cached = load_params_cache()
            return jsonify({
                'success': bool(cached),
                'params': cached,
                'cached': True
            })
    except Exception as e:
        cached = load_params_cache()
        return jsonify({
            'success': bool(cached),
            'params': cached,
            'cached': True,
            'error': str(e)
        })


@app.route('/api/params', methods=['POST'])
def api_params_set():
    """Set a single animation param live on the ESP32."""
    data = request.get_json()
    key   = data.get('key', '').strip()
    value = data.get('value')

    if not key or value is None:
        return jsonify({'success': False, 'error': 'key and value required'}), 400

    command = f'PARAM:SET:{key}:{int(value)}'
    response = esp32.send_command(command)
    success = response and response.startswith('OK:PARAM:SET')

    if success:
        # Update local cache
        cached = load_params_cache()
        cached[key] = int(value)
        save_params_cache(cached)

    return jsonify({
        'success': success,
        'key': key,
        'value': value,
        'response': response
    })


@app.route('/api/params/save', methods=['POST'])
def api_params_save():
    """Tell ESP32 to persist current params to EEPROM."""
    response = esp32.send_command('PARAM:SAVE')
    success = response == 'OK:PARAM:SAVED'
    return jsonify({'success': success, 'response': response})


@app.route('/api/params/reset', methods=['POST'])
def api_params_reset():
    """Reset ESP32 params to defaults and clear local cache."""
    response = esp32.send_command('PARAM:RESET')
    success = response == 'OK:PARAM:RESET'
    if success:
        # Clear local cache so next GET fetches fresh defaults
        if os.path.exists(PARAMS_FILE):
            os.remove(PARAMS_FILE)
    return jsonify({'success': success, 'response': response})


@app.route('/api/animations/registry', methods=['GET'])
def api_animations_registry():
    """Return animation registry and current assignments."""
    registry = load_animation_registry()
    assignments = load_animation_assignments()
    return jsonify({
        'success': True,
        'registry': registry,
        'assignments': assignments
    })


@app.route('/api/animations/assignments', methods=['POST'])
def api_animations_assignments_save():
    """Save animation assignments."""
    data = request.get_json()
    if not data:
        return jsonify({'success': False, 'error': 'No data provided'}), 400
    success = save_animation_assignments(data)
    return jsonify({'success': success})


# The race roster for displays elsewhere (the splash display read it for its
# roster slide; anything else may): La Quiniela's store is the one home of
# race information, so this is built from it, in the shape it always had.
@app.route('/api/race', methods=['GET'])
def api_race():
    """Read-only race roster, from La Quiniela. Always 200, with CORS.

    race_state: La Quiniela's race state as the four words this route always
    spoke (0-3 "pre-race", 4 "running", 5-6 "post-race"; "unknown" while no
    horse in the field has a name). post_time: the post time on the race's
    clock ("5:57 PM CDT"), post_time_iso: the same instant in ISO 8601 with
    that clock's offset ("" both while no post time is set). horses: the
    field in numeric order, each under its own program number (22 for Ocelli
    standing in for 9; a horse scratched either way is not listed), the name
    as typed (a horse with no name is left out), the track's odds for that
    number or null, finish 1/2/3 from the results or null. winner: the WIN
    horse's number or null. last_updated: now, UTC."""
    from datetime import datetime, timezone

    state_str, winner, horses = 'unknown', None, []
    post_time_human, post_time_iso = '', ''
    try:
        board = get_board()
        model = board.model()
        typed = board.store.horses()                      # names as typed
        results = model.get('results') or {}
        finish = {horse: place for place, horse in ((1, results.get('win')), (2, results.get('place')),
                                                    (3, results.get('show'))) if horse}
        for n in range(1, 25):
            h = model['horses'].get(str(n)) or {}
            name = ((typed.get(n) or {}).get('name') or '').strip()
            if not h.get('in_field') or not name:
                continue
            horses.append({'number': n, 'name': name, 'odds': h.get('odds'), 'finish': finish.get(n)})
        state = int(model.get('race_state') or 0)
        state_str = 'running' if state == 4 else ('post-race' if state >= 5 else 'pre-race')
        winner = results.get('win')
        race = model.get('race') or {}
        if race.get('post_at') is not None:
            post_time_human = race.get('post_local') or ''
            post_time_iso = racetime.describe(race['post_at'], race.get('tz'))['iso']
    except Exception as e:
        print(f'[/api/race] La Quiniela lookup failed: {e}')

    if not horses:
        state_str = 'unknown'

    payload = {
        'race_state': state_str,
        'post_time': post_time_human,
        'post_time_iso': post_time_iso,
        'last_updated': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
        'horses': horses,
        'winner': winner,
    }
    response = make_response(jsonify(payload))
    response.headers['Access-Control-Allow-Origin'] = '*'
    return response


@app.route('/api/reset', methods=['POST'])
def api_reset():
    """Reset to idle state"""
    response = esp32.reset()
    
    # Send welcome to tote board
    tote_send('welcome')
    
    return jsonify({
        'success': not response.startswith('ERROR'),
        'response': response
    })


@app.route('/api/tote/ping', methods=['GET'])
def api_tote_ping():
    """Test connection to tote board"""
    if not TOTE_ENABLED or not tote:
        return jsonify({
            'success': False,
            'status': 'DISABLED',
            'message': 'Tote board is disabled'
        })
    
    is_connected = tote.ping()
    return jsonify({
        'success': is_connected,
        'status': 'ONLINE' if is_connected else 'OFFLINE',
        'response': tote.get_last_response()
    })


@app.route('/api/tote/command', methods=['POST'])
def api_tote_command():
    """Send a command to tote board"""
    if not TOTE_ENABLED or not tote:
        return jsonify({
            'success': False,
            'error': 'Tote board is disabled'
        }), 503
    
    data = request.get_json()
    action = data.get('action', '')
    params = data.get('params', {})
    
    if not action:
        return jsonify({
            'success': False,
            'error': 'No action provided'
        }), 400
    
    response = tote_send(action, **params)
    success = response and not response.startswith('ERROR')
    
    return jsonify({
        'success': success,
        'action': action,
        'response': response
    })


@app.route('/api/power', methods=['GET'])
def api_power():
    """Get software-estimated power draw from ESP32"""
    power = esp32.get_power_status()
    if power:
        return jsonify({'success': True, **power})
    return jsonify({'success': False, 'current_ma': 0, 'peak_ma': 0, 'min_ma': 0})


@app.route('/api/status', methods=['GET'])
def api_status():
    """Get system status"""
    is_connected = esp32.is_connected()
    
    status = {
        'esp32_connected': is_connected,
        'esp32_ip': esp32.ip,
        'esp32_port': esp32.port,
        'num_cups': NUM_CUPS,
        'total_leds': TOTAL_LEDS,
        'version': VERSION,
        'tote_enabled': TOTE_ENABLED
    }
    
    if TOTE_ENABLED and tote:
        status['tote_connected'] = tote.is_connected
        status['tote_ip'] = tote.ip
    else:
        status['tote_connected'] = False
        status['tote_ip'] = None
    
    return jsonify(status)


@app.route('/api/esp32/config', methods=['GET'])
def api_esp32_config_get():
    """Return the ESP32 client's current IP and port."""
    return jsonify({'ip': esp32.ip, 'port': esp32.port})


@app.route('/api/esp32/config', methods=['POST'])
def api_esp32_config_set():
    """Update the ESP32 client's IP, persist to config.py, and ping to verify."""
    import re

    data = request.get_json(silent=True) or {}
    new_ip = (data.get('ip') or '').strip()

    ip_pattern = re.compile(r'^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})$')
    match = ip_pattern.match(new_ip)
    if not match or any(int(o) > 255 for o in match.groups()):
        return jsonify({'success': False, 'error': 'Invalid IP address'}), 400

    esp32.ip = new_ip

    config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'config.py')
    persisted = False
    try:
        with open(config_path, 'r', encoding='utf-8') as f:
            contents = f.read()
        new_contents, count = re.subn(
            r'^ESP32_IP\s*=.*$',
            f'ESP32_IP = "{new_ip}"',
            contents,
            count=1,
            flags=re.MULTILINE,
        )
        if count == 1:
            with open(config_path, 'w', encoding='utf-8') as f:
                f.write(new_contents)
            persisted = True
    except OSError as e:
        print(f"[ESP32 CONFIG] Failed to persist IP to config.py: {e}")

    connected = esp32.ping()

    return jsonify({
        'success': True,
        'connected': connected,
        'persisted': persisted,
        'ip': esp32.ip,
        'port': esp32.port,
    })


def weather_data():
    """The weather, from WeatherAPI.com through the cache: (payload, status).
    payload is what GET /api/weather answers: {"success", "hourly" (12 hours
    from the current one), "current", "location", "cached"}, plus "stale"
    when a failed fetch fell back to an expired cache; {"success": False,
    "error"} with 503 when there is no key or nothing to fall back to. The
    dashboard's panel and La Quiniela's weather feed (the TV's crawl) both
    read it, so the API is asked at most once per WEATHER_CACHE_MINUTES."""
    global weather_cache

    # Check if API key is configured
    if not WEATHER_API_KEY:
        return {'success': False, 'error': 'Weather API key not configured'}, 503

    # Check cache
    now = datetime.now()
    if weather_cache['data'] and weather_cache['timestamp']:
        cache_age = (now - weather_cache['timestamp']).total_seconds() / 60
        if cache_age < WEATHER_CACHE_MINUTES:
            cached_data = weather_cache['data']
            return {
                'success': True,
                'hourly': cached_data.get('hourly', []),
                'current': cached_data.get('current', {}),
                'location': cached_data.get('location', 'Dallas, TX'),
                'cached': True
            }, 200

    # Fetch fresh data from WeatherAPI.com
    try:
        url = "http://api.weatherapi.com/v1/forecast.json"
        params = {
            'key': WEATHER_API_KEY,
            'q': WEATHER_LOCATION,
            'days': 2  # Request 2 days to ensure we have tomorrow's hours
        }

        response = requests.get(url, params=params, timeout=10)
        response.raise_for_status()

        data = response.json()

        # Get current hour from API's location time
        localtime = data.get('location', {}).get('localtime', '')  # Format: "2025-12-07 20:35"
        current_hour = int(localtime.split(' ')[1].split(':')[0]) if localtime else datetime.now().hour

        # Get forecast days
        forecast_days = data.get('forecast', {}).get('forecastday', [])

        # Collect hourly data starting from current hour
        all_hours = []

        # Get today's hours (from current hour onwards)
        if len(forecast_days) > 0:
            today_hours = forecast_days[0].get('hour', [])
            for hour_data in today_hours:
                hour_time_str = hour_data.get('time', '')  # Format: "2025-12-07 20:00"
                hour = int(hour_time_str.split(' ')[1].split(':')[0]) if hour_time_str else 0
                if hour >= current_hour:
                    all_hours.append(hour_data)

        # Get tomorrow's hours if we need more to reach 12 hours
        if len(forecast_days) > 1 and len(all_hours) < 12:
            tomorrow_hours = forecast_days[1].get('hour', [])
            needed_hours = 12 - len(all_hours)
            all_hours.extend(tomorrow_hours[:needed_hours])

        # Take exactly 12 hours
        hourly_data = all_hours[:12]

        # Extract current conditions
        current_data = data.get('current', {})

        # Cache the results
        weather_cache['data'] = {
            'hourly': hourly_data,
            'current': current_data,
            'location': data.get('location', {}).get('name', WEATHER_LOCATION)
        }
        weather_cache['timestamp'] = now

        return {
            'success': True,
            'hourly': hourly_data,
            'current': current_data,
            'location': data.get('location', {}).get('name', WEATHER_LOCATION),
            'cached': False
        }, 200

    except (requests.RequestException, ValueError) as e:
        print(f"Error fetching weather: {e}")
        # Return cached data if available, even if expired (the cache's own
        # fields: it used to hand back the whole cache as "hourly")
        if weather_cache['data']:
            cached_data = weather_cache['data']
            return {
                'success': True,
                'hourly': cached_data.get('hourly', []),
                'current': cached_data.get('current', {}),
                'location': cached_data.get('location', WEATHER_LOCATION),
                'cached': True,
                'stale': True
            }, 200

        return {'success': False, 'error': 'Failed to fetch weather data'}, 503


@app.route('/api/weather', methods=['GET'])
def api_weather():
    """Get weather forecast with caching - returns 12 hours starting from current hour"""
    payload, status = weather_data()
    return jsonify(payload), status


def weather_for_board(payload):
    """What La Quiniela's model carries of the weather (the TV's crawl reads
    it): {"location": "Dallas", "temp_f": 88, "condition": "Sunny"}, or
    None when the payload has no current conditions."""
    if not isinstance(payload, dict) or not payload.get('success'):
        return None
    current = payload.get('current')
    if not isinstance(current, dict) or not current:
        return None
    condition = current.get('condition')
    return {'location': payload.get('location'), 'temp_f': current.get('temp_f'),
            'condition': condition.get('text') if isinstance(condition, dict) else None}


def feed_weather_to_board(stop_event=None):
    """Daemon target: the weather into La Quiniela's model every
    WEATHER_CACHE_MINUTES (5 at least), so the TV's crawl can show it. A
    failed fetch leaves the last weather in place; with no API key there is
    none and the crawl leaves the item out."""
    period = max(5, int(WEATHER_CACHE_MINUTES or 15)) * 60
    stop_event = stop_event or Event()
    while not stop_event.is_set():
        try:
            payload, _status = weather_data()
            weather = weather_for_board(payload)
            if weather is not None:
                get_board().set_weather(weather)
        except Exception as e:
            print(f"[Weather] feed to La Quiniela failed: {e}")
        stop_event.wait(period)


# ---------------------------------------------------------------------------
# Initialize Racing Data Service and register blueprint
# ---------------------------------------------------------------------------
racing_service = init_racing_service(socketio=socketio, use_mock=True, esp32_client=esp32)
app.register_blueprint(racing_bp)
app.register_blueprint(guest_ui)
print("Racing data service initialised (mock mode)")

# La Subasta auction blueprint. Its horses are La Quiniela's (names, program
# numbers 1-24, the field, the scratches), read from the board's store in
# this process; the mock racing service above no longer feeds it.
init_la_subasta(socketio=socketio)
app.register_blueprint(la_subasta_bp)
print("La Subasta initialised (/la-subasta)")

# La Quiniela gateway bridge: routes + SocketIO room now, the serial thread
# only from the __main__ block below (importing this module never opens a port)
init_la_quiniela(socketio=socketio)
app.register_blueprint(la_quiniela_bp)
print("La Quiniela bridge initialised (/api/lq)")
init_board()
app.register_blueprint(quiniela_board_bp)
print("La Quiniela board initialised (/api/quiniela)")
# La Subasta follows the board's store from here on: a scratch recorded on
# the LQ admin page applies to the auction as it is saved, and one recorded
# while pi5 was down is applied now (idempotent: nothing is voided twice).
follow_la_quiniela()
# The real track's odds, for the TV's roster slide: fetched only while
# POST /api/quiniela/odds/start has it running, keyed by program number and
# served in La Quiniela's model as horses[n].odds (null without them).
init_odds(api_key=ANTHROPIC_API_KEY,
          sink=lambda odds: get_board().set_odds(odds),
          race=lambda: get_board().race_view(),
          emit=lambda payload: socketio.emit('odds_update', payload))


# ---------------------------------------------------------------------------
# Socket.IO event handlers
# ---------------------------------------------------------------------------

@socketio.on('request_racing_state')
def handle_request_racing_state():
    """Emit current racing state to the requesting client."""
    state_info = racing_service.get_state()
    emit('race_state_change', {
        'old_state': state_info['state'],
        'new_state': state_info['state'],
        'timestamp': time.time(),
        'state_info': state_info,
        'horses': racing_service.get_horses(),
        'mode': racing_service.get_mode(),
    })


if __name__ == '__main__':
    print("\n" + "="*60)
    print(f"  {SYSTEM_NAME}")
    print(f"  Version {VERSION}")
    print("="*60)
    print(f"\n  Dashboard:     http://{FLASK_HOST}:{FLASK_PORT}")
    print(f"  Spectator TV:  http://localhost:{FLASK_PORT}/guest/spectator")
    print(f"  Spectator TV:  http://localhost:{FLASK_PORT}/spectator")
    print(f"  ESP32 Target:  {esp32.ip}:{esp32.port}")
    
    # Tote board information and startup
    if TOTE_ENABLED and tote:
        print(f"  Tote Board: {tote.ip}:{tote.port}")
        print("\n  Testing tote board connection...")
        if tote.ping():
            print("  ✓ Tote board ONLINE")
            # Send welcome message on startup
            tote.welcome()
            print("  ✓ Welcome message sent to tote board")
        else:
            print("  ✗ Tote board OFFLINE")
    else:
        print("  Tote Board: DISABLED")
    
    print(f"  Racing Service: READY (auto-progression via POST /api/racing/start)")
    print(f"\n  Debug Mode: {FLASK_DEBUG}\n")
    print("="*60 + "\n")
    
    # Start the La Quiniela serial bridge exactly once. With debug on, the
    # Werkzeug reloader runs this file twice; only the child that serves
    # requests has WERKZEUG_RUN_MAIN set, and only it may open the port.
    if not FLASK_DEBUG or os.environ.get('WERKZEUG_RUN_MAIN') == 'true':
        start_la_quiniela()
        try:
            migrate_race_setup(RACE_SETUP_FILE)      # the old Race Setup file, once; left in place
        except Exception as e:
            print(f"[La Quiniela] Race Setup migration skipped: {e}")
        start_board()
        if WEATHER_API_KEY:
            # The weather into La Quiniela's model for the TV's crawl
            Thread(target=feed_weather_to_board, name='weather-feed', daemon=True).start()
    
    socketio.run(app, host=FLASK_HOST, port=FLASK_PORT, debug=FLASK_DEBUG, allow_unsafe_werkzeug=True)
