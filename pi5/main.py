# main.py - Flask app entry point for DDM Horse Dashboard

# pi5 runs as the ddm-pi5 service (deploy/), never alongside it: a copy started
# by hand refuses while the service is active, before it opens anything.
import service_mode
if __name__ == '__main__':
    service_mode.refuse_alongside_the_service('ddm-pi5')

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
from communication.esp32_client import esp32
from communication.tote_client import ToteClient
from routes.racing_routes import racing_bp, init_racing_service
from routes.guest import guest_ui
from routes.guest import spectator as guest_spectator
from la_subasta import la_subasta_bp, init_la_subasta, follow_la_quiniela
from la_quiniela import (la_quiniela_bp, init_la_quiniela, start_la_quiniela,
                         quiniela_board_bp, init_board, start_board, get_board)
from la_quiniela import racetime
from la_quiniela.betting import POST_HELD_STATES, bets_at_post, finishers_needed, ordinal, race_state_name
from la_quiniela.board import field_by_post, migrate_race_setup
from la_quiniela.odds import init_odds
import config as _config
import page_rev

# Initialize Flask app
app = Flask(__name__)
app.config['SECRET_KEY'] = _config.FLASK_SECRET_KEY   # DDM_FLASK_SECRET_KEY in pi5/.env, or a random one per start
app.config['LS_ADMIN_PIN'] = _config.LS_ADMIN_PIN     # DDM_LS_ADMIN_PIN in pi5/.env: La Subasta's admin page and routes; empty = they are off

# Initialize Socket.IO
socketio = SocketIO(app, cors_allowed_origins="*")

# Initialize tote board client
tote = ToteClient(TOTE_IP, TOTE_PORT, TOTE_TIMEOUT) if TOTE_ENABLED else None

# Data directory for persistence
DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data')
RESULTS_FILE = os.path.join(DATA_DIR, 'results.json')
# The old Race Setup store. Race information is La Quiniela's now: pi5 copies
# what it can from this file once, at start (migrate_race_setup), and leaves
# the file where it is.
RACE_SETUP_FILE = getattr(_config, 'RACE_SETUP_FILE', None) or os.path.join(DATA_DIR, 'race_setup.json')


# Pages come up fresh after a pull (page_rev.py): static URLs carry the pages'
# revision (?v=), every response says it (X-Page-Rev) for the pages that
# reload themselves, and every HTML page is Cache-Control: no-store, the
# La Subasta pages included. The one place for all of pi5.
page_rev.init_app(app)
static_url = page_rev.static_url

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
    """The tote board's `action` (welcome, official, ...), when there is a
    board: it hands the figures to the board's worker and returns."""
    if tote:
        getattr(tote, action)(*args, **kwargs)


def leds_word():
    """The LED wall's health in a word, for a reply: online, unreachable,
    unknown (nothing exchanged yet), off (no address set)."""
    h = esp32.health()
    if not h['enabled']:
        return 'off'
    if h['online'] is None:
        return 'unknown'
    return 'online' if h['online'] else 'unreachable'


def led_reply(queued, **fields):
    """A LED route's reply: the command handed to the wall's worker and the
    wall's health as last known. The request never waits on the wall."""
    return jsonify({'success': True, 'queued': queued, 'leds': leds_word(), **fields})


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
    """Full-screen tote board display for spectator TV: the same page as
    /guest/spectator (routes/guest.py), which the Control Center links. It
    used to render spectator.html without the horses the template needs, so
    it answered 500."""
    return guest_spectator()


@app.route('/api/ping', methods=['GET'])
def api_ping():
    """Ask the LED wall for its PONG: an ask, which waits up to the wall's
    timeout and answers at once while the wall is marked down."""
    reply = esp32.ask('PING')
    return jsonify({
        'success': reply == 'PONG',
        'status': 'ONLINE' if reply == 'PONG' else 'OFFLINE',
        'response': reply
    })


@app.route('/api/command', methods=['POST'])
def api_command():
    """A raw command to the LED wall (the Test modal, the Animations list)"""
    data = request.get_json()
    command = data.get('command', '')

    if not command:
        return jsonify({
            'success': False,
            'error': 'No command provided'
        }), 400

    return led_reply(esp32.send(command), command=command)


