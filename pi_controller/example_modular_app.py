"""
Example of how to use the new modular DDM system.
This demonstrates the Flask Blueprint separation.
"""

from flask import Flask, render_template, Blueprint, jsonify, request, redirect, url_for
from datetime import datetime

app = Flask(__name__)
app.config['SECRET_KEY'] = 'your-secret-key-here'

# Create separate blueprints for admin and guest
admin_bp = Blueprint('admin', __name__, url_prefix='/admin')
guest_bp = Blueprint('guest', __name__, url_prefix='/guest')

# Admin routes
@admin_bp.route('/')
@admin_bp.route('/dashboard')
def dashboard():
    """Admin dashboard with all controls."""
    return render_template('admin/dashboard_new.html')

@admin_bp.route('/api/status')
def admin_status():
    """Admin-only status endpoint."""
    return jsonify({
        'status': 'admin_online',
        'timestamp': datetime.now().isoformat(),
        'admin_features': ['voice_control', 'manual_control', 'system_settings']
    })

# Guest routes
@guest_bp.route('/')
@guest_bp.route('/view')
def guest_index():
    """Guest view for race display."""
    return render_template('guest/index.html')

@guest_bp.route('/api/race-info')
def race_info():
    """Guest-accessible race information."""
    return jsonify({
        'race_number': 1,
        'status': 'pre-race',
        'horses': 20,
        'next_race': '5:30 PM'
    })

# API routes (could be separate blueprint)
@app.route('/api/display/mode-animations')
def mode_animations():
    """Get available animations for a mode."""
    mode = request.args.get('mode', 'pre_race')
    
    animations = {
        'pre_race': ['warm_up', 'preparation', 'countdown'],
        'betting_open': ['betting_lights', 'countdown_timer', 'last_call'],
        'during_race': ['theyre_off', 'down_the_stretch', 'finish_race'],
        'results': ['victory_celebration', 'results_display', 'reset_for_next']
    }
    
    return jsonify({
        'success': True,
        'animations': animations.get(mode, [])
    })

@app.route('/api/display/command', methods=['POST'])
def display_command():
    """Send command to display system."""
    data = request.get_json()
    
    # In a real implementation, this would send to LED controllers
    print(f"Display command received: {data}")
    
    return jsonify({
        'success': True,
        'message': 'Command sent to display system'
    })

# Register blueprints
app.register_blueprint(admin_bp)
app.register_blueprint(guest_bp)

# Root route redirect
@app.route('/')
def index():
    """Redirect to admin dashboard by default."""
    return redirect(url_for('admin.dashboard'))

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)
