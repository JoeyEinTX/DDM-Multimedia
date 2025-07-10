"""
DDM Racing System Main Application Entry Point

This is the main entry point for the DDM Racing System Flask application.
Run this file to start the development server.
"""

import os
import sys

# Add the current directory to Python path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ddm import create_app, socketio

# Create the Flask application
app = create_app()

if __name__ == '__main__':
    # Run the application with SocketIO support
    print("🏇 Starting DDM Racing System...")
    print(f"🌐 Admin Interface: http://localhost:{app.config['PORT']}/admin")
    print(f"📱 Guest Interface: http://localhost:{app.config['PORT']}/guest")
    print(f"🔧 API Base: http://localhost:{app.config['PORT']}/api")
    
    socketio.run(
        app,
        host=app.config['HOST'],
        port=app.config['PORT'],
        debug=app.config['DEBUG'],
        use_reloader=app.config['DEBUG']
    )