@app.route('/api/led/all_on', methods=['POST'])
def api_led_all_on():
    """Turn all LEDs on"""
    return led_reply(esp32.all_on())


@app.route('/api/led/all_off', methods=['POST'])
def api_led_all_off():
    """Turn all LEDs off, and stop the tote board's display"""
    queued = esp32.all_off()
    tote_send('stop')
    return led_reply(queued)


@app.route('/api/led/brightness', methods=['POST'])
def api_led_brightness():
    """Set LED brightness"""
    data = request.get_json()
    brightness = data.get('brightness', 50)
    return led_reply(esp32.set_brightness(brightness), brightness=brightness)


@app.route('/api/led/color', methods=['POST'])
def api_led_color():
    """Set all LEDs to a color"""
    data = request.get_json()
    color = data.get('color', 'FFFFFF')
    return led_reply(esp32.set_color(color), color=color)


@app.route('/api/led/cup', methods=['POST'])
def api_led_cup():
    """Set a specific horse to a color"""
    data = request.get_json()
    cup_number = data.get('cup', 1)
    color = data.get('color', 'FFFFFF')
    return led_reply(esp32.set_cup(cup_number, color), horse=cup_number, color=color)


@app.route('/api/animation/<anim_name>', methods=['POST'])
def api_animation(anim_name):
    """Start an animation: handed to the wall's worker, and the tote board
    told where the animation has a display of its own"""
    # RESULTS_ACTIVE with the three horses: the winners' cups on the wall, the official figures on the tote
    if anim_name == 'RESULTS_ACTIVE':
        data = request.get_json()
        if data and 'win' in data and 'place' in data and 'show' in data:
            win = data.get('win', 1)
            place = data.get('place', 2)
            show = data.get('show', 3)
            tote_send('official', win, place, show)
            return led_reply(esp32.send(f"ANIM:RESULTS_ACTIVE:{win}:{place}:{show}"), animation=anim_name)

    # The tote board's display for the animations that have one
    tote_mapping = {
        'WELCOME': ('welcome',),
        'RACE_START': ('race_start',),
        'BETTING_60': ('betting_open', 60),
        'BETTING_30': ('betting_open', 30),
        'BETTING_15': ('betting_open', 15),
        'BETTING_5': ('final_call',),
    }
    if anim_name in tote_mapping:
        tote_send(*tote_mapping[anim_name])

    return led_reply(esp32.start_animation(anim_name), animation=anim_name)


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
    results do not wait on La Quiniela. Returns (what was set, None), or
    (None, La Quiniela's reason), which the dashboard shows in red."""
    try:
        return get_board().set_mode(mode), None
    except Exception as e:
        print(f'[La Quiniela] mode {mode} not set: {e}')
        return None, f'La Quiniela: {e}'


def _and(items):
    """'a', 'a and b', 'a, b and c'."""
    items = list(items)
    return items[0] if len(items) == 1 else ', '.join(items[:-1]) + ' and ' + items[-1]


def results_arg(data):
    """(win, place, show, next, ties), None, 200, or None, why, status for a
    body that is not the real finish: three different horses in the field,
    after them the next finishers ("next", a list, 4th first, each in the
    field and named once) when one of those named had no bets at the post,
    as many as La Quiniela needs to skip it (la_quiniela.betting.finishers_needed),
    and the dead heats ("ties": lists of two or three horses consecutive in
    the finish, no horse in two). Every place is required: a missing one is
    refused, never filled in. The field is never guessed: with no La
    Quiniela, or a store that could not read its database (its field is a
    default, not the operator's), the results are refused rather than
    checked against posts 1-20. Before the post (the race in 0, 1 or 2)
    there are no figures to pay from, so a result is refused (409) until AT
    THE GATE has taken them."""
    if not isinstance(data, dict):
        return None, 'results must be {"win": N, "place": N, "show": N}, and "next": [N, ...] (the finishers after SHOW) when a horse named had no bets at the post', 400
    picks = []
    for key in ('win', 'place', 'show'):
        if key not in data or data[key] is None:
            return None, f'{key} is missing: WIN, PLACE and SHOW are all required', 400
        value = data[key]
        if isinstance(value, bool) or not isinstance(value, int):
            return None, f'{key} must be a horse number, not {value!r}', 400
        picks.append(value)
    if len(set(picks)) != 3:
        return None, 'WIN, PLACE and SHOW must be three different horses', 400
    after = data.get('next')
    if after is None:
        after = []
    if not isinstance(after, list):
        return None, 'next must be a list of horse numbers (the finishers after SHOW, 4th first)', 400
    named = list(picks)
    for value in after:
        if isinstance(value, bool) or not isinstance(value, int):
            return None, f'next: {value!r} is not a horse number', 400
        if value in named:
            return None, f'next: horse {value} is named twice', 400
        named.append(value)
    ties = data.get('ties')
    if ties is None:
        ties = []
    if not isinstance(ties, list) or not all(isinstance(group, list) for group in ties):
        return None, 'ties must be a list of dead heats, each a list of horse numbers ([[7, 12]])', 400
    tied = set()
    groups = []
    for group in ties:
        if not 2 <= len(group) <= 3:
            return None, 'ties: a dead heat is two or three horses', 400
        for value in group:
            if isinstance(value, bool) or not isinstance(value, int):
                return None, f'ties: {value!r} is not a horse number', 400
            if value not in named:
                return None, f'ties: horse {value} is not in the finish', 400
            if value in tied:
                return None, f'ties: horse {value} is tied twice', 400
        at = sorted(named.index(h) for h in group)
        if at != list(range(at[0], at[0] + len(at))):
            return None, f'ties: {group} are not consecutive in the finish', 400
        tied.update(group)
        groups.append([named[i] for i in at])
    try:
        board = get_board()
    except Exception as e:
        return None, f'La Quiniela is not running, so the field is unknown: results not saved ({e})', 400
    if board.store.load_failed:
        return None, ("La Quiniela's store could not read its database, so the field is unknown: results not saved "
                      "(restart pi5 once the database is readable)"), 400
    state = board.race_state_now()
    if state not in POST_HELD_STATES:
        return None, (f"results before the post: the race is in {race_state_name(state).replace('_', ' ')}, so there are no "
                      "figures at the post to pay from; AT THE GATE takes them first"), 409
    field = {entry['horse'] for entry in field_by_post(board)['posts']}
    for key, horse in zip(('win', 'place', 'show'), picks):
        if horse not in field:
            return None, f'{key}: horse {horse} is not in the field', 400
    for horse in after:
        if horse not in field:
            return None, f'next: horse {horse} is not in the field', 400
    bets = bets_at_post(board.model().get('closing'))
    needed = finishers_needed(named, bets, field)
    if needed:
        labels = ['WIN', 'PLACE', 'SHOW'] + [ordinal(i + 4).upper() for i in range(len(after))]
        skipped = [f'#{horse} ({label})' for horse, label in zip(named, labels) if not bets.get(horse)]
        asked = [ordinal(len(named) + 1 + i) for i in range(needed)]
        return None, (f"{_and(skipped)} had no bets at the post: La Quiniela skips {'it' if len(skipped) == 1 else 'them'}, "
                      f"so enter the next finisher{'s' if needed > 1 else ''} too ({_and(asked)})"), 400
    return (picks[0], picks[1], picks[2], after, groups), None, 200


def save_results(win, place, show, after, ties):
    """Save the record to file: the real 1st, 2nd and 3rd, the finishers
    after them and the dead heats. The file outlives a restart, so it is
    written whole or not at all (a temporary file, flushed to the card,
    renamed over it): a power cut mid-write leaves the old file, never half
    a new one."""
    results = {
        'win': win,
        'place': place,
        'show': show,
        'next': list(after),
        'ties': [list(group) for group in ties],
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
        # GET: the record as the board reads it (the one reader; La Subasta and the TV read the same)
        record = get_board().results()
        if record:
            return jsonify({'success': True, 'results': record})
        return jsonify({
            'success': False,
            'message': 'No results available'
        })

    else:
        # POST - set new results. They are facts about the race, not about
        # the LEDs: saved first, always, and La Quiniela goes to WINNER with
        # them; the LED controller is told after, and whether it answered is
        # reported ('leds'), never a reason to drop the results.
        # The real finish: all three, distinct and in the field, and the next
        # finishers after them when one named had no bets at the post, or a
        # 400 and nothing saved (a missing key used to be filled in with
        # horse 1, 2 or 3).
        picks, error, status = results_arg(request.get_json(silent=True))
        if error:
            return jsonify({
                'success': False,
                'error': error
            }), status
        win, place, show, after, ties = picks

        # Save to file, first
        if not save_results(win, place, show, after, ties):
            return jsonify({
                'success': False,
                'error': 'results not saved: pi5 could not write results.json (its console says why)',
                'results': {'win': win, 'place': place, 'show': show, 'next': after, 'ties': ties},
                'race': None
            }), 500

        # SET WINNERS, results applied: La Quiniela goes to WINNER with them.
        # When it refuses, the results stand and 'race_error' says why.
        race, race_error = quiniela_mode('RESULTS')

        # Send official results to tote board
        tote_send('official', win, place, show)

        # Broadcast to all connected clients via SSE
        broadcast_sse('results', {
            'win': win,
            'place': place,
            'show': show
        })

        # Then the LED wall, through its worker. The three cups were locked as they were
        # picked (CUP:LOCK); what is left of the results on the LEDs is
        # RESULTS:FINALIZE, the winners' chase settling into the heartbeat,
        # which the page used to send itself (/api/results/finalize). The
        # results stand whatever the wall does: 'leds' is its health as last known.
        esp32.send('RESULTS:FINALIZE')

        return jsonify({
            'success': True,
            'results': {
                'win': win,
                'place': place,
                'show': show,
                'next': after,
                'ties': ties
            },
            'leds': leds_word(),
            'race': race,
            'race_error': race_error
        })


@app.route('/api/spectator/state')
def spectator_state():
    """Return current race state and horse data for spectator display."""
    try:
        state_data = racing_service.get_current_state_data()
        results = get_board().results()
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
        race, race_error = quiniela_mode('RESET')

        # Turn off all LEDs
        esp32.all_off()
        
        # Send welcome to tote board
        tote_send('welcome')
        
        return jsonify({
            'success': True,
            'message': 'Results cleared',
            'race': race,
            'race_error': race_error
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

    return led_reply(esp32.send(f"CUP:LOCK:{cup}:{r}:{g}:{b}"), cup=cup, color={'r': r, 'g': g, 'b': b})


@app.route('/api/cup/unlock', methods=['POST'])
def api_cup_unlock():
    """Unlock cup(s) to return to animation"""
    data = request.get_json()
    cup = data.get('cup', 'ALL')
    return led_reply(esp32.send(f"CUP:UNLOCK:{cup}"), cup=cup)


@app.route('/api/results/finalize', methods=['POST'])
def api_results_finalize():
    """Signal ESP32 to begin seamless winner blend (no LED interruption)"""
    return led_reply(esp32.send('RESULTS:FINALIZE'))


@app.route('/api/params', methods=['GET'])
def api_params_get():
    """Get all animation params — fetch from ESP32 and cache locally."""
    try:
        response = esp32.ask('PARAM:GET:ALL')
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
    response = esp32.ask(command)
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
    response = esp32.ask('PARAM:SAVE')
    success = response == 'OK:PARAM:SAVED'
    return jsonify({'success': success, 'response': response})


@app.route('/api/params/reset', methods=['POST'])
def api_params_reset():
    """Reset ESP32 params to defaults and clear local cache."""
    response = esp32.ask('PARAM:RESET')
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
    """Reset to idle state: RESET to the wall, the welcome on the tote board"""
    queued = esp32.reset()
    tote_send('welcome')
    return led_reply(queued)


@app.route('/api/tote/ping', methods=['GET'])
def api_tote_ping():
    """Ask the tote board for its /status: an ask, which waits up to the
    board's timeout and answers at once while the board is marked down."""
    if not tote:
        return jsonify({
            'success': False,
            'status': 'DISABLED',
            'message': 'Tote board is disabled'
        })

    online = tote.ping()
    return jsonify({
        'success': online,
        'status': 'ONLINE' if online else 'OFFLINE'
    })


TOTE_ACTIONS = ('welcome', 'scroll', 'betting_open', 'final_call', 'race_start', 'official', 'test', 'stop')


@app.route('/api/tote/command', methods=['POST'])
def api_tote_command():
    """A display call on the tote board by name (TOTE_ACTIONS), with its arguments"""
    if not tote:
        return jsonify({
            'success': False,
            'error': 'Tote board is disabled'
        }), 503

    data = request.get_json()
    action = data.get('action', '')

    if action not in TOTE_ACTIONS:
        return jsonify({
            'success': False,
            'error': f"action must be one of {', '.join(TOTE_ACTIONS)}"
        }), 400

    tote_send(action, **data.get('params', {}))
    return jsonify({
        'success': True,
        'action': action
    })


@app.route('/api/status', methods=['GET'])
def api_status():
    """Both devices' health as their workers last learned it (the Control
    Center's two pills read it every 5 s): nothing here asks a device."""
    return jsonify({
        'leds': esp32.health(),
        'tote': tote.health() if tote else {'enabled': False},
        'num_cups': NUM_CUPS,
        'total_leds': TOTAL_LEDS,
        'version': VERSION
    })


@app.route('/api/esp32/config', methods=['GET'])
def api_esp32_config_get():
    """Return the ESP32 client's current IP and port."""
    return jsonify({'ip': esp32.ip, 'port': esp32.port})


def persist_env_setting(name, value, path=None):
    """Set NAME=value in pi5/.env (git-ignored: the box's own settings), in
    place of an existing NAME line or added at the end; every other line is
    kept as it is. True when written."""
    path = path or _config.ENV_FILE
    try:
        with open(path, 'r', encoding='utf-8', newline='') as f:
            text = f.read()
    except FileNotFoundError:
        text = ''
    except OSError as e:
        print(f"[CONFIG] Failed to read {path}: {e}")
        return False
    eol = '\r\n' if '\r\n' in text else '\n'
    lines = text.splitlines()
    line = f'{name}={value}'
    for i, existing in enumerate(lines):
        stripped = existing.strip()
        if stripped.startswith('export '):
            stripped = stripped[len('export '):].lstrip()
        if stripped.partition('=')[0].strip() == name:
            lines[i] = line
            break
    else:
        lines.append(line)
    try:
        with open(path, 'w', encoding='utf-8', newline='') as f:
            f.write(eol.join(lines) + eol)
    except OSError as e:
        print(f"[CONFIG] Failed to write {path}: {e}")
        return False
    return True


@app.route('/api/esp32/config', methods=['POST'])
def api_esp32_config_set():
    """Update the ESP32 client's IP, persist it to pi5/.env (DDM_ESP32_IP: the
    box's own setting, never the tracked config.py), and ping to verify."""
    import re

    data = request.get_json(silent=True) or {}
    new_ip = (data.get('ip') or '').strip()

    ip_pattern = re.compile(r'^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})$')
    match = ip_pattern.match(new_ip)
    if not match or any(int(o) > 255 for o in match.groups()):
        return jsonify({'success': False, 'error': 'Invalid IP address'}), 400

    esp32.ip = new_ip
    persisted = persist_env_setting('DDM_ESP32_IP', new_ip)

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
    if esp32.ip:
        print(f"  ESP32 Target:  {esp32.ip}:{esp32.port}")
    else:
        print("  ESP32 Target:  none: no LED controller set (DDM_ESP32_IP in pi5/.env), the LEDs are off")
    if not WEATHER_API_KEY:
        print("  Weather:       no weather key (DDM_WEATHER_API_KEY in pi5/.env): the board's weather line is off")
    if not _config.LS_ADMIN_PIN:
        print("  La Subasta:    no host PIN (DDM_LS_ADMIN_PIN in pi5/.env): its admin page and routes are off")

    if tote:
        print(f"  Tote Board:    {tote.ip}:{tote.port}")
        tote.welcome()                      # the welcome at start, through its worker (which says if the board does not answer)
    else:
        print("  Tote Board:    none (DDM_TOTE_IP in pi5/.env)")

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
    
    service_mode.quiet_access_log()   # no line per request in the journal (DDM_ACCESS_LOG=1 brings them back)
    socketio.run(app, host=FLASK_HOST, port=FLASK_PORT, debug=FLASK_DEBUG, allow_unsafe_werkzeug=True)
