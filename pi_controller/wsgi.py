"""
WSGI Entry Point for Production Deployment

This file is used by production WSGI servers like Gunicorn.
"""

from ddm import create_app, socketio

# Create the application
application = create_app('production')

if __name__ == "__main__":
    # This won't be called in production, but useful for testing
    socketio.run(application, host='0.0.0.0', port=5000)
